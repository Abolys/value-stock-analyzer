import pandas as pd
import pytest

from tests.conftest import HANDMADE

import config
from data import universe as uv


def test_ishares_parsing_drops_non_equity_and_normalises():
    df = uv.parse_holdings((HANDMADE / "ishares_sample.csv").read_bytes(), "S&P 400", "US", "IJH.csv")
    assert list(df["ticker"]) == ["FIX", "BRK-B"]  # cash, money market, futures dropped; duplicate FIX merged
    assert set(df["source"]) == {"S&P 400"} and set(df["as_of"]) == {"2026-09-24"}


def test_tsx_tickers_get_to_suffix():
    df = uv.parse_holdings((HANDMADE / "xic_sample.csv").read_bytes(), "TSX Composite", "TSX", "XIC.csv")
    assert list(df["ticker"]) == ["RY.TO", "TECK-B.TO", "CNR.TO"]


def test_pacer_format_uses_money_market_flag_and_ticker_validity():
    df = uv.parse_holdings((HANDMADE / "pacer_sample.csv").read_bytes(), "COWZ", "US", "cowz.csv")
    assert list(df["ticker"]) == ["EOG", "BRK-B"]


def test_name_filter_keeps_companies_with_cash_in_their_name():
    content = b"StockTicker,SecurityName\nFCFS,FIRSTCASH HOLDINGS INC\nTWE,TREASURY WINE ESTATES\nXX1,CASH & OTHER\n"
    assert list(uv.parse_holdings(content, "CALF", "US")["ticker"]) == ["FCFS", "TWE"]


def test_changed_format_raises():
    with pytest.raises(ValueError):
        uv.parse_holdings(b"<!DOCTYPE html><html>blocked</html>", "S&P 400", "US")


def test_merge_removes_duplicates_and_keeps_all_sources():
    a = pd.DataFrame([{"ticker": "EOG", "name": "EOG", "source": "COWZ", "as_of": "2026-09-24"},
                      {"ticker": "FIX", "name": "Comfort", "source": "COWZ", "as_of": "2026-09-24"}])
    b = pd.DataFrame([{"ticker": "EOG", "name": "EOG", "source": "Dataroma", "as_of": "2026-Q2"}])
    m = uv.merge_lists([a, b])
    assert len(m) == 2
    eog = m[m.ticker == "EOG"].iloc[0]
    assert eog["source"] == "COWZ, Dataroma" and eog["as_of"] == "2026-09-24, 2026-Q2"


def test_failed_download_keeps_stale_list(tmp_path):
    good = (HANDMADE / "xic_sample.csv").read_bytes()
    raw = tmp_path / "raw"
    raw.mkdir()
    ok = uv.refresh_list("tsx_composite", http_get=lambda url: good, directory=tmp_path, raw_dir=raw)
    assert ok.status == "updated" and ok.count == 3

    def broken(url):
        raise ConnectionError("403 Forbidden")

    stale = uv.refresh_list("tsx_composite", http_get=broken, directory=tmp_path, raw_dir=raw)
    assert stale.status == "stale" and "stale since 2026-09-24" in stale.message
    assert len(uv.load_list("tsx_composite", tmp_path)) == 3  # previous list untouched
    changed = uv.refresh_list("tsx_composite", http_get=lambda url: b"<html>new page</html>",
                              directory=tmp_path, raw_dir=raw)
    assert changed.status == "stale" and "format changed" in changed.message


def test_manual_raw_file_used_when_download_fails(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "cowz.csv").write_bytes((HANDMADE / "pacer_sample.csv").read_bytes())
    out = uv.refresh_list("cowz", http_get=lambda url: b"<html>Just a moment...</html>", directory=tmp_path, raw_dir=raw)
    assert out.status == "raw file" and out.count == 2
    assert list(uv.load_list("cowz", tmp_path)["ticker"]) == ["EOG", "BRK-B"]


def test_missing_list_without_raw_file(tmp_path):
    out = uv.refresh_list("sp600", http_get=lambda url: b"", directory=tmp_path, raw_dir=tmp_path)
    assert out.status == "missing" and "download the holdings file by hand" in out.message


def test_manual_templates_exist():
    for key in config.MANUAL_UNIVERSE_LISTS:
        assert uv.list_path(key).read_text().strip() == "ticker,name,source,as_of"
