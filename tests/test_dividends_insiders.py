"""Dividend safety and the insider-activity summary."""

from datetime import date

import pandas as pd
import pytest

from data.form4 import InsiderTransaction
from data.insiders import InsiderData
from signals.dividends import NO_DIVIDEND, dividend_safety
from signals.insider_activity import insider_summary
from tests.screen_helpers import TODAY, make_fundamentals, price


def quarterly(per_year: dict[int, float]) -> pd.Series:
    idx, vals = [], []
    for y, total in per_year.items():
        for m in (3, 6, 9, 12):
            idx.append(pd.Timestamp(y, m, 15))
            vals.append(total / 4)
    return pd.Series(vals, index=pd.DatetimeIndex(idx))


def test_fcf_payout_above_100_percent_is_at_risk():
    # Dividends paid 200 vs FCF 160 → payout 125%.
    f = make_fundamentals({"dividends_paid": (-200, -180)})
    d = dividend_safety(quarterly({2024: 2.0, 2025: 2.0, 2026: 2.0}), f, price(), TODAY)
    assert d.payer and d.fcf_payout.value == pytest.approx(1.25)
    assert d.at_risk and "125%" in d.at_risk_reason
    assert d.earnings_payout.value == pytest.approx(200 / 150)


def test_negative_fcf_while_paying_is_at_risk():
    f = make_fundamentals({"dividends_paid": (-50, -50), "free_cash_flow": (-10, 20)})
    d = dividend_safety(quarterly({2025: 1.0, 2026: 1.0}), f, price(), TODAY)
    assert d.at_risk and d.fcf_payout.is_nm


def test_cut_detected_and_uninterrupted_years():
    divs = quarterly({2021: 1.0, 2022: 1.0, 2023: 1.0, 2024: 0.5, 2025: 0.55})
    d = dividend_safety(divs, make_fundamentals({"dividends_paid": (-50, -50)}), price(), date(2026, 2, 15))
    assert [c.year for c in d.cuts] == [2024] and d.cuts[0].change == pytest.approx(-0.5)
    assert d.uninterrupted_years == 5
    assert not d.at_risk  # payout 50 / 160


def test_non_payer_is_na():
    d = dividend_safety(pd.Series(dtype=float), make_fundamentals(), price(), TODAY)
    assert d.status == NO_DIVIDEND and not d.payer


@pytest.mark.parametrize("paid,ni,at_risk", [(-50, 150, False), (-200, 150, True), (-50, -10, True)])
def test_financials_use_earnings_payout(paid, ni, at_risk):
    # Bank FCF is negative and not meaningful; the earnings payout decides (cap 100%).
    f = make_fundamentals({"dividends_paid": (paid, -50), "free_cash_flow": (-500, -400), "net_income": (ni, 110)})
    d = dividend_safety(quarterly({2025: 1.0, 2026: 1.0}), f, price(), TODAY, sector_adjusted=True)
    assert d.fcf_payout.is_nm and d.at_risk is at_risk
    if at_risk:
        assert "financials: earnings payout used" in d.at_risk_reason


def tx(day, who, kind="buy", plan=False, shares=100.0, px=10.0):
    return InsiderTransaction(ticker="TEST", date=day, insider=who, role="Director", type=kind, shares=shares,
                              price=px, value=shares * px, is_10b5_1=plan)


def test_cluster_buy_and_10b5_1_counted_separately():
    data = InsiderData(ticker="TEST", coverage=["Form 4, full history"], transactions=[
        tx(date(2026, 1, 5), "Alice"), tx(date(2026, 1, 20), "Bob"), tx(date(2026, 2, 10), "Carol"),
        tx(date(2026, 1, 25), "Dan", "sell"), tx(date(2026, 1, 26), "Erin", "sell", plan=True),
        tx(date(2025, 1, 5), "Old", "buy")])  # outside the 6-month lookback
    s = insider_summary(data, TODAY)
    assert s.buyers == 3 and s.cluster_buy
    assert s.sellers_discretionary == 1 and s.sellers_10b5_1 == 1
    assert s.net_shares == pytest.approx(100.0)  # 3 buys − 2 sells
    assert s.coverage == "Form 4, full history"


def test_buys_spread_out_are_not_a_cluster():
    data = InsiderData(ticker="TEST", coverage=["manual"], transactions=[
        tx(date(2025, 9, 1), "Alice"), tx(date(2025, 11, 15), "Bob"), tx(date(2026, 2, 10), "Carol")])
    assert not insider_summary(data, TODAY).cluster_buy


def test_no_source_reports_coverage_na():
    s = insider_summary(None, TODAY)
    assert not s.available and s.coverage.startswith("N/A - no insider data source")
