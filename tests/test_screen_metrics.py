"""Each screen metric against hand-computed inputs, the Rule 2b table, and sector routing."""

import logging

import pytest

import config
from data.sector import route
from data.shares import ShareTrend
from data.values import NA_INCOMPLETE, Datum
from screening import metrics as mx
from screening.models import FAIL, NA, NM, NOT_APPLICABLE, PASS, SLOT_FCF, SLOT_LEVERAGE, SLOT_MOS, STATUS_FAIL
from signals.returns import ROA_SUBSTITUTE, roic
from signals.valuation import liquid_cash
from tests.screen_helpers import T, P, P2, P3, make_fundamentals, make_info, run_eval, trend


def d(v):
    return Datum(value=v)


# --------------------------------------------------------------------------
# 1. Margin of safety
# --------------------------------------------------------------------------
def test_margin_of_safety_hand_computed():
    # Graham = sqrt(22.5 × 2 × 20) = 30; price 20 → MoS = (30 − 20)/20 = +50%
    m = mx.margin_of_safety(d(2), d(20), d(20))
    assert m.inputs["Graham Number"].value == pytest.approx(30)
    assert m.value.value == pytest.approx(0.5) and m.outcome == PASS
    assert mx.margin_of_safety(d(2), d(20), d(26)).outcome == FAIL  # +15.4% < 20%


@pytest.mark.parametrize("eps,bvps,reason", [(-1, 20, "negative EPS"), (2, -5, "negative book value")])
def test_graham_nm_on_negative_eps_or_book(eps, bvps, reason):
    m = mx.margin_of_safety(d(eps), d(bvps), d(20))
    assert m.value.status == f"n/m - {reason}" and m.outcome == NM and m.failing


# --------------------------------------------------------------------------
# 2. FCF yield / cash runway
# --------------------------------------------------------------------------
def test_sbc_adjusted_fcf_yield_hand_computed():
    # (120 − 20) / 1000 = 10% vs 10-year 4% → pass; vs 12% → fail
    m = mx.fcf_yield(d(120), d(20), d(1000), d(0.04))
    assert m.value.value == pytest.approx(0.10) and m.outcome == PASS
    assert mx.fcf_yield(d(120), d(20), d(1000), d(0.12)).outcome == FAIL


def test_missing_sbc_uses_raw_fcf_labelled():
    m = mx.fcf_yield(d(120), Datum.missing("N/A - field not found: stock_based_compensation"), d(1000), d(0.04))
    assert m.value.value == pytest.approx(0.12) and m.outcome == PASS
    assert any(n.startswith(mx.SBC_NOT_REPORTED) for n in m.notes)
    assert "unadjusted" in m.name


def test_fcf_yield_na_without_risk_free_source():
    m = mx.fcf_yield(d(120), d(20), d(1000), Datum.missing("N/A - no risk-free source configured for EUR"))
    assert m.outcome == NA and "no risk-free source configured for EUR" in m.na_reason
    from screening.table import _cell
    from screening.models import ScreenResult
    cell = _cell(ScreenResult(ticker="X", metrics=[m]), SLOT_FCF)
    assert cell.endswith("[N/A: comparison N/A - no risk-free source configured for EUR]")


def test_cash_runway_counts_sti_and_uses_raw_burn():
    # Cash 200 + STI 50 = 250; raw FCF −60 → burn 5/month → 50 months.
    # SBC-adjusted burn would be 60 + 30 = 90 → 33.3 months: it must not be used.
    f = make_fundamentals({"free_cash_flow": (-60, -50), "stock_based_compensation": (30, 30)})
    cash = liquid_cash(f)
    assert cash.value == 250
    runway = mx.cash_runway(cash, mx.ttm_fcf(f))
    assert runway.value == pytest.approx(50)


def test_runway_nm_when_not_burning_cash():
    r = mx.cash_runway(d(250), d(10))
    assert r.status == "n/m - not burning cash"


@pytest.mark.parametrize("values,expected", [
    ((10, -5, -3), mx.FCF_NEGATIVE),          # 3 fiscal years, 2 negative
    ((10, -5, -3, 8), mx.NOT_FCF_NEGATIVE),   # 2 of 4 is not a majority
    ((-5,), mx.TOO_LITTLE_HISTORY),           # a single year
])
def test_fcf_negative_rule(values, expected):
    years = (T, P, P2, P3)[: len(values)]
    f = make_fundamentals({"free_cash_flow": values}, years=years)
    assert mx.fcf_negative_test(mx.annual_fcf(f)).status == expected


def test_too_little_history_still_screens_fcf_yield():
    res = run_eval(make_fundamentals(years=(T,)))
    assert res.fcf_negative == mx.TOO_LITTLE_HISTORY
    m = res.metric(SLOT_FCF)
    assert m.name.startswith("SBC-adjusted") and any("too little history" in n for n in m.notes)


def test_fcf_negative_company_uses_runway():
    f = make_fundamentals({"free_cash_flow": (-120, -100), "operating_cash_flow": (-60, -45)}, years=(T, P))
    res = run_eval(f)
    m = res.metric(SLOT_FCF)
    assert res.fcf_negative == mx.FCF_NEGATIVE
    assert m.name.startswith("Cash runway") and m.value.value == pytest.approx(250 / 10)  # 25 months ≥ 24
    assert m.outcome == PASS


# --------------------------------------------------------------------------
# 3. Leverage and the Rule 2b table
# --------------------------------------------------------------------------
def test_net_debt_ebitda_hand_computed():
    m = mx.leverage_metric(d(300), d(100), d(100))  # (300 − 100) / 100 = 2.0x
    assert m.value.value == pytest.approx(2.0) and m.outcome == PASS
    assert mx.leverage_metric(d(500), d(100), d(100)).outcome == FAIL  # 4.0x


def test_negative_ebitda_with_net_debt_fails_leverage_screen():
    m = mx.leverage_metric(d(300), d(100), d(-50))
    assert m.value.status == "n/m - negative EBITDA" and m.failing
    res = run_eval(make_fundamentals({"ebitda": (-50, 20)}))
    assert res.status == STATUS_FAIL and res.metric(SLOT_LEVERAGE).outcome == NM
    lev = next(s for s in res.quality.subscores if s.name == "leverage")
    assert lev.score == config.SCORE_MIN


def test_negative_ebitda_with_net_cash_scores_leverage_from_runway():
    f = make_fundamentals({"ebitda": (-50, 20), "total_debt": (100, 100), "free_cash_flow": (-60, 10)})
    res = run_eval(f)
    m = res.metric(SLOT_LEVERAGE)
    assert m.value.status == "n/m - negative EBITDA, net cash"
    lev = next(s for s in res.quality.subscores if s.name == "leverage")
    # runway = 250 / (60/12) = 50 months → RUNWAY_BREAKPOINTS > 36 → 5
    assert lev.score == 5 and "runway 50 months" in lev.input_display


def test_negative_eps_and_book_reach_stage_2():
    from screening.stage1 import stage1
    from tests.screen_helpers import info_values, price, rf

    iv = info_values(trailing_eps=-2.0, book_value_per_share=-1.0, info_free_cashflow=100.0, info_ebitda=200.0,
                     info_total_debt=100.0, info_total_cash=50.0, shares_outstanding=100.0)
    s1 = stage1(iv, price(10), rf(), route(make_info()))
    assert s1.metric(SLOT_MOS).outcome == NM and s1.survives


def test_negative_equity_gives_nm_roe_and_pb():
    f = make_fundamentals({"stockholders_equity": (-100, 50)})
    res = run_eval(f, info=make_info("Financial Services", "Insurance - Property & Casualty"))
    assert res.metric(SLOT_FCF).inputs["ROE"].status == "n/m - negative equity"
    assert res.metric(SLOT_LEVERAGE).value.status == "n/m - negative equity"
    assert res.metric(SLOT_MOS).value.status == "n/m - negative book value"
    assert res.status == STATUS_FAIL


@pytest.mark.parametrize("px", [0.0, None])
def test_zero_or_missing_market_cap_makes_price_ratios_na(px):
    from screening.stage2 import evaluate
    from tests.screen_helpers import TODAY, rf

    info = make_info()
    p = Datum(value=px) if px is not None else Datum.missing()
    res = evaluate("TEST", info, {}, route(info), make_fundamentals(), trend(), p, rf(), TODAY)
    assert res.market_cap.is_na
    assert res.metric(SLOT_FCF).value.is_na and res.metric(SLOT_FCF).outcome == NA
    assert res.earnings_yield.value.is_na
    assert res.asset_floor.p_tbv.is_na


def test_invested_capital_nonpositive_substitutes_roa():
    r = roic(make_fundamentals({"invested_capital": (-10, 5)}))
    assert r.label == ROA_SUBSTITUTE and r.value.value == pytest.approx(150 / 2000)


def test_nm_and_na_reported_distinctly():
    nm = mx.leverage_metric(d(300), d(100), d(-50))
    na = mx.leverage_metric(d(300), d(100), Datum.missing())
    assert nm.outcome == NM and nm.available and nm.value.status.startswith("n/m - ")
    assert na.outcome == NA and not na.available and na.value.status == NA_INCOMPLETE


# --------------------------------------------------------------------------
# 4. Share-count trend
# --------------------------------------------------------------------------
def test_share_trend_metric_rewards_reduction_and_flags_dilution():
    assert mx.share_trend_metric(trend(-0.02)).outcome == PASS
    m = mx.share_trend_metric(trend(0.05))
    assert m.outcome == FAIL and any(n.startswith("dilution flag") for n in m.notes)
    assert any(n.startswith("span 2022-12-31 to 2025-12-31") for n in m.notes)
    m = mx.share_trend_metric(ShareTrend(status="N/A - unexplained share-count jump; not scored"))
    assert m.outcome == NA and m.value.status.startswith("N/A - unexplained")


# --------------------------------------------------------------------------
# 5. Sector-adjusted routing and metrics
# --------------------------------------------------------------------------
@pytest.mark.parametrize("industry,subsector,slots", [
    ("Banks - Regional", "bank", ("ROE", "Price / tangible")),
    ("Insurance - Life", "insurer", ("ROE", "Price / book")),
    ("REIT - Retail", "reit", ("FFO yield", "Book-value slot")),
])
def test_financials_routed_by_industry(industry, subsector, slots):
    info = make_info("Financial Services" if subsector != "reit" else "Real Estate", industry)
    rt = route(info)
    assert rt.sector_adjusted and rt.subsector == subsector
    res = run_eval(info=info)
    assert res.metric(SLOT_FCF).name.startswith(slots[0])
    assert res.metric(SLOT_LEVERAGE).name.startswith(slots[1])
    assert res.piotroski.status.startswith("n/m") and res.earnings_yield.value.is_nm
    assert res.quality.of == 2 and "sector-adjusted" in res.quality.display


def test_unmatched_financial_industry_is_logged(caplog):
    info = make_info("Financial Services", "Shell Companies")
    with caplog.at_level(logging.WARNING):
        rt = route(info)
    assert rt.subsector == config.SUBSECTOR_DEFAULT and rt.unmatched_industry
    assert "Shell Companies" in caplog.text


def test_sector_adjusted_hand_computed():
    # Bank: ROE = 150/1200 = 12.5% → spread +3.5% pass; P/TBV = 1000/1050 = 0.95x pass
    res = run_eval(info=make_info("Financial Services", "Banks - Diversified"))
    assert res.metric(SLOT_FCF).value.value == pytest.approx(0.125 - config.COST_OF_CAPITAL)
    assert res.metric(SLOT_LEVERAGE).value.value == pytest.approx(1000 / 1050)
    # Insurer: P/B = 1000/1200
    res = run_eval(info=make_info("Financial Services", "Insurance - Diversified"))
    assert res.metric(SLOT_LEVERAGE).value.value == pytest.approx(1000 / 1200)
    # REIT: FFO = 150 + 50 D&A − 0 gains (not reported) = 200 → yield 20% vs 4%
    res = run_eval(info=make_info("Real Estate", "REIT - Industrial"))
    m = res.metric(SLOT_FCF)
    assert m.value.value == pytest.approx(0.20) and m.outcome == PASS
    assert res.metric(SLOT_LEVERAGE).outcome == NOT_APPLICABLE and not res.metric(SLOT_LEVERAGE).available


def test_missing_fields_return_na_without_exceptions():
    empty = make_fundamentals(base={})
    res = run_eval(empty, tr=trend(None))
    assert res.status == "Incomplete" and res.metrics_available == 0
    assert all(m.outcome == NA for m in res.metrics)
    assert res.piotroski.status.startswith("Insufficient data")
    assert res.altman.z.is_na or res.altman.z.status.startswith("Insufficient data")
    assert res.quality.score is None
    assert res.rationale
