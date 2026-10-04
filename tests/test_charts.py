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
from screening.models import SLOT_FCF, SLOT_LEVERAGE, SLOT_MOS
from signals.asset_floor import AssetFloor
from signals.dcf import ReverseDcf, SensitivityGrid
from signals.dividends import NO_DIVIDEND, DividendCut, DividendSafety
from signals.trap_scores import AltmanResult, BeneishResult, NM_FINANCIALS, PiotroskiResult, nm
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
    assert not any(t.hovertemplate and t.hovertemplate.startswith("Piotroski") for t in out.fig.data)  # no marker
    assert "Beneish is probabilistic; false positives happen." in out.notes
    rects = {}
    for s in out.fig.layout.shapes:
        if s.type == "rect":
            rects[s.yref] = rects.get(s.yref, 0) + 1
    assert rects == {"y": 3, "y2": 3, "y3": 1}  # Piotroski bands, Altman zones, Beneish flag side


def test_trap_panel_missing_notes_are_pinned_to_their_own_rows():
    """The 'n/m / N/A' notes for rows without data sit in the top headroom of their OWN row,
    not on top of the shaded bands or the Beneish threshold vline: per-row domain refs with a
    top-left anchor, subplot titles untouched."""
    pio = PiotroskiResult(status=nm(NM_FINANCIALS))
    alt = AltmanResult(z=Datum.missing(nm(NM_FINANCIALS)))
    ben = BeneishResult(m=Datum.missing(nm(NM_FINANCIALS)))
    out = charts.trap_panel(pio, alt, ben)
    notes = [a for a in out.fig.layout.annotations if "not meaningful" in (a.text or "")]
    assert [a.xref for a in notes] == ["x domain", "x2 domain", "x3 domain"]
    assert [a.yref for a in notes] == ["y domain", "y2 domain", "y3 domain"]
    assert all(a.x == 0 and a.xanchor == "left" and a.y == 1 and a.yanchor == "top" for a in notes)
    # the Beneish note is anchored at the row's left edge, clear of the threshold vline whose
    # "flag above …" label occupies the top-right of the line
    flag = next(a for a in out.fig.layout.annotations if "flag above" in (a.text or ""))
    assert flag.xref == "x3" and flag.x == config.BENEISH_THRESHOLD
    # the subplot titles stay untouched
    titles = out.fig.layout.annotations[:3]
    assert all(t.xref == "paper" and t.yref == "paper" and t.x == 0 and t.xanchor == "left" for t in titles)


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


def test_dividend_payout_for_financials_uses_earnings():
    div = DividendSafety(annual_per_share={2024: 4.0, 2025: 4.6}, payout_basis="earnings",
                         fcf_payout_history={"FY ending Dec 2025": Datum.missing("n/m - FCF not meaningful")},
                         earnings_payout_history={"FY ending Dec 2024": d(0.28), "FY ending Dec 2025": d(0.26)})
    _, payout = charts.dividend_panel(div)
    assert payout.title == "Earnings payout by fiscal year" and payout.fig.data[0].y == (0.28, 0.26)
    assert payout.fig.layout.shapes[0].y0 == config.DIVIDEND_EARNINGS_PAYOUT_MAX_FINANCIALS


def test_dividend_payout_chart_left_off_when_no_year_has_a_value():
    div = DividendSafety(annual_per_share={2024: 1.0},
                         fcf_payout_history={"FY ending Dec 2025": Datum.missing("n/m - FCF ≤ 0")})
    bars, payout = charts.dividend_panel(div)
    assert payout is None  # never an empty frame
    assert any("FCF payout chart: no fiscal year with a value" in e and "n/m - FCF ≤ 0" in e for e in bars.excluded)


def test_jpm_dividend_payout_is_plotted_on_earnings(fx_provider, db_path):
    from analysis.pipeline import view_run
    from tests.analysis_helpers import fixture_inputs

    run = view_run(fixture_inputs(fx_provider, "JPM", db_path))
    assert run.dividends.payout_basis == "earnings"
    _, payout = charts.dividend_panel(run.dividends)
    assert payout is not None and len(payout.fig.data[0].y) >= 2 and all(0 < v < 1 for v in payout.fig.data[0].y)


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


def test_peer_strip_missing_metric_notes_are_pinned_to_their_own_row():
    """Each 'n/m / N/A' note must be anchored to its own subplot row, in that row's top headroom and
    left-anchored. A whole-figure anchor stacks every note on top of each other; the old
    annotations[:4] patch loop hit the subplot titles instead of the notes (leaving them
    right-anchored mid-row, at the dots' height)."""
    me = run_eval(px=6.0)
    me.ticker, me.name = "BHF", "Berkshire Hathaway"
    me.treatment = "Sector-adjusted"
    me.metric(SLOT_FCF).name = "ROE − cost of capital (9%)"
    me.metric(SLOT_LEVERAGE).name = "Price / book"
    me.inputs.pop("ROIC", None)
    peer1, peer2 = run_eval(px=6.0), run_eval(px=7.0)
    peer1.ticker, peer1.name = "PEER1", "Peer One"
    peer2.ticker, peer2.name = "PEER2", "Peer Two"
    out = charts.peer_strip(me, [peer1, peer2])
    notes = [a for a in out.fig.layout.annotations if a.text.startswith("BHF:")]
    assert [a.text for a in notes] == [
        "BHF: n/m - sector-adjusted (ROE − cost of capital (9%))",
        "BHF: n/m - sector-adjusted (Price / book)",
        "BHF: N/A - Data Incomplete"]
    assert [a.xref for a in notes] == ["x2 domain", "x3 domain", "x4 domain"]  # own row, not the figure's
    assert [a.yref for a in notes] == ["y2 domain", "y3 domain", "y4 domain"]
    assert all(a.x == 0 and a.xanchor == "left" and a.y == 1 and a.yanchor == "top" for a in notes)
    # the subplot titles stay untouched (left-aligned, paper-anchored — not re-pointed at a row)
    titles = out.fig.layout.annotations[:4]
    assert [t.text for t in titles] == [label for _, label, _ in charts.PEER_METRICS]
    assert all(t.xref == "paper" and t.yref == "paper" and t.x == 0 and t.xanchor == "left" for t in titles)


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


def test_progressive_events_rebuild_only_their_charts(monkeypatch):
    """Each lens / turnaround event rebuilds only the charts it changes; the rest are kept as they are."""
    from app import stock_view as sv

    assert set(sv.CHARTS_FOR_EVENT) == {"quant", "macro", "moat", "devils_advocate", "aggregate", "turnaround"}
    assert all(set(v) <= set(sv.CHART_BUILDERS) for v in sv.CHARTS_FOR_EVENT.values())
    built = []
    monkeypatch.setattr(sv, "CHART_BUILDERS", {k: (lambda run, b, k=k: built.append(k) or k)
                                               for k in sv.CHART_BUILDERS})
    everything = sv.build_charts(None, None)
    assert set(everything) == set(sv.CHART_BUILDERS) and len(built) == len(sv.CHART_BUILDERS)
    built.clear()
    after = sv.build_charts(None, None, only=sv.CHARTS_FOR_EVENT["macro"], current={**everything, "trap": "kept"})
    assert built == ["dot_strip"] and after["trap"] == "kept"


def test_heatmap_shades_still_separate_cells_when_all_are_above_price():
    rates, growths = [0.07, 0.08, 0.09, 0.10, 0.11], [0.06, 0.085, 0.11, 0.135, 0.16]
    values = [[166 + 30 * j + 40 * (4 - i) for j in range(5)] for i in range(5)]  # all well above 101
    out = charts.sensitivity_heatmap(SensitivityGrid(rates=rates, growths=growths, values=values), d(101.0),
                                     ReverseDcf(implied_growth=-0.106, history=d(0.11)))
    hm = out.fig.data[0]
    flat = [v for row in hm.z for v in row]
    assert min(flat) > 0 and max(flat) == hm.zmax  # the grid's own range: the darkest cell is its maximum
    assert min(flat) / hm.zmax < 0.5  # the cheapest cell is visibly lighter, not saturated like the rest
    assert hm.text[0][0].endswith("%") and "<br>" in hm.text[0][0]  # value and upside in every cell
    assert any("price implies -10.6%" in (a.text or "") for a in out.fig.layout.annotations)  # off-grid marker


def test_small_multiples_compare_the_latest_quarter_with_a_year_earlier():
    q = [date(2025, 5, 3), date(2025, 8, 2), date(2025, 11, 1), date(2026, 2, 1), date(2026, 5, 3)]
    rev = [2.5e9, 2.6e9, 2.6e9, 3.6e9, 2.4e9]  # a holiday-quarter spike, then a normal quarter
    series = FundamentalSeries(points={"total_revenue": [SeriesPoint(period_end=d_, value=v) for d_, v in zip(q, rev)],
                                       "net_income": [SeriesPoint(period_end=d_, value=v) for d_, v in
                                                      zip(q, [-1e8, 3e8, 3e8, 5.8e8, 1.9e8])]},
                               freqs={"total_revenue": "quarterly", "net_income": "quarterly"}, currency="USD")
    out = charts.small_multiples(None, series, ticker="T")
    texts = [a.text for a in out.fig.layout.annotations]
    assert "vs year-ago quarter (○): -4%" in texts  # 2.4 vs 2.5, not -33% vs the holiday quarter
    assert "vs year-ago quarter (○): n/m - year-ago quarter ≤ 0" in texts  # negative year-ago net income
    assert "same quarter a year earlier" in out.caption
