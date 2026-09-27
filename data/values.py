"""The value model every fundamental number travels in (Rules 2, 2b, 3b).

A Datum carries its value, a status (ok / N/A / field not found / n/m), the end
date and kind of period it came from, the provider that supplied it, its
currency and any notes (FX conversion, mixed periods). Missing data is never
turned into zero and never raises.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

OK = "ok"
NA_INCOMPLETE = "N/A - Data Incomplete"
NA_FIELD_NOT_FOUND = "N/A - field not found: {name}"
NM_PREFIX = "n/m - "


def na_field_not_found(name: str) -> str:
    return NA_FIELD_NOT_FOUND.format(name=name)


def nm(reason: str) -> str:
    return f"{NM_PREFIX}{reason}"


class Datum(BaseModel):
    value: float | None = None
    status: str = OK
    period_end: date | None = None
    period_label: str = ""
    provider: str = ""
    currency: str | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == OK and self.value is not None

    @property
    def is_na(self) -> bool:
        return self.status.startswith("N/A")

    @property
    def is_nm(self) -> bool:
        return self.status.startswith(NM_PREFIX)

    def display(self) -> str:
        """Human-readable value or its N/A / n/m reason (never a silent blank)."""
        if not self.ok:
            return self.status
        return f"{self.value:,.4g}"

    @classmethod
    def missing(cls, status: str = NA_INCOMPLETE, **kw: Any) -> "Datum":
        return cls(value=None, status=status, **kw)


def is_missing(v: Any) -> bool:
    """True for None and NaN-like values (pandas / numpy / float)."""
    if v is None:
        return True
    try:
        return v != v  # NaN is the only value not equal to itself
    except Exception:
        return False
