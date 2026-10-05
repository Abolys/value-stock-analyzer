"""Finnhub client (free tier), used only by the info fallback (data/info_fallback.py) when Yahoo
refuses `info` requests.

One call: `stock/profile2` (name, country, reporting currency, exchange, Finnhub industry, shares and
market cap in millions). The key comes from FINNHUB_API_KEY; without one every call raises
ProviderUnavailable and the fallback goes on without Finnhub. Requests respect
FINNHUB_MAX_REQUESTS_PER_SECOND; profiles are cached under the fundamentals rule.
"""

from __future__ import annotations

from typing import Any

import requests

import config
from data.cache import DiskCache
from data.provider import ProviderError, ProviderUnavailable
from data.throttle import FINNHUB_LIMITER, RateLimiter

PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"


class FinnhubClient:
    def __init__(self, api_key: str | None = None, session: Any = None,
                 limiter: RateLimiter = FINNHUB_LIMITER, cache: DiskCache | None = None):
        self.api_key = api_key if api_key is not None else config.FINNHUB_API_KEY
        self.session = session or requests.Session()
        self.limiter = limiter
        self.cache = cache

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())

    def _fetch_profile(self, ticker: str) -> dict[str, Any]:
        self.limiter.wait()
        try:
            r = self.session.get(PROFILE_URL, params={"symbol": ticker, "token": self.api_key}, timeout=30)
            r.raise_for_status()
            data = r.json() or {}
        except Exception as exc:
            # Never echo the URL: the token travels in the query string.
            raise ProviderError(f"Finnhub profile request failed for {ticker}: {type(exc).__name__}") from None
        if not data.get("ticker") and not data.get("name"):
            raise ProviderError(f"Finnhub has no profile for {ticker} (free tier covers mainly US listings)")
        return data

    def profile(self, ticker: str) -> dict[str, Any]:
        if not self.configured:
            raise ProviderUnavailable("FINNHUB_API_KEY is not set")
        if self.cache is None:
            return self._fetch_profile(ticker)
        return self.cache.fetch(f"finnhub|profile2|{ticker.upper()}", "fundamentals",
                                lambda: self._fetch_profile(ticker), ticker=ticker, method="finnhub_profile")
