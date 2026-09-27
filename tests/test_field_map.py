import pandas as pd

from data import field_map as fm
from data.values import NA_INCOMPLETE


def test_alias_resolves_when_primary_label_missing():
    df = pd.DataFrame({pd.Timestamp("2025-12-31"): [100.0, 5.0]},
                      index=["Operating Revenue", "Net Income Common Stockholders"])
    values, not_found = fm.canonicalise_statement(df, "income")
    assert values["total_revenue"] == {pd.Timestamp("2025-12-31").date(): 100.0}
    assert values["net_income"][pd.Timestamp("2025-12-31").date()] == 5.0
    assert "total_revenue" not in not_found


def test_field_not_found_when_no_alias_matches():
    df = pd.DataFrame({pd.Timestamp("2025-12-31"): [1.0]}, index=["Some Renamed Label"])
    values, not_found = fm.canonicalise_statement(df, "income")
    assert "total_revenue" in not_found and "total_revenue" not in values


def test_info_field_not_found_vs_ordinary_missing():
    assert fm.resolve_info({"freeCashflow": 10}, "info_free_cashflow") == (10, "ok")
    assert fm.resolve_info({"freeCashflow": None}, "info_free_cashflow") == (None, NA_INCOMPLETE)
    v, st = fm.resolve_info({}, "info_free_cashflow")
    assert v is None and st == "N/A - field not found: info_free_cashflow"


def test_info_alias_fallback():
    assert fm.resolve_info({"epsTrailingTwelveMonths": 2.5}, "trailing_eps") == (2.5, "ok")


def test_signal_inputs_are_mapped():
    needed = ["held_percent_insiders", "short_percent_of_float", "working_capital", "retained_earnings",
              "receivables", "sga", "depreciation_amortization", "net_ppe", "current_assets", "current_liabilities",
              "long_term_debt", "gross_profit", "dividends_paid", "stock_based_compensation", "total_liabilities",
              "earnings_estimate", "revenue_estimate", "eps_trend", "eps_revisions", "growth_estimates"]
    assert not [n for n in needed if n not in fm.FIELDS]


def test_missing_analyst_estimate_property_returns_na_without_error():
    class OldTicker:  # a yfinance version without the estimate properties
        pass

    v, st = fm.resolve_estimate_property(OldTicker(), "eps_revisions")
    assert v is None and st == "N/A - field not found: eps_revisions"

    class RaisingTicker:
        @property
        def eps_trend(self):
            raise KeyError("boom")

    v, st = fm.resolve_estimate_property(RaisingTicker(), "eps_trend")
    assert v is None and st.startswith("N/A")

    class EmptyTicker:
        eps_trend = pd.DataFrame()

    assert fm.resolve_estimate_property(EmptyTicker(), "eps_trend") == (None, NA_INCOMPLETE)


def test_estimates_via_provider_on_fixture(fx_provider):
    est = fx_provider.get_analyst_estimates("LULU")
    assert est.statuses["eps_trend"] == "ok"
    spy = fx_provider.get_analyst_estimates("SPY")  # an ETF: no estimates saved
    assert all(s.startswith("N/A") for s in spy.statuses.values())


def test_graceful_na_for_missing_fields(fx_provider):
    info = fx_provider.get_info("SPY")  # ETF: no fundamentals at all
    for name in ("info_free_cashflow", "info_ebitda", "held_percent_insiders", "sector"):
        assert info.get(name) is None
        assert info.status(name).startswith("N/A")
    stmt = fx_provider.get_statement("SPY", "income", "annual")
    assert stmt.empty and "total_revenue" in stmt.not_found
