"""The only place yfinance `info` keys, statement row labels and analyst-estimate
property names are written down (SPEC "Data source resilience").

Each canonical field lists its known aliases in priority order. Code outside
the provider layer uses canonical names only. A label that matches none of
its aliases resolves to "N/A - field not found: <name>", which is distinct from
an ordinary missing value ("N/A - Data Incomplete").

Aliases verified against yfinance 1.7.0 output (LULU, JPM, HTZ, LCID, MELI,
ABX.TO, CNR.TO) in 2026-09.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from data.values import NA_INCOMPLETE, is_missing, na_field_not_found

Source = Literal["info", "income", "balance", "cashflow", "estimates"]
# monetary: amounts in the reporting currency (converted on FX mismatch)
# per_share: per-share amounts in the reporting currency (converted too)
# trading_monetary: already in the trading currency (market cap) — never converted
# shares / ratio / text / date / table: never converted
Kind = Literal["monetary", "per_share", "trading_monetary", "shares", "ratio", "text", "date", "table"]


@dataclass(frozen=True)
class FieldSpec:
    canonical: str
    source: Source
    aliases: tuple[str, ...]
    kind: Kind
    flow: bool = False  # income/cash-flow items summed into TTM (Rule 3b)
    health_required: bool = False  # checked on equity canaries by data/health.py


def _f(canonical, source, aliases, kind, flow=False, health=False) -> FieldSpec:
    return FieldSpec(canonical, source, tuple(aliases), kind, flow, health)


FIELDS: dict[str, FieldSpec] = {
    f.canonical: f
    for f in [
        # ---------------- info ----------------
        _f("currency", "info", ["currency"], "text", health=True),
        _f("financial_currency", "info", ["financialCurrency"], "text", health=True),
        _f("sector", "info", ["sector", "sectorDisp"], "text", health=True),
        _f("industry", "info", ["industry", "industryDisp"], "text", health=True),
        _f("long_name", "info", ["longName", "shortName"], "text"),
        _f("business_summary", "info", ["longBusinessSummary"], "text"),
        _f("country", "info", ["country"], "text"),
        _f("exchange", "info", ["exchange", "fullExchangeName"], "text"),
        _f("quote_type", "info", ["quoteType"], "text"),
        _f("trailing_eps", "info", ["trailingEps", "epsTrailingTwelveMonths"], "per_share", health=True),
        _f("book_value_per_share", "info", ["bookValue"], "per_share", health=True),
        _f("info_free_cashflow", "info", ["freeCashflow"], "monetary"),
        _f("info_ebitda", "info", ["ebitda"], "monetary"),
        _f("info_total_debt", "info", ["totalDebt"], "monetary"),
        _f("info_total_cash", "info", ["totalCash"], "monetary"),
        _f("info_total_revenue", "info", ["totalRevenue"], "monetary"),
        _f("shares_outstanding", "info", ["sharesOutstanding", "impliedSharesOutstanding"], "shares", health=True),
        _f("market_cap", "info", ["marketCap"], "trading_monetary", health=True),
        _f("held_percent_insiders", "info", ["heldPercentInsiders"], "ratio"),
        _f("short_percent_of_float", "info", ["shortPercentOfFloat"], "ratio"),
        _f("dividend_rate", "info", ["dividendRate", "trailingAnnualDividendRate"], "per_share"),
        _f("company_officers", "info", ["companyOfficers"], "table"),
        _f("last_fiscal_year_end", "info", ["lastFiscalYearEnd"], "date"),
        _f("most_recent_quarter", "info", ["mostRecentQuarter"], "date"),
        # ---------------- income statement ----------------
        _f("total_revenue", "income", ["Total Revenue", "Operating Revenue"], "monetary", flow=True, health=True),
        _f("cost_of_revenue", "income", ["Cost Of Revenue", "Reconciled Cost Of Revenue"], "monetary", flow=True),
        _f("gross_profit", "income", ["Gross Profit"], "monetary", flow=True),
        _f("sga", "income", ["Selling General And Administration", "General And Administrative Expense"], "monetary", flow=True),
        _f("operating_income", "income", ["Operating Income", "Total Operating Income As Reported"], "monetary", flow=True),
        _f("ebit", "income", ["EBIT"], "monetary", flow=True),
        _f("ebitda", "income", ["EBITDA", "Normalized EBITDA"], "monetary", flow=True),
        _f("interest_expense", "income", ["Interest Expense", "Interest Expense Non Operating"], "monetary", flow=True),
        _f("pretax_income", "income", ["Pretax Income"], "monetary", flow=True),
        _f("net_income", "income", ["Net Income", "Net Income Common Stockholders",
                                    "Net Income From Continuing Operation Net Minority Interest"], "monetary", flow=True, health=True),
        _f("diluted_eps", "income", ["Diluted EPS"], "per_share", flow=True),
        _f("diluted_shares", "income", ["Diluted Average Shares"], "shares"),
        _f("basic_shares", "income", ["Basic Average Shares"], "shares"),
        _f("depreciation_amortization", "income", ["Reconciled Depreciation",
                                                   "Depreciation And Amortization In Income Statement",
                                                   "Depreciation Amortization Depletion Income Statement"], "monetary", flow=True),
        _f("gain_on_sale_of_ppe", "income", ["Gain On Sale Of Ppe", "Gain On Sale Of Property Plant Equipment"], "monetary", flow=True),
        # ---------------- balance sheet ----------------
        _f("total_assets", "balance", ["Total Assets"], "monetary", health=True),
        _f("total_liabilities", "balance", ["Total Liabilities Net Minority Interest", "Total Liabilities"], "monetary"),
        _f("current_assets", "balance", ["Current Assets"], "monetary"),
        _f("current_liabilities", "balance", ["Current Liabilities"], "monetary"),
        _f("working_capital", "balance", ["Working Capital"], "monetary"),
        _f("retained_earnings", "balance", ["Retained Earnings"], "monetary"),
        _f("receivables", "balance", ["Accounts Receivable", "Receivables", "Net Receivables"], "monetary"),
        _f("inventory", "balance", ["Inventory"], "monetary"),
        _f("net_ppe", "balance", ["Net PPE"], "monetary"),
        _f("gross_ppe", "balance", ["Gross PPE"], "monetary"),
        _f("total_debt", "balance", ["Total Debt"], "monetary"),
        _f("long_term_debt", "balance", ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"], "monetary"),
        _f("current_debt", "balance", ["Current Debt", "Current Debt And Capital Lease Obligation"], "monetary"),
        _f("cash_and_equivalents", "balance", ["Cash And Cash Equivalents",
                                               "Cash Cash Equivalents And Federal Funds Sold"], "monetary"),
        _f("cash_and_short_term_investments", "balance", ["Cash Cash Equivalents And Short Term Investments"], "monetary"),
        _f("short_term_investments", "balance", ["Other Short Term Investments", "Short Term Investments"], "monetary"),
        _f("stockholders_equity", "balance", ["Stockholders Equity", "Common Stock Equity"], "monetary", health=True),
        _f("tangible_book_value", "balance", ["Tangible Book Value"], "monetary"),
        _f("invested_capital", "balance", ["Invested Capital"], "monetary"),
        _f("goodwill_and_intangibles", "balance", ["Goodwill And Other Intangible Assets"], "monetary"),
        _f("preferred_stock", "balance", ["Preferred Stock", "Preferred Stock Equity"], "monetary"),
        _f("minority_interest", "balance", ["Minority Interest"], "monetary"),
        _f("ordinary_shares", "balance", ["Ordinary Shares Number", "Share Issued"], "shares"),
        # ---------------- cash-flow statement ----------------
        _f("operating_cash_flow", "cashflow", ["Operating Cash Flow",
                                               "Cash Flow From Continuing Operating Activities"], "monetary", flow=True, health=True),
        _f("capital_expenditure", "cashflow", ["Capital Expenditure", "Purchase Of PPE"], "monetary", flow=True),
        _f("free_cash_flow", "cashflow", ["Free Cash Flow"], "monetary", flow=True),
        _f("stock_based_compensation", "cashflow", ["Stock Based Compensation"], "monetary", flow=True),
        _f("dividends_paid", "cashflow", ["Cash Dividends Paid", "Common Stock Dividend Paid"], "monetary", flow=True),
        _f("depreciation_cf", "cashflow", ["Depreciation And Amortization", "Depreciation Amortization Depletion",
                                           "Depreciation"], "monetary", flow=True),
        _f("gain_loss_on_sale_of_ppe", "cashflow", ["Gain Loss On Sale Of PPE"], "monetary", flow=True),
        _f("repurchase_of_stock", "cashflow", ["Repurchase Of Capital Stock", "Common Stock Payments"], "monetary", flow=True),
        _f("issuance_of_stock", "cashflow", ["Issuance Of Capital Stock", "Common Stock Issuance"], "monetary", flow=True),
        # ---------------- analyst estimates (yfinance Ticker properties) ----------------
        # Verified present in yfinance 1.7.0. Coverage is patchy; missing → N/A.
        _f("earnings_estimate", "estimates", ["earnings_estimate"], "table"),
        _f("revenue_estimate", "estimates", ["revenue_estimate"], "table"),
        _f("eps_trend", "estimates", ["eps_trend"], "table"),
        _f("eps_revisions", "estimates", ["eps_revisions"], "table"),
        _f("growth_estimates", "estimates", ["growth_estimates"], "table"),
    ]
}

STATEMENT_SOURCES: tuple[Source, ...] = ("income", "balance", "cashflow")

# yfinance Ticker attribute per (statement kind, frequency).
YF_STATEMENT_ATTRS = {
    ("income", "annual"): "income_stmt",
    ("income", "quarterly"): "quarterly_income_stmt",
    ("balance", "annual"): "balance_sheet",
    ("balance", "quarterly"): "quarterly_balance_sheet",
    ("cashflow", "annual"): "cash_flow",
    ("cashflow", "quarterly"): "quarterly_cash_flow",
}
# Column names in yfinance price frames and calendar dicts.
YF_PRICE_CLOSE = "Close"
YF_PRICE_ADJ_CLOSE = "Adj Close"
YF_CALENDAR_EARNINGS_DATE = ("Earnings Date",)
YF_SPLITS_NAME = "Stock Splits"


def spec(canonical: str) -> FieldSpec:
    return FIELDS[canonical]


def fields_for(source: Source) -> list[FieldSpec]:
    return [f for f in FIELDS.values() if f.source == source]


def resolve_info(info: dict[str, Any] | None, canonical: str) -> tuple[Any, str]:
    """Return (value, status) for an info field.

    - key present under some alias with a value → (value, "ok")
    - key present but empty → (None, "N/A - Data Incomplete")
    - no alias present at all → (None, "N/A - field not found: <canonical>")
    """
    fs = FIELDS[canonical]
    info = info or {}
    seen_key = False
    for alias in fs.aliases:
        if alias in info:
            seen_key = True
            v = info[alias]
            if not is_missing(v) and v != "":
                return v, "ok"
    if seen_key:
        return None, NA_INCOMPLETE
    return None, na_field_not_found(canonical)


def resolve_row_label(index: pd.Index | list[str], canonical: str) -> str | None:
    """The first alias present in a statement's row index, or None."""
    labels = set(index)
    for alias in FIELDS[canonical].aliases:
        if alias in labels:
            return alias
    return None


def canonicalise_statement(df: pd.DataFrame | None, source: Source) -> tuple[dict[str, dict], list[str]]:
    """Translate a raw yfinance statement (rows = labels, columns = period ends)
    into {canonical: {period_end_date: value}}.

    Returns the mapping and the list of canonical fields not found under any
    alias. Values that are NaN in a period are simply absent for that period.
    """
    out: dict[str, dict] = {}
    not_found: list[str] = []
    if df is None or df.empty:
        return out, [f.canonical for f in fields_for(source)]
    for fs in fields_for(source):
        label = resolve_row_label(df.index, fs.canonical)
        if label is None:
            not_found.append(fs.canonical)
            continue
        row = df.loc[label]
        if isinstance(row, pd.DataFrame):  # duplicated label: keep first
            row = row.iloc[0]
        values = {}
        for col, v in row.items():
            if not is_missing(v):
                values[pd.Timestamp(col).date()] = float(v)
        out[fs.canonical] = values
    return out, not_found


def resolve_estimate_property(ticker_obj: Any, canonical: str) -> tuple[Any, str]:
    """Read an analyst-estimate property; a missing property or empty table → N/A."""
    fs = FIELDS[canonical]
    for alias in fs.aliases:
        if not hasattr(type(ticker_obj), alias) and not hasattr(ticker_obj, alias):
            continue
        try:
            v = getattr(ticker_obj, alias)
        except Exception as exc:  # yfinance raises on unsupported tickers
            return None, f"N/A - {canonical} unavailable ({type(exc).__name__})"
        if v is None or (isinstance(v, pd.DataFrame) and v.empty):
            return None, NA_INCOMPLETE
        return v, "ok"
    return None, na_field_not_found(canonical)
