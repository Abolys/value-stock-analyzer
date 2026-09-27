"""Run history (analysis_runs) write/read, monthly spend, estimate scoring (first
estimate per drawdown episode only; recovered / not yet / missed) and the Estimate
accuracy page's not-enough-data message."""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import config
from analysis.models import (
    AggregateResult, AnalysisRun, DevilsAdvocateResult, MacroResult, MoatResult, QuantResult,
)
from analysis.turnaround_models import COMPANY_SPECIFIC, CurrentDrawdown, Segment, TurnaroundResult
from storage import history, llm_store
from storage.history import MISSED, NOT_YET, RECOVERED, SAME_EPISODE, HistoryRow


def finished_run(ticker: str = "TEST", median: float = 6.0, iqr=(4.0, 9.0), high: date = date(2026, 1, 5)):
    cur = CurrentDrawdown(drawdown=0.3, as_of=date(2026, 9, 1), high_date=high, high_price=100.0, latest_price=70.0,
                          qualifying=True, episode_type=COMPANY_SPECIFIC)
    t = TurnaroundResult(ticker=ticker, median_months=median, iqr_months=iqr, confidence="Medium", current=cur,
                         segments=[Segment(index=0, start=date(2016, 1, 4), end=date(2026, 9, 1), closes=2600)])
    return AnalysisRun(ticker=ticker, quant=QuantResult(score=7.0), macro=MacroResult(score=6.0),
                       moat=MoatResult(status="Insufficient data - LLM not configured"),
                       devils_advocate=DevilsAdvocateResult(score=4.0),
                       aggregate=AggregateResult(score=5.7, verdict="neutral", lenses_used=3), turnaround=t,
                       input_hash="abc")


def test_run_history_write_and_read(db_path):
    aid = llm_store.create_analysis("TEST", db_path)
    llm_store.log_call(llm_store.LLMCallRecord(ticker="TEST", lens="devils_advocate", model="m", prompt_version="v",
                                               input_tokens=1000, output_tokens=200, cost=0.004, analysis_id=aid),
                       db_path)
    total = llm_store.finish_analysis(aid, finished_run(), db_path)
    assert total == pytest.approx(0.004)
    (row,) = history.load_history("TEST", db_path)
    assert row.aggregate_score == 5.7 and row.verdict == "neutral" and row.lenses_used == 3
    assert (row.quant_score, row.macro_score, row.moat_score, row.da_score) == (7.0, 6.0, None, 4.0)
    assert (row.turnaround_p25, row.turnaround_median, row.turnaround_p75) == (4.0, 6.0, 9.0)
    assert row.turnaround_confidence == "Medium" and row.episode_type == COMPANY_SPECIFIC
    assert row.episode_key == "TEST:2016-01-04:2026-01-05" and row.episode_high_date == date(2026, 1, 5)
    assert (row.input_tokens, row.output_tokens, row.input_hash) == (1000, 200, "abc")
    assert history.load_all_estimates(db_path)[0].analysis_id == aid


def test_failed_load_is_stored_without_estimate(db_path):
    aid = llm_store.create_analysis("BAD", db_path)
    llm_store.finish_analysis(aid, AnalysisRun(ticker="BAD", load_error="no data"), db_path)
    (row,) = history.load_history("BAD", db_path)
    assert row.verdict == "failed to load" and not row.has_estimate


def test_monthly_spend_sums_logged_costs(db_path):
    now = datetime(2026, 9, 15, 10)
    for cost, when, aid in ((0.01, now, 1), (0.02, now, 1), (0.05, now, 2), (0.0, now, 2),
                            (9.99, datetime(2026, 8, 31, 23), 3)):
        llm_store.log_call(llm_store.LLMCallRecord(ticker="T", lens="moat", model="m", prompt_version="v",
                                                   cost=cost, cache_hit=cost == 0, created_at=when, analysis_id=aid),
                           db_path)
    spend, calls, hits = llm_store.month_spend(date(2026, 9, 20), db_path)
    assert spend == pytest.approx(0.08) and calls == 4 and hits == 1
    assert llm_store.month_analyses(date(2026, 9, 20), db_path) == 2


def row(aid: int, created: datetime, key: str = "T:2016-01-04:2026-01-05", p75: float = 3.0,
        typ: str = COMPANY_SPECIFIC, ticker: str = "T") -> HistoryRow:
    return HistoryRow(analysis_id=aid, ticker=ticker, created_at=created, finished_at=created, aggregate_score=5.0,
                      turnaround_p25=1.0, turnaround_median=2.0, turnaround_p75=p75, episode_key=key,
                      episode_type=typ, episode_high_date=date(2026, 1, 5))


def closes(recover_on: date | None, end: date = date(2026, 12, 31)) -> pd.Series:
    idx = pd.bdate_range("2025-12-01", end)
    s = pd.Series(70.0, index=idx)
    s[s.index == pd.Timestamp("2026-01-05")] = 100.0  # the episode's prior high
    if recover_on:
        s[s.index >= pd.Timestamp(recover_on)] = 95.0  # within RECOVERY_BAND (10%) of 100
    return s


def test_two_runs_in_one_episode_score_once():
    rows = [row(1, datetime(2026, 3, 1)), row(2, datetime(2026, 4, 1)), row(3, datetime(2026, 4, 2), key="T:other")]
    scored = history.score_estimates(rows, {"T": closes(date(2026, 5, 1))}, date(2026, 9, 1))
    assert [s.status for s in scored] == [RECOVERED, SAME_EPISODE, RECOVERED]
    assert [s.scored for s in scored] == [True, False, True]
    summary = history.accuracy_summary(scored)
    assert summary.total_scored == 2 and summary.by_type[0].recovered == 2


def test_recovered_not_yet_and_missed():
    today = date(2026, 9, 1)
    start = datetime(2026, 3, 1)
    recovered = history.score_estimates([row(1, start)], {"T": closes(date(2026, 4, 15))}, today)[0]
    assert recovered.status == RECOVERED and recovered.recovered_on == date(2026, 4, 15)
    assert recovered.window_end == start.date() + timedelta(days=round(3.0 * config.DAYS_PER_MONTH))
    missed = history.score_estimates([row(1, start)], {"T": closes(date(2026, 8, 1))}, today)[0]
    assert missed.status == MISSED  # recovered only after the window closed
    open_ = history.score_estimates([row(1, datetime(2026, 8, 1))], {"T": closes(None)}, today)[0]
    assert open_.status == NOT_YET
    unscorable = history.score_estimates([row(1, start)], {"T": None}, today)[0]
    assert unscorable.status == history.UNSCORABLE


def test_scoring_edge_median(monkeypatch):
    monkeypatch.setattr(config, "ESTIMATE_SCORING_EDGE", "median")
    s = history.score_estimates([row(1, datetime(2026, 3, 1))], {"T": closes(date(2026, 5, 15))}, date(2026, 9, 1))[0]
    assert s.status == MISSED  # the median (2 months) window ends before May 15


@pytest.mark.parametrize("n,enough", [(3, False), (config.ACCURACY_MIN_SCORED, True)])
def test_accuracy_page_not_enough_message(n, enough):
    at = AppTest.from_string(_ACCURACY_SCRIPT.replace("__N__", str(n)), default_timeout=60)
    at.run()
    assert not at.exception
    msgs = " ".join(i.value for i in at.info)
    assert ("Not enough scored estimates yet" in msgs) is (not enough)
    if not enough:
        assert f"({n} of {config.ACCURACY_MIN_SCORED} needed)" in msgs


_ACCURACY_SCRIPT = """
import sys
from datetime import datetime
import pandas as pd
sys.path.insert(0, %r)
from app.views import accuracy
from storage import history as h
from tests.test_history import row
rows = [row(i, datetime(2026, 3, 1), key=f"T:{i}") for i in range(__N__)]
h.load_all_estimates = lambda path=None: rows
accuracy.closes_for = lambda provider, tickers: {t: pd.Series([100.0, 70.0],
    index=pd.to_datetime(["2026-01-05", "2026-02-01"])) for t in tickers}
accuracy.render(None)
""" % str(config.ROOT)
