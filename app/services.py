"""Construction of the provider stack, the health check and the EDGAR client used
by the Streamlit app. Tests replace these functions to run the app offline.

Set VSA_DATA_SOURCE=fixtures to run the app from tests/fixtures (offline).
"""

from __future__ import annotations

import os

import config

from data.cache import CachedProvider, DiskCache
from data.edgar import EdgarClient
from data.finnhub import FinnhubClient
from data.health import HealthReport, cached_health_check
from data.info_fallback import InfoFallback
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
    provider = CachedProvider(YFinanceProvider(), DiskCache())
    # When Yahoo refuses `info`: rebuild it from Yahoo's chart data, the statements, SEC EDGAR and
    # (with FINNHUB_API_KEY) Finnhub. The fallback reads statements through the cached provider.
    provider.info_fallback = InfoFallback(provider, edgar=EdgarClient(cache=provider.cache),
                                          finnhub=FinnhubClient(cache=provider.cache) if config.FINNHUB_API_KEY else None)
    return provider


def valet_fetch():
    """The BoC Valet fetcher; offline (fixtures) mode has no BoC data, so CAD risk-free is N/A with that reason."""
    if offline():
        def unavailable(series: str):
            raise ProviderError(f"offline fixture mode has no BoC Valet data for {series}")

        return unavailable
    return fetch_boc_valet


def health(provider: CachedProvider) -> HealthReport:
    # The check hits the live source behind the cache, never the cache itself. It runs before the
    # first page renders, so the live probe never retries: a throttled Yahoo (common on shared cloud
    # IPs) would otherwise hold the page through minutes of back-off; it shows the banner instead.
    source = provider.inner
    if isinstance(source, YFinanceProvider):
        source = YFinanceProvider(retries=config.HEALTH_CHECK_RETRIES)
    report = cached_health_check(source, provider.cache)
    if report.info_blocked and provider.info_fallback is not None and not provider.info_block.active:
        # Later info calls skip Yahoo (and its back-off) and use the saved copy or the fallback.
        provider.info_block.trip(f"health check at {report.checked_at:%H:%M}: Yahoo refused info for "
                                 + ", ".join(report.tickers))
    return report


def build_edgar(provider: CachedProvider) -> EdgarClient:
    if offline():
        from data.fixture_provider import fixture_edgar

        return fixture_edgar()
    return EdgarClient(cache=provider.cache)
