"""Dividend safety (SPEC "Value-trap, valuation and ownership signals").

Dividend payers only; a ticker with no dividend in the last year and no
dividends paid in the TTM cash flow → N/A "no dividend".
- Trailing yield = per-share dividends over the last 365 days / actual latest price.
- FCF payout = dividends paid / raw TTM FCF; > DIVIDEND_FCF_PAYOUT_MAX, or FCF ≤ 0
  while paying → "dividend at risk".
- Earnings payout = dividends paid / TTM net income (n/m when net income ≤ 0).
- Financials and REITs (Rule 5: FCF is not meaningful): FCF payout is n/m and the
  at-risk test is the earnings payout above DIVIDEND_EARNINGS_PAYOUT_MAX_FINANCIALS,
  or net income ≤ 0 while paying.
- Uninterrupted years and cuts: calendar-year per-share totals (yfinance history is
  split-adjusted); a year-over-year drop > DIVIDEND_CUT_THRESHOLD is a cut. The
  current, incomplete calendar year is left out.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from pydantic import BaseModel, Field

import config
from data import periods
from data.fundamentals import Fundamentals
from data.ratios import combine, safe_ratio
from data.values import Datum, nm
from screening.metrics import ttm_fcf

NO_DIVIDEND = "N/A - no dividend"
DAYS_PER_YEAR = 365


class DividendCut(BaseModel):
    year: int
    previous: float
    current: float
    change: float


class DividendSafety(BaseModel):
    status: str = "ok"  # ok | NO_DIVIDEND
    trailing_yield: Datum = Field(default_factory=Datum.missing)
    fcf_payout: Datum = Field(default_factory=Datum.missing)
    earnings_payout: Datum = Field(default_factory=Datum.missing)
    at_risk: bool = False
    at_risk_reason: str = ""
    uninterrupted_years: int = 0
    cuts: list[DividendCut] = Field(default_factory=list)
    annual_per_share: dict[int, float] = Field(default_factory=dict)
    history_span: str = ""
    # FCF payout per fiscal year (dividends paid / raw FCF), keyed by the company's own FY label,
    # oldest first; each an n/m or N/A Datum with its reason when it can't be computed.
    fcf_payout_history: dict[str, Datum] = Field(default_factory=dict)
    # Financials and REITs: dividends paid / net income per fiscal year instead (FCF is not meaningful).
    earnings_payout_history: dict[str, Datum] = Field(default_factory=dict)
    payout_basis: str = "fcf"  # "fcf" | "earnings" (financials and REITs)

    @property
    def payer(self) -> bool:
        return self.status == "ok"


def annual_totals(dividends: pd.Series, today: date) -> dict[int, float]:
    if dividends is None or len(dividends) == 0:
        return {}
    s = dividends[dividends > 0]
    by_year = s.groupby(s.index.year).sum()
    return {int(y): float(v) for y, v in by_year.items() if int(y) < today.year}


def find_cuts(totals: dict[int, float]) -> list[DividendCut]:
    years = sorted(totals)
    out = []
    for prev, cur in zip(years, years[1:]):
        if cur != prev + 1:
            continue
        change = totals[cur] / totals[prev] - 1
        if change < -config.DIVIDEND_CUT_THRESHOLD:
            out.append(DividendCut(year=cur, previous=totals[prev], current=totals[cur], change=change))
    return out


def uninterrupted_years(totals: dict[int, float], today: date) -> int:
    n, y = 0, today.year - 1
    while totals.get(y, 0) > 0:
        n += 1
        y -= 1
    return n


def dividend_safety(dividends: pd.Series | None, f: Fundamentals, price: Datum, today: date,
                    sector_adjusted: bool = False) -> DividendSafety:
    divs = dividends if dividends is not None else pd.Series(dtype=float)
    recent = divs[divs.index >= pd.Timestamp(today - timedelta(days=DAYS_PER_YEAR))] if len(divs) else divs
    paid = f.ttm("dividends_paid")
    paid_abs = paid.model_copy(update={"value": abs(paid.value)}) if paid.ok else paid
    if (len(recent) == 0 or recent.sum() <= 0) and not (paid_abs.ok and paid_abs.value > 0):
        return DividendSafety(status=NO_DIVIDEND)
    res = DividendSafety()
    if len(recent) and price.ok and price.value > 0:
        res.trailing_yield = combine(float(recent.sum()) / price.value, {"price": price},
                                     label="trailing 12-month dividends per share / actual latest price")
    res.earnings_payout = safe_ratio(paid_abs, f.ttm("net_income"), name="earnings payout",
                                     nonpositive_reason="net income ≤ 0", num_name="dividends paid",
                                     den_name="net income")
    fcf = ttm_fcf(f)
    if sector_adjusted:
        res.fcf_payout = Datum.missing(nm("FCF not meaningful for financials and REITs"))
        ep, cap = res.earnings_payout, config.DIVIDEND_EARNINGS_PAYOUT_MAX_FINANCIALS
        if (ep.ok and ep.value > cap) or ep.is_nm:
            res.at_risk = True
            res.at_risk_reason = ((f"earnings payout {ep.value:.0%} > {cap:.0%}" if ep.ok
                                   else f"earnings payout {ep.status} while paying")
                                  + " (financials: earnings payout used, FCF not meaningful)")
    elif paid_abs.ok and fcf.ok and fcf.value <= 0:
        res.fcf_payout = Datum.missing(nm("FCF ≤ 0 while paying a dividend"))
        res.at_risk, res.at_risk_reason = True, f"FCF ≤ 0 ({fcf.value:,.4g}) while paying a dividend"
    else:
        res.fcf_payout = safe_ratio(paid_abs, fcf, name="FCF payout", nonpositive_reason="FCF ≤ 0",
                                    num_name="dividends paid", den_name="FCF")
        if res.fcf_payout.ok and res.fcf_payout.value > config.DIVIDEND_FCF_PAYOUT_MAX:
            res.at_risk = True
            res.at_risk_reason = (f"FCF payout {res.fcf_payout.value:.0%} > "
                                  f"{config.DIVIDEND_FCF_PAYOUT_MAX:.0%} (DIVIDEND_FCF_PAYOUT_MAX)")
    totals = annual_totals(divs, today)
    res.annual_per_share = totals
    res.cuts = find_cuts(totals)
    res.uninterrupted_years = uninterrupted_years(totals, today)
    if totals:
        res.history_span = f"{min(totals)}–{max(totals)}"
    res.fcf_payout_history = fcf_payout_history(f, sector_adjusted)
    if sector_adjusted:
        res.payout_basis = "earnings"
        res.earnings_payout_history = earnings_payout_history(f)
    return res


def earnings_payout_history(f: Fundamentals) -> dict[str, Datum]:
    """Dividends paid ÷ net income for each fiscal year, oldest first (financials and REITs)."""
    out: dict[str, Datum] = {}
    for fy in sorted(f.fiscal_year_ends()):
        paid = f.fy("dividends_paid", fy)
        paid_abs = paid.model_copy(update={"value": abs(paid.value)}) if paid.ok else paid
        out[periods.fiscal_year_label(fy)] = safe_ratio(
            paid_abs, f.fy("net_income", fy), name="earnings payout", nonpositive_reason="net income ≤ 0",
            num_name="dividends paid", den_name="net income")
    return out


def fcf_payout_history(f: Fundamentals, sector_adjusted: bool = False) -> dict[str, Datum]:
    """Dividends paid ÷ raw FCF for each fiscal year in the annual statements, oldest first."""
    out: dict[str, Datum] = {}
    for fy in sorted(f.fiscal_year_ends()):
        label = periods.fiscal_year_label(fy)
        if sector_adjusted:
            out[label] = Datum.missing(nm("FCF not meaningful for financials and REITs"), period_end=fy)
            continue
        paid = f.fy("dividends_paid", fy)
        paid_abs = paid.model_copy(update={"value": abs(paid.value)}) if paid.ok else paid
        out[label] = safe_ratio(paid_abs, f.fy("free_cash_flow", fy), name="FCF payout", nonpositive_reason="FCF ≤ 0",
                                num_name="dividends paid", den_name="FCF")
    return out
