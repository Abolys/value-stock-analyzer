"""Data-source health check (SPEC "Data source resilience").

Runs at app start and before every screen run. Fetches CANARY_TICKERS live
(prices, info, quarterly statements) and checks that every health-required
field in the field map resolves and that values are sane (price > 0, market
cap > 0, statements not empty). On failure the app shows a banner and cached
data stays usable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from pydantic import BaseModel, Field

import config
from data import field_map as fm
from data.cache import DiskCache
from data.provider import DataProvider, ProviderError

BANNER = "Data source not responding correctly — try updating yfinance (python scripts/upgrade_yfinance.py)"
INFO_BLOCKED_BANNER = ("Yahoo is refusing company-info requests from this server (common on cloud hosts); "
                       "company info comes from saved copies or fallback sources, each labelled")
INFO_FAILED = "info failed"
CACHE_KEY = "health|canaries"


class HealthReport(BaseModel):
    ok: bool
    failures: list[str] = Field(default_factory=list)
    checked_at: datetime
    tickers: list[str] = Field(default_factory=list)

    @property
    def info_blocked(self) -> bool:
        """Every canary's `info` request failed: Yahoo is refusing `info` (prices and statements may
        still work). Derived from the failures, so cached reports need no new field."""
        return bool(self.tickers) and all(
            any(f.startswith(f"{t}: {INFO_FAILED}") for f in self.failures) for t in self.tickers)

    @property
    def only_info_blocked(self) -> bool:
        return self.info_blocked and all(f": {INFO_FAILED}" in f for f in self.failures)


def _check_ticker(provider: DataProvider, ticker: str) -> list[str]:
    failures: list[str] = []
    try:
        prices = provider.get_price_history(ticker, adjusted=False).dropna()
        if prices.empty or float(prices.iloc[-1]) <= 0:
            failures.append(f"{ticker}: latest price missing or ≤ 0")
    except ProviderError as exc:
        failures.append(f"{ticker}: prices failed ({exc})")
    try:
        info = provider.get_info(ticker)
    except ProviderError as exc:
        return failures + [f"{ticker}: {INFO_FAILED} ({exc})"]
    if info.get("quote_type") not in (None, "EQUITY"):
        return failures  # ETF canary: prices and a basic info response are enough
    for fs in fm.fields_for("info"):
        if fs.health_required and info.status(fs.canonical) != "ok":
            failures.append(f"{ticker}: info {fs.canonical} → {info.status(fs.canonical)}")
    mcap = info.get("market_cap")
    if mcap is not None and mcap <= 0:
        failures.append(f"{ticker}: market cap ≤ 0")
    for kind in fm.STATEMENT_SOURCES:
        try:
            stmt = provider.get_statement(ticker, kind, "quarterly")
        except ProviderError as exc:
            failures.append(f"{ticker}: quarterly {kind} failed ({exc})")
            continue
        if stmt.empty:
            failures.append(f"{ticker}: quarterly {kind} statement is empty")
            continue
        for fs in fm.fields_for(kind):
            if fs.health_required and fs.canonical in stmt.not_found:
                failures.append(f"{ticker}: {kind} row {fs.canonical} → field not found under any alias")
    return failures


def run_health_check(provider: DataProvider, tickers: list[str] | None = None,
                     clock: Callable[[], datetime] = datetime.now) -> HealthReport:
    tickers = tickers or config.CANARY_TICKERS
    failures = [f for t in tickers for f in _check_ticker(provider, t)]
    return HealthReport(ok=not failures, failures=failures, checked_at=clock(), tickers=tickers)


def cached_health_check(provider: DataProvider, cache: DiskCache,
                        tickers: list[str] | None = None) -> HealthReport:
    """Reuse a recent result (HEALTH_CHECK_TTL_MINUTES) so the app starts quickly."""
    now = cache.clock()
    entry = cache.get(CACHE_KEY)
    if entry is not None and entry.expires_at > now:
        return entry.payload
    report = run_health_check(provider, tickers, clock=cache.clock)
    ttl = config.HEALTH_CHECK_TTL_MINUTES
    cache.put(CACHE_KEY, "health", report, now + timedelta(minutes=ttl), f"health TTL {ttl}m")
    return report
