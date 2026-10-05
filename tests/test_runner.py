"""The screen runner: health check, circuit breaker, resume, retries, caching,
officer snapshots, divergence logging and the per-field N/A report."""

from collections import Counter
from datetime import date, datetime

import pytest

import config
from data.cache import CachedProvider, DiskCache
from data.fixture_provider import FixtureTicker, fixture_download, fixture_provider
from data.health import HealthReport
from data.provider import DataProvider, ProviderError
from data.values import Datum
from data.yfinance_provider import YFinanceProvider
from screening.engine import ScreenContext, screen_ticker
from screening.models import STATUS_FAILED_TO_LOAD, ScreenResult
from screening.runner import LIKELY_RENAMED, field_na_report, run_screen
from storage import screen_store as store
from storage.db import get_officer_snapshots

TODAY = date(2026, 9, 27)
NOW = datetime(2026, 9, 27, 12, 0)
VALET = lambda series: (3.1, date(2026, 9, 25))  # noqa: E731


def healthy(_provider):
    return HealthReport(ok=True, checked_at=NOW)


class Counting(DataProvider):
    """Wraps the fixture provider, counts every call, and can fail or be interrupted on demand."""

    name = "yfinance"

    def __init__(self, down: bool = False, interrupt_on: str | None = None):
        self.inner = fixture_provider(today=TODAY)
        self.calls: Counter = Counter()
        self.down, self.interrupt_on = down, interrupt_on

    def _call(self, method, ticker, *args):
        self.calls[(method, ticker)] += 1
        if method == "get_info" and ticker == self.interrupt_on:
            raise KeyboardInterrupt
        if self.down and method == "get_info":
            raise ProviderError("Yahoo refused the request")
        return getattr(self.inner, method)(ticker, *args)

    def get_info(self, t): return self._call("get_info", t)
    def get_statement(self, t, k, f): return self._call("get_statement", t, k, f)
    def get_price_history(self, t, a): return self._call("get_price_history", t, a)
    def get_splits(self, t): return self._call("get_splits", t)
    def get_dividends(self, t): return self._call("get_dividends", t)
    def get_shares_history(self, t): return self._call("get_shares_history", t)
    def get_earnings_dates(self, t): return self._call("get_earnings_dates", t)
    def get_analyst_estimates(self, t): return self._call("get_analyst_estimates", t)

    def batch_latest_prices(self, tickers):
        self.calls[("batch", len(tickers))] += 1
        return self.inner.batch_latest_prices(tickers)

    def count(self, method):
        return sum(n for (m, _), n in self.calls.items() if m == method)


@pytest.fixture
def universe(tmp_path):
    def make(*tickers):
        d = tmp_path / "universe"
        d.mkdir(exist_ok=True)
        (d / "watchlist.csv").write_text("ticker,name,source,as_of\n" + "".join(f"{t},,Watchlist,2026-09\n"
                                                                              for t in tickers))
        return d
    return make


def ctx_for(inner, tmp_path, cache_name="cache.db"):
    cache = DiskCache(tmp_path / cache_name, clock=lambda: NOW)
    return ScreenContext(provider=CachedProvider(inner, cache), db_path=tmp_path / "runs.db", today=TODAY,
                         valet_fetch=VALET)


def test_failed_health_check_blocks_the_run(tmp_path, universe):
    inner = Counting()
    ctx = ctx_for(inner, tmp_path)
    blocked = lambda p: HealthReport(ok=False, failures=["SPY: prices failed"], checked_at=NOW)  # noqa: E731
    run = run_screen(ctx, ["watchlist"], health_fn=blocked, universe_dir=universe("MSFT"))
    assert run.status == store.BLOCKED == "blocked: health check failed"
    assert run.health_failures == ["SPY: prices failed"]
    assert store.load_results(run.run_id, ctx.db_path) == []
    assert inner.count("get_info") == 0


def test_failed_health_check_marks_the_precreated_row_blocked(tmp_path, universe):
    ctx = ctx_for(Counting(), tmp_path)
    rid = store.create_run(["watchlist"], total=0, path=ctx.db_path)  # the app created it on launch
    blocked = lambda p: HealthReport(ok=False, failures=["SPY: prices failed"], checked_at=NOW)  # noqa: E731
    run = run_screen(ctx, ["watchlist"], health_fn=blocked, universe_dir=universe("MSFT"), run_id=rid)
    assert run.run_id == rid and run.status == store.BLOCKED and run.ended_at is not None
    assert len(store.list_runs(ctx.db_path)) == 1  # no second row was created


def test_run_uses_the_precreated_row_when_given_run_id(tmp_path, universe):
    u = universe("MSFT", "JPM")
    ctx = ctx_for(Counting(), tmp_path)
    rid = store.create_run(["watchlist"], total=0, path=ctx.db_path)  # the app created it on launch
    run = run_screen(ctx, ["watchlist"], health_fn=healthy, universe_dir=u, run_id=rid)
    assert run.run_id == rid and run.status == store.COMPLETED and run.attempted == 2
    assert len(store.list_runs(ctx.db_path)) == 1


def test_run_writes_results_counts_and_officer_snapshots(tmp_path, universe):
    ctx = ctx_for(Counting(), tmp_path)
    run = run_screen(ctx, ["watchlist"], health_fn=healthy, universe_dir=universe("MSFT", "JPM", "NOPE"))
    assert run.status == store.COMPLETED and run.total == 3 and run.attempted == 3
    results = {r.ticker: r for r in store.load_results(run.run_id, ctx.db_path)}
    assert results["MSFT"].sources == "Watchlist"
    assert results["NOPE"].status == STATUS_FAILED_TO_LOAD and run.failed_to_load == 1
    for t in ("MSFT", "JPM"):
        snaps = get_officer_snapshots(t, ctx.db_path)
        assert snaps and snaps[-1].snapshot_date == TODAY
    assert store.latest_completed_run(ctx.db_path).run_id == run.run_id


def test_second_run_without_passed_earnings_makes_no_fundamentals_calls(tmp_path, universe):
    u = universe("MSFT", "LULU")
    run_screen(ctx_for(Counting(), tmp_path), ["watchlist"], health_fn=healthy, universe_dir=u)
    second = Counting()
    run = run_screen(ctx_for(second, tmp_path), ["watchlist"], health_fn=healthy, universe_dir=u)
    for method in ("get_info", "get_statement", "get_shares_history", "get_earnings_dates"):
        assert second.count(method) == 0, method
    assert run.served_from_cache == 2 and run.refetched_reported == 0


def test_circuit_breaker_trips_then_resume_completes(tmp_path, universe, monkeypatch):
    monkeypatch.setattr(config, "CIRCUIT_BREAKER_WINDOW", 4)
    monkeypatch.setattr(config, "CIRCUIT_BREAKER_FAIL_RATE", 0.5)
    u = universe("MSFT", "JPM", "LULU", "MELI", "HTZ", "LCID")
    run = run_screen(ctx_for(Counting(down=True), tmp_path), ["watchlist"], health_fn=healthy, universe_dir=u)
    assert run.status == store.STOPPED == "stopped: source failing"
    assert run.attempted == 4 and run.failed_to_load == 4 and "--resume" in run.note
    # Failed to load is never a screen Fail.
    assert all(r.status == STATUS_FAILED_TO_LOAD for r in store.load_results(run.run_id, tmp_path / "runs.db"))

    back = Counting()
    resumed = run_screen(ctx_for(back, tmp_path, "cache2.db"), resume=True, health_fn=healthy, universe_dir=u)
    assert resumed.run_id == run.run_id and resumed.status == store.COMPLETED
    assert resumed.attempted == 6 and resumed.failed_to_load == 0
    # every ticker loaded exactly once in the resumed session (failed loads retried, nothing twice)
    assert all(back.calls[("get_info", t)] == 1 for t in ("MSFT", "JPM", "LULU", "MELI", "HTZ", "LCID"))


def test_resume_continues_interrupted_run_without_redoing_finished_tickers(tmp_path, universe):
    u = universe("MSFT", "JPM", "LULU", "MELI")
    with pytest.raises(KeyboardInterrupt):
        run_screen(ctx_for(Counting(interrupt_on="LULU"), tmp_path), ["watchlist"], health_fn=healthy,
                   universe_dir=u)
    interrupted = store.latest_resumable_run(tmp_path / "runs.db")
    assert interrupted.status == store.RUNNING
    assert store.finished_tickers(interrupted.run_id, tmp_path / "runs.db") == {"MSFT", "JPM"}

    fresh = Counting()
    run = run_screen(ctx_for(fresh, tmp_path, "cache2.db"), resume=True, health_fn=healthy, universe_dir=u)
    assert run.run_id == interrupted.run_id and run.status == store.COMPLETED and run.attempted == 4
    assert fresh.calls[("get_info", "MSFT")] == 0 and fresh.calls[("get_info", "JPM")] == 0
    assert fresh.calls[("get_info", "LULU")] == 1 and fresh.calls[("get_info", "MELI")] == 1


def test_refusal_is_retried_then_recorded_as_failed_to_load(tmp_path, universe):
    attempts, sleeps = [], []

    class Refusing(FixtureTicker):
        @property
        def info(self):
            attempts.append(self.ticker)
            raise RuntimeError("429 Too Many Requests")

    yf = YFinanceProvider(ticker_factory=lambda t: Refusing(t), download_fn=fixture_download(),
                          throttle=type("T", (), {"wait": lambda self: None})(), sleep=sleeps.append,
                          today=lambda: TODAY)
    ctx = ctx_for(yf, tmp_path)
    run = run_screen(ctx, ["watchlist"], health_fn=healthy, universe_dir=universe("MSFT"))
    assert len(attempts) == config.FETCH_MAX_RETRIES + 1
    assert sleeps == [config.FETCH_BACKOFF_SECONDS * 2 ** i for i in range(config.FETCH_MAX_RETRIES)]
    [res] = store.load_results(run.run_id, ctx.db_path)
    assert res.status == STATUS_FAILED_TO_LOAD and res.status != "Fail" and "Too Many Requests" in res.load_error
    assert run.failed_to_load == 1 and run.passed_stage2 == 0


def test_stage_divergence_is_logged_in_the_run(tmp_path, universe):
    ctx = ctx_for(Counting(), tmp_path)
    run = run_screen(ctx, ["watchlist"], health_fn=healthy, universe_dir=universe("ABX.TO"))
    div = store.load_divergences(run.run_id, ctx.db_path)
    row = next(d for d in div if d["ticker"] == "ABX.TO" and d["metric"] == "margin of safety")
    assert row["stage1"] > 0 > row["stage2"] and row["rel_diff"] > config.STAGE_DIVERGENCE


def test_field_na_spike_flagged_as_likely_rename():
    results = [ScreenResult(ticker=f"T{i}", field_statuses={
        "ttm.ebitda": "N/A - field not found: ebitda" if i < 9 else "ok",
        "bal.total_debt": "ok" if i else "N/A - Data Incomplete"}) for i in range(10)]
    counts, flagged = field_na_report(results)
    assert counts["ttm.ebitda"] == {"na": 9, "of": 10}
    assert flagged == ["ttm.ebitda"]  # 90% > FIELD_NA_SPIKE; total_debt at 10% is not
    assert LIKELY_RENAMED == "likely renamed upstream"


def test_field_na_report_saved_on_completed_run(tmp_path, universe):
    ctx = ctx_for(Counting(), tmp_path)
    run = run_screen(ctx, ["watchlist"], health_fn=healthy, universe_dir=universe("MSFT", "LULU"))
    assert run.field_na["info.trailing_eps"] == {"na": 0, "of": 2}
    assert isinstance(run.flagged_fields, list)


def test_manual_ticker_bypasses_screen(fx_provider, tmp_path):
    from screening.engine import analyse_manual

    res = analyse_manual(ScreenContext(provider=fx_provider, db_path=tmp_path / "r.db", today=TODAY), "MELI")
    assert res.decided_at_stage == 2 and res.sources == "manual"
    assert any("entered manually" in n for n in res.notes)


def test_unexpected_price_gap_is_failed_to_load(fx_provider, tmp_path):
    ctx = ScreenContext(provider=fx_provider, db_path=tmp_path / "r.db", today=TODAY)
    res = screen_ticker(ctx, "MSFT", "x", price=Datum.missing("N/A - Data Incomplete (price unavailable)"))
    assert res.status == STATUS_FAILED_TO_LOAD and res.load_error.startswith("price")
