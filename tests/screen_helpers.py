"""Synthetic statements and info for screener and signal tests, so every
expected value can be hand-computed. Annual statements only unless a test adds
quarters: TTM then falls back to the latest fiscal year (Rule 3b), which keeps
the arithmetic simple."""

from __future__ import annotations

from datetime import date
from typing import Any

from data import field_map as fm
from data.fundamentals import Fundamentals
from data.provider import InfoResult, Statement
from data.sector import route
from data.shares import ShareTrend
from data.values import Datum

T, P, P2, P3 = date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31), date(2022, 12, 31)
TODAY = date(2026, 2, 15)  # < STALE_FUNDAMENTALS_DAYS after T

# field: (year t, year t-1). A healthy company that passes every screen metric at
# price 10 (market cap 1,000) and scores Piotroski 9.
BASE: dict[str, tuple[float, float]] = {
    # income
    "total_revenue": (1000, 900), "cost_of_revenue": (600, 560), "gross_profit": (400, 340), "sga": (150, 140),
    "operating_income": (200, 150), "ebit": (200, 150), "ebitda": (250, 195), "net_income": (150, 110),
    "net_income_continuing": (150, 110), "pretax_income": (190, 140), "tax_provision": (40, 30),
    "diluted_eps": (1.5, 1.1), "diluted_shares": (100, 100), "depreciation_amortization": (50, 45),
    # balance
    "total_assets": (2000, 1900), "total_liabilities": (800, 820), "current_assets": (600, 550),
    "current_liabilities": (300, 310), "working_capital": (300, 240), "retained_earnings": (700, 600),
    "receivables": (150, 140), "inventory": (100, 95), "net_ppe": (900, 880), "total_debt": (300, 350),
    "long_term_debt": (250, 300), "cash_and_equivalents": (200, 150), "short_term_investments": (50, 40),
    "stockholders_equity": (1200, 1080), "invested_capital": (1300, 1280), "goodwill": (100, 100),
    "other_intangible_assets": (50, 50), "ordinary_shares": (100, 100),
    # cash flow
    "operating_cash_flow": (220, 180), "capital_expenditure": (-60, -55), "free_cash_flow": (160, 125),
    "stock_based_compensation": (10, 9),
}


def make_fundamentals(overrides: dict[str, Any] | None = None, base: dict | None = None,
                      years: tuple[date, ...] = (T, P), ticker: str = "TEST") -> Fundamentals:
    """overrides: field → tuple of values per year (newest first), or None to remove the row."""
    data = dict(BASE if base is None else base)
    for k, v in (overrides or {}).items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    stmts = {}
    for kind in ("income", "balance", "cashflow"):
        values = {}
        for fs in fm.fields_for(kind):
            if fs.canonical in data:
                vals = data[fs.canonical]
                values[fs.canonical] = {d: float(v) for d, v in zip(years, vals) if v is not None}
        not_found = [fs.canonical for fs in fm.fields_for(kind) if fs.canonical not in data]
        stmts[f"{kind}_annual"] = Statement(ticker=ticker, kind=kind, freq="annual", values=values,
                                           not_found=not_found, currency="USD", provider="test")
        stmts[f"{kind}_quarterly"] = Statement(ticker=ticker, kind=kind, freq="quarterly", currency="USD",
                                              provider="test")
    return Fundamentals(ticker=ticker, statements=stmts)


def make_info(sector: str = "Industrials", industry: str = "Specialty Industrial Machinery",
              currency: str = "USD", financial_currency: str = "USD", ticker: str = "TEST",
              **values: Any) -> InfoResult:
    vals = {"sector": sector, "industry": industry, "currency": currency, "financial_currency": financial_currency,
            "long_name": f"{ticker} Corp", **values}
    return InfoResult(ticker=ticker, values=vals, statuses={k: "ok" for k in vals}, provider="test")


def info_values(**kw: float | None) -> dict[str, Datum]:
    """Stage-1 info Datums (already in the trading currency); None → N/A."""
    return {k: (Datum(value=v, provider="test") if v is not None else Datum.missing()) for k, v in kw.items()}


def trend(per_year: float | None = -0.01) -> ShareTrend:
    if per_year is None:
        return ShareTrend(status="N/A - Data Incomplete", notes=["no share-count history"])
    return ShareTrend(trend_per_year=per_year, span_start=date(2022, 12, 31), span_end=T, points=13,
                      source="test")


def price(v: float = 10.0) -> Datum:
    return Datum(value=v, period_end=date(2026, 2, 13), period_label="actual latest close", provider="test")


def rf(v: float | None = 0.04) -> Datum:
    return Datum(value=v, provider="test") if v is not None else Datum.missing("N/A - no risk-free source configured for XXX")


def run_eval(f: Fundamentals | None = None, px: float = 10.0, info: InfoResult | None = None,
             tr: ShareTrend | None = None, risk_free: Datum | None = None, today: date = TODAY, **kw):
    from screening.stage2 import evaluate

    info = info or make_info()
    return evaluate("TEST", info, {}, route(info), f or make_fundamentals(), tr or trend(), price(px),
                    risk_free or rf(), today, **kw)
