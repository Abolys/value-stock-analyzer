"""Chart builders follow SPEC "Charts": asset floor, sensitivity heatmap, trap scores,
insider markers, dividend panel, 52-week bar, small multiples, dot strip, peer strip
and the drawdown history."""

from datetime import date

import pandas as pd
import pytest

import config
from analysis.models import AggregateResult, FundamentalSeries, LensResult, SeriesPoint
from analysis.turnaround_models import COMPANY_SPECIFIC, Episode, TurnaroundResult, Week52
from app import charts, theme
from data.form4 import InsiderTransaction
from data.values import Datum
from screening.models import SLOT_MOS
from signals.asset_floor import AssetFloor
from signals.dcf import ReverseDcf, SensitivityGrid
from signals.dividends import NO_DIVIDEND, DividendCut, DividendSafety
from signals.trap_scores import AltmanResult, BeneishResult, PiotroskiResult
from tests.screen_helpers import run_eval


def d(v):
    return Datum(value=v)


def _floor(ncav: float) -> AssetFloor:
    return AssetFloor(tbv=d(50.0), p_tbv=d(2.0), ncav=d(ncav), nnwc=d(-60.0), coverage=d(0.5),
                      coverage_band="partly covered")


def test_asset_floor_negative_ncav_left_of_zero_and_no_net_net_flag():
    out = charts.asset_floor_panel(_floor(-40.0), d(100.0))
    bar = out.fig.data[0]
    values = dict(zip(bar.y, bar.x))
    assert values["NCAV"] == -40.0 and values["NNWC"] == -60.0  # negative bars extend left of zero
    assert out.fig.layout.xaxis.range[0] < -60.0
    assert out.flags == []
    assert any(s.x0 == 100.0 for s in out.fig.layout.shapes)  # line at the market cap
    assert "50% of the price covered by tangible book (partly covered)" in out.caption


def test_asset_floor_net_net_flag_only_when_ncav_reaches_market_cap():
    assert charts.asset_floor_panel(_floor(99.0), d(100.0)).flags == []
    af = _floor(120.0)
    af.burn_line = "discount gone in ~5 quarters at current burn"
    flags = charts.asset_floor_panel(af, d(100.0)).flags
    assert len(flags) == 1 and "net current assets" in flags[0] and "~5 quarters" in flags[0]


def test_asset_floor_lists_nm_items():
    af = _floor(-40.0)
    af.tbv = Datum.missing("n/m - negative tangible book")
    out = charts.asset_floor_panel(af, d(100.0))
    assert "Tangible book" not in out.fig.data[0].y
    assert "Tangible book: n/m - negative tangible book" in out.excluded


def _grid(price: float) -> SensitivityGrid:
    rates = [0.07, 0.08, 0.09, 0.10, 0.11]
    growths = [0.0, 0.025, 0.05, 0.075, 0.10]
    values = [[price * (1.6 - 0.2 * i + 0.05 * j) for j in range(5)] for i in range(5)]
    values[2][2] = price * 1.03  # centre within the neutral band
    return SensitivityGrid(rates=rates, growths=growths, values=values)


def test_heatmap_colours_relative_to_price_and_outlines_base_case():
    grid = _grid(100.0)
    out = charts.sensitivity_heatmap(grid, d(100.0), ReverseDcf(implied_growth=0.04, history=d(0.06)))
    hm = out.fig.data[0]
    classes = hm.customdata
    assert classes[0][4] == "above" and hm.z[0][4] > 0  # 180 vs 100
    assert classes[4][0] == "below" and hm.z[4][0] < 0  # 80 vs 100
    assert classes[2][2] == "neutral" and hm.z[2][2] == 0  # 103: within HEATMAP_NEUTRAL_BAND
    base = next(s for s in out.fig.layout.shapes if s.name == "base case")
    assert base.x0 < grid.growths[2] < base.x1 and base.y0 < grid.rates[2] < base.y1
    assert any(s.type == "line" and s.x0 == 0.04 for s in out.fig.layout.shapes)  # reverse-DCF growth marked
    assert charts.heat_class(100 * (1 + config.HEATMAP_NEUTRAL_BAND), 100) == "neutral"


def test_trap_panel_insufficient_piotroski():
    pio = PiotroskiResult(score=None, available=5, status="Insufficient data - 5 of 9 checks available (needs 7)")
    out = charts.trap_panel(pio, AltmanResult(z=d(1.8), zone="grey"), BeneishResult(m=d(-2.4)))
    assert any(e.startswith("Piotroski: Insufficient data") for e in out.excluded)
    assert any("Insufficient data" in (a.text or "") for a in out.fig.layout.annotations)
    assert not any(getattr(t, "orientation", None) == "h" for t in out.fig.data)  # no Piotroski bar drawn
    assert "Beneish is probabilistic; false positives happen." in out.notes
    zones = [s for s in out.fig.layout.shapes if s.type == "rect"]
    assert len(zones) == 3  # Altman's three zones shaded


def _tx(kind: str, plan: bool = False, value: float = 1e5) -> InsiderTransaction:
    return InsiderTransaction(ticker="T", date=date(2026, 5, 1), insider="A. Person", role="CEO", type=kind,
                              shares=1000, price=10, value=value, is_10b5_1=plan)


def test_insider_markers_hollow_for_10b5_1_sales():
    closes = pd.Series([10.0 + i * 0.01 for i in range(300)], index=pd.bdate_range("2025-09-01", periods=300))
    series = FundamentalSeries(points={"total_revenue": [SeriesPoint(period_end=date(2026, 3, 31), value=100.0)]})
    out = charts.small_multiples(closes, series, [_tx("buy"), _tx("sell"), _tx("sell", plan=True)], "T")
    symbols = {t.name: t.marker.symbol for t in out.fig.data if t.name and "Insider" in t.name}
    assert symbols == {"Insider buy": "triangle-up", "Insider sale": "triangle-down",
                       "Insider sale (10b5-1 plan)": "triangle-down-open"}


def test_insider_marker_size_scales_with_value():
    lo, hi = config.INSIDER_MARKER_SIZE_RANGE
    assert charts._marker_sizes([1e6, 5e5, None]) == [hi, lo + (hi - lo) * 0.5, lo]


def test_dividend_panel_hidden_for_non_payers():
    assert charts.dividend_panel(DividendSafety(status=NO_DIVIDEND)) is None
    assert charts.dividend_panel(None) is None


def test_dividend_panel_highlights_cuts_and_lists_nm_payout_years():
    div = DividendSafety(annual_per_share={2022: 1.0, 2023: 1.1, 2024: 0.8},
                         cuts=[DividendCut(year=2024, previous=1.1, current=0.8, change=-0.27)],
                         fcf_payout_history={"FY ending Dec 2024": d(0.9),
                                             "FY ending Dec 2025": Datum.missing("n/m - FCF ≤ 0")})
    bars, payout = charts.dividend_panel(div)
    colours = dict(zip(bars.fig.data[0].x, bars.fig.data[0].marker.color))
    assert colours["2024"] == theme.DIVERGING_BELOW and colours["2023"] == theme.PRIMARY
    assert payout.fig.data[0].y == (0.9,) and "FY ending Dec 2025: n/m - FCF ≤ 0" in payout.excluded
    assert "yaxis2" not in payout.fig.layout  # the payout line is its own chart, never a second axis


def test_week52_bar_places_marker_and_labels_drawdown():
    w = Week52(low=50.0, high=100.0, latest=62.5, low_date=date(2026, 3, 1), high_date=date(2025, 11, 1),
               as_of=date(2026, 9, 25), drawdown=0.375)
    out = charts.week52_bar(w)
    marker = next(t for t in out.fig.data if t.name == "latest")
    assert marker.x == (62.5,) and marker.customdata[0][0] == pytest.approx(0.25)
    assert any(a.text == "−38% from high" for a in out.fig.layout.annotations)


def test_small_multiples_never_index_net_income():
    pts = [SeriesPoint(period_end=date(2025, 9, 30), value=-50.0), SeriesPoint(period_end=date(2025, 12, 31), value=20.0),
           SeriesPoint(period_end=date(2026, 3, 31), value=30.0)]
    series = FundamentalSeries(points={"net_income": pts}, freqs={"net_income": "quarterly"},
                               missing={"total_revenue": "N/A - field not found: total_revenue"})
    out = charts.small_multiples(None, series, [], "T")
    ni = next(t for t in out.fig.data if t.name == "Net income")
    assert list(ni.y) == [-50.0, 20.0, 30.0]  # own units, the loss kept negative
    assert "revenue: N/A - field not found: total_revenue" in out.excluded
    assert "price: history unavailable" in out.excluded
    assert any(s.type == "line" and s.y0 == 0 for s in out.fig.layout.shapes)  # zero line for the loss


def test_dot_strip_hollow_marker_for_insufficient_lens_and_controversy_gap():
    lenses = [LensResult(lens="quant", score=8.0), LensResult(lens="macro", score=7.0),
              LensResult(lens="moat", status="Insufficient data - LLM not configured"),
              LensResult(lens="devils_advocate", score=3.0)]
    agg = AggregateResult(score=6.0, controversy=True, controversy_gap=4.5, lenses_used=3)
    out = charts.dot_strip(lenses, agg)
    moat = next(t for t in out.fig.data if t.name == "Business Moat")
    assert moat.marker.symbol == "circle-open" and "Insufficient data" in moat.text[0]
    assert moat.x[0] != 0  # never a dot at zero
    gap = next(s for s in out.fig.layout.shapes if s.name == "controversy gap")
    assert gap.x0 == 3.0 and gap.x1 == 7.5
    assert any(s.type == "line" and s.x0 == 6.0 for s in out.fig.layout.shapes)  # aggregate line
    no_gap = charts.dot_strip(lenses, AggregateResult(score=6.0, lenses_used=3))
    assert not any(s.name == "controversy gap" for s in no_gap.fig.layout.shapes)


def test_peer_strip_lists_na_and_nm_peers():
    me = run_eval()
    peer_ok = run_eval(px=8.0)
    peer_ok.ticker, peer_ok.name = "PEER1", "Peer One"
    peer_nm = run_eval(px=8.0)
    peer_nm.ticker = "PEER2"
    peer_nm.metric(SLOT_MOS).value = Datum.missing("n/m - negative EPS")
    peer_nm.inputs.pop("ROIC", None)
    out = charts.peer_strip(me, [peer_ok, peer_nm], "2 peers from your universe lists")
    assert "PEER2 Margin of safety: n/m - negative EPS" in out.excluded
    assert "PEER2 ROIC: N/A - Data Incomplete" in out.excluded
    grey = next(t for t in out.fig.data if t.name == "peers")
    assert any("PEER1 — Peer One" in s for s in grey.text)  # peers named on hover
    assert "2 peers from your universe lists" in out.caption


def test_drawdown_history_indexes_both_to_100_on_one_axis():
    idx = pd.bdate_range("2024-01-01", periods=400)
    closes = pd.Series([50.0 + (i % 50) for i in range(400)], index=idx)
    bench = pd.Series([400.0 + i for i in range(400)], index=idx)
    ep = Episode(ticker="T", segment=0, peak_date=idx[10].date(), peak_price=60, threshold_date=idx[20].date(),
                 trough_date=idx[30].date(), trough_price=40, drop=0.33, episode_type=COMPANY_SPECIFIC)
    t = TurnaroundResult(ticker="T", benchmark="SPY", episodes=[ep])
    out = charts.drawdown_history(closes, bench, t, "T")
    lines = {tr.name: tr for tr in out.fig.data if tr.mode == "lines"}
    assert lines["T"].y[0] == pytest.approx(100) and lines["SPY"].y[0] == pytest.approx(100)
    assert "yaxis2" not in out.fig.layout
    assert any(s.type == "rect" for s in out.fig.layout.shapes)  # the episode is shaded
    assert any(tr.name == "company-specific drop" for tr in out.fig.data)  # legend entry by type


def test_indexing_refuses_non_positive_start():
    with pytest.raises(ValueError):
        charts.indexed(pd.Series([-1.0, 2.0], index=pd.bdate_range("2024-01-01", periods=2)),
                       pd.Timestamp("2024-01-01"))
