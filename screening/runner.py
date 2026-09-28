"""A full screen run over the selected universe lists (SPEC "Screener execution"
and "Data source resilience").

1. Health check first; a failure records the run as "blocked: health check
   failed" and nothing starts.
2. Batch prices for every remaining ticker; tickers the batch misses are tried
   one at a time.
3. One ticker at a time through both stages; each result is committed, so
   --resume continues without redoing finished tickers (tickers that failed
   to load are retried).
4. Circuit breaker: when more than CIRCUIT_BREAKER_FAIL_RATE of the last
   CIRCUIT_BREAKER_WINDOW tickers failed to load, the run pauses as
   "stopped: source failing".
5. At the end, the per-field N/A report flags fields that are N/A for more than
   FIELD_NA_SPIKE of tickers as "likely renamed upstream".
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime
from typing import Callable

import config
from data import prices
from data.health import HealthReport, run_health_check
from data.provider import BatchPriceResult, DataProvider, ProviderError
from data.universe import load_universe
from screening.engine import ScreenContext, screen_ticker
from screening.models import STATUS_FAILED_TO_LOAD, ScreenResult
from storage import screen_store as store

log = logging.getLogger(__name__)
LIKELY_RENAMED = "likely renamed upstream"


class CircuitBreaker:
    def __init__(self, window: int | None = None, fail_rate: float | None = None):
        self.window = window or config.CIRCUIT_BREAKER_WINDOW
        self.fail_rate = config.CIRCUIT_BREAKER_FAIL_RATE if fail_rate is None else fail_rate
        self.outcomes: deque[bool] = deque(maxlen=window)  # True = failed to load

    def record(self, failed: bool) -> None:
        self.outcomes.append(failed)

    @property
    def tripped(self) -> bool:
        return len(self.outcomes) == self.window and sum(self.outcomes) / self.window > self.fail_rate


def field_na_report(results: list[ScreenResult]) -> tuple[dict[str, dict[str, int]], list[str]]:
    """Per-field N/A counts over the tickers that loaded; fields above FIELD_NA_SPIKE are flagged."""
    counts: dict[str, dict[str, int]] = {}
    for r in results:
        for fld, status in r.field_statuses.items():
            c = counts.setdefault(fld, {"na": 0, "of": 0})
            c["of"] += 1
            c["na"] += status.startswith("N/A")
    flagged = sorted(f for f, c in counts.items() if c["of"] and c["na"] / c["of"] > config.FIELD_NA_SPIKE)
    return counts, flagged


def _inner(provider: DataProvider) -> DataProvider:
    return getattr(provider, "inner", provider)


def batch_prices(ctx: ScreenContext, tickers: list[str]) -> BatchPriceResult:
    try:
        return prices.batch_latest_prices(ctx.provider, tickers)
    except ProviderError as exc:
        log.warning("batch price download failed (%s); falling back to per-ticker prices", exc)
        return BatchPriceResult(failed={t: str(exc) for t in tickers}, provider=ctx.provider.name)


def _cache_stats(ctx: ScreenContext) -> tuple[int, int]:
    cache = ctx.cache
    if cache is None:
        return 0, 0
    return cache.refetched_because_reported(), len(cache.info_hit_tickers)


def run_screen(ctx: ScreenContext, lists: list[str] | None = None, resume: bool = False,
               health_fn: Callable[[DataProvider], HealthReport] = run_health_check,
               universe_dir=config.UNIVERSE_DIR, log_path: str | None = None,
               progress: Callable[[str], None] | None = None) -> store.ScreenRun:
    say = progress or (lambda msg: log.info(msg))
    db = ctx.db_path
    run: store.ScreenRun | None = None
    if resume:
        run = store.latest_resumable_run(db)
        if run is None:
            raise RuntimeError("no interrupted or stopped run to resume")
        lists = run.lists
    if not lists:
        raise ValueError("choose at least one universe list")

    report = health_fn(_inner(ctx.provider))
    if not report.ok:
        say("health check failed: " + "; ".join(report.failures))
        if run is not None:
            store.update_run(run.run_id, db, note=f"resume blocked by health check at {datetime.now():%Y-%m-%d %H:%M}",
                             health_failures=report.failures)
            return store.get_run(run.run_id, db)
        run_id = store.create_run(lists, store.BLOCKED, report.failures, path=db, log_path=log_path)
        return store.get_run(run_id, db)

    universe = load_universe(lists, universe_dir)
    sources = dict(zip(universe["ticker"], universe["source"]))
    tickers = list(sources)
    if run is None:
        run_id = store.create_run(lists, total=len(tickers), pid=os.getpid(), log_path=log_path, path=db)
        prior_refetched = prior_served = 0
    else:
        run_id = run.run_id
        prior_refetched, prior_served = run.refetched_reported, run.served_from_cache
        store.update_run(run_id, db, status=store.RUNNING, pid=os.getpid(), total=len(tickers), note=None)
    # On resume, tickers that failed to load are retried (the source may be back);
    # every ticker with a real result is kept and never redone.
    done = store.finished_tickers(run_id, db, include_failed=False)
    todo = [t for t in tickers if t not in done]
    say(f"run {run_id}: {len(todo)} of {len(tickers)} tickers to screen ({len(done)} already done)")

    batch = batch_prices(ctx, todo)
    breaker = CircuitBreaker()
    for i, ticker in enumerate(todo, 1):
        price = prices.actual_latest_price(ctx.provider, ticker, batch)  # batch price, else single-ticker fetch
        try:
            result = screen_ticker(ctx, ticker, sources[ticker], price=price)
        except Exception as exc:  # never let one ticker kill the run; record it as a load failure
            log.exception("unexpected error screening %s", ticker)
            result = ScreenResult(ticker=ticker, sources=sources[ticker], status=STATUS_FAILED_TO_LOAD,
                                  load_error=f"unexpected error: {type(exc).__name__}: {exc}", decided_at_stage=0)
        store.write_result(run_id, result, db)
        breaker.record(result.status == STATUS_FAILED_TO_LOAD)
        counts = store.count_results(run_id, db)
        refetched, served = _cache_stats(ctx)
        store.update_run(run_id, db, attempted=counts.done, passed_stage1=counts.passed_stage1,
                         passed_stage2=counts.passed_stage2, failed_to_load=counts.failed_to_load,
                         refetched_reported=prior_refetched + refetched, served_from_cache=prior_served + served)
        say(f"[{i}/{len(todo)}] {ticker}: {result.display_status}")
        if breaker.tripped:
            store.update_run(run_id, db, status=store.STOPPED,
                             note=(f"{sum(breaker.outcomes)} of the last {breaker.window} tickers failed to load "
                                   f"(> {breaker.fail_rate:.0%}); continue with --resume"))
            say("circuit breaker tripped: run stopped (source failing)")
            return store.get_run(run_id, db)

    results = [r for r in store.load_results(run_id, db) if r.status != STATUS_FAILED_TO_LOAD]
    field_na, flagged = field_na_report(results)
    store.update_run(run_id, db, status=store.COMPLETED, ended_at=datetime.now().isoformat(timespec="seconds"),
                     field_na=field_na, flagged_fields=flagged)
    if flagged:
        say(f"fields {LIKELY_RENAMED}: {', '.join(flagged)}")
    return store.get_run(run_id, db)
