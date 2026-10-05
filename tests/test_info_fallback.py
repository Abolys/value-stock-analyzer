"""The `info` fallback (data/info_fallback.py) and how the cached provider uses it when Yahoo refuses
`info` requests: a fresh or stale Yahoo copy first, the rebuilt info only when none exists."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from data.cache import CachedProvider, DiskCache
from data.finnhub import FinnhubClient
from data.fixture_provider import fixture_provider
from data.health import HealthReport
from data.info_fallback import InfoFallback, fallback_reason, sector_from_finnhub, sector_from_sic
from data.provider import FALLBACK_PROVIDER, InfoResult, ProviderError, ProviderUnavailable
from data.sector import route
from data.xbrl import CompanyFacts, Concept

TODAY = date(2026, 9, 1)


class BlockedYahoo:
    """The fixture provider with Yahoo `info` refused and a canned chart-data profile."""

    def __init__(self, profile: dict | None = None):
        self._inner = fixture_provider(today=TODAY)
        self.name = self._inner.name
        self.profile = profile if profile is not None else {"currency": "USD", "quote_type": "EQUITY",
                                                           "long_name": "Fixture Co", "market_cap": 1e9}
        self.info_calls = 0

    def get_info(self, ticker):
        self.info_calls += 1
        raise ProviderError("yfinance returned no info")

    def get_quote_profile(self, ticker):
        return dict(self.profile)

    def __getattr__(self, name):  # statements, dividends, ... from the fixtures
        return getattr(self._inner, name)


class FakeEdgar:
    def __init__(self, cik=1, sic="6022", desc="State Commercial Banks", unit="USD"):
        self.cik, self.sic, self.desc, self.unit = cik, sic, desc, unit

    def lookup_cik(self, ticker):
        return self.cik

    def submissions(self, cik):
        return {"sic": self.sic, "sicDescription": self.desc, "name": "EDGAR Name"}

    def company_facts(self, cik):
        return CompanyFacts(cik=cik, concepts={"net_income": Concept(canonical="net_income",
                                                                      tag="us-gaap:NetIncomeLoss", unit=self.unit)})


class FakeFinnhub:
    def __init__(self, industry="Banking", currency="USD", fail=False):
        self.industry, self.currency, self.fail = industry, currency, fail

    def profile(self, ticker):
        if self.fail:
            raise ProviderError("Finnhub has no profile")
        return {"ticker": ticker, "name": "Finnhub Name", "finnhubIndustry": self.industry,
                "currency": self.currency, "country": "US"}


def _provider(tmp_path, inner=None, edgar=None, finnhub=None):
    inner = inner or BlockedYahoo()
    p = CachedProvider(inner, DiskCache(tmp_path / "c.db"))
    p.info_fallback = InfoFallback(p, edgar=edgar, finnhub=finnhub, today=lambda: TODAY)
    return p, inner


# -- sector mapping ------------------------------------------------------------------------------
@pytest.mark.parametrize("sic,expected", [
    (6798, ("Real Estate", "REIT - Diversified")),
    (6021, ("Financial Services", "Banks - Regional")),
    (6311, ("Financial Services", "Insurance - Diversified")),
    (3714, ("Consumer Cyclical", "Auto Parts")),
    (3711, ("Consumer Cyclical", "Auto Manufacturers")),
    (7372, ("Technology", "Services-Prepackaged Software")),
])
def test_sic_codes_map_to_yfinance_labels(sic, expected):
    assert sector_from_sic(sic, "SERVICES-PREPACKAGED SOFTWARE") == expected


def test_sic_routes_banks_reits_and_insurers_to_their_treatment():
    for sic, subsector in ((6022, "bank"), (6798, "reit"), (6331, "insurer")):
        sector, industry = sector_from_sic(sic, None)
        info = InfoResult(ticker="X", values={"sector": sector, "industry": industry})
        assert route(info).subsector == subsector


def test_unknown_codes_and_industries_map_to_nothing():
    assert sector_from_sic(9999, "Nonclassifiable") is None
    assert sector_from_sic(None, None) is None
    assert sector_from_finnhub("Something New") is None
    assert sector_from_finnhub("Banking") == ("Financial Services", "Banks - Regional")


# -- building ------------------------------------------------------------------------------------
def test_build_labels_every_value_with_its_source(tmp_path):
    p, _ = _provider(tmp_path, edgar=FakeEdgar())
    info = p.get_info("JPM")
    assert info.provider == FALLBACK_PROVIDER and info.is_fallback
    assert info.get("sector") == "Financial Services" and info.get("industry") == "Banks - Regional"
    assert info.sources["sector"].startswith("SEC EDGAR SIC 6022")
    assert info.get("currency") == "USD" and info.sources["currency"] == "Yahoo chart data"
    assert info.get("financial_currency") == "USD" and info.sources["financial_currency"].startswith("SEC XBRL")
    assert info.get("long_name") == "Fixture Co"  # Yahoo chart data outranks EDGAR's name
    assert info.status("trailing_eps") == "ok" and info.sources["trailing_eps"].startswith("statements")
    assert info.get("book_value_per_share") > 0
    assert info.date("most_recent_quarter") is not None
    # No source has these: N/A with the reason, never zero (Rule 2).
    assert info.get("business_summary") is None
    assert info.status("business_summary").startswith("N/A - Yahoo info unavailable")
    assert info.status("company_officers").startswith("N/A")
    assert route(info).subsector == "bank"
    line = fallback_reason(info)
    assert "SEC EDGAR SIC 6022" in line and "Not available:" in line and "business summary" in line


def test_finnhub_fills_in_when_sec_has_nothing(tmp_path):
    p, _ = _provider(tmp_path, edgar=FakeEdgar(cik=None), finnhub=FakeFinnhub(industry="Insurance", currency="CAD"))
    info = p.get_info("JPM")
    assert info.get("industry") == "Insurance - Diversified" and info.sources["industry"].startswith("Finnhub")
    assert info.get("financial_currency") == "CAD" and info.get("country") == "US"


def test_sec_sic_outranks_finnhub(tmp_path):
    p, _ = _provider(tmp_path, edgar=FakeEdgar(sic="6798", desc="REITs"), finnhub=FakeFinnhub(industry="Real Estate"))
    info = p.get_info("JPM")
    assert info.get("industry") == "REIT - Diversified" and route(info).subsector == "reit"


def test_unknown_reporting_currency_is_assumed_and_labelled(tmp_path):
    p, _ = _provider(tmp_path, edgar=FakeEdgar(cik=None), finnhub=FakeFinnhub(fail=True))
    info = p.get_info("JPM")
    assert info.get("financial_currency") == "USD"
    assert info.sources["financial_currency"].startswith("assumed = trading currency")
    assert info.get("sector") is None and info.status("sector").startswith("N/A")
    assert any("not an SEC filer" in n for n in info.raw["_notes"])


def test_dividend_rate_left_out_when_currencies_differ(tmp_path):
    p, _ = _provider(tmp_path, edgar=FakeEdgar(unit="CAD"))
    info = p.get_info("JPM")
    assert info.get("financial_currency") == "CAD" and info.get("dividend_rate") is None


def test_fund_gets_no_company_lookups(tmp_path):
    inner = BlockedYahoo(profile={"currency": "USD", "quote_type": "ETF", "long_name": "Some ETF"})
    edgar = FakeEdgar()
    edgar.lookup_cik = lambda t: pytest.fail("a fund needs no SEC company lookup")
    p, _ = _provider(tmp_path, inner=inner, edgar=edgar)
    info = p.get_info("JPM")
    assert info.get("quote_type") == "ETF" and info.get("trailing_eps") is None


# -- the cached provider -------------------------------------------------------------------------
def test_stale_yahoo_copy_outranks_the_fallback(tmp_path):
    p, inner = _provider(tmp_path, edgar=FakeEdgar())
    old = InfoResult(ticker="JPM", values={"sector": "Financial Services"}, statuses={"sector": "ok"},
                     provider="yfinance")
    key = p.key_for("get_info", "JPM")
    past = datetime.now() - timedelta(days=1)
    p.cache.put(key, "info", old, past, "expired", ticker="JPM", method="get_info", fetched_at=past)
    info = p.get_info("JPM")
    assert info.provider == "yfinance" and inner.info_calls == 1
    assert p.cache.last_event(key).outcome == "stale"


def test_block_skips_yahoo_after_a_refusal(tmp_path):
    p, inner = _provider(tmp_path, edgar=FakeEdgar())
    p.get_info("JPM")
    assert inner.info_calls == 1 and p.info_block.active
    p.get_info("MSFT")  # no second wait through Yahoo's back-off
    assert inner.info_calls == 1
    p.info_block.clear()
    p.get_info("LULU")
    assert inner.info_calls == 2


def test_fallback_is_cached_for_a_day(tmp_path):
    p, _ = _provider(tmp_path, edgar=FakeEdgar())
    first = p.get_info("JPM")
    entry = p.cache.get(p.key_for("fallback_info", "JPM"))
    assert entry is not None and entry.payload.provider == FALLBACK_PROVIDER
    assert entry.expires_at - entry.fetched_at <= timedelta(days=2)
    assert p.get_info("JPM").values == first.values


def test_without_a_fallback_the_error_propagates(tmp_path):
    p = CachedProvider(BlockedYahoo(), DiskCache(tmp_path / "c.db"))
    with pytest.raises(ProviderError):
        p.get_info("JPM")
    assert not p.info_block.active  # only tripped when there is somewhere else to go


def test_nothing_anywhere_raises_with_both_reasons(tmp_path):
    inner = BlockedYahoo()
    inner.get_quote_profile = lambda t: (_ for _ in ()).throw(ProviderError("no chart data"))
    inner.get_statement = lambda *a: (_ for _ in ()).throw(ProviderError("no statements"))
    inner.get_dividends = lambda t: (_ for _ in ()).throw(ProviderError("no dividends"))
    p, _ = _provider(tmp_path, inner=inner)
    with pytest.raises(ProviderError, match="no info.*fallback: no fallback source"):
        p.get_info("JPM")


def test_screen_saves_no_officer_snapshot_from_a_fallback_info(tmp_path):
    from screening.engine import ScreenContext, screen_ticker_full
    from storage.db import get_officer_snapshots

    p, _ = _provider(tmp_path, edgar=FakeEdgar())
    db = tmp_path / "runs.db"
    screen_ticker_full(ScreenContext(provider=p, db_path=db, today=TODAY), "JPM")
    assert get_officer_snapshots("JPM", db) == []


# -- health check and Finnhub client ---------------------------------------------------------------
def test_health_report_recognises_an_info_block():
    tickers = ["SPY", "MSFT"]
    blocked = HealthReport(ok=False, checked_at=datetime.now(), tickers=tickers,
                           failures=["SPY: info failed (no info)", "MSFT: info failed (no info)"])
    assert blocked.info_blocked and blocked.only_info_blocked
    mixed = blocked.model_copy(update={"failures": blocked.failures + ["MSFT: prices failed (x)"]})
    assert mixed.info_blocked and not mixed.only_info_blocked
    partial = blocked.model_copy(update={"failures": ["SPY: info failed (no info)"]})
    assert not partial.info_blocked


def test_finnhub_without_a_key_is_unavailable():
    with pytest.raises(ProviderUnavailable):
        FinnhubClient(api_key="").profile("MSFT")


def test_finnhub_errors_never_echo_the_token():
    class Session:
        def get(self, url, params=None, timeout=None):
            raise RuntimeError(f"failed: {url}?token={params['token']}")

    class NoWait:
        def wait(self):
            pass

    with pytest.raises(ProviderError) as e:
        FinnhubClient(api_key="SECRET", session=Session(), limiter=NoWait()).profile("MSFT")
    assert "SECRET" not in str(e.value) and e.value.__cause__ is None
