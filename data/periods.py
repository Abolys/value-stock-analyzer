"""Period handling (CLAUDE.md Rule 3b).

- Flows: trailing twelve months from the last four consecutive quarters, dated
  by the latest quarter; otherwise the latest fiscal year labelled
  "annual, not TTM".
- Balance-sheet items: latest quarter, with its date.
- Fiscal years are labelled by the company's own year end ("FY ending Jan 2026").
- Staleness: latest period older than STALE_FUNDAMENTALS_DAYS, or an earnings
  date passed since that period's end with no newer statements.
- Mixed periods: a ratio whose inputs' period ends are more than
  MIXED_PERIOD_DAYS apart.
"""

from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel, Field

import config
from data.provider import EarningsDates, Statement
from data.values import Datum, NA_INCOMPLETE, na_field_not_found

ANNUAL_NOT_TTM = "annual, not TTM"


def fiscal_year_label(period_end: date) -> str:
    return f"FY ending {period_end.strftime('%b %Y')}"


def _field_status(stmt: Statement | None, canonical: str) -> str:
    if stmt is not None and canonical in stmt.not_found:
        return na_field_not_found(canonical)
    return NA_INCOMPLETE


def _consecutive(dates: list[date]) -> bool:
    lo, hi = config.QUARTER_GAP_DAYS
    return all(lo <= (a - b).days <= hi for a, b in zip(dates, dates[1:]))


def ttm(quarterly: Statement | None, annual: Statement | None, canonical: str) -> Datum:
    """TTM sum of a flow field (four consecutive quarters), else latest fiscal year."""
    n = config.TTM_QUARTERS
    q = quarterly.series(canonical) if quarterly else {}
    if len(q) >= n:
        dates = sorted(q, reverse=True)[:n]
        if _consecutive(dates):
            return Datum(value=sum(q[d] for d in dates), period_end=dates[0],
                         period_label=f"TTM to {dates[0].isoformat()} ({n} quarters)",
                         provider=quarterly.provider, currency=quarterly.currency, notes=list(quarterly.notes))
    a = annual.series(canonical) if annual else {}
    if a:
        latest = max(a)
        note = []
        if q:
            note.append(f"only {len(q)} quarter(s) or non-consecutive quarters available; used latest fiscal year")
        return Datum(value=a[latest], period_end=latest,
                     period_label=f"{ANNUAL_NOT_TTM} ({fiscal_year_label(latest)})",
                     provider=annual.provider, currency=annual.currency, notes=[*annual.notes, *note])
    return Datum.missing(_field_status(quarterly if quarterly and canonical in quarterly.not_found else annual, canonical))


def latest_balance(quarterly: Statement | None, annual: Statement | None, canonical: str) -> Datum:
    """Latest-quarter balance-sheet value with its date (falls back to the latest fiscal year)."""
    for stmt, label in ((quarterly, "quarter ending {d}"), (annual, "fiscal year end {d}")):
        s = stmt.series(canonical) if stmt else {}
        if s:
            d = max(s)
            return Datum(value=s[d], period_end=d, period_label=label.format(d=d.isoformat()),
                         provider=stmt.provider, currency=stmt.currency, notes=list(stmt.notes))
    return Datum.missing(_field_status(quarterly if quarterly and canonical in quarterly.not_found else annual, canonical))


def fiscal_years(annual: Statement | None, canonical: str) -> list[Datum]:
    """One Datum per fiscal year, newest first, labelled by the company's own year end."""
    if annual is None:
        return []
    s = annual.series(canonical)
    return [Datum(value=s[d], period_end=d, period_label=fiscal_year_label(d), provider=annual.provider,
                  currency=annual.currency, notes=list(annual.notes))
            for d in sorted(s, reverse=True)]


def latest_period_end(*stmts: Statement | None) -> date | None:
    ends = [p for s in stmts if s is not None for p in s.periods[:1]]
    return max(ends) if ends else None


class Staleness(BaseModel):
    stale: bool
    as_of: date | None
    reasons: list[str] = Field(default_factory=list)

    @property
    def label(self) -> str:
        if not self.stale:
            return ""
        return f"fundamentals may be stale (as of {self.as_of}): " + "; ".join(self.reasons)


def staleness(latest_end: date | None, earnings: EarningsDates | None, today: date) -> Staleness:
    """Flag stale fundamentals.

    The first earnings date after a period's end normally reports that period
    itself, so a *newer* period has been reported (but is missing here) only
    when two or more earnings dates have passed since the period end. Dates
    within EARNINGS_REFETCH_GRACE_DAYS of today are not counted, since
    statements appear a few days after the report.
    """
    if latest_end is None:
        return Staleness(stale=True, as_of=None, reasons=["no reported period found"])
    reasons = []
    age = (today - latest_end).days
    if age > config.STALE_FUNDAMENTALS_DAYS:
        reasons.append(f"latest period ended {age} days ago (> {config.STALE_FUNDAMENTALS_DAYS})")
    if earnings is not None:
        cutoff = today - timedelta(days=config.EARNINGS_REFETCH_GRACE_DAYS)
        passed = sorted(d for d in earnings.past if latest_end < d <= cutoff)
        if len(passed) >= 2:
            reasons.append(f"earnings reported on {passed[-1].isoformat()} after the period ending "
                           f"{latest_end.isoformat()}, but no newer statements are available")
    return Staleness(stale=bool(reasons), as_of=latest_end, reasons=reasons)


def mark_mixed_periods(inputs: dict[str, Datum]) -> tuple[bool, str]:
    """(mixed?, note). Mixed when the dated inputs' period ends differ by more than MIXED_PERIOD_DAYS."""
    dated = {k: d.period_end for k, d in inputs.items() if d.period_end is not None}
    if len(dated) < 2:
        return False, ""
    spread = (max(dated.values()) - min(dated.values())).days
    if spread <= config.MIXED_PERIOD_DAYS:
        return False, ""
    listing = ", ".join(f"{k} {v.isoformat()}" for k, v in dated.items())
    return True, f"mixed periods ({spread} days apart): {listing}"
