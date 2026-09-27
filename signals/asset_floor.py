"""Asset floor (SPEC "Asset floor", information only).

How much of the market cap is backed by net assets if the earnings case
fails. Never changes a score, the quality score or the screen status.

- TBV = equity − goodwill − other intangibles − preferred equity where reported.
- P/TBV = market cap / TBV; asset coverage = TBV / market cap, with its band.
  TBV ≤ 0 → both n/m "negative tangible book", coverage band "none".
- NCAV = current assets − total liabilities − preferred where reported.
- NNWC = cash & short-term investments + 0.75 × receivables + 0.5 × inventory
  − total liabilities (weights in config).
- NCAV and NNWC may be negative (shown as negative, never n/m), except for
  financials and REITs, whose balance sheets aren't split current/non-current.
- Net-net flag when NCAV ≥ market cap, plus the burn-duration line when the
  company is burning cash: (NCAV − market cap) / quarterly raw FCF burn.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

import config
from data.fundamentals import Fundamentals
from data.ratios import first_unusable, safe_ratio, sum_datums
from data.values import Datum, nm
from signals.valuation import liquid_cash, optional_part

NEG_TBV = "negative tangible book"
NM_FIN = "not meaningful for financials and REITs (balance sheet not split current/non-current)"
NET_NET_LABEL = "trades below net current assets"


class AssetFloor(BaseModel):
    tbv: Datum = Field(default_factory=Datum.missing)
    p_tbv: Datum = Field(default_factory=Datum.missing)
    ncav: Datum = Field(default_factory=Datum.missing)
    nnwc: Datum = Field(default_factory=Datum.missing)
    coverage: Datum = Field(default_factory=Datum.missing)
    coverage_band: str = "N/A"
    net_net: bool = False
    burn_line: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.coverage.ok:
            return f"Asset floor: {self.coverage.value:.0%} of the price covered by tangible book ({self.coverage_band})"
        return f"Asset floor: coverage {self.coverage_band} ({self.coverage.status})"


def coverage_band(ratio: float) -> str:
    for lower, band in config.ASSET_COVERAGE_BANDS:
        if ratio >= lower:
            return band
    return config.ASSET_COVERAGE_BANDS[-1][1]


def _intangibles(f: Fundamentals) -> tuple[Datum | None, list[str]]:
    """Goodwill + other intangibles, or None when only the provider's TBV row can be used."""
    combined = f.bal("goodwill_and_intangibles")
    if combined.ok:
        return combined, []
    gw, oi = f.bal("goodwill"), f.bal("other_intangible_assets")
    if gw.ok or oi.ok:
        gw, n1 = optional_part(gw, "goodwill")
        oi, n2 = optional_part(oi, "other intangible assets")
        return sum_datums({"goodwill": gw, "other intangibles": oi}, "goodwill + intangibles"), \
            [n for n in (n1, n2) if n]
    return None, []


def tangible_book(f: Fundamentals) -> tuple[Datum, list[str]]:
    pref, pref_note = optional_part(f.bal("preferred_stock"), "preferred equity")
    notes = [pref_note] if pref_note else []
    intang, n = _intangibles(f)
    notes += n
    if intang is not None:
        return sum_datums({"equity": f.bal("stockholders_equity"), "intangibles": intang, "preferred": pref},
                          "tangible book value", signs={"intangibles": -1, "preferred": -1}), notes
    provider_tbv = f.bal("tangible_book_value")
    if provider_tbv.ok:
        notes.append("provider's tangible book value row used (goodwill and intangibles not itemised)")
        return sum_datums({"provider TBV": provider_tbv, "preferred": pref}, "tangible book value",
                          signs={"preferred": -1}), notes
    equity = f.bal("stockholders_equity")
    notes.append("no goodwill or intangible assets reported; none subtracted")
    return sum_datums({"equity": equity, "preferred": pref}, "tangible book value",
                      signs={"preferred": -1}), notes


def asset_floor(f: Fundamentals, mcap: Datum, fcf_ttm: Datum, sector_adjusted: bool = False) -> AssetFloor:
    tbv, notes = tangible_book(f)
    out = AssetFloor(tbv=tbv, notes=notes)

    if tbv.ok and tbv.value <= 0:
        out.p_tbv = Datum.missing(nm(NEG_TBV))
        out.coverage = Datum.missing(nm(NEG_TBV))
        out.coverage_band = "none"
    else:
        out.p_tbv = safe_ratio(mcap, tbv, name="P/TBV", nonpositive_reason=NEG_TBV,
                               num_name="market cap", den_name="TBV")
        out.coverage = safe_ratio(tbv, mcap, name="asset coverage", nonpositive_reason="market cap ≤ 0",
                                  num_name="TBV", den_name="market cap")
        out.coverage_band = coverage_band(out.coverage.value) if out.coverage.ok else "N/A"

    if sector_adjusted:
        out.ncav = Datum.missing(nm(NM_FIN))
        out.nnwc = Datum.missing(nm(NM_FIN))
        return out

    pref, _ = optional_part(f.bal("preferred_stock"), "preferred equity")
    total_liab = f.bal("total_liabilities")
    out.ncav = sum_datums({"current assets": f.bal("current_assets"), "total liabilities": total_liab,
                           "preferred": pref}, "NCAV", signs={"total liabilities": -1, "preferred": -1})
    rec, n1 = optional_part(f.bal("receivables"), "receivables")
    inv, n2 = optional_part(f.bal("inventory"), "inventory")
    out.notes += [f"NNWC: {n}" for n in (n1, n2) if n]
    rw, iw = config.NNWC_RECEIVABLES_WEIGHT, config.NNWC_INVENTORY_WEIGHT
    weighted_rec = rec.model_copy(update={"value": rec.value * rw})
    weighted_inv = inv.model_copy(update={"value": inv.value * iw})
    out.nnwc = sum_datums({"cash and short-term investments": liquid_cash(f), f"{rw:g} × receivables": weighted_rec,
                           f"{iw:g} × inventory": weighted_inv, "total liabilities": total_liab},
                          "NNWC", signs={"total liabilities": -1})

    if first_unusable(out.ncav, mcap) is None and out.ncav.value >= mcap.value:
        out.net_net = True
        if fcf_ttm.ok and fcf_ttm.value < 0:
            quarterly_burn = -fcf_ttm.value / config.QUARTERS_PER_YEAR
            quarters = max(1, math.ceil((out.ncav.value - mcap.value) / quarterly_burn))
            out.burn_line = f"discount gone in ~{quarters} quarters at current burn"
    return out
