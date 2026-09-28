"""SEC EDGAR XBRL company facts (free, official, no key): long histories of what US filers
reported in their 10-K and 10-Q filings, used where yfinance's ~4 years fall short:

- historical valuation ratios (P/E, P/B) for the valuation-based recovery clock;
- the 10-K debt maturity schedule (principal due in the next 12 months, years 2–5, after 5).

Rules:
- Every XBRL concept is named once, in XBRL_TAGS (with its alternatives), like data/field_map.py
  does for yfinance labels. A concept under none of its names is "N/A - not reported in XBRL".
- Point in time: each period keeps the value as FIRST filed, and is known only from its filing
  date, so a ratio on a past day never uses a number that wasn't public yet (no look-ahead).
- Only periodic reports count (XBRL_FORMS); proxy statements and 8-Ks repeat facts.
- Flows become trailing twelve months from four consecutive quarters (Q4 = fiscal year − Q1..Q3,
  as 10-Ks report only the full year), like data/periods.ttm.
- Coverage: companies that file with the SEC. TSX-only companies aren't covered (SEDAR+ must
  never be automated); IFRS filers' concepts are listed where the IFRS taxonomy has them.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

import config

NOT_REPORTED = "N/A - not reported in XBRL"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# canonical → [(taxonomy, concept)], tried in order. Units: currency for money, "shares", or USD/shares.
XBRL_TAGS: dict[str, list[tuple[str, str]]] = {
    "net_income": [("us-gaap", "NetIncomeLoss"), ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
                   ("us-gaap", "ProfitLoss"), ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"),
                   ("ifrs-full", "ProfitLoss")],
    "equity": [("us-gaap", "StockholdersEquity"),
               ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
               ("ifrs-full", "EquityAttributableToOwnersOfParent"), ("ifrs-full", "Equity")],
    "shares_outstanding": [("dei", "EntityCommonStockSharesOutstanding"),
                           ("us-gaap", "CommonStockSharesOutstanding")],
    "debt_due_12m": [("us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths")],
    "debt_due_y2": [("us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo")],
    "debt_due_y3": [("us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree")],
    "debt_due_y4": [("us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour")],
    "debt_due_y5": [("us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive")],
    "debt_due_after_y5": [("us-gaap", "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive")],
}
MATURITY_BUCKETS = {"debt_due_12m": "next 12 months", "debt_due_y2": "year 2", "debt_due_y3": "year 3",
                    "debt_due_y4": "year 4", "debt_due_y5": "year 5", "debt_due_after_y5": "after year 5"}
# Canadian and other foreign issuers listed in the US (MJDS / 40-F filers) furnish their tagged quarterly
# and annual statements on 6-K, so 6-K facts count too (plain press-release 6-Ks carry no financial facts).
XBRL_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT", "6-K", "6-K/A")


class Fact(BaseModel):
    start: date | None = None
    end: date
    value: float
    filed: date
    form: str
    unit: str

    @property
    def days(self) -> int | None:
        return (self.end - self.start).days if self.start else None


class Concept(BaseModel):
    canonical: str
    tag: str = ""  # taxonomy:concept actually used
    unit: str = ""
    facts: list[Fact] = Field(default_factory=list)  # one per period (first filed), oldest end first
    status: str = "ok"


class CompanyFacts(BaseModel):
    cik: int
    entity: str = ""
    concepts: dict[str, Concept] = Field(default_factory=dict)

    def concept(self, canonical: str) -> Concept:
        return self.concepts.get(canonical) or Concept(canonical=canonical, status=NOT_REPORTED)


def _first_filed(rows: list[dict[str, Any]], unit: str) -> list[Fact]:
    """One fact per (start, end) period: the value as first filed in a periodic report."""
    best: dict[tuple, Fact] = {}
    for r in rows:
        if r.get("form") not in XBRL_FORMS or r.get("val") is None or not r.get("end") or not r.get("filed"):
            continue
        f = Fact(start=date.fromisoformat(r["start"]) if r.get("start") else None, end=date.fromisoformat(r["end"]),
                 value=float(r["val"]), filed=date.fromisoformat(r["filed"]), form=r["form"], unit=unit)
        key = (f.start, f.end)
        if key not in best or f.filed < best[key].filed:
            best[key] = f
    return sorted(best.values(), key=lambda f: (f.end, f.start or f.end))


def parse_company_facts(data: dict[str, Any], currency: str | None = None) -> CompanyFacts:
    """Pick each canonical concept from the alias with the most recent data (companies switch concepts
    and even taxonomies over the years, e.g. US GAAP to IFRS), ties going to the earlier alias; money
    in `currency` when given (else USD, else the unit with the most facts), shares in "shares"."""
    out = CompanyFacts(cik=int(data.get("cik") or 0), entity=data.get("entityName") or "")
    facts = data.get("facts") or {}
    for canonical, aliases in XBRL_TAGS.items():
        best: Concept | None = None
        for taxonomy, concept in aliases:
            units = (facts.get(taxonomy) or {}).get(concept, {}).get("units") or {}
            if not units:
                continue
            if canonical == "shares_outstanding":
                unit = "shares" if "shares" in units else None
            elif (currency or "USD") in units:
                unit = currency or "USD"
            else:
                unit = max((u for u in units if u != "shares" and "/" not in u), key=lambda u: len(units[u]),
                           default=None)
            if unit is None:
                continue
            rows = _first_filed(units[unit], unit)
            if rows and (best is None or rows[-1].end > best.facts[-1].end):
                best = Concept(canonical=canonical, tag=f"{taxonomy}:{concept}", unit=unit, facts=rows)
        if best is not None:
            out.concepts[canonical] = best
    return out


# --------------------------------------------------------------------------
# Point-in-time series
# --------------------------------------------------------------------------
def _is_quarter(f: Fact) -> bool:
    lo, hi = config.QUARTER_GAP_DAYS
    return f.days is not None and lo <= f.days <= hi


def _is_year(f: Fact) -> bool:
    lo, hi = (config.TTM_QUARTERS * d for d in config.QUARTER_GAP_DAYS)
    return f.days is not None and lo <= f.days <= hi


def discrete_quarters(c: Concept) -> list[Fact]:
    """Three-month facts plus each Q4 derived as fiscal year − the three quarters inside it
    (known from the 10-K's filing date). Oldest first."""
    quarters = {f.end: f for f in c.facts if _is_quarter(f)}
    for y in (f for f in c.facts if _is_year(f)):
        inside = [q for q in quarters.values() if y.start < q.end < y.end]
        if y.end not in quarters and len(inside) == config.TTM_QUARTERS - 1:
            q_start = max(q.end for q in inside) + timedelta(days=1)
            quarters[y.end] = Fact(start=q_start, end=y.end, value=y.value - sum(q.value for q in inside),
                                   filed=max([y.filed, *(q.filed for q in inside)]), form=y.form, unit=y.unit)
    return sorted(quarters.values(), key=lambda f: f.end)


def ttm_points(c: Concept) -> list[tuple[date, date, float]]:
    """(period end, known from, TTM value) wherever four consecutive quarters exist."""
    qs = discrete_quarters(c)
    lo, hi = config.QUARTER_GAP_DAYS
    out = []
    for i in range(config.TTM_QUARTERS - 1, len(qs)):
        window = qs[i - config.TTM_QUARTERS + 1: i + 1]
        if all(lo <= (b.end - a.end).days <= hi for a, b in zip(window, window[1:])):
            out.append((window[-1].end, max(q.filed for q in window), sum(q.value for q in window)))
    return out


def known_on(points: list[tuple[date, date, float]], days: pd.DatetimeIndex) -> pd.Series:
    """The latest value already filed on each day (NaN before the first filing)."""
    if not points:
        return pd.Series(index=days, dtype=float)
    s = pd.Series({pd.Timestamp(known): v for _, known, v in sorted(points, key=lambda p: (p[1], p[0]))})
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s.reindex(days, method="ffill")


def instant_points(c: Concept) -> list[tuple[date, date, float]]:
    return [(f.end, f.filed, f.value) for f in c.facts if f.start is None]


# --------------------------------------------------------------------------
# Debt maturity schedule
# --------------------------------------------------------------------------
class DebtMaturities(BaseModel):
    status: str = "ok"  # ok | N/A reason
    as_of: date | None = None  # the fiscal year end the schedule is at
    filed: date | None = None
    currency: str = ""
    buckets: dict[str, float] = Field(default_factory=dict)  # label → principal due
    source: str = "SEC EDGAR XBRL (10-K debt maturity schedule)"

    @property
    def total(self) -> float:
        return sum(self.buckets.values())

    @property
    def within_two_years(self) -> float:
        return sum(v for k, v in self.buckets.items() if k in ("next 12 months", "year 2"))

    def line(self) -> str:
        if self.status != "ok":
            return self.status
        from analysis.fmt import human

        parts = ", ".join(f"{k} {human(v)}" for k, v in self.buckets.items())
        share = f"; {self.within_two_years / self.total:.0%} due within 2 years" if self.total > 0 else ""
        return f"{parts} ({self.currency}, 10-K schedule at {self.as_of}{share})"


def debt_maturities(facts: CompanyFacts, today: date) -> DebtMaturities:
    """The latest filed 10-K schedule of principal due, by bucket (all buckets from the same year end)."""
    first = facts.concept("debt_due_12m")
    if first.status != "ok":
        return DebtMaturities(status=f"{NOT_REPORTED} (no debt maturity schedule)")
    known = [f for f in first.facts if f.start is None and f.filed <= today]
    if not known:
        return DebtMaturities(status=f"{NOT_REPORTED} (no schedule filed yet)")
    latest = max(known, key=lambda f: (f.end, f.filed))
    age = (today - latest.end).days
    if age > config.DEBT_SCHEDULE_MAX_AGE_DAYS:
        return DebtMaturities(status=f"N/A - the latest XBRL maturity schedule is at {latest.end} ({age} days old; "
                                     f"DEBT_SCHEDULE_MAX_AGE_DAYS {config.DEBT_SCHEDULE_MAX_AGE_DAYS})",
                              as_of=latest.end, filed=latest.filed, currency=first.unit)
    res = DebtMaturities(as_of=latest.end, filed=latest.filed, currency=first.unit)
    for canonical, label in MATURITY_BUCKETS.items():
        c = facts.concept(canonical)
        v = next((f for f in c.facts if f.start is None and f.end == latest.end and f.filed <= today), None)
        if v is not None:
            res.buckets[label] = v.value
    return res
