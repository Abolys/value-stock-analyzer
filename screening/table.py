"""Flatten ScreenResults into table rows for the Screener page and exports.

Metric cells show the value or the full N/A / n/m reason (Rule 2b: n/m is
shown with its reason in table cells). Numeric companion columns exist only
for sorting.
"""

from __future__ import annotations

from typing import Any

from screening.models import SLOT_FCF, SLOT_LEVERAGE, SLOT_MOS, SLOT_SHARES, ScreenResult


def _cell(r: ScreenResult, slot: str) -> str:
    m = r.metric(slot)
    if m is None:
        return "—"
    label = "" if slot in (SLOT_MOS, SLOT_SHARES) else f"{m.name.split(' (')[0].split(' vs')[0]}: "
    outcome = f"{m.outcome}: {m.na_reason}" if m.na_reason else m.outcome
    return f"{label}{m.display} [{outcome}]"


def flags(r: ScreenResult) -> list[str]:
    out = []
    if r.trap_risk:
        out.append("trap risk")
    if r.net_cash_flag:
        out.append("net cash > market cap")
    if r.asset_floor and r.asset_floor.net_net:
        out.append("net-net" + (f" ({r.asset_floor.burn_line})" if r.asset_floor.burn_line else ""))
    if r.dilution_flag:
        out.append("dilution")
    if r.beneish and r.beneish.flag:
        out.append("Beneish")
    if r.divergences:
        out.append("stage divergence")
    return out


def result_row(r: ScreenResult) -> dict[str, Any]:
    af = r.asset_floor
    return {
        "ticker": r.ticker,
        "name": r.name,
        "source": r.sources,
        "status": r.display_status,
        "stage": r.decided_at_stage,
        "treatment": r.treatment,
        "margin of safety": _cell(r, SLOT_MOS),
        "FCF / runway / returns": _cell(r, SLOT_FCF),
        "leverage / book": _cell(r, SLOT_LEVERAGE),
        "share trend": _cell(r, SLOT_SHARES),
        "metrics available": r.metrics_available,
        "quality": r.quality.display if r.quality else "—",
        "quality (sort)": r.quality.score if r.quality else None,
        "Piotroski": r.piotroski.display if r.piotroski else "—",
        "Altman zone": (r.altman.zone or r.altman.status) if r.altman else "—",
        "Beneish": ("flag" if r.beneish.flag else r.beneish.display) if r.beneish else "—",
        "EV/EBIT yield": r.earnings_yield.display if r.earnings_yield else "—",
        "EV/EBIT (sort)": r.earnings_yield.value.value if r.earnings_yield and r.earnings_yield.value.ok else None,
        "P/TBV": af.p_tbv.display() if af else "—",
        "asset coverage": (f"{af.coverage.value:.0%} {af.coverage_band}" if af.coverage.ok else af.coverage_band)
        if af else "—",
        "flags": ", ".join(flags(r)),
        "fundamentals as of": r.fundamentals_as_of.isoformat() if r.fundamentals_as_of else "N/A",
        "stale": "fundamentals may be stale" if r.stale else "",
        "reason": "; ".join(r.status_reasons) if r.status != "Pass" else "",
    }
