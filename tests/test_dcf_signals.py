"""DCF, reverse DCF, sensitivity grid, peak earnings and Graham against hand-computed inputs."""

import math

import pytest

import config
from data.values import Datum
from signals import dcf
from signals.valuation import graham_number
from tests.screen_helpers import P, P2, P3, T, make_fundamentals


def inputs(price=100.0, base=100.0, growth=0.10, shares=10.0, net_cash=50.0, rate=0.09):
    return dcf.DcfInputs(base=base, growth=growth, shares=shares, net_cash=net_cash, price=price, rate=rate)


def test_dcf_value_hand_computed():
    # Stage 1: 110/1.09 + 121/1.09² + 133.1/1.09³ + 146.41/1.09⁴ + 161.051/1.09⁵ = 513.930966
    # Terminal: 161.051 × 1.025 / (0.09 − 0.025) / 1.09⁵ = 1650.598495
    v = dcf.dcf_value(100.0, 0.10, 0.09, 10.0, 50.0)
    assert v.pv_stage1 == pytest.approx(513.930966, abs=1e-5)
    assert v.pv_terminal == pytest.approx(1650.598495, abs=1e-5)
    assert v.per_share == pytest.approx(221.452946, abs=1e-5)  # (513.93 + 1650.60 + 50) / 10


def test_dcf_net_cash_bridge_toggle(monkeypatch):
    monkeypatch.setattr(config, "DCF_ADD_NET_CASH", False)
    assert dcf.dcf_value(100.0, 0.10, 0.09, 10.0, 50.0).per_share == pytest.approx(216.452946, abs=1e-5)


def test_dcf_rate_not_above_terminal_is_none():
    assert dcf.dcf_value(100.0, 0.1, config.TERMINAL_GROWTH, 10.0, 0.0) is None


def test_graham_number_hand_computed():
    g = graham_number(Datum(value=1.5), Datum(value=12.0))
    assert g.value == pytest.approx(math.sqrt(22.5 * 1.5 * 12))  # 20.1246
    assert graham_number(Datum(value=-1.5), Datum(value=12.0)).status == "n/m - negative EPS"


def test_reverse_dcf_recovers_known_growth():
    target = inputs().value(growth=0.07).per_share
    rev = dcf.reverse_dcf(inputs(price=target), Datum(value=0.06))
    assert rev.status == "ok"
    assert rev.implied_growth == pytest.approx(0.07, abs=1e-4)
    assert "Price implies +7.0%/yr growth; history shows +6.0%/yr" == rev.display


@pytest.mark.parametrize("price,side", [(1e7, "above"), (0.01, "below")])
def test_reverse_dcf_beyond_range(price, side):
    rev = dcf.reverse_dcf(inputs(price=price), Datum(value=0.05))
    assert rev.status == "beyond range" and rev.side == side
    assert "beyond range" in rev.display


def test_reverse_dcf_nm_for_unstable_base():
    rev = dcf.reverse_dcf(None, Datum(value=0.05), dcf.UNSTABLE_BASE)
    assert rev.status.startswith("n/m")


def test_grid_centre_equals_base_case_and_range_is_central_3x3():
    inp = inputs()
    base = dcf.run_dcf(inp)
    grid = dcf.sensitivity_grid(inp)
    assert len(grid.values) == 5 and all(len(r) == 5 for r in grid.values)
    assert grid.center == pytest.approx(base.fair_value)
    central = [grid.values[i][j] for i in (1, 2, 3) for j in (1, 2, 3)]
    assert grid.range_low == pytest.approx(min(central)) and grid.range_high == pytest.approx(max(central))
    assert grid.rates[2] == inp.rate and grid.growths[2] == inp.growth


def test_growth_is_revenue_cagr_not_fcf_growth():
    # Revenue 810 → 900 → 1000 over two years: CAGR = (1000/810)^(1/2) − 1 = 11.11%.
    # FCF moves very differently (50 → 300), which must not matter.
    f = make_fundamentals({"total_revenue": (1000, 900, 810), "free_cash_flow": (300, 100, 50)}, years=(T, P, P2))
    g = dcf.revenue_cagr(f)
    assert g.cagr.value == pytest.approx((1000 / 810) ** 0.5 - 1)
    assert g.used == pytest.approx(g.cagr.value) and not g.clamped


@pytest.mark.parametrize("revs,expected", [((2000, 1000), config.STAGE1_GROWTH_CAP),
                                           ((500, 1000), config.STAGE1_GROWTH_FLOOR)])
def test_growth_is_clamped(revs, expected):
    g = dcf.revenue_cagr(make_fundamentals({"total_revenue": revs}))
    assert g.clamped and g.used == expected
    assert "clamped" in g.detail


def test_dcf_base_is_average_sbc_adjusted_fcf_of_last_three_fiscal_years():
    f = make_fundamentals({"free_cash_flow": (300, 200, 100, 50), "stock_based_compensation": (30, 20, 10, 5)},
                          years=(T, P, P2, P3))
    b = dcf.dcf_base(f)
    # (300 − 30 + 200 − 20 + 100 − 10) / 3; the 4th year is ignored
    assert b.ok and b.value == pytest.approx(180.0) and b.raw_value == pytest.approx(200.0)
    assert len(b.years) == config.DCF_BASE_YEARS and "SBC deducted in every year" in b.detail


def test_dcf_base_uses_raw_fcf_where_sbc_is_not_reported():
    b = dcf.dcf_base(make_fundamentals({"free_cash_flow": (300, 200, 100), "stock_based_compensation": None},
                                       years=(T, P, P2)))
    assert b.value == pytest.approx(200.0) and "SBC not reported" in b.sbc_note


@pytest.mark.parametrize("fcf,why", [((100, -50, 80), "changed sign"), ((-10, -20, -30), "FCF ≤ 0")])
def test_unstable_fcf_base(fcf, why):
    b = dcf.dcf_base(make_fundamentals({"free_cash_flow": fcf}, years=(T, P, P2)))
    assert b.status == "Insufficient data - unstable FCF base" and why in b.detail


def test_too_little_fcf_history():
    b = dcf.dcf_base(make_fundamentals({"free_cash_flow": (100,)}, years=(T,)))
    assert b.status == "Insufficient data - too little history"


def test_peak_margin_cyclical_flagged_and_normalised():
    # Operating margins 40%, 10%, 10% → average 20%; TTM (latest year) 40% ≥ 1.5 × 20% → flagged.
    # FCF margins 30%, 5%, 5% → average 13.33%; normalised base = 1000 × 13.33% = 133.3.
    f = make_fundamentals({"total_revenue": (1000, 1000, 1000), "operating_income": (400, 100, 100),
                           "free_cash_flow": (300, 50, 50), "stock_based_compensation": None}, years=(T, P, P2))
    pk = dcf.peak_earnings(f, config.CYCLICALITY_SCORES["cyclical"])
    assert pk.flagged and pk.average_margin == pytest.approx(0.2)
    assert pk.normalised_base == pytest.approx(1000 * (0.3 + 0.05 + 0.05) / 3)
    assert not dcf.peak_earnings(f, config.CYCLICALITY_SCORES["mixed"]).checked
