"""The provider-neutral data interface.

Screening and analysis code talks only to `DataProvider` and the result models
below, using canonical field names from data/field_map.py. A new price or statement source
is added by implementing this interface; nothing downstream changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

StatementKind = Literal["income", "balance", "cashflow"]
Freq = Literal["annual", "quarterly"]


FALLBACK_PROVIDER = "fallback"  # InfoResult.provider of an info rebuilt from other sources


class ProviderError(Exception):
    """A fetch failed (network, throttling, empty response)."""


class ProviderUnavailable(ProviderError):
    """The provider is not configured or does not implement this call."""


class InfoResult(BaseModel):
    """`info` fields keyed by canonical name, each with a status string."""

    ticker: str
    values: dict[str, Any] = Field(default_factory=dict)
    statuses: dict[str, str] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)  # kept for the officer snapshot and debugging
    provider: str = ""
    # Where each value came from when it is not Yahoo `info` (the fallback, data/info_fallback.py):
    # canonical → source label, shown next to the value.
    sources: dict[str, str] = Field(default_factory=dict)

    def __setstate__(self, state: Any) -> None:
        # Entries pickled into the disk cache before `sources` existed load with it empty.
        if isinstance(state, dict) and isinstance(state.get("__dict__"), dict):
            state["__dict__"].setdefault("sources", {})
        super().__setstate__(state)

    @property
    def is_fallback(self) -> bool:
        return self.provider == FALLBACK_PROVIDER

    def get(self, canonical: str) -> Any:
        return self.values.get(canonical)

    def status(self, canonical: str) -> str:
        from data.values import na_field_not_found

        return self.statuses.get(canonical, na_field_not_found(canonical))

    def date(self, canonical: str) -> date | None:
        """A date field (yfinance gives epoch seconds) as a UTC date, or None."""
        v = self.get(canonical)
        if v is None:
            return None
        try:
            return datetime.fromtimestamp(float(v), tz=timezone.utc).date()
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    def next_earnings(self, today: date) -> date | None:
        """The info's own next earnings date, when it lies after `today` (else None)."""
        d = self.date("earnings_timestamp")
        return d if d is not None and d > today else None


class Statement(BaseModel):
    """One financial statement: {canonical field: {period_end: value}}."""

    ticker: str
    kind: StatementKind
    freq: Freq
    values: dict[str, dict[date, float]] = Field(default_factory=dict)
    not_found: list[str] = Field(default_factory=list)
    currency: str | None = None  # reporting (financial) currency
    provider: str = ""
    notes: list[str] = Field(default_factory=list)

    @property
    def periods(self) -> list[date]:
        """All period ends present, newest first."""
        ps: set[date] = set()
        for v in self.values.values():
            ps.update(v)
        return sorted(ps, reverse=True)

    @property
    def empty(self) -> bool:
        return not any(self.values.values())

    def series(self, canonical: str) -> dict[date, float]:
        return self.values.get(canonical, {})


class EarningsDates(BaseModel):
    ticker: str
    past: list[date] = Field(default_factory=list)
    next: date | None = None
    provider: str = ""


class SharesHistory(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: str
    series: Any = None  # pd.Series of share counts indexed by date
    source: str = ""  # "get_shares_full" or "annual diluted shares"
    provider: str = ""


class PricePoint(BaseModel):
    price: float
    as_of: date


class BatchPriceResult(BaseModel):
    prices: dict[str, PricePoint] = Field(default_factory=dict)
    failed: dict[str, str] = Field(default_factory=dict)
    provider: str = ""


class AnalystEstimates(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: str
    tables: dict[str, Any] = Field(default_factory=dict)  # canonical → DataFrame or None
    statuses: dict[str, str] = Field(default_factory=dict)
    provider: str = ""


class DataProvider(ABC):
    """Every method raises ProviderError on failure; missing fields are never errors."""

    name: str = "abstract"

    @abstractmethod
    def get_info(self, ticker: str) -> InfoResult: ...

    @abstractmethod
    def get_statement(self, ticker: str, kind: StatementKind, freq: Freq) -> Statement: ...

    @abstractmethod
    def get_price_history(self, ticker: str, adjusted: bool) -> pd.Series:
        """Daily closes indexed by date. adjusted=True → dividend- and split-adjusted
        closes; adjusted=False → actual traded closes (Rule 5)."""

    def get_price_frame(self, ticker: str) -> pd.DataFrame:
        """Daily prices in one frame (PRICE_FRAME_COLUMNS: "close", "adj_close" and, when the provider
        has them, raw "high" / "low"), so one download serves both kinds of close and the ranges.
        The default builds it from `get_price_history`; providers with a single download override it."""
        frame = pd.DataFrame({"close": self.get_price_history(ticker, False),
                              "adj_close": self.get_price_history(ticker, True)})
        frame.attrs["provider"] = self.name
        return frame

    def get_price_range(self, ticker: str) -> pd.DataFrame:
        """Daily highs and lows, dividend- and split-adjusted like `adjusted=True` closes
        (columns "high", "low"). Optional: providers without it raise ProviderUnavailable
        and callers fall back to close-based indicators."""
        raise ProviderUnavailable(f"{self.name} has no daily highs and lows")

    @abstractmethod
    def get_splits(self, ticker: str) -> pd.Series:
        """Split ratios (new shares per old share; 0.1 = 1-for-10 reverse) indexed by date."""

    @abstractmethod
    def get_dividends(self, ticker: str) -> pd.Series: ...

    @abstractmethod
    def get_shares_history(self, ticker: str) -> SharesHistory: ...

    @abstractmethod
    def get_earnings_dates(self, ticker: str) -> EarningsDates: ...

    @abstractmethod
    def get_analyst_estimates(self, ticker: str) -> AnalystEstimates: ...

    @abstractmethod
    def batch_latest_prices(self, tickers: list[str]) -> BatchPriceResult: ...


PRICE_FRAME_COLUMNS = ("close", "adj_close", "high", "low")


def closes_from_frame(frame: pd.DataFrame, ticker: str, adjusted: bool) -> pd.Series:
    """Adjusted or actual daily closes from a price frame (Rule 5: never mixed)."""
    col = "adj_close" if adjusted else "close"
    if frame is None or frame.empty or col not in frame.columns:
        raise ProviderError(f"no price history for {ticker}")
    s = frame[col].dropna()
    if s.empty:
        raise ProviderError(f"no price history for {ticker}")
    s = s.copy()
    s.name = "adjusted_close" if adjusted else "close"
    s.attrs["provider"] = frame.attrs.get("provider", "")
    return s


def range_from_frame(frame: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Highs and lows scaled onto the adjusted closes (× adj_close / close for each day)."""
    if frame is None or frame.empty or any(c not in frame.columns for c in PRICE_FRAME_COLUMNS):
        raise ProviderUnavailable(f"no daily highs and lows for {ticker}")
    df = frame[list(PRICE_FRAME_COLUMNS)].dropna()
    df = df[df["close"] > 0]
    if df.empty:
        raise ProviderUnavailable(f"no daily highs and lows for {ticker}")
    factor = df["adj_close"] / df["close"]
    out = pd.DataFrame({"high": df["high"] * factor, "low": df["low"] * factor})
    out.attrs["provider"] = frame.attrs.get("provider", "")
    return out


def to_date_index(s: pd.Series) -> pd.Series:
    """Normalise a pandas time series to a tz-naive, sorted DatetimeIndex at midnight."""
    if s is None or len(s) == 0:
        return pd.Series(dtype=float)
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out = pd.Series(s.to_numpy(), index=idx.normalize(), name=s.name)
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()
