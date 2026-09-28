"""The Stock page auto-runs the analysis and renders every section; invalid tickers and a failed
health check show clear errors (offline fixtures, no API key)."""

from datetime import datetime

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from data.cache import CachedProvider, DiskCache
from data.fixture_provider import fixture_edgar, fixture_provider
from data.health import HealthReport


@pytest.fixture(autouse=True)
def _fresh_resource_cache():
    """The app caches its provider with st.cache_resource, which outlives one AppTest run;
    clear it so this test's fixture provider never leaks into other app tests."""
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


def test_stock_page_renders_every_section(tmp_path, monkeypatch):
    from app import services

    provider = CachedProvider(fixture_provider(), DiskCache(tmp_path / "cache.db"))
    monkeypatch.setattr(services, "build_provider", lambda: provider)
    monkeypatch.setattr(services, "health", lambda p: HealthReport(ok=True, failures=[], checked_at=datetime(2026, 9, 1)))
    monkeypatch.setattr(services, "build_edgar", lambda p: fixture_edgar())
    monkeypatch.setattr(services, "valet_fetch", lambda: None)

    at = AppTest.from_file("../app/main.py", default_timeout=180)
    at.run()
    assert not at.exception
    assert any("API spend this month" in c.value for c in at.sidebar.caption)
    at.sidebar.text_input(key="ticker_box").set_value("LULU").run()  # opens the Stock page and auto-runs
    assert not at.exception
    assert any("No ANTHROPIC_API_KEY and no Claude Code CLI found" in i.value for i in at.info)
    lens_boxes = " ".join(b.value for b in [*at.info, *at.warning])
    for label in ("Quantitative Fundamental:", "Macro & Balance Sheet Risk:", "Business Moat:", "Devil's Advocate:"):
        assert label in lens_boxes, label
    assert "LLM not configured" in lens_boxes
    md = " ".join(m.value for m in at.markdown)
    assert "2 of 4 lenses" in md  # verdict badge: the LLM lenses excluded, not zero
    assert "Price as of" in " ".join(c.value for c in at.caption)
    assert "How this score was built:" in md and "Leadership:" in md
    heads = [h.value for h in at.subheader]
    for h in ("Lens scores", "Fundamentals over time", "Versus peers", "Asset floor · if the earnings case fails",
              "Valuation", "Value-trap scores", "Turnaround outlook", "Dividend and context", "Export"):
        assert h in heads, h
    captions = " ".join(c.value for c in at.caption)
    assert "delisted aren't included" in captions  # the survivorship caveat
    assert "dividend panel hidden for non-payers" in captions
    assert "API cost" in captions
    assert [t.label for t in at.tabs][-3:] == ["Turnaround details", "History", "Raw data"]
    assert len(at.get("plotly_chart")) >= 6
    assert any("peak" in df.value.columns for df in at.dataframe)  # the episode list


def test_invalid_ticker_shows_clear_error(tmp_path, monkeypatch):
    from app import services

    provider = CachedProvider(fixture_provider(), DiskCache(tmp_path / "cache.db"))
    monkeypatch.setattr(services, "build_provider", lambda: provider)
    monkeypatch.setattr(services, "health", lambda p: HealthReport(ok=True, failures=[], checked_at=datetime(2026, 9, 1)))
    monkeypatch.setattr(services, "build_edgar", lambda p: fixture_edgar())
    monkeypatch.setattr(services, "valet_fetch", lambda: None)
    at = AppTest.from_file("../app/main.py", default_timeout=120)
    at.run()
    at.sidebar.text_input(key="ticker_box").set_value("NOPE").run()
    assert not at.exception
    assert any("Could not load **NOPE**" in e.value for e in at.error)


def test_banner_on_failed_health_check(tmp_path, monkeypatch):
    from app import services

    provider = CachedProvider(fixture_provider(), DiskCache(tmp_path / "cache.db"))
    monkeypatch.setattr(services, "build_provider", lambda: provider)
    monkeypatch.setattr(services, "health", lambda p: HealthReport(ok=False, failures=["SPY: statements empty"],
                                                                   checked_at=datetime(2026, 9, 1)))
    at = AppTest.from_file("../app/main.py", default_timeout=60)
    at.run()
    err = " ".join(e.value for e in at.error)
    assert "Data source not responding correctly" in err and "SPY: statements empty" in err
    assert "upgrade_yfinance.py" in err and "shown, each with its age" in err


def _stock_page(provider):
    import streamlit as st

    from app.views import stock

    st.session_state.setdefault("ticker", "LULU")
    stock.render(provider)


def test_session_analysis_from_an_earlier_day_is_refreshed(tmp_path, monkeypatch):
    from datetime import timedelta

    from app import services

    provider = CachedProvider(fixture_provider(), DiskCache(tmp_path / "cache.db"))
    monkeypatch.setattr(services, "build_edgar", lambda p: fixture_edgar())
    monkeypatch.setattr(services, "valet_fetch", lambda: None)
    at = AppTest.from_function(_stock_page, args=(provider,), default_timeout=180)
    at.run()
    assert not at.exception
    at.run()  # same day: the session's analysis is reused and says when it ran
    assert any("Analysed at" in c.value for c in at.caption)
    assert not any("refreshing it" in i.value for i in at.info)
    entry = at.session_state["analyses"]["LULU"]
    entry.created_at -= timedelta(days=1)  # the tab was left open overnight
    at.run()
    assert not at.exception
    assert any("refreshing it" in i.value for i in at.info)
    assert at.session_state["analyses"]["LULU"].created_at.date() == datetime.now().date()
