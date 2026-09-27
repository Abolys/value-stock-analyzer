from datetime import date

import pandas as pd
import pytest

from data import periods
from data.provider import EarningsDates, Statement
from data.risk_free import risk_free_for, risk_free_rate
from data.values import Datum


def _stmt(freq, values, notfound=()):
    return Statement(ticker="X", kind="cashflow", freq=freq, values=values, not_found=list(notfound),
                     provider="yfinance", currency="USD")


Q = {date(2026, 6, 30): 10.0, date(2026, 3, 31): 20.0, date(2025, 12, 31): 30.0, date(2025, 9, 30): 40.0,
     date(2025, 6, 30): 999.0}
A = {date(2025, 12, 31): 88.0, date(2024, 12, 31): 77.0}


def test_ttm_from_four_quarters_with_latest_end_date():
    d = periods.ttm(_stmt("quarterly", {"free_cash_flow": Q}), _stmt("annual", {"free_cash_flow": A}), "free_cash_flow")
    assert d.value == 100.0 and d.period_end == date(2026, 6, 30)
    assert d.period_label.startswith("TTM to 2026-06-30")


def test_annual_fallback_label_when_quarters_missing_or_gapped():
    three = {k: v for k, v in list(Q.items())[:3]}
    d = periods.ttm(_stmt("quarterly", {"free_cash_flow": three}), _stmt("annual", {"free_cash_flow": A}),
                    "free_cash_flow")
    assert d.value == 88.0 and d.period_end == date(2025, 12, 31)
    assert d.period_label.startswith("annual, not TTM")
    gapped = {date(2026, 6, 30): 1.0, date(2026, 3, 31): 1.0, date(2025, 9, 30): 1.0, date(2025, 6, 30): 1.0}
    d = periods.ttm(_stmt("quarterly", {"free_cash_flow": gapped}), _stmt("annual", {"free_cash_flow": A}),
                    "free_cash_flow")
    assert d.period_label.startswith("annual, not TTM")


def test_ttm_missing_is_na_not_zero():
    d = periods.ttm(_stmt("quarterly", {}, ["free_cash_flow"]), _stmt("annual", {}, ["free_cash_flow"]), "free_cash_flow")
    assert d.value is None and d.status == "N/A - field not found: free_cash_flow"
    d = periods.ttm(None, None, "free_cash_flow")
    assert d.value is None and d.status == "N/A - Data Incomplete"


def test_latest_balance_uses_latest_quarter():
    d = periods.latest_balance(_stmt("quarterly", {"total_debt": {date(2026, 6, 30): 5.0, date(2026, 3, 31): 6.0}}),
                               _stmt("annual", {"total_debt": {date(2025, 12, 31): 7.0}}), "total_debt")
    assert d.value == 5.0 and d.period_end == date(2026, 6, 30)


def test_lulu_fiscal_year_labelled_by_own_year_end(fx_provider):
    stmt = fx_provider.get_statement("LULU", "income", "annual")
    labels = [periods.fiscal_year_label(d) for d in stmt.periods]
    assert labels[0] == "FY ending Jan 2026"
    assert all("Dec" not in lab for lab in labels)
    fy = periods.fiscal_years(stmt, "total_revenue")
    assert fy[0].period_label == "FY ending Jan 2026"


def test_ttm_on_lulu_fixture_uses_its_own_quarter_end(fx_provider):
    q = fx_provider.get_statement("LULU", "cashflow", "quarterly")
    a = fx_provider.get_statement("LULU", "cashflow", "annual")
    d = periods.ttm(q, a, "operating_cash_flow")
    assert d.ok and d.period_end == date(2026, 7, 31)


def test_staleness_by_age():
    s = periods.staleness(date(2026, 3, 31), EarningsDates(ticker="X"), date(2026, 9, 1))
    assert s.stale and "days ago" in s.reasons[0]
    assert not periods.staleness(date(2026, 6, 30), EarningsDates(ticker="X"), date(2026, 9, 1)).stale


def test_staleness_by_passed_earnings_date():
    # Period ends 2026-06-30 (fresh by age). The 2026-07-28 report covers that quarter;
    # the 2026-08-25 report would be a newer period that the statements don't have yet.
    ed = EarningsDates(ticker="X", past=[date(2026, 4, 28), date(2026, 7, 28), date(2026, 8, 25)])
    s = periods.staleness(date(2026, 6, 30), ed, date(2026, 9, 1))
    assert s.stale and len(s.reasons) == 1 and "earnings reported on 2026-08-25" in s.reasons[0]
    # Only the report of the latest period itself has passed → not stale.
    ed = EarningsDates(ticker="X", past=[date(2026, 4, 28), date(2026, 7, 28)])
    assert not periods.staleness(date(2026, 6, 30), ed, date(2026, 9, 1)).stale


def test_mixed_periods_marking():
    debt = Datum(value=1.0, period_end=date(2026, 6, 30))
    ebitda = Datum(value=1.0, period_end=date(2025, 12, 31))
    mixed, note = periods.mark_mixed_periods({"net_debt": debt, "ebitda": ebitda})
    assert mixed and "net_debt 2026-06-30" in note and "ebitda 2025-12-31" in note
    close = Datum(value=1.0, period_end=date(2026, 3, 31))
    assert periods.mark_mixed_periods({"net_debt": debt, "ebitda": close}) == (False, "")


# ---------------------------------------------------------------- risk-free
class TNXProvider:
    name = "yfinance"

    def __init__(self):
        self.asked = []

    def get_price_history(self, ticker, adjusted):
        self.asked.append(ticker)
        s = pd.Series([4.5, 4.25], index=pd.to_datetime(["2026-09-24", "2026-09-25"]))
        s.attrs["provider"] = "yfinance"
        return s


def test_cad_source_for_tsx_ticker_and_us_source_for_us_ticker(fx_provider):
    valet_calls = []

    def valet(series):
        valet_calls.append(series)
        return 3.1, date(2026, 9, 25)

    cnr = fx_provider.get_info("CNR.TO")
    p = TNXProvider()
    cad = risk_free_for(cnr, p, valet_fetch=valet)
    assert valet_calls == ["BD.CDN.10YR.DQ.YLD"] and p.asked == []
    assert cad.value == pytest.approx(0.031) and "Canada" in cad.period_label

    msft = fx_provider.get_info("MSFT")
    usd = risk_free_for(msft, p, valet_fetch=valet)
    assert p.asked == ["^TNX"] and len(valet_calls) == 1
    assert usd.value == pytest.approx(0.0425) and usd.period_end == date(2026, 9, 25)


def test_tnx_scale_on_fixture(fx_provider):
    d = risk_free_rate("USD", fx_provider)
    assert d.ok and 0.0 < d.value < 0.2  # quoted in percent, stored as a decimal


def test_unknown_currency_is_na_with_reason():
    d = risk_free_rate("EUR", TNXProvider())
    assert d.value is None and d.status == "N/A - no risk-free source configured for EUR"
