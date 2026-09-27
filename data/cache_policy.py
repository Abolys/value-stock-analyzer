"""Cache expiry rules (SPEC "Caching" and the Thresholds table).

- prices (and FX, risk-free, splits, dividends): CACHE_TTL_PRICES_DAYS
- fundamentals: until the next earnings date + EARNINGS_REFETCH_GRACE_DAYS,
  capped at CACHE_TTL_FUNDAMENTALS_MAX_DAYS; no known date → the cap
- info: the fundamentals rule, but at most OFFICER_REFRESH_DAYS old

Each rule returns (expires_at, reason); the reason is stored with the entry.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal

import config

CacheKind = Literal["prices", "fundamentals", "info"]

REASON_PRICE = "price TTL {days}d"
REASON_EARNINGS = "next earnings {d} + {g}d grace"
REASON_CAP_NO_DATE = "cap {days}d (no earnings date)"
REASON_CAP = "cap {days}d (next earnings beyond cap)"
REASON_CAP_PASSED = "cap {days}d (earnings date already passed)"
REASON_OFFICER = "officer refresh {days}d"

# A refetch of an entry whose reason starts with this prefix means the company reported.
EARNINGS_REASON_PREFIX = "next earnings"


def price_expiry(fetched_at: datetime) -> tuple[datetime, str]:
    days = config.CACHE_TTL_PRICES_DAYS
    return fetched_at + timedelta(days=days), REASON_PRICE.format(days=days)


def fundamentals_expiry(fetched_at: datetime, next_earnings: date | None) -> tuple[datetime, str]:
    cap_days = config.CACHE_TTL_FUNDAMENTALS_MAX_DAYS
    cap = fetched_at + timedelta(days=cap_days)
    if next_earnings is None:
        return cap, REASON_CAP_NO_DATE.format(days=cap_days)
    grace = config.EARNINGS_REFETCH_GRACE_DAYS
    earnings_expiry = datetime.combine(next_earnings + timedelta(days=grace), time.min)
    if earnings_expiry <= fetched_at:
        return cap, REASON_CAP_PASSED.format(days=cap_days)
    if earnings_expiry > cap:
        return cap, REASON_CAP.format(days=cap_days)
    return earnings_expiry, REASON_EARNINGS.format(d=next_earnings.isoformat(), g=grace)


def info_expiry(fetched_at: datetime, next_earnings: date | None) -> tuple[datetime, str]:
    f_exp, f_reason = fundamentals_expiry(fetched_at, next_earnings)
    officer_days = config.OFFICER_REFRESH_DAYS
    o_exp = fetched_at + timedelta(days=officer_days)
    if o_exp < f_exp:
        return o_exp, REASON_OFFICER.format(days=officer_days)
    return f_exp, f_reason


def expiry(kind: CacheKind, fetched_at: datetime, next_earnings: date | None = None) -> tuple[datetime, str]:
    if kind == "prices":
        return price_expiry(fetched_at)
    if kind == "fundamentals":
        return fundamentals_expiry(fetched_at, next_earnings)
    return info_expiry(fetched_at, next_earnings)
