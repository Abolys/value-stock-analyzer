"""yfinance implementation of DataProvider.

Every call goes through the yfinance throttle and retry/back-off. Raw labels
are translated to canonical names through data/field_map.py only.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

import pandas as pd

import config
from data import field_map as fm
from data.provider import (
    AnalystEstimates, BatchPriceResult, DataProvider, EarningsDates, Freq, InfoResult,
    PricePoint, ProviderError, ProviderUnavailable, SharesHistory, Statement, StatementKind, closes_from_frame,
    range_from_frame, to_date_index,
)
from data.throttle import YF_THROTTLE, with_retries
from data.values import is_missing

PROVIDER = "yfinance"


class _EmptyInfo(Exception):
    """Yahoo answered but returned no usable info payload: the classic throttling symptom.
    Retried with back-off like a hard failure (spec: "retry with back-off when yfinance
    refuses or throttles requests")."""


def _yf():
    import yfinance as yf  # imported lazily so offline tests never need network setup

    return yf


class YFinanceProvider(DataProvider):
    name = PROVIDER

    def __init__(self, ticker_factory: Callable[[str], Any] | None = None,
                 download_fn: Callable[..., pd.DataFrame] | None = None,
                 throttle=YF_THROTTLE, sleep=None, today: Callable[[], date] = date.today,
                 retries: int | None = None):
        self._ticker_factory = ticker_factory or (lambda t: _yf().Ticker(t))
        self._download = download_fn or (lambda *a, **k: _yf().download(*a, **k))
        self._throttle = throttle
        self._retry_kw: dict[str, Any] = {"sleep": sleep} if sleep else {}
        if retries is not None:
            self._retry_kw["retries"] = retries
        self._today = today

    # -- helpers ---------------------------------------------------------
    def _call(self, fn: Callable[[], Any]) -> Any:
        def attempt():
            self._throttle.wait()
            return fn()

        try:
            return with_retries(attempt, **self._retry_kw)
        except Exception as exc:
            raise ProviderError(f"yfinance call failed: {type(exc).__name__}: {exc}") from exc

    def _t(self, ticker: str):
        return self._ticker_factory(ticker)

    # -- DataProvider ----------------------------------------------------
    def get_info(self, ticker: str) -> InfoResult:
        def fetch():
            raw = self._call(lambda: self._t(ticker).info) or {}
            if not raw or not any(k in raw for k in ("quoteType", "currency", "symbol")):
                raise _EmptyInfo(f"yfinance returned no info for {ticker}")
            return raw

        try:
            # Only _EmptyInfo is retried here: hard failures were already retried and
            # wrapped inside _call, and waiting again on top of that would stack the
            # back-off delays.
            raw = with_retries(fetch, retry_on=(_EmptyInfo,), **self._retry_kw)
        except _EmptyInfo as exc:
            raise ProviderError(str(exc)) from exc
        values, statuses = {}, {}
        for fs in fm.fields_for("info"):
            v, st = fm.resolve_info(raw, fs.canonical)
            values[fs.canonical] = v
            statuses[fs.canonical] = st
        return InfoResult(ticker=ticker, values=values, statuses=statuses, raw=raw, provider=PROVIDER)

    def get_quote_profile(self, ticker: str) -> dict[str, Any]:
        """Currency, exchange, quote type, name, shares and market cap from Yahoo's chart data (yfinance
        `fast_info` and the history metadata), the request behind prices. Used by the info fallback when
        Yahoo refuses `info`; keys are canonical info names, absent when Yahoo gave nothing."""
        t = self._t(ticker)

        def fetch() -> dict[str, Any]:
            fi = t.fast_info
            out: dict[str, Any] = {}
            for canonical, key in fm.YF_FAST_INFO_KEYS.items():
                try:
                    v = fi[key]
                except Exception:  # each key is its own lookup; one failing (e.g. shares) keeps the rest
                    continue
                if not is_missing(v) and v != "":
                    out[canonical] = v
            try:
                meta = t.history_metadata or {}
            except Exception:
                meta = {}
            for canonical, keys in fm.YF_HISTORY_METADATA_KEYS.items():
                v = next((meta[k] for k in keys if meta.get(k)), None)
                if v is not None and canonical not in out:
                    out[canonical] = v
            return out

        out = self._call(fetch)
        if not out.get("currency"):
            raise ProviderError(f"Yahoo chart data has no currency for {ticker}")
        return out

    def get_statement(self, ticker: str, kind: StatementKind, freq: Freq) -> Statement:
        attr = fm.YF_STATEMENT_ATTRS[(kind, freq)]
        df = self._call(lambda: getattr(self._t(ticker), attr))
        values, not_found = fm.canonicalise_statement(df, kind)
        return Statement(ticker=ticker, kind=kind, freq=freq, values=values,
                         not_found=not_found, provider=PROVIDER)

    def get_price_frame(self, ticker: str) -> pd.DataFrame:
        """One daily-history download: actual and adjusted closes plus raw highs and lows."""
        df = self._call(lambda: self._t(ticker).history(period=config.PRICE_HISTORY_PERIOD, auto_adjust=False))
        if df is None or df.empty:
            raise ProviderError(f"no price history for {ticker}")
        if fm.YF_PRICE_CLOSE not in df.columns:
            raise ProviderError(f"price column {fm.YF_PRICE_CLOSE!r} missing for {ticker}")
        cols = {"close": fm.YF_PRICE_CLOSE, "adj_close": fm.YF_PRICE_ADJ_CLOSE, "high": fm.YF_PRICE_HIGH,
                "low": fm.YF_PRICE_LOW}
        frame = pd.DataFrame({k: to_date_index(df[v]) for k, v in cols.items() if v in df.columns})
        frame.attrs["provider"] = PROVIDER
        return frame

    def get_price_history(self, ticker: str, adjusted: bool) -> pd.Series:
        frame = self.get_price_frame(ticker)
        if adjusted and "adj_close" not in frame.columns:
            raise ProviderError(f"price column {fm.YF_PRICE_ADJ_CLOSE!r} missing for {ticker}")
        return closes_from_frame(frame, ticker, adjusted)

    def get_price_range(self, ticker: str) -> pd.DataFrame:
        """Highs and lows scaled onto the adjusted closes (× Adj Close / Close for each day)."""
        return range_from_frame(self.get_price_frame(ticker), ticker)

    def get_splits(self, ticker: str) -> pd.Series:
        s = self._call(lambda: self._t(ticker).splits)
        s = to_date_index(s if s is not None else pd.Series(dtype=float))
        return s[s > 0].astype(float) if len(s) else s

    def get_dividends(self, ticker: str) -> pd.Series:
        s = self._call(lambda: self._t(ticker).dividends)
        return to_date_index(s if s is not None else pd.Series(dtype=float)).astype(float)

    def get_shares_history(self, ticker: str) -> SharesHistory:
        start = (self._today() - timedelta(days=365 * 10)).isoformat()
        try:
            s = self._call(lambda: self._t(ticker).get_shares_full(start=start))
        except ProviderError:
            s = None
        if s is not None and len(s):
            return SharesHistory(ticker=ticker, series=to_date_index(s.astype(float)),
                                 source="get_shares_full", provider=PROVIDER)
        stmt = self.get_statement(ticker, "income", "annual")
        diluted = stmt.series("diluted_shares")
        series = pd.Series({pd.Timestamp(k): v for k, v in diluted.items()}, dtype=float).sort_index()
        return SharesHistory(ticker=ticker, series=series, source="annual diluted shares", provider=PROVIDER)

    def get_earnings_dates(self, ticker: str) -> EarningsDates:
        t = self._t(ticker)
        dates: set[date] = set()
        try:
            ed = self._call(lambda: t.earnings_dates)
            if ed is not None and len(ed):
                dates.update(pd.Timestamp(i).date() for i in ed.index)
        except ProviderError:
            pass
        try:
            cal = self._call(lambda: t.calendar) or {}
            for key in fm.YF_CALENDAR_EARNINGS_DATE:
                for d in cal.get(key, []) or []:
                    dates.add(d if isinstance(d, date) else pd.Timestamp(d).date())
        except ProviderError:
            pass
        today = self._today()
        past = sorted(d for d in dates if d <= today)
        future = sorted(d for d in dates if d > today)
        return EarningsDates(ticker=ticker, past=past, next=future[0] if future else None, provider=PROVIDER)

    def get_analyst_estimates(self, ticker: str) -> AnalystEstimates:
        t = self._t(ticker)
        tables, statuses = {}, {}
        for fs in fm.fields_for("estimates"):
            self._throttle.wait()
            v, st = fm.resolve_estimate_property(t, fs.canonical)
            tables[fs.canonical] = v
            statuses[fs.canonical] = st
        return AnalystEstimates(ticker=ticker, tables=tables, statuses=statuses, provider=PROVIDER)

    def batch_latest_prices(self, tickers: list[str]) -> BatchPriceResult:
        result = BatchPriceResult(provider=PROVIDER)
        size = config.BATCH_PRICE_CHUNK_SIZE
        for i in range(0, len(tickers), size):
            chunk = tickers[i:i + size]
            try:
                df = self._call(lambda: self._download(
                    chunk, period="5d", auto_adjust=False, group_by="ticker",
                    progress=False, threads=False))
                self._absorb_batch(df, chunk, result)
            except ProviderError:
                # The whole chunk failed: fall back to one ticker at a time so a
                # single bad symbol never loses the rest.
                for tk in chunk:
                    try:
                        df = self._call(lambda: self._download(
                            [tk], period="5d", auto_adjust=False, group_by="ticker",
                            progress=False, threads=False))
                        self._absorb_batch(df, [tk], result)
                    except ProviderError as exc:
                        result.failed[tk] = str(exc)
        return result

    @staticmethod
    def _absorb_batch(df: pd.DataFrame | None, chunk: list[str], result: BatchPriceResult) -> None:
        for tk in chunk:
            close = None
            if df is not None and not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    if (tk, fm.YF_PRICE_CLOSE) in df.columns:
                        close = df[(tk, fm.YF_PRICE_CLOSE)]
                elif fm.YF_PRICE_CLOSE in df.columns and len(chunk) == 1:
                    close = df[fm.YF_PRICE_CLOSE]
            if close is None:
                result.failed[tk] = "no price returned"
                continue
            close = close.dropna()
            if close.empty or is_missing(close.iloc[-1]) or float(close.iloc[-1]) <= 0:
                result.failed[tk] = "no valid price returned"
                continue
            ts = pd.Timestamp(close.index[-1])
            result.prices[tk] = PricePoint(price=float(close.iloc[-1]), as_of=ts.date())

