"""Piotroski, Altman Z'' and Beneish against hand-computed values; n/m for financials."""

import pytest

from tests.screen_helpers import T, P, make_fundamentals
from signals.trap_scores import altman_z2, beneish, piotroski

# Beneish hand-computed case (numbers in the comments are the worked values).
BENEISH_BASE = {
    "total_revenue": (1200, 1000), "gross_profit": (500, 400), "receivables": (240, 150),
    "current_assets": (600, 500), "net_ppe": (300, 300), "total_assets": (1200, 1000),
    "depreciation_amortization": (60, 50), "sga": (240, 180), "net_income_continuing": (100, 90),
    "net_income": (100, 90), "operating_cash_flow": (50, 80), "current_liabilities": (300, 250),
    "long_term_debt": (200, 200),
}


def test_piotroski_exactly_nine():
    res = piotroski(make_fundamentals())
    assert [c.outcome for c in res.checks] == ["pass"] * 9
    assert res.score == 9 and res.available == 9
    assert res.display == "9 / 9 (9 checks available)"
    assert res.years == [T, P]


def test_piotroski_with_missing_checks():
    # No current assets → current-ratio check N/A; no gross profit or cost of revenue → margin check N/A.
    f = make_fundamentals({"current_assets": None, "gross_profit": None, "cost_of_revenue": None})
    res = piotroski(f)
    assert res.available == 7 and res.score == 7
    assert res.display == "7 / 9 (7 checks available)"
    na = [c.name for c in res.checks if not c.available]
    assert na == ["current ratio rose", "gross margin rose"]
    assert all(c.outcome.startswith("N/A") for c in res.checks if not c.available)
    # One more missing check drops below PIOTROSKI_MIN_CHECKS (7).
    res = piotroski(make_fundamentals({"current_assets": None, "gross_profit": None, "cost_of_revenue": None,
                                       "long_term_debt": None}))
    assert res.score is None and res.status.startswith("Insufficient data")


def test_piotroski_needs_two_fiscal_years():
    res = piotroski(make_fundamentals(years=(T,)))
    assert res.score is None and "two consecutive fiscal years" in res.status


def test_altman_hand_computed_safe():
    # X1 = 300/2000 = 0.15, X2 = 700/2000 = 0.35, X3 = 200/2000 = 0.10, X4 = 1200/800 = 1.5
    # Z'' = 6.56×0.15 + 3.26×0.35 + 6.72×0.10 + 1.05×1.5 = 0.984 + 1.141 + 0.672 + 1.575 = 4.372
    res = altman_z2(make_fundamentals())
    assert res.z.value == pytest.approx(4.372, abs=1e-9)
    assert res.zone == "safe"


def test_altman_hand_computed_distress_and_grey():
    # X1 = -100/1000, X2 = -200/1000, X3 = -50/1000, X4 = 100/900
    # Z'' = -0.656 - 0.652 - 0.336 + 0.116667 = -1.527333
    f = make_fundamentals({"total_assets": (1000, 1000), "working_capital": (-100, 0), "retained_earnings": (-200, 0),
                           "ebit": (-50, 0), "stockholders_equity": (100, 0), "total_liabilities": (900, 0)})
    res = altman_z2(f)
    assert res.z.value == pytest.approx(-1.527333, abs=1e-6) and res.zone == "distress"
    # Grey: X1 = 0.1, X2 = 0.1, X3 = 0.05, X4 = 0.5 → 0.656 + 0.326 + 0.336 + 0.525 = 1.843
    f = make_fundamentals({"total_assets": (1000, 1000), "working_capital": (100, 0), "retained_earnings": (100, 0),
                           "ebit": (50, 0), "stockholders_equity": (400, 0), "total_liabilities": (800, 0)})
    res = altman_z2(f)
    assert res.z.value == pytest.approx(1.843) and res.zone == "grey"


def test_altman_working_capital_falls_back_to_current_items():
    f = make_fundamentals({"working_capital": None})  # CA 600 − CL 300 = 300, same as the row
    assert altman_z2(f).z.value == pytest.approx(4.372)


def test_beneish_hand_computed_flag():
    # DSRI = (240/1200)/(150/1000) = 1.333333      GMI = 0.4/0.416667 = 0.96
    # AQI = (1-900/1200)/(1-800/1000) = 1.25        SGI = 1.2
    # DEPI = (50/350)/(60/360) = 0.857143           SGAI = (240/1200)/(180/1000) = 1.111111
    # TATA = (100-50)/1200 = 0.041667               LVGI = (500/1200)/(450/1000) = 0.925926
    # M = -4.84 + 0.920×1.333333 + 0.528×0.96 + 0.404×1.25 + 0.892×1.2 + 0.115×0.857143
    #     - 0.172×1.111111 + 4.679×0.041667 - 0.327×0.925926 = -1.731413
    res = beneish(make_fundamentals(base=BENEISH_BASE))
    v = res.variables
    assert v["DSRI"].value == pytest.approx(1.333333, abs=1e-6)
    assert v["GMI"].value == pytest.approx(0.96)
    assert v["AQI"].value == pytest.approx(1.25)
    assert v["SGI"].value == pytest.approx(1.2)
    assert v["DEPI"].value == pytest.approx(0.857143, abs=1e-6)
    assert v["SGAI"].value == pytest.approx(1.111111, abs=1e-6)
    assert v["TATA"].value == pytest.approx(0.041667, abs=1e-6)
    assert v["LVGI"].value == pytest.approx(0.925926, abs=1e-6)
    assert res.m.value == pytest.approx(-1.731413, abs=1e-5)
    assert res.flag  # above BENEISH_THRESHOLD (-1.78)
    assert "probabilistic" in res.caveat


def test_beneish_below_threshold_and_missing_variable():
    res = beneish(make_fundamentals())
    assert res.m.ok and not res.flag
    res = beneish(make_fundamentals({"sga": None}))
    assert res.m.value is None and res.m.status.startswith("Insufficient data") and "SGAI" in res.m.status


@pytest.mark.parametrize("fn", [piotroski, altman_z2, beneish])
def test_financials_get_nm(fn):
    res = fn(make_fundamentals(), sector_adjusted=True)
    assert res.status == "n/m - not meaningful for financials and REITs"
