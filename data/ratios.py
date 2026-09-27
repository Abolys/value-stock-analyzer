"""The one place ratios are computed (CLAUDE.md Rule 2b).

Every ratio checks its inputs before dividing:
- a missing input → that input's own N/A status ("N/A - Data Incomplete" or
  "N/A - field not found: <name>"), never zero and never an exception;
- a zero or negative denominator → "n/m - <reason>", because the number would
  point the wrong way;
- otherwise the value, dated by the latest input period, with a "mixed periods"
  note when the inputs are more than MIXED_PERIOD_DAYS apart (Rule 3b).
"""

from __future__ import annotations

import logging
from datetime import date

from data.periods import mark_mixed_periods
from data.values import Datum, NA_INCOMPLETE, nm

log = logging.getLogger(__name__)


def first_unusable(*inputs: Datum) -> Datum | None:
    """The first input that is N/A or n/m, or None when all are usable."""
    for d in inputs:
        if d is None:
            return Datum.missing(NA_INCOMPLETE)
        if not d.ok:
            return d
    return None


def _latest_end(inputs: list[Datum]) -> date | None:
    ends = [d.period_end for d in inputs if d.period_end is not None]
    return max(ends) if ends else None


def combine(value: float, inputs: dict[str, Datum], label: str = "") -> Datum:
    """A computed Datum carrying its inputs' latest period, providers, notes and mixed-period marking."""
    ds = list(inputs.values())
    notes: list[str] = []
    for d in ds:
        for n in d.notes:
            if n not in notes:
                notes.append(n)
    mixed, note = mark_mixed_periods(inputs)
    if mixed:
        notes.append(note)
    providers = sorted({d.provider for d in ds if d.provider})
    currencies = {d.currency for d in ds if d.currency}
    return Datum(value=float(value), period_end=_latest_end(ds), period_label=label,
                 provider=", ".join(providers), currency=currencies.pop() if len(currencies) == 1 else None,
                 notes=notes)


def safe_ratio(num: Datum, den: Datum, *, name: str, nonpositive_reason: str,
               num_name: str = "numerator", den_name: str = "denominator") -> Datum:
    """num / den with the Rule 2b checks. `nonpositive_reason` becomes "n/m - <reason>"."""
    bad = first_unusable(num, den)
    if bad is not None:
        return Datum.missing(bad.status, notes=list(bad.notes))
    if den.value <= 0:
        return Datum.missing(nm(nonpositive_reason), period_end=_latest_end([num, den]),
                             notes=[f"{den_name} = {den.value:,.4g}"])
    return combine(num.value / den.value, {num_name: num, den_name: den}, label=name)


def sum_datums(parts: dict[str, Datum], label: str, signs: dict[str, int] | None = None) -> Datum:
    """Signed sum of inputs; any missing part makes the sum N/A (never treated as zero)."""
    bad = first_unusable(*parts.values())
    if bad is not None:
        return Datum.missing(bad.status, notes=list(bad.notes))
    signs = signs or {}
    total = sum(signs.get(k, 1) * d.value for k, d in parts.items())
    return combine(total, parts, label=label)


def scale(d: Datum, factor: float, label: str = "") -> Datum:
    if not d.ok:
        return d
    return d.model_copy(update={"value": d.value * factor, "period_label": label or d.period_label})


def market_cap(shares: Datum, price: Datum, ticker: str = "") -> Datum:
    """Market cap = latest shares outstanding × actual latest (batch) price.

    Missing or non-positive → N/A and logged as a data error (Rule 2b last row):
    every price-based ratio is then N/A.
    """
    bad = first_unusable(shares, price)
    if bad is not None:
        log.warning("market cap unavailable for %s: %s", ticker, bad.status)
        return Datum.missing(f"{NA_INCOMPLETE} (market cap: {bad.status})")
    value = shares.value * price.value
    if value <= 0:
        log.warning("market cap ≤ 0 for %s (shares %s × price %s): data error", ticker, shares.value, price.value)
        return Datum.missing(f"{NA_INCOMPLETE} (market cap ≤ 0: data error)")
    # period_end stays None: the price is live, so a ratio against market cap is
    # dated (and checked for mixed periods) by its fundamental inputs only.
    shares_when = shares.period_end.isoformat() if shares.period_end else shares.period_label or "info"
    price_when = price.period_end.isoformat() if price.period_end else "latest"
    return Datum(value=value, period_label=f"shares ({shares_when}) × actual latest price ({price_when})",
                 provider=", ".join(sorted({d.provider for d in (shares, price) if d.provider})),
                 currency=price.currency or shares.currency, notes=[*shares.notes, *price.notes])
