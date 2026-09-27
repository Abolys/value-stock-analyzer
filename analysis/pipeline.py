"""Run the four lenses for one ticker.

Quant, Macro and Moat are independent and run concurrently; the Devil's
Advocate starts only once all three have finished (it reads their structured
fields). The turnaround estimate runs last and reads all four. `on_result(name, result)` is called in the caller's thread as each
result arrives, so a UI can show each lens as soon as it is ready. The run and
its LLM costs are stored in analysis_runs / llm_calls.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from analysis.aggregate import aggregate
from analysis.devils_advocate import devils_advocate_lens
from analysis.inputs import AnalysisInputs, AnalysisLoadError, load_inputs
from analysis.macro import macro_lens
from analysis.models import (
    AnalysisRun, DevilsAdvocateResult, LensResult, MacroResult, MoatResult, QuantResult, insufficient,
)
from analysis.moat import moat_lens
from analysis.quant import quant_lens
from analysis.turnaround import turnaround
from analysis.turnaround_models import TurnaroundResult
from data.edgar import EdgarClient
from llm.client import LLMClient
from llm.departure import confirm_with
from screening.engine import ScreenContext
from storage import llm_store

log = logging.getLogger(__name__)

OnResult = Callable[[str, Any], None]
RESULT_TYPES = {"quant": QuantResult, "macro": MacroResult, "moat": MoatResult, "devils_advocate": DevilsAdvocateResult}


def _failed(name: str, exc: BaseException) -> LensResult:
    msg = f"{type(exc).__name__}: {exc}"
    r = RESULT_TYPES[name]()
    r.status = insufficient(f"lens error ({msg})")
    r.rationale = f"**{r.label} — {r.status}**"
    return r


def run_lenses(x: AnalysisInputs, llm: LLMClient, on_result: OnResult | None = None,
               lenses: dict[str, Callable] | None = None) -> AnalysisRun:
    """Quant, Macro and Moat concurrently; then the Devil's Advocate; then the aggregate."""
    fns = {"quant": quant_lens, "macro": macro_lens, "moat": lambda i: moat_lens(i, llm),
           "devils_advocate": lambda i, q, m, mo: devils_advocate_lens(i, q, m, mo, llm)}
    fns.update(lenses or {})
    run = AnalysisRun(ticker=x.ticker, analysis_id=llm.analysis_id, today=x.today,
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
            llm_store.finish_analysis(llm.analysis_id, None, "failed to load", run.model_dump_json(), ctx.db_path)
        return run
    run = run_lenses(x, llm, on_result, lenses)
    run.turnaround = run_turnaround(ctx, x, run)
    if on_result:
        on_result("turnaround", run.turnaround)
    if store:
        run.total_cost = llm_store.finish_analysis(llm.analysis_id, run.aggregate.score, run.aggregate.verdict,
                                                   run.model_dump_json(), ctx.db_path)
    return run
