"""Offline provider backed by tests/fixtures/<TICKER>/ (written by
scripts/capture_fixtures.py).

Fixtures store the raw yfinance objects (info JSON, statement frames with
their original row labels, price frames...). `FixtureTicker` mimics the
yfinance Ticker attributes, so the real YFinanceProvider code path — including
the field map — runs unchanged in tests. `FixtureSession` replays saved EDGAR
responses for EdgarClient.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

import config
from data import field_map as fm
from data.yfinance_provider import YFinanceProvider

STATEMENT_FILES = {attr: f"{kind}_{freq}.csv" for (kind, freq), attr in fm.YF_STATEMENT_ATTRS.items()}


class _NoThrottle:
    def wait(self) -> None:
        pass


def ticker_dir(ticker: str, root: Path = config.FIXTURES_DIR) -> Path:
    return Path(root) / ticker.replace("^", "_").replace("=", "_")


def _read_series(path: Path) -> pd.Series:
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(path, index_col=0)
    s = df.iloc[:, 0] if not df.empty else pd.Series(dtype=float)
    s.index = pd.DatetimeIndex(pd.to_datetime(s.index, utc=True)).tz_convert(None)
    return s.astype(float)


class FixtureTicker:
    """A stand-in for yfinance.Ticker reading saved files."""

    def __init__(self, ticker: str, root: Path = config.FIXTURES_DIR):
        self.ticker = ticker
        self.dir = ticker_dir(ticker, root)
        if not self.dir.exists():
            raise FileNotFoundError(f"no fixture for {ticker} in {root}")

    def __getattr__(self, name: str) -> Any:
        if name in STATEMENT_FILES:
            p = self.dir / STATEMENT_FILES[name]
            if not p.exists():
                return pd.DataFrame()
            df = pd.read_csv(p, index_col=0)
            df.columns = pd.to_datetime(df.columns)
            return df
        if name in {f.canonical for f in fm.fields_for("estimates")}:
            p = self.dir / "estimates" / f"{name}.csv"
            return pd.read_csv(p, index_col=0) if p.exists() else pd.DataFrame()
        raise AttributeError(name)

    @property
    def info(self) -> dict:
        p = self.dir / "info.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def history(self, period: str | None = None, auto_adjust: bool = False, **_: Any) -> pd.DataFrame:
        p = self.dir / "prices.csv"
        if not p.exists():
            return pd.DataFrame()
        df = pd.read_csv(p, index_col=0)
        df.index = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True)).tz_convert(None)
        return df

    @property
    def splits(self) -> pd.Series:
        return _read_series(self.dir / "splits.csv")

    @property
    def dividends(self) -> pd.Series:
        return _read_series(self.dir / "dividends.csv")

    def get_shares_full(self, start: str | None = None, **_: Any) -> pd.Series:
        return _read_series(self.dir / "shares_full.csv")

    @property
    def earnings_dates(self) -> pd.DataFrame:
        p = self.dir / "earnings_dates.json"
        dates = json.loads(p.read_text()).get("dates", []) if p.exists() else []
        return pd.DataFrame(index=pd.DatetimeIndex(pd.to_datetime(dates)))

    @property
    def calendar(self) -> dict:
        p = self.dir / "earnings_dates.json"
        if not p.exists():
            return {}
        cal = json.loads(p.read_text()).get("calendar", [])
        return {fm.YF_CALENDAR_EARNINGS_DATE[0]: [date.fromisoformat(d) for d in cal]}


def captured_on(ticker: str, root: Path = config.FIXTURES_DIR) -> date:
    p = ticker_dir(ticker, root) / "meta.json"
    if p.exists():
        return date.fromisoformat(json.loads(p.read_text())["captured_on"])
    return date.today()


def fixture_download(root: Path = config.FIXTURES_DIR):
    """A yf.download stand-in: MultiIndex (ticker, field) frame from saved prices."""

    def download(tickers: list[str], **_: Any) -> pd.DataFrame:
        frames = {}
        for t in tickers:
            try:
                df = FixtureTicker(t, root).history()
            except FileNotFoundError:
                continue
            if not df.empty:
                frames[t] = df[[c for c in ("Close", "Adj Close") if c in df.columns]].tail(5)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)

    return download


def fixture_provider(root: Path = config.FIXTURES_DIR, today: date | None = None) -> YFinanceProvider:
    def factory(t: str) -> FixtureTicker:
        return FixtureTicker(t, root)

    return YFinanceProvider(ticker_factory=factory, download_fn=fixture_download(root), throttle=_NoThrottle(),
                            sleep=lambda _s: None, today=(lambda: today) if today else date.today)


# --------------------------------------------------------------------------
# EDGAR replay
# --------------------------------------------------------------------------
class _Resp:
    def __init__(self, content: bytes, url: str):
        self.content, self.url = content, url
        self.text = content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        pass


class FixtureSession:
    """Replays EDGAR responses saved as tests/fixtures/<T>/edgar/<file> with an index.json url map."""

    def __init__(self, root: Path = config.FIXTURES_DIR):
        self.map: dict[str, Path] = {}
        for idx in Path(root).glob("*/edgar/index.json"):
            for url, fname in json.loads(idx.read_text()).items():
                self.map[url] = idx.parent / fname

    def get(self, url: str, **_: Any) -> _Resp:
        if url not in self.map:
            raise ConnectionError(f"offline fixture has no response for {url}")
        return _Resp(self.map[url].read_bytes(), url)


def fixture_edgar(root: Path = config.FIXTURES_DIR, overrides_path: Path = config.SEC_CIK_OVERRIDES_CSV):
    from data.edgar import EdgarClient

    return EdgarClient(user_agent="fixture replay test@example.com", session=FixtureSession(root),
                       limiter=_NoThrottle(), overrides_path=overrides_path)
