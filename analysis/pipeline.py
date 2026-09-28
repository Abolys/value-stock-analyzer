"""Run the four lenses for one ticker.

Quant, Macro and Moat are independent and run concurrently; the Devil's
Advocate starts only once all three have finished (it reads their structured
fields). The turnaround estimate runs last and reads all four. `on_result(name, result)` is called in the caller's thread as each
result arrives, so a UI can show each lens as soon as it is ready. The first
event is "inputs": an AnalysisRun with the view fields filled (header, 52-week
range, signals, fundamentals series) and no lenses yet, so the page can draw its
header and charts before the lenses finish. The run and its LLM costs are
stored in analysis_runs / llm_calls.
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from analysis.aggregate import aggregate
from analysis.devils_advocate import devils_advocate_lens
from analysis.inputs import AnalysisInputs, AnalysisLoadError, fundamental_series, load_inputs
from analysis.macro import macro_lens
import config
from analysis.models import (
    AggregateResult, AnalysisRun, DevilsAdvocateResult, LensResult, MacroResult, MoatResult, QuantResult, insufficient,
)
from analysis.moat import moat_lens
from analysis.quant import quant_lens
from analysis.turnaround import load_history, turnaround, week52_range
from analysis.turnaround_models import TurnaroundResult
from data.edgar import EdgarClient
from llm.client import LLMClient
from llm.departure import confirm_with
from llm.prompts import PROMPT_VERSIONS
from data.provider import DataProvider, ProviderError
from screening.engine import ScreenContext
from storage import llm_store

log = logging.getLogger(__name__)

OnResult = Callable[[str, Any], None]
RESULT_TYPES = {"quant": QuantResult, "macro": MacroResult, "moat": MoatResult, "devils_advocate": DevilsAdvocateResult}


FUND_VERDICT = "Not applicable (fund)"
FUND_NOTE = ("an ETF or fund: the four lenses and the screen are built for individual companies, so only the "
             "price-based view applies (price, 52-week range, drawdowns and recovery, dividends, return vs benchmark)")


def is_fund(x: AnalysisInputs) -> bool:
    return (x.info.get("quote_type") or "").upper() in config.FUND_QUOTE_TYPES


def _failed(name: str, exc: BaseException) -> LensResult:
    msg = f"{type(exc).__name__}: {exc}"
    r = RESULT_TYPES[name]()
    r.status = insufficient(f"lens error ({msg})")
    r.rationale = f"**{r.label} — {r.status}**"
    return r


def input_hash(x: AnalysisInputs) -> str:
    """sha256 over the deterministic inputs every lens reads, plus the LLM prompt versions."""
    blob = {"screen": x.screen.model_dump(mode="json"), "dividends": x.dividends.model_dump(mode="json"),
            "insiders": x.insiders.model_dump(mode="json"),
            "leadership": x.leadership.model_dump(mode="json") if x.leadership else None,
            "context": x.context.model_dump(mode="json"), "cyclicality": x.cyclicality.model_dump(mode="json"),
            "prompts": PROMPT_VERSIONS}
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()


def view_run(x: AnalysisInputs, provider: DataProvider | None = None, analysis_id: int | None = None) -> AnalysisRun:
    """An AnalysisRun with the view fields filled and no lenses yet (the "inputs" event)."""
    rev = x.f.ttm("total_revenue")
    run = AnalysisRun(
        ticker=x.ticker, analysis_id=analysis_id, today=x.today, fundamentals_as_of=x.screen.fundamentals_as_of,
        stale=x.screen.stale, stale_label=x.screen.stale_label, company=x.company, sector=x.info.get("sector"),
        industry=x.info.get("industry"), treatment=x.route.label, sector_adjusted=x.route.sector_adjusted,
        currency=x.info.get("currency"), price_as_of=x.price.period_end,
        fundamentals_label=rev.period_label if rev.ok else rev.status,
        providers=sorted({p for p in (x.info.provider, x.f.provider, x.price.provider) if p}),
        input_hash=input_hash(x), screen=x.screen, dividends=x.dividends, insiders=x.insiders,
        leadership=x.leadership, context=x.context, series=fundamental_series(x.f, x.info.get("currency")),
        notes=list(x.notes))
    if provider is not None:
        try:
            h = load_history(provider, x.ticker)
            run.week52 = week52_range(h.closes, h.breaks)
        except ProviderError as exc:
            run.notes.append(f"52-week range unavailable: {exc}")
    return run


def run_lenses(x: AnalysisInputs, llm: LLMClient, on_result: OnResult | None = None,
               lenses: dict[str, Callable] | None = None, base: AnalysisRun | None = None) -> AnalysisRun:
    """Quant, Macro and Moat concurrently; then the Devil's Advocate; then the aggregate.
    `base` (from view_run) is filled in place; without it a bare run is started."""
    fns = {"quant": quant_lens, "macro": macro_lens, "moat": lambda i: moat_lens(i, llm),
           "devils_advocate": lambda i, q, m, mo: devils_advocate_lens(i, q, m, mo, llm)}
    fns.update(lenses or {})
    run = base or AnalysisRun(ticker=x.ticker, analysis_id=llm.analysis_id, today=x.today,
                              fundamentals_as_of=x.screen.fundamentals_as_of, stale=x.screen.stale,
                              stale_label=x.screen.stale_label)
    results: dict[str, LensResult] = {}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="lens") as pool:
        futures = {pool.submit(fns[name], x): name for name in ("quant", "macro", "moat")}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                results[name] = fut.result()
            except Exception as exc:  # a lens bug must not take the others down; it is recorded
                log.exception("%s lens failed for %s", name, x.ticker)
                run.errors.append(f"{name}: {type(exc).__name__}: {exc}")
                results[name] = _failed(name, exc)
            if on_result:
                on_result(name, results[name])
    try:
        results["devils_advocate"] = fns["devils_advocate"](x, results["quant"], results["macro"], results["moat"])
    except Exception as exc:
        log.exception("devils_advocate lens failed for %s", x.ticker)
        run.errors.append(f"devils_advocate: {type(exc).__name__}: {exc}")
        results["devils_advocate"] = _failed("devils_advocate", exc)
    if on_result:
        on_result("devils_advocate", results["devils_advocate"])
    for name, r in results.items():
        setattr(run, name, r)
    run.aggregate = aggregate(results)
    if on_result:
        on_result("aggregate", run.aggregate)
    return run


def run_turnaround(ctx: ScreenContext, x: AnalysisInputs, run: AnalysisRun) -> TurnaroundResult:
    """The turnaround estimate after the lenses; an exception is recorded, never fatal."""
    try:
        return turnaround(x, run, ctx.provider, ctx.db_path)
    except Exception as exc:
        log.exception("turnaround failed for %s", x.ticker)
        run.errors.append(f"turnaround: {type(exc).__name__}: {exc}")
        status = insufficient(f"turnaround error ({type(exc).__name__}: {exc})")
        return TurnaroundResult(ticker=x.ticker, status=status, headline=status)


def run_analysis(ctx: ScreenContext, ticker: str, llm: LLMClient | None = None, edgar: EdgarClient | None = None,
                 on_result: OnResult | None = None, store: bool = True,
                 lenses: dict[str, Callable] | None = None) -> AnalysisRun:
    llm = llm or LLMClient(db_path=ctx.db_path)
    if store:
        llm.analysis_id = llm_store.create_analysis(ticker, ctx.db_path)
    try:
        x = load_inputs(ctx, ticker, edgar=edgar, confirm=confirm_with(llm) if llm.configured else None)
    except AnalysisLoadError as exc:
        run = AnalysisRun(ticker=ticker, analysis_id=llm.analysis_id, today=ctx.today, load_error=str(exc))
        if store:
            llm_store.finish_analysis(llm.analysis_id, run, ctx.db_path)
        return run
    base = view_run(x, ctx.provider, llm.analysis_id)
    if on_result:
        on_result("inputs", base)
    if is_fund(x):
        run = base
        run.fund = True
        run.aggregate = AggregateResult(not_applicable=FUND_VERDICT, verdict=FUND_VERDICT,
                                        rationale=f"**{FUND_VERDICT}**: {FUND_NOTE}")
        run.notes.append(FUND_NOTE)
        if on_result:
            on_result("aggregate", run.aggregate)
    else:
        run = run_lenses(x, llm, on_result, lenses, base=base)
    run.turnaround = run_turnaround(ctx, x, run)
    if on_result:
        on_result("turnaround", run.turnaround)
    if store:
        run.total_cost = llm_store.finish_analysis(llm.analysis_id, run, ctx.db_path)
    return run
