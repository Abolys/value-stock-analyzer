from datetime import date, datetime, timedelta

import pandas as pd
import pytest

import config
from data import cache_policy
from data.cache import CachedProvider
from data.provider import (
    AnalystEstimates, BatchPriceResult, DataProvider, EarningsDates, InfoResult, PricePoint, ProviderError,
    SharesHistory, Statement,
)


class CountingProvider(DataProvider):
    """Records calls; next earnings date and failures are controllable."""

    name = "fake"

    def __init__(self, next_earnings=None):
        self.calls: list[tuple] = []
        self.next_earnings = next_earnings
        self.fail = False

    def _hit(self, *key):
        self.calls.append(key)
        if self.fail:
            raise ProviderError("source down")

    def get_info(self, ticker):
        self._hit("info", ticker)
        return InfoResult(ticker=ticker, values={"currency": "USD"}, statuses={"currency": "ok"}, provider=self.name)

    def get_statement(self, ticker, kind, freq):
        self._hit("stmt", ticker, kind, freq)
        return Statement(ticker=ticker, kind=kind, freq=freq, values={"total_revenue": {date(2026, 6, 30): 1.0}},
                         provider=self.name)

    def get_price_history(self, ticker, adjusted):
        self._hit("prices", ticker, adjusted)
        return pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-08-30", "2026-08-31"]))

    def get_splits(self, ticker):
        return pd.Series(dtype=float)

    def get_dividends(self, ticker):
        return pd.Series(dtype=float)

    def get_shares_history(self, ticker):
        return SharesHistory(ticker=ticker)

    def get_earnings_dates(self, ticker):
        self._hit("earnings", ticker)
        return EarningsDates(ticker=ticker, next=self.next_earnings)

    def get_analyst_estimates(self, ticker):
        return AnalystEstimates(ticker=ticker)

    def batch_latest_prices(self, tickers):
        self._hit("batch", tuple(tickers))
        return BatchPriceResult(prices={t: PricePoint(price=10.0, as_of=date(2026, 8, 31)) for t in tickers})


def stmt_calls(p):
    return [c for c in p.calls if c[0] == "stmt"]


def test_policy_rules():
    now = datetime(2026, 9, 1, 12)
    exp, why = cache_policy.fundamentals_expiry(now, date(2026, 10, 20))
    assert exp == datetime(2026, 10, 23) and why.startswith("next earnings 2026-10-20 + 3d")
    exp, why = cache_policy.fundamentals_expiry(now, None)
    assert exp == now + timedelta(days=100) and "no earnings date" in why
    exp, why = cache_policy.fundamentals_expiry(now, date(2027, 6, 1))
    assert exp == now + timedelta(days=100) and "beyond cap" in why
    exp, why = cache_policy.info_expiry(now, date(2026, 11, 20))
    assert exp == now + timedelta(days=30) and why.startswith("officer refresh")
    exp, why = cache_policy.price_expiry(now)
    assert exp == now + timedelta(days=1)


def test_fundamentals_valid_before_earnings_and_expire_after_grace(cache, clock):
    p = CountingProvider(next_earnings=date(2026, 10, 20))
    cp = CachedProvider(p, cache)
    cp.get_statement("X", "income", "quarterly")
    clock.now = datetime(2026, 10, 22, 23, 59)  # after earnings, inside the grace window
    cp.get_statement("X", "income", "quarterly")
    assert len(stmt_calls(p)) == 1
    clock.now = datetime(2026, 10, 23, 0, 1)  # earnings date + 3 days passed
    cp.get_statement("X", "income", "quarterly")
    assert len(stmt_calls(p)) == 2
    assert cache.events[-1].outcome == "refetch"
    assert cache.refetched_because_reported() == 1


def test_cap_applies_without_earnings_date(cache, clock):
    p = CountingProvider(next_earnings=None)
    cp = CachedProvider(p, cache)
    cp.get_statement("X", "balance", "annual")
    clock.now += timedelta(days=config.CACHE_TTL_FUNDAMENTALS_MAX_DAYS - 1)
    cp.get_statement("X", "balance", "annual")
    assert len(stmt_calls(p)) == 1
    clock.now += timedelta(days=2)
    cp.get_statement("X", "balance", "annual")
    assert len(stmt_calls(p)) == 2
    entry = cache.get(cp.key_for("get_statement", "X", "balance", "annual"))
    assert "no earnings date" in entry.expiry_reason
    assert entry.fetched_at == clock.now


def test_info_refreshes_after_officer_days_while_fundamentals_still_valid(cache, clock):
    p = CountingProvider(next_earnings=date(2026, 11, 25))  # fundamentals valid until Nov 28
    cp = CachedProvider(p, cache)
    cp.get_info("X")
    cp.get_statement("X", "income", "annual")
    clock.now += timedelta(days=config.OFFICER_REFRESH_DAYS + 1)
    cp.get_info("X")
    cp.get_statement("X", "income", "annual")
    assert len([c for c in p.calls if c[0] == "info"]) == 2
    assert len(stmt_calls(p)) == 1


def test_cache_hit_miss_and_price_expiry(cache, clock):
    p = CountingProvider()
    cp = CachedProvider(p, cache)
    cp.get_price_history("X", True)
    assert cache.events[-1].outcome == "miss"
    cp.get_price_history("X", True)
    assert cache.events[-1].outcome == "hit"
    assert len([c for c in p.calls if c[0] == "prices"]) == 1
    clock.now += timedelta(days=config.CACHE_TTL_PRICES_DAYS, seconds=1)
    cp.get_price_history("X", True)
    assert len([c for c in p.calls if c[0] == "prices"]) == 2


def test_stale_entry_served_when_source_fails(cache, clock):
    p = CountingProvider()
    cp = CachedProvider(p, cache)
    first = cp.get_price_history("X", False)
    clock.now += timedelta(days=5)
    p.fail = True
    served = cp.get_price_history("X", False)
    assert served.equals(first)
    ev = cache.events[-1]
    assert ev.outcome == "stale" and ev.age == "5d" and "source down" in ev.error


def test_no_entry_and_failure_raises(cache):
    p = CountingProvider()
    p.fail = True
    with pytest.raises(ProviderError):
        CachedProvider(p, cache).get_info("NEW")


def test_batch_prices_use_cache_for_fresh_tickers(cache):
    p = CountingProvider()
    cp = CachedProvider(p, cache)
    cp.batch_latest_prices(["A", "B"])
    res = cp.batch_latest_prices(["A", "B", "C"])
    assert [c for c in p.calls if c[0] == "batch"] == [("batch", ("A", "B")), ("batch", ("C",))]
    assert set(res.prices) == {"A", "B", "C"}
