"""Stage 1: slack, cuts, FX conversion, market cap from the batch price, and the engine's stage handling."""

from datetime import date

import pytest

import config
from data.sector import route
from data.values import Datum
from screening.engine import ScreenContext, screen_ticker, stage1_info_values
from screening.models import FAIL, NM, PASS, SLOT_FCF, SLOT_LEVERAGE, SLOT_MOS, STATUS_FAIL
from screening.stage1 import stage1
from tests.screen_helpers import info_values, make_info, price, rf

CAPTURED = date(2026, 9, 27)
VALET = lambda series: (3.1, date(2026, 9, 25))  # noqa: E731


def iv(eps=1.5, bvps=12.0, fcf=150.0, ebitda=250.0, debt=300.0, cash=250.0, shares=100.0):
    return info_values(trailing_eps=eps, book_value_per_share=bvps, info_free_cashflow=fcf, info_ebitda=ebitda,
                       info_total_debt=debt, info_total_cash=cash, shares_outstanding=shares)


STD = route(make_info())


def test_slack_lets_borderline_through_and_cuts_clear_failure():
    # Graham = sqrt(22.5 × 1.5 × 12) = 20.12. Price 19 → MoS +5.9%: fails the 20% screen,
    # but passes the stage-1 hurdle (1.2 × 0.75 − 1 = −10%).
    s1 = stage1(iv(), price(19), rf(), STD)
    assert s1.metric(SLOT_MOS).outcome == PASS and s1.survives
    assert "≥ -10%" in s1.metric(SLOT_MOS).threshold
    # Price 40 → MoS −50%: a clear failure is cut.
    s1 = stage1(iv(), price(40), rf(), STD)
    assert not s1.survives and s1.metric(SLOT_MOS).outcome == FAIL


def test_slack_applies_to_zero_thresholds_via_the_hurdle():
    # FCF yield 3.2% vs 10-year 4%: fails stage 2, passes stage 1 (4% × 0.75 = 3%)
    s1 = stage1(iv(fcf=32.0), price(10), rf(0.04), STD)
    assert s1.metric(SLOT_FCF).outcome == PASS
    # Leverage 3.5x: fails 3.0x, passes 3.75x in stage 1
    s1 = stage1(iv(debt=1125.0, cash=250.0), price(10), rf(), STD)
    assert s1.metric(SLOT_LEVERAGE).value.value == pytest.approx(3.5) and s1.metric(SLOT_LEVERAGE).outcome == PASS


def test_missing_stage1_fields_go_to_stage_2():
    s1 = stage1(info_values(shares_outstanding=100.0), price(10), rf(), STD)
    assert s1.survives and all(m.outcome == "N/A" for m in s1.metrics)


def test_negative_ebitda_with_net_debt_is_cut_in_stage1():
    s1 = stage1(iv(ebitda=-10.0), price(10), rf(), STD)
    assert s1.metric(SLOT_LEVERAGE).outcome == NM and not s1.survives


def test_bank_skips_stage1_leverage_test():
    bank = route(make_info("Financial Services", "Banks - Diversified"))
    s1 = stage1(iv(ebitda=-10.0, fcf=-500.0), price(10), rf(), bank)
    assert s1.metric(SLOT_LEVERAGE) is None and s1.metric(SLOT_FCF) is None
    assert s1.survives and any("skipped" in n for n in s1.notes)


def test_market_cap_from_shares_times_batch_price_not_info():
    s1 = stage1(iv(shares=100.0), price(12.5), rf(), STD)
    assert s1.market_cap.value == 1250


def test_market_cap_from_batch_price_in_engine(fx_provider, tmp_path):
    ctx = ScreenContext(provider=fx_provider, db_path=tmp_path / "r.db", today=CAPTURED)
    info = fx_provider.get_info("MSFT")
    batch_price = Datum(value=123.0, period_end=CAPTURED)
    res = screen_ticker(ctx, "MSFT", "test", price=batch_price, force_stage2=True)
    shares = res.inputs["shares outstanding"].value
    assert res.market_cap.value == pytest.approx(shares * 123.0)
    assert res.market_cap.value != pytest.approx(info.get("market_cap"))


def test_stage1_fx_conversion_on_usd_reporting_tsx_fixture(fx_provider, tmp_path):
    ctx = ScreenContext(provider=fx_provider, db_path=tmp_path / "r.db", today=CAPTURED, valet_fetch=VALET)
    info = fx_provider.get_info("ABX.TO")
    assert (info.get("financial_currency"), info.get("currency")) == ("USD", "CAD")
    fx = fx_provider.get_price_history("USDCAD=X", adjusted=False).dropna().iloc[-1]
    vals = stage1_info_values(ctx, info)
    for field in ("trailing_eps", "book_value_per_share", "info_free_cashflow", "info_ebitda", "info_total_debt"):
        assert vals[field].value == pytest.approx(info.get(field) * fx)
        assert any("converted USD→CAD" in n for n in vals[field].notes)
    assert vals["shares_outstanding"].value == info.get("shares_outstanding")  # never converted


def test_final_result_comes_from_stage2_even_when_stage1_optimistic(fx_provider, tmp_path):
    ctx = ScreenContext(provider=fx_provider, db_path=tmp_path / "r.db", today=CAPTURED, valet_fetch=VALET)
    res = screen_ticker(ctx, "ABX.TO", "TSX Composite")
    assert res.stage1.survives and res.stage1.metric(SLOT_MOS).outcome == PASS
    assert res.decided_at_stage == 2 and res.metric(SLOT_MOS).outcome == FAIL
    assert res.status == STATUS_FAIL
    # the stage-1/stage-2 gap on margin of safety is logged with both values
    div = next(d for d in res.divergences if d.metric == "margin of safety")
    assert div.rel_diff > config.STAGE_DIVERGENCE
    assert div.stage1 == pytest.approx(res.stage1.metric(SLOT_MOS).value.value)
    assert div.stage2 == pytest.approx(res.metric(SLOT_MOS).value.value)


def test_stage1_cut_ticker_carries_as_of_and_stale_flag(fx_provider, tmp_path):
    ctx = ScreenContext(provider=fx_provider, db_path=tmp_path / "r.db", today=CAPTURED)
    res = screen_ticker(ctx, "MELI", "COWZ")
    assert res.decided_at_stage == 1 and res.status == STATUS_FAIL
    assert res.fundamentals_as_of is not None and res.status_reasons[0].startswith("cut at stage 1")


def test_stale_ticker_still_screens_with_flag():
    from tests.screen_helpers import run_eval

    res = run_eval(today=date(2026, 9, 27))  # latest period 2025-12-31 is > 120 days old
    assert res.stale and "fundamentals may be stale" in res.stale_label
    assert res.status in ("Pass", "Fail", "Incomplete") and res.metrics_available == 4
    assert res.stale_label in res.rationale
