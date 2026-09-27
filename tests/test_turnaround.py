"""Turnaround timing (Phase 4) on synthetic price series with known episodes."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

import config
from analysis.models import AnalysisRun, DevilsAdvocateResult
from analysis.turnaround import (
    NO_SCREEN_RUN, VALUATION_FMP_TODO, PriceHistory, benchmark_drop, benchmark_for, build_history, classify,
    confidence_level, double_bottom, insider_signal, load_history, macd_crossover, recovery_stats, rolling_high,
    segments, select_peers, turnaround, valuation_recovery_months, williams_r_signal,
)
from analysis.turnaround_models import (
    BASIS_ALL_TYPES, BASIS_PEERS, BASIS_SAME_TYPE, COMPANY_SPECIFIC, MARKET_DRIVEN, PEER_LABEL,
    STATUS_NOT_IN_DRAWDOWN, STATUS_OK, STATUS_WITHHELD, STRUCTURAL_LABEL, VALUATION_UNAVAILABLE, Episode,
)
from data.corporate_actions import Break
from data.form4 import InsiderTransaction
from data.provider import DataProvider, ProviderError, ProviderUnavailable, SharesHistory
from data.values import Datum
from screening.models import ScreenResult
from signals.asset_floor import AssetFloor
from signals.insider_activity import InsiderSummary
from storage import screen_store
from tests.analysis_helpers import make_inputs
from tests.screen_helpers import TODAY


# --------------------------------------------------------------------------
# Synthetic series
# --------------------------------------------------------------------------
def build(legs, start=(100.0, 100.0)):
    """Stock and benchmark closes from legs of (trading days, stock target, benchmark target),
    each moving linearly from the previous level; the series ends on TODAY."""
    s, b = [start[0]], [start[1]]
    for n, st_t, bn_t in legs:
        s.extend(np.linspace(s[-1], st_t, n + 1)[1:])
        b.extend(np.linspace(b[-1], bn_t, n + 1)[1:])
    idx = pd.bdate_range(end=pd.Timestamp(TODAY), periods=len(s))
    return pd.Series(s, index=idx, dtype=float), pd.Series(b, index=idx, dtype=float)


def drop_and_recover(recover_days=60, bench_low=100.0, low=65.0):
    return [(40, low, bench_low), (recover_days, 100.0, 100.0), (30, 100.0, 100.0)]


def scenario_a(last=70.0):
    """Three company-specific drops that recovered (benchmark flat), then the current move."""
    legs = drop_and_recover(60) + drop_and_recover(120) + drop_and_recover(180) + [(40, last, 100.0)]
    return build(legs)


class FakeProvider(DataProvider):
    name = "fake"

    def __init__(self, series: dict[str, pd.Series]):
        self.series = series
        self.requested: list[str] = []

    def get_price_history(self, ticker, adjusted):
        self.requested.append(ticker)
        if ticker not in self.series:
            raise ProviderError(f"no price history for {ticker}")
        return self.series[ticker]

    def get_splits(self, ticker):
        return pd.Series(dtype=float)

    def get_shares_history(self, ticker):
        return SharesHistory(ticker=ticker, series=None)

    def get_info(self, ticker):
        raise ProviderUnavailable("fake")

    def get_statement(self, ticker, kind, freq):
        raise ProviderUnavailable("fake")

    def get_dividends(self, ticker):
        raise ProviderUnavailable("fake")

    def get_earnings_dates(self, ticker):
        raise ProviderUnavailable("fake")

    def get_analyst_estimates(self, ticker):
        raise ProviderUnavailable("fake")

    def batch_latest_prices(self, tickers):
        raise ProviderUnavailable("fake")


def inputs(ticker="TEST", **kw):
    x = make_inputs(**kw)
    x.ticker = ticker
    return x


def run_with_da(impairment="cyclical") -> AnalysisRun:
    return AnalysisRun(ticker="TEST", devils_advocate=DevilsAdvocateResult(score=4.0, impairment_type=impairment,
                                                                            impairment_reasoning="demand is gone"))


def estimate(stock, bench, ticker="TEST", run=None, x=None, db_path=None, **kw):
    prov = FakeProvider({ticker: stock, benchmark_for(ticker): bench})
    x = x or inputs(ticker)
    return turnaround(x, run or run_with_da(), prov, db_path or config.RUNS_DB_PATH, **kw), prov


# --------------------------------------------------------------------------
# Qualifying drawdown or not
# --------------------------------------------------------------------------
def test_stock_8pct_off_is_not_in_a_qualifying_drawdown_but_keeps_its_history():
    stock, bench = scenario_a(last=92.0)
    r, _ = estimate(stock, bench)
    assert r.status == STATUS_NOT_IN_DRAWDOWN
    assert r.headline == "Not in a qualifying drawdown (currently −8% from 52-week high)"
    assert not r.has_range and r.median_months is None and r.confidence is None
    assert len(r.episodes) == 3 and r.recovered_count == 3


def test_stock_30pct_off_returns_a_range_from_same_type_episodes():
    stock, bench = scenario_a()
    r, _ = estimate(stock, bench)
    assert r.status == STATUS_OK and r.has_range
    assert r.current.qualifying and r.current.label == "−30% from 52-week high"
    assert r.current.episode_type == COMPANY_SPECIFIC
    assert r.basis == BASIS_SAME_TYPE and r.episodes_used == 3
    assert r.headline.endswith("based on 3 past company-specific drops") and "months" in r.headline
    assert r.confidence == "Medium"
    lo, hi = r.iqr_months
    assert lo <= r.median_months <= hi


def test_recovery_months_run_from_the_trough():
    stock, bench = scenario_a()
    r, _ = estimate(stock, bench)
    for e in (e for e in r.episodes if e.recovered):
        assert e.recovery_months == pytest.approx((e.recovery_date - e.trough_date).days / config.DAYS_PER_MONTH)
        assert e.trough_price < 0.75 * e.peak_price + 1e-9 and e.peak_date < e.trough_date < e.recovery_date


def test_unrecovered_episodes_are_counted_not_dropped():
    stock, bench = scenario_a()
    r, _ = estimate(stock, bench)
    open_eps = [e for e in r.episodes if not e.recovered]
    assert r.unrecovered_count == 1 and len(open_eps) == 1
    assert open_eps[0].recovery_months is None and "still below the prior high" in open_eps[0].unrecovered_reason
    assert "1 unrecovered" in r.rationale


# --------------------------------------------------------------------------
# Episode classification
# --------------------------------------------------------------------------
def test_stock_and_benchmark_falling_together_is_market_driven():
    stock, bench = build([(40, 60.0, 70.0), (40, 100.0, 100.0)])
    ep = build_history(PriceHistory(ticker="T", closes=stock), bench, "SPY").episodes[0]
    assert ep.episode_type == MARKET_DRIVEN and ep.benchmark_drop == pytest.approx(0.30)


def test_stock_falling_alone_is_company_specific():
    stock, bench = build([(40, 60.0, 100.0), (40, 100.0, 100.0)])
    ep = build_history(PriceHistory(ticker="T", closes=stock), bench, "SPY").episodes[0]
    assert ep.episode_type == COMPANY_SPECIFIC and ep.benchmark_drop == pytest.approx(0.0)


def test_benchmark_drop_exactly_at_market_driven_ratio_counts_as_market_driven():
    stock, bench = build([(40, 60.0, 80.0)])  # stock −40%, benchmark −20% = 0.5 × the stock's drop
    bd, _ = benchmark_drop(bench, stock.index[0].date(), stock.index[-1].date())
    assert bd / (1 - 60 / 100) == pytest.approx(config.MARKET_DRIVEN_RATIO)
    assert classify(1 - 60 / 100, bd) == MARKET_DRIVEN
    assert classify(1 - 60 / 100, bd - 0.01) == COMPANY_SPECIFIC


def test_missing_benchmark_leaves_episodes_unclassified_with_the_reason():
    stock, _ = build([(40, 60.0, 100.0), (40, 100.0, 100.0)])
    ep = build_history(PriceHistory(ticker="T", closes=stock), None, "SPY", "SPY history unavailable").episodes[0]
    assert ep.episode_type == "unclassified" and "SPY history unavailable" in ep.classification_note


def test_canadian_tickers_use_the_canadian_benchmark():
    assert benchmark_for("CNR.TO") == "^GSPTSE" and benchmark_for("MSFT") == "SPY"
    stock, bench = scenario_a()
    r, prov = estimate(stock, bench, ticker="ABC.TO")
    assert r.benchmark == "^GSPTSE" and r.listing_country == "CA"
    assert "^GSPTSE" in prov.requested and "SPY" not in prov.requested
    assert all(e.benchmark == "^GSPTSE" and e.episode_type == COMPANY_SPECIFIC for e in r.episodes)


# --------------------------------------------------------------------------
# Same-type estimate, fallback to all types
# --------------------------------------------------------------------------
def test_estimate_uses_only_episodes_of_the_current_type():
    legs = (drop_and_recover(150) * 3  # three slow company-specific recoveries
            + [(40, 65.0, 75.0), (20, 100.0, 100.0), (30, 100.0, 100.0)] * 3  # three fast market-driven ones
            + [(40, 70.0, 100.0)])
    stock, bench = build(legs)
    r, _ = estimate(stock, bench)
    same = [e.recovery_months for e in r.episodes if e.recovered and e.episode_type == COMPANY_SPECIFIC]
    other = [e.recovery_months for e in r.episodes if e.recovered and e.episode_type == MARKET_DRIVEN]
    assert len(same) == 3 and len(other) == 3
    assert r.basis == BASIS_SAME_TYPE and r.episodes_used == 3
    assert r.median_months == pytest.approx(float(np.median(same)))
    assert r.median_months > max(other)


def test_fallback_to_all_types_says_so_and_lowers_confidence():
    legs = (drop_and_recover(60)
            + [(40, 65.0, 75.0), (60, 100.0, 100.0), (30, 100.0, 100.0)] * 3
            + [(40, 70.0, 100.0)])
    stock, bench = build(legs)
    r, _ = estimate(stock, bench)
    assert r.current.episode_type == COMPANY_SPECIFIC
    assert r.basis == BASIS_ALL_TYPES and r.episodes_used == 4
    assert "only 1 past company-specific drop" in r.basis_note and "confidence lowered one level" in r.basis_note
    assert r.confidence == "Low"  # 4 episodes → Medium, minus one level
    assert "past drops of all types" in r.headline


def test_recovery_stats_and_confidence_rule_directly():
    def ep(t, m):
        return Episode(ticker="T", segment=0, peak_date=TODAY, peak_price=1, threshold_date=TODAY, trough_date=TODAY,
                       trough_price=0.5, drop=0.5, recovered=True, recovery_months=m, episode_type=t)

    stats, fallback, _ = recovery_stats([ep(COMPANY_SPECIFIC, m) for m in (2, 4, 6)] + [ep(MARKET_DRIVEN, 50)],
                                        COMPANY_SPECIFIC)
    assert not fallback and stats.median_months == 4 and stats.episodes_used == 3
    stats, fallback, _ = recovery_stats([ep(COMPANY_SPECIFIC, 2), ep(MARKET_DRIVEN, 50)], COMPANY_SPECIFIC)
    assert stats is None and fallback
    assert [confidence_level(n, 0) for n in (2, 3, 6)] == ["Low", "Medium", "High"]
    assert confidence_level(6, 1) == "Medium" and confidence_level(3, 2) == "Low"


# --------------------------------------------------------------------------
# Corporate-action breaks
# --------------------------------------------------------------------------
def _broken_series():
    pre, _ = build([(100, 100.0, 100.0), (30, 60.0, 100.0)])  # falls 40% before the break
    post_idx = pd.bdate_range(pre.index[-1] + timedelta(days=1), periods=80)
    post = pd.Series(np.linspace(10.0, 12.0, 80), index=post_idx)  # new equity at a tenth of the price
    return pd.concat([pre, post]), post_idx[0].date()


def test_no_episode_or_rolling_high_crosses_a_break():
    closes, brk = _broken_series()
    h = build_history(PriceHistory(ticker="T", closes=closes, breaks=[Break(date=brk, type="test")]), None, "SPY")
    assert [s.start for s in h.segments][1] == brk and len(h.segments) == 2
    assert rolling_high(segments(closes, [brk])[1]).iloc[0] == pytest.approx(10.0)
    assert all(e.segment == 0 and (e.recovery_date or e.trough_date) < brk for e in h.episodes)
    cut = h.episodes[-1]
    assert not cut.recovered and f"corporate-action break {brk}" in cut.unrecovered_reason
    assert not h.current.qualifying  # post-break new equity is at its own high, not 90% below the old one
    # Without the break the same series would read as one long drawdown into the new equity.
    joined = build_history(PriceHistory(ticker="T", closes=closes), None, "SPY")
    assert joined.episodes[-1].trough_date >= brk


def test_no_htz_episode_crosses_the_2021_break(fx_provider):
    h = load_history(fx_provider, "HTZ")
    brk = next(b.date for b in h.breaks if b.date.isoformat() == "2021-07-01")
    old_idx = pd.bdate_range(end=pd.Timestamp(brk) - timedelta(days=1), periods=200)
    old = pd.Series(np.linspace(50.0, 5.0, 200), index=old_idx)  # the pre-bankruptcy equity's collapse
    h.closes = pd.concat([old, h.closes])
    hist = build_history(h, None, "SPY")
    assert hist.segments[0].end < brk <= hist.segments[1].start
    for e in hist.episodes:
        assert (e.peak_date < brk) == ((e.recovery_date or e.trough_date) < brk), e


# --------------------------------------------------------------------------
# Peers
# --------------------------------------------------------------------------
PEER_CAPS = {"P1": 1.1e9, "P2": 0.9e9, "P3": 2e9, "P4": 0.5e9, "P5": 3e9, "P6": 10e9, "P7": 0.05e9}


def _peer_world(tmp_path):
    db = tmp_path / "runs.db"
    run_id = screen_store.create_run(["watchlist"], status=screen_store.COMPLETED, path=db)
    for t, cap in {**PEER_CAPS, "Q1": 1e9}.items():
        screen_store.write_result(run_id, ScreenResult(ticker=t, industry="Widgets" if t != "Q1" else "Gadgets",
                                                       market_cap=Datum(value=cap)), db)
    uni = tmp_path / "universe"
    uni.mkdir()
    rows = [f"{t},{t} Corp,Watchlist,2026-01" for t in [*PEER_CAPS, "Q1", "U1", "TEST"]]
    (uni / "watchlist.csv").write_text("ticker,name,source,as_of\n" + "\n".join(rows) + "\n")
    return db, uni


def test_select_peers_same_industry_nearest_market_cap_from_the_universe(tmp_path):
    db, uni = _peer_world(tmp_path)
    sel = select_peers("TEST", "Widgets", 1e9, db, ["watchlist"], uni)
    assert sel.ok and {p.ticker for p in sel.peers} == {"P1", "P2", "P3", "P4", "P5"}
    assert sel.peers[0].ticker == "P1" and sel.not_screened == 1
    assert "universe lists (Watchlist)" in sel.source_note and "screen run" in sel.source_note


def test_select_peers_without_a_screen_run_is_unavailable(tmp_path):
    _, uni = _peer_world(tmp_path)
    sel = select_peers("TEST", "Widgets", 1e9, tmp_path / "empty.db", ["watchlist"], uni)
    assert sel.status == NO_SCREEN_RUN and not sel.ok


def test_peer_fallback_below_min_episodes(tmp_path):
    db, uni = _peer_world(tmp_path)
    stock, _ = build(drop_and_recover(60) + [(40, 70.0, 100.0)])  # one recovered episode of its own
    peer, bench = build(drop_and_recover(60) + drop_and_recover(90) + [(40, 70.0, 100.0)])  # SPY covers both
    prov = FakeProvider({"TEST": stock, "SPY": bench, **{t: peer for t in PEER_CAPS}})
    x = inputs()
    x.screen.industry, x.screen.market_cap = "Widgets", Datum(value=1e9)
    r = turnaround(x, run_with_da(), prov, db, ["watchlist"], uni)
    assert r.status == STATUS_OK and r.basis == BASIS_PEERS
    assert PEER_LABEL in r.headline and PEER_LABEL in r.basis_note
    assert {p.ticker for p in r.peer.peers} == {"P1", "P2", "P3", "P4", "P5"} and "universe" in r.peer.source_note
    assert r.episodes_used == 10 and r.confidence == "Medium"  # 10 → High, minus one for peer-based
    assert "past company-specific drops at 5 peers" in r.headline
    assert "P6" not in prov.requested and "Q1" not in prov.requested


def test_no_peer_fallback_when_there_are_enough_own_episodes(tmp_path):
    stock, bench = scenario_a()
    r, _ = estimate(stock, bench)
    assert r.peer is None and r.basis == BASIS_SAME_TYPE


# --------------------------------------------------------------------------
# Structural flag, caveat, asset floor, valuation
# --------------------------------------------------------------------------
def test_structural_flag_withholds_the_estimate_by_default():
    stock, bench = scenario_a()
    r, _ = estimate(stock, bench, run=run_with_da("structural"))
    assert r.status == STATUS_WITHHELD and r.structural_flag
    assert not r.has_range and r.confidence is None and "Withheld" in r.headline
    assert len(r.episodes) == 4  # the history is still there for the chart


def test_structural_flag_downgrades_when_configured(monkeypatch):
    monkeypatch.setattr(config, "TURNAROUND_STRUCTURAL_ACTION", "downgrade")
    stock, bench = scenario_a()
    r, _ = estimate(stock, bench, run=run_with_da("structural"))
    assert r.has_range and r.confidence == "Low" and r.headline.startswith(STRUCTURAL_LABEL)


def test_structural_flag_is_not_overridden_by_signals():
    stock, bench = scenario_a()
    x = inputs()
    x.insiders = _insiders([TODAY - timedelta(days=d) for d in (20, 15, 10)])
    r, _ = estimate(stock, bench, run=run_with_da("structural"), x=x)
    assert r.active_signals and r.status == STATUS_WITHHELD and not r.has_range


def test_survivorship_caveat_is_always_present():
    stock, bench = scenario_a()
    near, _ = scenario_a(last=92.0)
    results = [estimate(stock, bench)[0], estimate(near, bench)[0],
               estimate(stock, bench, run=run_with_da("structural"))[0],
               turnaround(inputs(), run_with_da(), FakeProvider({}))]
    assert [r.status for r in results][:3] == [STATUS_OK, STATUS_NOT_IN_DRAWDOWN, STATUS_WITHHELD]
    assert results[3].status.startswith("Insufficient data - price history unavailable")
    for r in results:
        assert r.survivorship_caveat == config.TURNAROUND_SURVIVORSHIP_CAVEAT
        assert r.survivorship_caveat in r.rationale


def test_asset_floor_line_appears_and_never_changes_range_or_confidence():
    stock, bench = scenario_a()
    x_with = inputs()
    x_with.screen.asset_floor = AssetFloor(coverage=Datum(value=0.85), coverage_band="partly covered")
    x_without = inputs()
    x_without.screen.asset_floor = None
    a, _ = estimate(stock, bench, x=x_with)
    b, _ = estimate(stock, bench, x=x_without)
    assert a.asset_floor_line == "Asset floor: 85% of the price covered by tangible book (partly covered)"
    assert a.asset_floor_line in a.rationale and b.asset_floor_line.startswith("Asset floor: N/A")
    assert (a.headline, a.iqr_months, a.median_months, a.confidence, a.confidence_reasons) == \
           (b.headline, b.iqr_months, b.median_months, b.confidence, b.confidence_reasons)


def test_valuation_recovery_is_unavailable_without_fmp(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "")
    stock, bench = scenario_a()
    r, _ = estimate(stock, bench)
    assert r.valuation_recovery == VALUATION_UNAVAILABLE
    assert "Debt maturity dates: not shown" in r.debt_maturity_note
    monkeypatch.setenv("FMP_API_KEY", "key")
    assert estimate(stock, bench)[0].valuation_recovery == VALUATION_FMP_TODO


def test_valuation_recovery_helper_measures_spells_back_to_the_median():
    idx = pd.date_range(end="2026-01-31", periods=4 * 365, freq="D")
    pe = pd.Series(10.0, index=idx)
    pe.iloc[400:491] = 5.0  # 91 days on the cheap side
    med, spells = valuation_recovery_months(pe)
    assert med == 10.0 and spells == [pytest.approx(91 / config.DAYS_PER_MONTH)]
    fcf_yield = pd.Series(0.05, index=idx)
    fcf_yield.iloc[400:461] = 0.10  # FCF yield: cheap is above the median
    assert valuation_recovery_months(fcf_yield, cheap_below=False)[1] == [pytest.approx(61 / config.DAYS_PER_MONTH)]


# --------------------------------------------------------------------------
# Near-term signals
# --------------------------------------------------------------------------
def _insiders(dates) -> InsiderSummary:
    buys = [InsiderTransaction(ticker="TEST", date=d, insider=f"Insider {i}", role="Director", type="buy",
                               shares=1000, price=10, value=10000, source="manual") for i, d in enumerate(dates)]
    since = TODAY - timedelta(days=183)
    return InsiderSummary(coverage="manual", since=since, buyers=len(buys), buys=buys)


def test_insider_cluster_buy_inside_the_drawdown_is_a_near_term_signal():
    stock, bench = scenario_a()
    x = inputs()
    x.insiders = _insiders([TODAY - timedelta(days=d) for d in (20, 15, 10)])
    r, _ = estimate(stock, bench, x=x)
    sig = next(s for s in r.signals if s.name.startswith("Insider cluster buy"))
    assert sig.active and "3 insiders bought" in sig.detail
    assert sig in r.active_signals and "Insider cluster buy" in r.rationale


def test_insider_cluster_buy_before_the_drawdown_is_not():
    stock, bench = scenario_a()
    r, _ = estimate(stock, bench)
    high = r.current.high_date
    sig = insider_signal(_insiders([high - timedelta(days=d) for d in (30, 20, 10)]), r.current, TODAY)
    assert not sig.active and "no cluster buy" in sig.detail


def test_no_insider_signal_without_a_qualifying_drawdown():
    near, bench = scenario_a(last=92.0)
    r, _ = estimate(near, bench)
    assert insider_signal(_insiders([TODAY - timedelta(days=d) for d in (3, 2, 1)]), r.current, TODAY).detail \
        == "no qualifying drawdown"


def test_macd_bullish_crossover():
    down, _ = build([(80, 60.0, 0.0)])
    up = pd.concat([down, pd.Series([62.0, 65.0, 69.0], index=pd.bdate_range(down.index[-1] + timedelta(days=1),
                                                                               periods=3))])
    assert macd_crossover(up).active
    steady, _ = build([(120, 200.0, 0.0)])
    assert not macd_crossover(steady).active
    assert "Insufficient data" in macd_crossover(steady.iloc[:10]).detail


def test_williams_r_rising_out_of_oversold():
    fall, _ = build([(30, 70.0, 0.0)])
    bounce = pd.concat([fall, pd.Series([74.0, 80.0], index=pd.bdate_range(fall.index[-1] + timedelta(days=1),
                                                                          periods=2))])
    sig = williams_r_signal(bounce)
    assert sig.active and sig.params["basis"] == "close-based"
    assert not williams_r_signal(fall).active  # still pinned at the lows
    rising, _ = build([(40, 150.0, 0.0)])
    assert not williams_r_signal(rising).active
    flat = pd.Series(50.0, index=pd.bdate_range(end=pd.Timestamp(TODAY), periods=30))
    assert not williams_r_signal(flat).active


def test_double_bottom_forming():
    s, _ = build([(30, 70.0, 0.0), (20, 85.0, 0.0), (20, 70.5, 0.0), (10, 78.0, 0.0)])
    sig = double_bottom(s)
    assert sig.active and "neckline" in sig.detail
    broke_out, _ = build([(30, 70.0, 0.0), (20, 85.0, 0.0), (20, 70.5, 0.0), (20, 90.0, 0.0)])
    assert not double_bottom(broke_out).active  # above the neckline: no longer "forming"
    slide, _ = build([(100, 50.0, 0.0)])
    assert not double_bottom(slide).active


# --------------------------------------------------------------------------
# Pipeline and catalysts
# --------------------------------------------------------------------------
def test_catalysts_include_da_requirements_and_leadership_status():
    stock, bench = scenario_a()
    run = run_with_da()
    run.devils_advocate.bull_case_requirements = ["margins recover to 12%"]
    r, _ = estimate(stock, bench, run=run)
    kinds = {c.kind for c in r.catalysts}
    assert {"earnings", "leadership", "lens"} <= kinds
    assert any(c.text == "margins recover to 12%" and "Devil's Advocate" in c.source for c in r.catalysts)


def test_run_analysis_attaches_the_turnaround(fx_provider, tmp_path):
    from analysis.pipeline import run_analysis
    from data.fixture_provider import captured_on
    from llm.cache import LLMCache
    from llm.client import LLMClient
    from screening.engine import ScreenContext
    from tests.llm_fakes import FakeAPI

    llm = LLMClient(api=FakeAPI(), cache=LLMCache(tmp_path / "llm.db"), db_path=tmp_path / "runs.db")
    seen = []
    run = run_analysis(ScreenContext(provider=fx_provider, db_path=tmp_path / "runs.db", today=captured_on("LULU")),
                       "LULU", llm=llm, on_result=lambda n, r: seen.append(n))
    assert not run.errors and run.turnaround is not None and seen[-1] == "turnaround"
    assert run.turnaround.benchmark == "SPY" and run.turnaround.episodes
    stored = AnalysisRun.model_validate_json(run.model_dump_json())
    assert stored.turnaround.episodes == run.turnaround.episodes
