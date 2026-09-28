from datetime import date

import pandas as pd
import pytest

import config
from data import currency, prices
from data.fixture_provider import _NoThrottle, fixture_provider
from data.provider import InfoResult, ProviderError, ProviderUnavailable, Statement
from data.yfinance_provider import YFinanceProvider


# ---------------------------------------------------------------- batch prices
def _frame(tickers, bad=()):
    cols = {}
    idx = pd.to_datetime(["2026-09-24", "2026-09-25"])
    for t in tickers:
        vals = [float("nan"), float("nan")] if t in bad else [10.0, 11.0]
        cols[(t, "Close")] = vals
        cols[(t, "Adj Close")] = vals
    return pd.DataFrame(cols, index=idx)


def test_batch_prices_chunk_with_one_bad_ticker(monkeypatch):
    monkeypatch.setattr(config, "BATCH_PRICE_CHUNK_SIZE", 3)
    calls = []

    def download(tickers, **_):
        calls.append(list(tickers))
        return _frame(tickers, bad={"BAD"})

    p = YFinanceProvider(download_fn=download, throttle=_NoThrottle(), sleep=lambda s: None)
    res = p.batch_latest_prices(["A", "B", "BAD", "C", "D"])
    assert calls == [["A", "B", "BAD"], ["C", "D"]]
    assert set(res.prices) == {"A", "B", "C", "D"}
    assert res.prices["A"].price == 11.0 and res.prices["A"].as_of == date(2026, 9, 25)
    assert "BAD" in res.failed


def test_batch_prices_whole_chunk_error_falls_back_per_ticker():
    def download(tickers, **_):
        if len(tickers) > 1 or tickers == ["BAD"]:
            raise ValueError("Yahoo refused the request")
        return _frame(tickers)

    p = YFinanceProvider(download_fn=download, throttle=_NoThrottle(), sleep=lambda s: None)
    res = p.batch_latest_prices(["A", "BAD", "B"])
    assert set(res.prices) == {"A", "B"} and "BAD" in res.failed


def test_retry_with_backoff_then_failure():
    sleeps, attempts = [], []

    class Flaky:
        @property
        def info(self):
            attempts.append(1)
            raise ConnectionError("429 Too Many Requests")

    p = YFinanceProvider(ticker_factory=lambda t: Flaky(), throttle=_NoThrottle(), sleep=sleeps.append)
    with pytest.raises(ProviderError):
        p.get_info("X")
    assert len(attempts) == config.FETCH_MAX_RETRIES + 1
    assert sleeps == [config.FETCH_BACKOFF_SECONDS * 2 ** i for i in range(config.FETCH_MAX_RETRIES)]


# ---------------------------------------------------------------- price accessors
def test_adjusted_and_actual_prices_differ_on_dividend_payer(fx_provider):
    assert len(fx_provider.get_dividends("JPM")) > 0
    adj = prices.adjusted_closes(fx_provider, "JPM")
    act = prices.actual_closes(fx_provider, "JPM")
    assert adj.iloc[0] < act.iloc[0]  # early prices are reduced by later dividends
    latest = prices.actual_latest_price(fx_provider, "JPM")
    assert latest.ok and latest.value == pytest.approx(act.iloc[-1])
    assert latest.period_label == prices.ACTUAL_PRICE_LABEL


# ---------------------------------------------------------------- currency
def test_usd_reporting_tsx_company_info_converted_to_cad(fx_provider):
    info = fx_provider.get_info("ABX.TO")
    assert currency.detect_mismatch(info) == ("USD", "CAD")
    fx = currency.fx_rate(fx_provider, "USD", "CAD")
    assert fx.ok and 1.0 < fx.value < 2.0
    vals = currency.info_datums(info, fx)
    for name in ("trailing_eps", "book_value_per_share", "info_free_cashflow", "info_ebitda", "info_total_debt",
                 "info_total_cash"):
        raw = info.get(name)
        if raw is None:
            continue
        assert vals[name].value == pytest.approx(raw * fx.value), name
        assert vals[name].currency == "CAD" and "converted USD→CAD" in vals[name].notes[0]
    assert vals["market_cap"].value == info.get("market_cap")  # already in trading currency
    assert vals["shares_outstanding"].value == info.get("shares_outstanding")


def test_statement_conversion_on_fixture(fx_provider):
    info = fx_provider.get_info("ABX.TO")
    stmt = fx_provider.get_statement("ABX.TO", "income", "annual")
    conv = currency.to_trading_currency(fx_provider, info, stmt)
    fx = currency.fx_rate(fx_provider, "USD", "CAD").value
    d = max(stmt.series("total_revenue"))
    assert conv.series("total_revenue")[d] == pytest.approx(stmt.series("total_revenue")[d] * fx)
    assert conv.series("diluted_shares") == stmt.series("diluted_shares")  # shares never converted
    assert conv.currency == "CAD" and "latest rate applied to all periods" in conv.notes[-1]


def test_same_currency_not_converted(fx_provider):
    info = fx_provider.get_info("CNR.TO")
    assert currency.detect_mismatch(info) is None
    stmt = fx_provider.get_statement("CNR.TO", "income", "annual")
    assert currency.to_trading_currency(fx_provider, info, stmt).values == stmt.values


def test_missing_fx_rate_gives_na_not_wrong_currency():
    info = InfoResult(ticker="X", values={"currency": "CAD", "financial_currency": "USD", "trailing_eps": 2.0},
                      statuses={"currency": "ok", "financial_currency": "ok", "trailing_eps": "ok"})
    vals = currency.info_datums(info, fx=None)
    assert vals["trailing_eps"].value is None and vals["trailing_eps"].status.startswith("N/A")


def test_every_value_records_its_provider(fx_provider):
    assert fx_provider.get_info("MSFT").provider == "yfinance"
    assert fx_provider.get_statement("MSFT", "balance", "quarterly").provider == "yfinance"
    assert prices.adjusted_closes(fx_provider, "MSFT").attrs["provider"] == "yfinance"


def test_fixture_provider_unknown_ticker_is_provider_error():
    with pytest.raises(ProviderError):
        fixture_provider().get_info("NOPE")
