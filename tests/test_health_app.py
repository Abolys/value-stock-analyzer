from datetime import datetime, timedelta

import pandas as pd
from streamlit.testing.v1 import AppTest

from data.cache import CachedProvider, DiskCache
from data.fixture_provider import FixtureTicker, fixture_provider
from data.health import BANNER, HealthReport, cached_health_check, run_health_check
from data.provider import ProviderError
from data.yfinance_provider import YFinanceProvider
from data.fixture_provider import _NoThrottle, fixture_download


def test_health_passes_on_canary_fixtures(fx_provider):
    r = run_health_check(fx_provider)
    assert r.ok, r.failures


class EmptyStatementTicker(FixtureTicker):
    @property
    def quarterly_balance_sheet(self):
        return pd.DataFrame()


class MissingFieldTicker(FixtureTicker):
    @property
    def info(self):
        raw = dict(super().info)
        raw.pop("marketCap", None)
        return raw


def _provider(ticker_cls):
    return YFinanceProvider(ticker_factory=lambda t: ticker_cls(t), download_fn=fixture_download(),
                            throttle=_NoThrottle(), sleep=lambda s: None)


def test_health_fails_on_empty_statement():
    r = run_health_check(_provider(EmptyStatementTicker), tickers=["MSFT"])
    assert not r.ok and "MSFT: quarterly balance statement is empty" in r.failures


def test_health_fails_on_missing_field():
    r = run_health_check(_provider(MissingFieldTicker), tickers=["MSFT"])
    assert not r.ok and any("market_cap → N/A - field not found" in f for f in r.failures)


def test_health_result_cached_for_ttl(tmp_path):
    calls = []

    class Counting(YFinanceProvider):
        def get_info(self, ticker):
            calls.append(ticker)
            return super().get_info(ticker)

    now = [datetime(2026, 9, 1, 9)]
    cache = DiskCache(tmp_path / "c.db", clock=lambda: now[0])
    p = Counting(ticker_factory=lambda t: FixtureTicker(t), throttle=_NoThrottle(), sleep=lambda s: None)
    cached_health_check(p, cache, ["MSFT"])
    cached_health_check(p, cache, ["MSFT"])
    assert calls == ["MSFT"]
    now[0] += timedelta(hours=2)
    cached_health_check(p, cache, ["MSFT"])
    assert calls == ["MSFT", "MSFT"]


class DownProvider(YFinanceProvider):
    """The source is completely down: every call fails."""

    def __init__(self):
        super().__init__(throttle=_NoThrottle(), sleep=lambda s: None)

    def _call(self, fn):
        raise ProviderError("Yahoo is not responding")


def test_app_shows_banner_and_serves_cached_data(tmp_path, monkeypatch):
    from app import services

    # Warm a cache from fixtures, then let every entry expire and take the source down.
    now = [datetime(2026, 9, 1, 9)]
    cache = DiskCache(tmp_path / "cache.db", clock=lambda: now[0])
    warm = CachedProvider(fixture_provider(), cache)
    for kind in ("income", "balance", "cashflow"):
        for freq in ("annual", "quarterly"):
            warm.get_statement("MSFT", kind, freq)
    warm.get_info("MSFT")
    warm.get_earnings_dates("MSFT")
    warm.get_price_history("MSFT", False)
    warm.get_price_history("MSFT", True)
    warm.get_splits("MSFT")
    warm.get_shares_history("MSFT")
    now[0] += timedelta(days=200)
    down = CachedProvider(DownProvider(), cache)
    down.cache.events.clear()

    failing = HealthReport(ok=False, failures=["SPY: prices failed (Yahoo is not responding)"],
                           checked_at=now[0])
    monkeypatch.setattr(services, "build_provider", lambda: down)
    monkeypatch.setattr(services, "health", lambda provider: failing)

    at = AppTest.from_file("../app/main.py", default_timeout=60)
    at.run()
    assert not at.exception
    assert any(BANNER in e.value for e in at.error)
    at.sidebar.text_input(key="ticker_box").set_value("MSFT").run()
    assert not at.exception
    text = " ".join(str(m.value) for m in at.markdown) + " ".join(str(w.value) for w in at.warning)
    assert "MSFT" in text
    assert "served from cache, 200d old" in text
    assert any(ev.outcome == "stale" for ev in down.cache.events)
