"""Context fields, never scored (SPEC "Context fields"): insider ownership, short
interest (yfinance info, often US-only) and the 90-day direction of analyst EPS
revisions. Missing → N/A."""

from __future__ import annotations

from pydantic import BaseModel, Field

import config
from data.provider import AnalystEstimates, InfoResult
from data.values import Datum, NA_INCOMPLETE


class ContextFields(BaseModel):
    insider_ownership: Datum = Field(default_factory=Datum.missing)
    short_interest: Datum = Field(default_factory=Datum.missing)
    revisions_90d: str = NA_INCOMPLETE  # "up" | "down" | "flat" | N/A reason
    revisions_detail: str = ""


def _info_datum(info: InfoResult, field: str) -> Datum:
    v = info.get(field)
    if v is None:
        return Datum.missing(info.status(field) if info.status(field) != "ok" else NA_INCOMPLETE)
    return Datum(value=float(v), provider=info.provider, period_label="yfinance info")


def revision_direction(estimates: AnalystEstimates | None) -> tuple[str, str]:
    table = estimates.tables.get("eps_trend") if estimates else None
    if table is None or len(table) == 0:
        status = estimates.statuses.get("eps_trend", NA_INCOMPLETE) if estimates else NA_INCOMPLETE
        return (status if status.startswith("N/A") else NA_INCOMPLETE), "EPS trend not available"
    for period in config.ESTIMATE_REVISION_PERIODS:
        if period not in table.index or "current" not in table.columns or "90daysAgo" not in table.columns:
            continue
        now, then = table.loc[period, "current"], table.loc[period, "90daysAgo"]
        try:
            now, then = float(now), float(then)
        except (TypeError, ValueError):
            continue
        if then != then or now != now or then == 0:
            continue
        change = now / abs(then) - 1 if then > 0 else (now - then) / abs(then)
        band = config.ESTIMATE_REVISION_FLAT_BAND
        direction = "up" if change > band else "down" if change < -band else "flat"
        return direction, f"{period} EPS consensus {then:.4g} → {now:.4g} over 90 days ({change:+.1%})"
    return NA_INCOMPLETE, "no usable EPS trend row"


def context_fields(info: InfoResult, estimates: AnalystEstimates | None) -> ContextFields:
    direction, detail = revision_direction(estimates)
    return ContextFields(insider_ownership=_info_datum(info, "held_percent_insiders"),
                         short_interest=_info_datum(info, "short_percent_of_float"),
                         revisions_90d=direction, revisions_detail=detail)
