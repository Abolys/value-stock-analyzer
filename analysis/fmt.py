"""Display helpers shared by the lenses: a value or its N/A / n/m reason, never a blank."""

from __future__ import annotations

from data.values import Datum


def pct(v: float | None, signed: bool = True, digits: int = 1) -> str:
    if v is None:
        return "N/A"
    return f"{v:+.{digits}%}" if signed else f"{v:.{digits}%}"


def d_pct(d: Datum, signed: bool = False, digits: int = 1) -> str:
    return pct(d.value, signed, digits) if d.ok else d.status


def d_x(d: Datum) -> str:
    return f"{d.value:.2f}x" if d.ok else d.status


def d_num(d: Datum, digits: int = 4) -> str:
    return human(d.value) if d.ok else d.status


def money(v: float | None, currency: str | None = None) -> str:
    if v is None:
        return "N/A"
    return f"{currency + ' ' if currency else ''}{v:,.2f}"


def is_mixed(d: Datum) -> bool:
    return any(n.startswith("mixed periods") for n in d.notes)


SUFFIXES = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))


def human(v: float | None) -> str:
    """Large amounts as 4.60B / 366.6M (sign kept); small values as plain numbers."""
    if v is None:
        return "N/A"
    for size, suffix in SUFFIXES:
        if abs(v) >= size:
            return f"{v / size:,.2f}{suffix}"
    return f"{v:,.4g}"


def d_human(d: Datum) -> str:
    return human(d.value) if d.ok else d.status
