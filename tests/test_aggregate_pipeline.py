"""Aggregate verdict and the lens pipeline (concurrency, ordering, storage)."""

import threading
import time

import pytest

import config
from analysis.aggregate import aggregate, verdict_label
from analysis.models import DevilsAdvocateResult, MacroResult, MoatResult, QuantResult
from analysis.pipeline import run_analysis, run_lenses
from llm.cache import LLMCache
from llm.client import LLMClient
from screening.engine import ScreenContext
from storage import llm_store
from tests.analysis_helpers import make_inputs
from tests.llm_fakes import FakeAPI


def scored(cls, score):
    return cls(score=score)


def missing(cls, why="Insufficient data - LLM not configured"):
    return cls(status=why)


def test_aggregate_weighted_mean_of_all_four():
    a = aggregate({"quant": scored(QuantResult, 8), "macro": scored(MacroResult, 6),
                   "moat": scored(MoatResult, 7), "devils_advocate": scored(DevilsAdvocateResult, 5)})
    assert a.score == pytest.approx(6.5) and a.display == "6.5 (4 of 4 lenses)" and a.verdict == "lean bullish"


def test_aggregate_renormalises_when_a_lens_is_missing():
    a = aggregate({"quant": scored(QuantResult, 8), "macro": scored(MacroResult, 6),
                   "moat": missing(MoatResult), "devils_advocate": scored(DevilsAdvocateResult, 4)})
    assert a.score == pytest.approx(6.0)  # (8 + 6 + 4) / 3, never averaged in as zero
    assert a.lenses_used == 3 and a.display == "6.0 (3 of 4 lenses)"
    assert sum(a.weights_used.values()) == pytest.approx(1.0) and "moat" not in a.weights_used
    assert "LLM not configured" in a.missing["moat"] and "Excluded moat" in a.rationale


def test_aggregate_with_nothing_scored():
    a = aggregate({"quant": missing(QuantResult)})
    assert a.score is None and a.display.startswith("Insufficient data")


@pytest.mark.parametrize("da,fires", [(4.0, True), (3.5, True), (4.01, False)])
def test_controversy_flag_at_threshold(da, fires):
    # Others' mean = 7.0; CONTROVERSY_GAP = 3.0 → fires when DA ≤ 4.0.
    a = aggregate({"quant": scored(QuantResult, 8), "macro": scored(MacroResult, 6),
                   "moat": scored(MoatResult, 7), "devils_advocate": scored(DevilsAdvocateResult, da)})
    assert a.controversy is fires
    assert a.controversy_gap == pytest.approx(7.0 - da)


@pytest.mark.parametrize("score,label", [(3.9, "bearish"), (4.0, "neutral"), (5.9, "neutral"), (6.0, "lean bullish"),
                                         (7.4, "lean bullish"), (7.5, "bullish")])
def test_verdict_bands(score, label):
    assert verdict_label(score) == label


def test_quant_macro_moat_run_concurrently_and_da_only_after(tmp_path):
    barrier = threading.Barrier(3, timeout=5)  # only passes if all three lenses are running at once
    ends: dict[str, float] = {}
    started: dict[str, float] = {}
    threads: dict[str, str] = {}

    def lens(name, cls, score):
        def run(x, *rest):
            started[name] = time.monotonic()
            threads[name] = threading.current_thread().name
            if name != "devils_advocate":
                barrier.wait()
                time.sleep(0.05)
            ends[name] = time.monotonic()
            return cls(score=score)
        return run

    seen = []
    llm = LLMClient(api=FakeAPI(), cache=LLMCache(tmp_path / "c.db"), db_path=tmp_path / "r.db")
    run = run_lenses(make_inputs(), llm, on_result=lambda n, r: seen.append(n), lenses={
        "quant": lens("quant", QuantResult, 6), "macro": lens("macro", MacroResult, 6),
        "moat": lens("moat", MoatResult, 6), "devils_advocate": lens("devils_advocate", DevilsAdvocateResult, 6)})
    assert len({threads[n] for n in ("quant", "macro", "moat")}) == 3
    assert started["devils_advocate"] >= max(ends["quant"], ends["macro"], ends["moat"])
    assert set(seen[:3]) == {"quant", "macro", "moat"} and seen[3:] == ["devils_advocate", "aggregate"]
    assert run.aggregate.score == 6 and not run.errors


def test_a_failing_lens_is_recorded_not_fatal(tmp_path):
    def boom(x):
        raise ValueError("bad input")

    llm = LLMClient(api=FakeAPI(), cache=LLMCache(tmp_path / "c.db"), db_path=tmp_path / "r.db")
    run = run_lenses(make_inputs(), llm, lenses={"macro": boom})
    assert run.macro.status.startswith("Insufficient data - lens error") and run.errors
    assert run.aggregate.lenses_used == 3


def test_run_analysis_stores_run_and_costs(tmp_path, fx_provider):
    from data.fixture_provider import captured_on

    db = tmp_path / "runs.db"
    llm = LLMClient(api=FakeAPI(), cache=LLMCache(tmp_path / "c.db"), db_path=db)
    ctx = ScreenContext(provider=fx_provider, db_path=db, today=captured_on("MELI"))
    run = run_analysis(ctx, "MELI", llm=llm)
    assert run.analysis_id is not None and not run.errors
    calls = llm_store.calls(run.analysis_id, path=db)
    assert {c.lens for c in calls} == {"moat", "devils_advocate"}
    assert run.total_cost > 0 and run.total_cost == pytest.approx(sum(c.cost for c in calls))
    spend, n, hits = llm_store.month_spend(today=calls[0].created_at.date(), path=db)
    assert spend == pytest.approx(run.total_cost) and n == 2 and hits == 0


def test_unknown_ticker_fails_to_load_cleanly(tmp_path, fx_provider):
    db = tmp_path / "runs.db"
    llm = LLMClient(api=FakeAPI(), cache=LLMCache(tmp_path / "c.db"), db_path=db)
    run = run_analysis(ScreenContext(provider=fx_provider, db_path=db), "NOPE", llm=llm)
    assert run.load_error and run.quant is None


def test_a_fund_gets_the_price_based_view_without_lenses_or_llm_calls(fx_provider, tmp_path):
    from analysis.pipeline import FUND_VERDICT, run_analysis
    from data.fixture_provider import captured_on
    from llm.cache import LLMCache
    from llm.client import LLMClient
    from screening.engine import ScreenContext
    from tests.llm_fakes import FakeAPI

    api = FakeAPI()
    llm = LLMClient(api=api, cache=LLMCache(tmp_path / "c.db"), db_path=tmp_path / "r.db")
    run = run_analysis(ScreenContext(provider=fx_provider, db_path=tmp_path / "r.db", today=captured_on("SPY")), "SPY",
                       llm=llm)
    assert not run.load_error, run.load_error
    assert run.fund and api.requests == []  # no Moat / Devil's Advocate calls for a fund
    assert all(run.lens(n) is None for n in ("quant", "macro", "moat", "devils_advocate"))
    assert run.aggregate.display == FUND_VERDICT
    assert run.turnaround is not None and run.week52 is not None  # the price-based view still runs
