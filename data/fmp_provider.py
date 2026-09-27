"""Financial Modeling Prep provider — STUB.

Enabled only when FMP_API_KEY is set (config.fmp_enabled()). It exists so the
fallback path and provider stamping are wired end to end; each call raises
ProviderUnavailable until a real implementation is added. When it is built,
FMP's own field names get their own alias table here and are translated to the
canonical names in data/field_map.py, so nothing downstream changes.
"""

from __future__ import annotations

import pandas as pd

import config
from data.provider import (
    AnalystEstimates, BatchPriceResult, DataProvider, EarningsDates, Freq, InfoResult,
    ProviderUnavailable, SharesHistory, Statement, StatementKind,
)

PROVIDER = "fmp"


class FMPProvider(DataProvider):
    name = PROVIDER

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key if api_key is not None else config.FMP_API_KEY
        if not self.api_key:
            raise ProviderUnavailable("FMP not configured (FMP_API_KEY is empty)")

    def _todo(self, what: str):
        raise ProviderUnavailable(f"FMP {what} not implemented yet")

    def get_info(self, ticker: str) -> InfoResult:
        self._todo("info")

    def get_statement(self, ticker: str, kind: StatementKind, freq: Freq) -> Statement:
        self._todo(f"{freq} {kind} statement")

    def get_price_history(self, ticker: str, adjusted: bool) -> pd.Series:
        self._todo("price history")

    def get_splits(self, ticker: str) -> pd.Series:
        self._todo("splits")

    def get_dividends(self, ticker: str) -> pd.Series:
        self._todo("dividends")

    def get_shares_history(self, ticker: str) -> SharesHistory:
        self._todo("shares history")

    def get_earnings_dates(self, ticker: str) -> EarningsDates:
        self._todo("earnings dates")

    def get_analyst_estimates(self, ticker: str) -> AnalystEstimates:
        self._todo("analyst estimates")

    def batch_latest_prices(self, tickers: list[str]) -> BatchPriceResult:
        self._todo("batch prices")
