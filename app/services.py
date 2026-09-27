"""Construction of the provider stack, the health check and the EDGAR client used
by the Streamlit app. Tests replace these functions to run the app offline.

Set VSA_DATA_SOURCE=fixtures to run the app from tests/fixtures (offline).
"""

from __future__ import annotations

import os

import config

from data.cache import CachedProvider, DiskCache
from data.edgar import EdgarClient
from data.fallback import FallbackProvider
from data.health import HealthReport, cached_health_check
from data.provider import ProviderError
from data.risk_free import fetch_boc_valet
from data.yfinance_provider import YFinanceProvider


def offline() -> bool:
    return os.getenv("VSA_DATA_SOURCE", "").lower() == "fixtures"


def build_provider() -> CachedProvider:
    if offline():
        from data.fixture_provider import fixture_provider

        # A separate cache file: fixture data must never be served to the live app later.
        return CachedProvider(fixture_provider(), DiskCache(config.CACHE_DB_PATH.with_name("fixtures_cache.db")))
    return CachedProvider(FallbackProvider(YFinanceProvider()), DiskCache())


def valet_fetch():
    """The BoC Valet fetcher; offline (fixtures) mode has no BoC data, so CAD risk-free is N/A with that reason."""
    if offline():
        def unavailable(series: str):
            raise ProviderError(f"offline fixture mode has no BoC Valet data for {series}")

        return unavailable
    return fetch_boc_valet


def health(provider: CachedProvider) -> HealthReport:
    # The check hits the live source behind the cache, never the cache itself.
    return cached_health_check(provider.inner, provider.cache)


def build_edgar(provider: CachedProvider) -> EdgarClient:
    if offline():
        from data.fixture_provider import fixture_edgar

        return fixture_edgar()
    return EdgarClient(cache=provider.cache)
