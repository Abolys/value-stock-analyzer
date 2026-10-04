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
    # st.switch_page() used to reset the sidebar input on the page change, making it look as if
    # typing did nothing; the box must keep the ticker the user just entered.
    assert at.sidebar.text_input(key="ticker_box").value == "LULU"
    # the Stock page's top search box (for switching to a different stock) mirrors the committed ticker too
    assert at.text_input(key="ticker_search").value == "LULU"
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
    # the Quant lens' key-figures table (the draw_lens figure/value grid)
    assert any(set(df.value.columns) == {"figure", "value"} for df in at.dataframe)


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
    # the top search box stays on the page even in the load-error state, so the typo can be fixed in place
    assert at.text_input(key="ticker_search").value == "NOPE"


def test_top_search_box_switches_to_a_different_stock(tmp_path, monkeypatch):
    """Typing a ticker in the Stock page's top search box and pressing Enter re-analyses that stock,
    instead of having to reach for the sidebar box mid-detail; both boxes mirror it afterwards.

    set_value() + run() is the browser's delivery path: Streamlit runs the widget's on_change
    callback before the script body (in AppTest and live), and the callback commits the value the
    user just entered — so the run must keep it, not let the other box's stale mirror undo it."""
    from app import services

    provider = CachedProvider(fixture_provider(), DiskCache(tmp_path / "cache.db"))
    monkeypatch.setattr(services, "build_provider", lambda: provider)
    monkeypatch.setattr(services, "health", lambda p: HealthReport(ok=True, failures=[], checked_at=datetime(2026, 9, 1)))
    monkeypatch.setattr(services, "build_edgar", lambda p: fixture_edgar())
    monkeypatch.setattr(services, "valet_fetch", lambda: None)
    at = AppTest.from_file("../app/main.py", default_timeout=180)
    at.run()
    assert not at.exception
    at.sidebar.text_input(key="ticker_box").set_value("LULU").run()
    assert not at.exception
    # the Stock page's top search box mirrors the committed ticker
    assert at.text_input(key="ticker_search").value == "LULU"
    # switch to a different stock from the top of the Stock page
    at.text_input(key="ticker_search").set_value("JPM").run()
    assert not at.exception
    md = " ".join(m.value for m in at.markdown)
    assert "JPM" in md  # the new stock's header
    assert at.session_state["ticker"] == "JPM"
    assert at.text_input(key="ticker_search").value == "JPM"
    assert at.sidebar.text_input(key="ticker_box").value == "JPM"
    # switching back re-serves the earlier analysis from this session's store (no re-run needed)
    at.text_input(key="ticker_search").set_value("LULU").run()
    assert not at.exception
    assert at.session_state["analyses"]["LULU"] is not None
    assert at.sidebar.text_input(key="ticker_box").value == "LULU"


def test_sidebar_box_switches_to_a_different_stock(tmp_path, monkeypatch):
    """The sidebar's Ticker box switches the stock while the Stock page is open: committing a new
    ticker there re-analyses it instead of leaving the previous stock on screen, and both boxes
    mirror the switch (the regression this file's first switch test guards on the other box)."""
    from app import services

    provider = CachedProvider(fixture_provider(), DiskCache(tmp_path / "cache.db"))
    monkeypatch.setattr(services, "build_provider", lambda: provider)
    monkeypatch.setattr(services, "health", lambda p: HealthReport(ok=True, failures=[], checked_at=datetime(2026, 9, 1)))
    monkeypatch.setattr(services, "build_edgar", lambda p: fixture_edgar())
    monkeypatch.setattr(services, "valet_fetch", lambda: None)
    at = AppTest.from_file("../app/main.py", default_timeout=180)
    at.run()
    assert not at.exception
    at.sidebar.text_input(key="ticker_box").set_value("LULU").run()
    assert not at.exception
    assert at.text_input(key="ticker_search").value == "LULU"
    # commit a different ticker in the sidebar box while the Stock page is open
    at.sidebar.text_input(key="ticker_box").set_value("JPM").run()
    assert not at.exception
    md = " ".join(m.value for m in at.markdown)
    assert "JPM" in md  # the new stock's header
    assert at.session_state["ticker"] == "JPM"
    assert at.sidebar.text_input(key="ticker_box").value == "JPM"
    assert at.text_input(key="ticker_search").value == "JPM"
    # and back: the earlier analysis is re-served from this session's store
    at.sidebar.text_input(key="ticker_box").set_value("LULU").run()
    assert not at.exception
    assert at.session_state["analyses"]["LULU"] is not None
    assert at.text_input(key="ticker_search").value == "LULU"


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
