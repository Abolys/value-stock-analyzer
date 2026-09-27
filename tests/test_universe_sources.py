"""Alternative universe sources: SSGA holdings files and SEC N-PORT filings."""

import os
from datetime import date

import pytest

from tests.conftest import HANDMADE

from data import universe as uv
from data.edgar import FUND_TICKER_MAP_URL, SERIES_FILINGS_URL, EdgarClient
from data.fixture_provider import _NoThrottle, _Resp
from data.nport import parse_nport

NPORT_DIR = HANDMADE / "edgar_nport"
COWZ_DOC = ("https://www.sec.gov/Archives/edgar/data/1616668/000089418926020734/primary_doc.xml")


class DictSession:
    def __init__(self, mapping):
        self.mapping, self.requested = mapping, []

    def get(self, url, **_):
        self.requested.append(url)
        if url not in self.mapping:
            raise ConnectionError(f"unexpected url {url}")
        return _Resp(self.mapping[url].read_bytes(), url)


def nport_edgar():
    session = DictSession({
        FUND_TICKER_MAP_URL: NPORT_DIR / "company_tickers_mf.json",
        SERIES_FILINGS_URL.format(series="S000055466", form="NPORT-P"): NPORT_DIR / "cowz_nport_atom.xml",
        COWZ_DOC: NPORT_DIR / "cowz_primary_doc.xml",
    })
    return EdgarClient(user_agent="test test@example.com", session=session, limiter=_NoThrottle())


# ---------------------------------------------------------------- N-PORT
def test_parse_nport_keeps_long_equity_with_tickers_and_reports_the_rest():
    r = parse_nport((HANDMADE / "nport_sample.xml").read_text())
    assert r.series_id == "S000099999" and r.report_period == date(2026, 4, 30)
    assert [h.ticker for h in r.holdings] == ["ACME", "BRK/B"]
    assert len(r.skipped) == 3
    assert any("no ticker" in s for s in r.skipped)
    assert any("assetCat STIV" in s for s in r.skipped)
    assert any("Short position" in s for s in r.skipped)


def test_edgar_fund_series_and_latest_nport():
    edgar = nport_edgar()
    s = edgar.fund_series("cowz")
    assert (s.cik, s.series_id) == (1616668, "S000055466")
    assert edgar.fund_series("NOPE") is None
    filing = edgar.latest_nport("COWZ")
    assert filing.accession == "0000894189-26-020734" and filing.filing_date == date(2026, 6, 29)


def test_cowz_list_from_real_nport_fixture(tmp_path):
    out = uv.refresh_list("cowz", http_get=lambda u: b"", directory=tmp_path, raw_dir=tmp_path / "raw",
                          edgar_factory=nport_edgar)
    assert out.status == "updated" and out.count == 100 and out.as_of == "2026-04-30"
    assert "SEC N-PORT" in out.message and "filed 2026-06-29" in out.message
    assert "2 holding(s) skipped" in out.message  # the two money-market lines, named in the message
    df = uv.load_list("cowz", tmp_path)
    assert {"T", "MO"} <= set(df["ticker"]) and set(df["source"]) == {"COWZ"}


def test_newer_manual_file_beats_nport(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "cowz.csv").write_bytes((HANDMADE / "pacer_sample.csv").read_bytes())  # file saved today
    out = uv.refresh_list("cowz", directory=tmp_path, raw_dir=raw, edgar_factory=nport_edgar)
    assert out.status == "raw file" and "newer than SEC N-PORT (COWZ)" in out.message
    assert list(uv.load_list("cowz", tmp_path)["ticker"]) == ["EOG", "BRK-B"]


def test_older_manual_file_loses_to_nport(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    p = raw / "cowz.csv"
    p.write_bytes((HANDMADE / "pacer_sample.csv").read_bytes())
    old = date(2026, 1, 15)
    ts = __import__("datetime").datetime(old.year, old.month, old.day).timestamp()
    os.utime(p, (ts, ts))
    out = uv.refresh_list("cowz", directory=tmp_path, raw_dir=raw, edgar_factory=nport_edgar)
    assert out.status == "updated" and out.as_of == "2026-04-30"


# ---------------------------------------------------------------- SSGA + fallback order
def test_ssga_holdings_file_parses():
    df = uv.parse_holdings((HANDMADE / "ssga_sample.xlsx").read_bytes(), "S&P 600", "US", "spsm.xlsx")
    assert len(df) == 8 and set(df["as_of"]) == {"2026-09-24"}
    names = " ".join(df["name"])
    assert "MONEY MARKET" not in names and "US DOLLAR" not in names and "EARNOUT" not in names


def test_second_source_used_when_first_fails(tmp_path):
    ishares = (HANDMADE / "ishares_sample.csv").read_bytes()

    def get(url):
        if "ssga.com" in url:
            raise ConnectionError("SSGA down")
        return ishares

    out = uv.refresh_list("sp400", http_get=get, directory=tmp_path, raw_dir=tmp_path / "raw")
    assert out.status == "updated" and "from iShares IJH" in out.message and "SSGA SPMD failed" in out.message


def test_first_source_preferred(tmp_path):
    ssga = (HANDMADE / "ssga_sample.xlsx").read_bytes()
    seen = []

    def get(url):
        seen.append(url)
        return ssga

    out = uv.refresh_list("sp600", http_get=get, directory=tmp_path, raw_dir=tmp_path / "raw")
    assert out.status == "updated" and "from SSGA SPSM" in out.message and len(seen) == 1


@pytest.mark.parametrize("key", ["cowz", "cash_cows_small", "sp400", "sp600", "tsx_composite"])
def test_every_list_has_sources(key):
    import config

    assert config.UNIVERSE_SOURCES[key]["sources"]
