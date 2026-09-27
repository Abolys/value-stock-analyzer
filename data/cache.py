"""SQLite disk cache wrapping every provider call (SPEC "Caching").

Each entry records when it was fetched, when it expires and why. When a live
fetch fails and an expired entry exists, the expired entry is served and marked
stale with its age — cached data stays usable during an outage.
`st.cache_data` may sit on top for in-session speed only.
"""

from __future__ import annotations

import pickle
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import config
from data import cache_policy
from data.provider import BatchPriceResult, DataProvider, PricePoint, ProviderError

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ticker TEXT,
    method TEXT,
    payload BLOB NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    expiry_reason TEXT NOT NULL,
    provider TEXT
)
"""


@dataclass
class CacheEntry:
    key: str
    kind: str
    payload: Any
    fetched_at: datetime
    expires_at: datetime
    expiry_reason: str
    provider: str | None = None

    def age(self, now: datetime) -> str:
        delta = now - self.fetched_at
        hours = delta.total_seconds() / 3600
        return f"{hours:.0f}h" if hours < 48 else f"{delta.days}d"


@dataclass
class CacheEvent:
    key: str
    outcome: str  # "hit" | "miss" | "refetch" | "stale"
    reason: str = ""  # expiry reason of the entry that was replaced / served
    fetched_at: datetime | None = None
    age: str = ""
    error: str = ""
    ticker: str | None = None
    method: str | None = None


class DiskCache:
    def __init__(self, path: Path | str = config.CACHE_DB_PATH,
                 clock: Callable[[], datetime] = datetime.now):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(SCHEMA)
        self._conn.commit()
        self.events: list[CacheEvent] = []

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, kind, payload, fetched_at, expires_at, expiry_reason, provider FROM cache WHERE key=?",
                (key,)).fetchone()
        if row is None:
            return None
        return CacheEntry(row[0], row[1], pickle.loads(row[2]), datetime.fromisoformat(row[3]),
                          datetime.fromisoformat(row[4]), row[5], row[6])

    def put(self, key: str, kind: str, payload: Any, expires_at: datetime, reason: str,
            ticker: str | None = None, method: str | None = None, provider: str | None = None,
            fetched_at: datetime | None = None) -> CacheEntry:
        fetched_at = fetched_at or self.clock()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?,?,?,?,?)",
                (key, kind, ticker, method, pickle.dumps(payload), fetched_at.isoformat(),
                 expires_at.isoformat(), reason, provider))
            self._conn.commit()
        return CacheEntry(key, kind, payload, fetched_at, expires_at, reason, provider)

    def fetch(self, key: str, kind: cache_policy.CacheKind, fetch_fn: Callable[[], Any], *,
              next_earnings: Callable[[], date | None] | None = None, ticker: str | None = None,
              method: str | None = None) -> Any:
        """Return a fresh cached value, or fetch, store and return a new one.

        On fetch failure with an existing (expired) entry, return the stale
        payload and record a "stale" event; with no entry, re-raise.
        """
        now = self.clock()
        entry = self.get(key)
        if entry is not None and entry.expires_at > now:
            self.events.append(CacheEvent(key, "hit", entry.expiry_reason, entry.fetched_at, entry.age(now),
                                          ticker=ticker, method=method))
            return entry.payload
        try:
            payload = fetch_fn()
        except ProviderError as exc:
            if entry is not None:
                self.events.append(CacheEvent(key, "stale", entry.expiry_reason, entry.fetched_at,
                                              entry.age(now), error=str(exc), ticker=ticker, method=method))
                return entry.payload
            raise
        ne = next_earnings() if (next_earnings and kind != "prices") else None
        expires_at, reason = cache_policy.expiry(kind, now, ne)
        provider = getattr(payload, "provider", None)
        if provider is None and isinstance(payload, pd.Series):
            provider = payload.attrs.get("provider")
        self.put(key, kind, payload, expires_at, reason, ticker, method, provider, fetched_at=now)
        if entry is None:
            self.events.append(CacheEvent(key, "miss", ticker=ticker, method=method))
        else:
            self.events.append(CacheEvent(key, "refetch", entry.expiry_reason, entry.fetched_at, entry.age(now),
                                          ticker=ticker, method=method))
        return payload

    def last_event(self, key: str) -> CacheEvent | None:
        for ev in reversed(self.events):
            if ev.key == key:
                return ev
        return None

    def refetched_because_reported(self) -> int:
        """Distinct tickers whose fundamentals were refetched because an earnings date passed."""
        return len({e.ticker for e in self.events
                    if e.outcome == "refetch" and e.method != "get_earnings_dates"
                    and e.reason.startswith(cache_policy.EARNINGS_REASON_PREFIX)})


def _key(provider: str, method: str, ticker: str, *args: Any) -> str:
    return "|".join([provider, method, ticker, *[str(a) for a in args]])


class CachedProvider(DataProvider):
    """Wraps any DataProvider with the disk cache and the expiry rules."""

    def __init__(self, inner: DataProvider, cache: DiskCache | None = None):
        self.inner = inner
        self.cache = cache or DiskCache()
        self.name = inner.name

    def _next_earnings(self, ticker: str) -> Callable[[], date | None]:
        def get() -> date | None:
            try:
                return self.get_earnings_dates(ticker).next
            except ProviderError:
                return None

        return get

    def _cached(self, kind: cache_policy.CacheKind, method: str, ticker: str, *args: Any) -> Any:
        key = _key(self.inner.name, method, ticker, *args)
        return self.cache.fetch(key, kind, lambda: getattr(self.inner, method)(ticker, *args),
                                next_earnings=self._next_earnings(ticker), ticker=ticker, method=method)

    def key_for(self, method: str, ticker: str, *args: Any) -> str:
        return _key(self.inner.name, method, ticker, *args)

    def get_info(self, ticker):
        return self._cached("info", "get_info", ticker)

    def get_statement(self, ticker, kind, freq):
        return self._cached("fundamentals", "get_statement", ticker, kind, freq)

    def get_price_history(self, ticker, adjusted):
        return self._cached("prices", "get_price_history", ticker, adjusted)

    def get_price_range(self, ticker):
        return self._cached("prices", "get_price_range", ticker)

    def get_splits(self, ticker):
        return self._cached("prices", "get_splits", ticker)

    def get_dividends(self, ticker):
        return self._cached("prices", "get_dividends", ticker)

    def get_shares_history(self, ticker):
        return self._cached("fundamentals", "get_shares_history", ticker)

    def get_earnings_dates(self, ticker):
        # Earnings dates follow the fundamentals rule using their own "next" date,
        # so they are refreshed once that date (+ grace) has passed.
        key = _key(self.inner.name, "get_earnings_dates", ticker)
        holder: dict[str, Any] = {}

        def fetch():
            holder["v"] = self.inner.get_earnings_dates(ticker)
            return holder["v"]

        return self.cache.fetch(key, "fundamentals", fetch,
                                next_earnings=lambda: holder["v"].next if "v" in holder else None,
                                ticker=ticker, method="get_earnings_dates")

    def get_analyst_estimates(self, ticker):
        return self._cached("fundamentals", "get_analyst_estimates", ticker)

    def batch_latest_prices(self, tickers: list[str]) -> BatchPriceResult:
        """Serve fresh per-ticker prices from cache; batch-download only the rest."""
        now = self.cache.clock()
        result = BatchPriceResult(provider=self.inner.name)
        missing: list[str] = []
        for tk in tickers:
            entry = self.cache.get(_key(self.inner.name, "latest_price", tk))
            if entry is not None and entry.expires_at > now:
                result.prices[tk] = entry.payload
            else:
                missing.append(tk)
        if missing:
            fresh = self.inner.batch_latest_prices(missing)
            for tk, pp in fresh.prices.items():
                exp, reason = cache_policy.price_expiry(now)
                self.cache.put(_key(self.inner.name, "latest_price", tk), "prices", pp, exp, reason,
                               ticker=tk, method="latest_price", provider=fresh.provider, fetched_at=now)
                result.prices[tk] = pp
            for tk, why in fresh.failed.items():
                entry = self.cache.get(_key(self.inner.name, "latest_price", tk))
                if entry is not None:  # outage: serve the stale price with its age
                    result.prices[tk] = entry.payload
                    self.cache.events.append(CacheEvent(tk, "stale", entry.expiry_reason, entry.fetched_at,
                                                        entry.age(now), error=why))
                else:
                    result.failed[tk] = why
        return result


def cached_value(cache: DiskCache, key: str, fetch_fn: Callable[[], Any],
                 kind: cache_policy.CacheKind = "prices") -> Any:
    """Cache a non-provider value (FX rates, risk-free yields) under the given rule."""
    return cache.fetch(key, kind, fetch_fn)


__all__ = ["DiskCache", "CachedProvider", "CacheEntry", "CacheEvent", "cached_value", "PricePoint"]
