"""Per-ticker fallback: when the primary provider fails for a ticker and FMP is
configured, retry that ticker through FMP (SPEC "Data source resilience").

Every result keeps the name of the provider that actually supplied it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import config
from data.provider import DataProvider, ProviderError

log = logging.getLogger(__name__)


def default_secondary() -> DataProvider | None:
    if not config.fmp_enabled():
        return None
    from data.fmp_provider import FMPProvider

    return FMPProvider(api_key=config.FMP_API_KEY or None)


class FallbackProvider(DataProvider):
    def __init__(self, primary: DataProvider,
                 secondary_factory: Callable[[], DataProvider | None] = default_secondary):
        self.primary = primary
        self._secondary_factory = secondary_factory
        self.name = primary.name
        self.fallback_log: list[tuple[str, str, str]] = []  # (method, ticker, reason)

    def _run(self, method: str, ticker: str, *args: Any) -> Any:
        try:
            return getattr(self.primary, method)(ticker, *args)
        except ProviderError as primary_exc:
            if not config.fmp_enabled():
                raise
            secondary = self._secondary_factory()
            if secondary is None:
                raise
            self.fallback_log.append((method, ticker, str(primary_exc)))
            log.info("%s %s failed on %s (%s); retrying via %s",
                     method, ticker, self.primary.name, primary_exc, secondary.name)
            result = getattr(secondary, method)(ticker, *args)
            _stamp(result, secondary.name)
            return result

    def get_info(self, ticker):
        return self._run("get_info", ticker)

    def get_statement(self, ticker, kind, freq):
        return self._run("get_statement", ticker, kind, freq)

    def get_price_frame(self, ticker):
        return self._run("get_price_frame", ticker)

    def get_price_history(self, ticker, adjusted):
        return self._run("get_price_history", ticker, adjusted)

    def get_price_range(self, ticker):
        return self._run("get_price_range", ticker)

    def get_splits(self, ticker):
        return self._run("get_splits", ticker)

    def get_dividends(self, ticker):
        return self._run("get_dividends", ticker)

    def get_shares_history(self, ticker):
        return self._run("get_shares_history", ticker)

    def get_earnings_dates(self, ticker):
        return self._run("get_earnings_dates", ticker)

    def get_analyst_estimates(self, ticker):
        return self._run("get_analyst_estimates", ticker)

    def batch_latest_prices(self, tickers):
        # Batch prices stay on the primary; tickers it could not price are the
        # per-ticker fallback's job in the screener.
        return self.primary.batch_latest_prices(tickers)


def _stamp(result: Any, provider: str) -> None:
    if hasattr(result, "provider"):
        try:
            result.provider = provider
            return
        except Exception:
            pass
    if hasattr(result, "attrs"):
        result.attrs["provider"] = provider
