"""The two kinds of price (CLAUDE.md Rule 5). Never mix them in one calculation.

- `adjusted_closes`: dividend- and split-adjusted closes, for drawdowns,
  recoveries, technical signals and indexed charts.
- `actual_latest_price`: the actual latest traded price, for the Graham Number
  comparison, yields, market cap and every valuation ratio.
"""

from __future__ import annotations

import pandas as pd

from data.provider import BatchPriceResult, DataProvider, ProviderError
from data.values import Datum, NA_INCOMPLETE

ACTUAL_PRICE_LABEL = "actual latest close"


def adjusted_closes(provider: DataProvider, ticker: str) -> pd.Series:
    """Dividend- and split-adjusted daily closes (drawdowns, signals, indexed charts)."""
    return provider.get_price_history(ticker, adjusted=True)


def actual_closes(provider: DataProvider, ticker: str) -> pd.Series:
    """Actual traded closes (split-adjusted by the source, never dividend-adjusted)."""
    return provider.get_price_history(ticker, adjusted=False)


def actual_latest_price(provider: DataProvider, ticker: str,
                        batch: BatchPriceResult | None = None) -> Datum:
    """The actual latest price for valuation. Uses the run's batch price when given."""
    if batch is not None and ticker in batch.prices:
        pp = batch.prices[ticker]
        return Datum(value=pp.price, period_end=pp.as_of, period_label=ACTUAL_PRICE_LABEL,
                     provider=batch.provider)
    try:
        s = actual_closes(provider, ticker).dropna()
    except ProviderError as exc:
        return Datum.missing(f"{NA_INCOMPLETE} (price unavailable: {exc})")
    if s.empty or s.iloc[-1] <= 0:
        return Datum.missing(NA_INCOMPLETE)
    return Datum(value=float(s.iloc[-1]), period_end=s.index[-1].date(), period_label=ACTUAL_PRICE_LABEL,
                 provider=s.attrs.get("provider", provider.name))


def batch_latest_prices(provider: DataProvider, tickers: list[str]) -> BatchPriceResult:
    """Latest actual price per ticker via the provider's chunked multi-ticker download."""
    return provider.batch_latest_prices(tickers)
