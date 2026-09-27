"""Price-based valuation signals used by the screener.

- Graham Number = sqrt(GRAHAM_MULTIPLIER × EPS × book value per share)
  (Graham, The Intelligent Investor: 15× earnings × 1.5× book = 22.5).
  EPS ≤ 0 or BVPS ≤ 0 → n/m (Rule 2b).
- EV/EBIT earnings yield (SPEC "Valuation extras"): EV = market cap + total
  debt + preferred + minority interest (where reported) − cash − short-term
  investments. EBIT ≤ 0 → n/m; EV ≤ 0 → n/m "net cash exceeds market cap"
  with a highlighted flag.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

import config
from data.fundamentals import Fundamentals
from data.ratios import combine, first_unusable, safe_ratio, sum_datums
from data.values import Datum, nm

NM_FINANCIALS = "not meaningful for financials and REITs"
NET_CASH_REASON = "net cash exceeds market cap"


def graham_number(eps: Datum, bvps: Datum) -> Datum:
    bad = first_unusable(eps, bvps)
    if bad is not None:
        return Datum.missing(bad.status)
    if eps.value <= 0:
        return Datum.missing(nm("negative EPS" if eps.value < 0 else "zero EPS"))
    if bvps.value <= 0:
        return Datum.missing(nm("negative book value" if bvps.value < 0 else "zero book value"))
    return combine(math.sqrt(config.GRAHAM_MULTIPLIER * eps.value * bvps.value), {"EPS": eps, "BVPS": bvps},
                   label=f"sqrt({config.GRAHAM_MULTIPLIER:g} × EPS × BVPS)")


def liquid_cash(f: Fundamentals) -> Datum:
    """Cash + cash equivalents + short-term investments (latest quarter).

    The provider's combined row is used when present; otherwise the two parts
    are added; when short-term investments are not reported, cash alone is used
    and the note says so (conservative: it can only understate cash).
    """
    combined = f.bal("cash_and_short_term_investments")
    if combined.ok:
        return combined
    cash, sti = f.bal("cash_and_equivalents"), f.bal("short_term_investments")
    if cash.ok and sti.ok:
        return sum_datums({"cash": cash, "short-term investments": sti}, "cash + short-term investments")
    if cash.ok:
        return cash.model_copy(update={"notes": [*cash.notes, "short-term investments not reported; cash only"]})
    return cash


def optional_part(d: Datum, what: str) -> tuple[Datum, str | None]:
    """An 'only where reported' input: missing → excluded (0 in the sum) with a visible note."""
    if d.ok:
        return d, None
    return Datum(value=0.0), f"{what} not reported; not included"


class EarningsYield(BaseModel):
    value: Datum = Field(default_factory=Datum.missing)
    ev: Datum = Field(default_factory=Datum.missing)
    ebit: Datum = Field(default_factory=Datum.missing)
    net_cash_flag: bool = False
    notes: list[str] = Field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.value.value:.1%}" if self.value.ok else self.value.status


def ttm_ebit(f: Fundamentals) -> Datum:
    ebit = f.ttm("ebit")
    if ebit.ok:
        return ebit
    op = f.ttm("operating_income")
    if op.ok:
        return op.model_copy(update={"notes": [*op.notes, "EBIT not reported; operating income used"]})
    return ebit


def enterprise_value(f: Fundamentals, mcap: Datum) -> tuple[Datum, list[str]]:
    notes: list[str] = []
    pref, n1 = optional_part(f.bal("preferred_stock"), "preferred equity")
    mino, n2 = optional_part(f.bal("minority_interest"), "minority interest")
    notes += [n for n in (n1, n2) if n]
    ev = sum_datums({"market cap": mcap, "total debt": f.bal("total_debt"), "preferred": pref,
                     "minority interest": mino, "cash and short-term investments": liquid_cash(f)},
                    "enterprise value", signs={"cash and short-term investments": -1})
    return ev, notes


def ev_ebit_yield(f: Fundamentals, mcap: Datum, sector_adjusted: bool = False) -> EarningsYield:
    if sector_adjusted:
        return EarningsYield(value=Datum.missing(nm(NM_FINANCIALS)))
    ebit = ttm_ebit(f)
    ev, notes = enterprise_value(f, mcap)
    res = EarningsYield(ev=ev, ebit=ebit, notes=notes)
    # The net-cash flag is a signal in its own right, raised whatever EBIT is.
    res.net_cash_flag = ev.ok and ev.value <= 0
    bad = first_unusable(ebit, ev)
    if bad is not None:
        res.value = Datum.missing(bad.status)
        return res
    if ebit.value <= 0:
        res.value = Datum.missing(nm("EBIT ≤ 0"))
        return res
    if ev.value <= 0:
        res.value = Datum.missing(nm(NET_CASH_REASON))
        return res
    res.value = safe_ratio(ebit, ev, name="EBIT / EV", nonpositive_reason=NET_CASH_REASON)
    return res
