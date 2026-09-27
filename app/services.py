"""Construction of the provider stack, the health check and the EDGAR client used
by the Streamlit app. Tests replace these functions to run the app offline.

Set VSA_DATA_SOURCE=fixtures to run the app from tests/fixtures (offline).
"""

from __future__ import annotations

import os

from data.cache import CachedProvider, DiskCache
from data.edgar import EdgarClient
from data.fallback import FallbackProvider
from data.health import HealthReport, cached_health_check
from data.yfinance_provider import YFinanceProvider


def offline() -> bool:
    return os.getenv("VSA_DATA_SOURCE", "").lower() == "fixtures"


def build_provider() -> CachedProvider:
    if offline():
        from data.fixture_provider import fixture_provider

        return CachedProvider(fixture_provider())
    return CachedProvider(FallbackProvider(YFinanceProvider()), DiskCache())


def health(provider: CachedProvider) -> HealthReport:
    # The check hits the live source behind the cache, never the cache itself.
    return cached_health_check(provider.inner, provider.cache)


def build_edgar(provider: CachedProvider) -> EdgarClient:
    if offline():
        from data.fixture_provider import fixture_edgar

        return fixture_edgar()
    return EdgarClient(cache=provider.cache)
