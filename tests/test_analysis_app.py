"""The Ticker page's four-lens analysis section renders in the app (offline fixtures, no API key)."""

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


def test_analysis_section_renders_lenses_and_aggregate(tmp_path, monkeypatch):
    from app import services

    provider = CachedProvider(fixture_provider(), DiskCache(tmp_path / "cache.db"))
    monkeypatch.setattr(services, "build_provider", lambda: provider)
    monkeypatch.setattr(services, "health", lambda p: HealthReport(ok=True, failures=[], checked_at=datetime(2026, 9, 1)))
    monkeypatch.setattr(services, "build_edgar", lambda p: fixture_edgar())
    monkeypatch.setattr(services, "valet_fetch", lambda: None)

    at = AppTest.from_file("../app/main.py", default_timeout=120)
    at.run()
    assert not at.exception
    assert any("API spend this month" in c.value for c in at.sidebar.caption)
    at.sidebar.text_input(key="ticker").set_value("LULU").run()
    assert not at.exception
    assert any("No ANTHROPIC_API_KEY and no Claude Code CLI found" in i.value for i in at.info)
    at.button(key="run-analysis-LULU").click().run()
    assert not at.exception
    heads = " ".join(m.value for m in at.markdown)
    for label in ("Quantitative Fundamental:", "Macro & Balance Sheet Risk:", "Business Moat:",
                  "Devil's Advocate:", "Aggregate:"):
        assert label in heads, label
    assert "LLM not configured" in heads
    assert "(2 of 4 lenses)" in heads  # Quant and Macro scored; the LLM lenses excluded, not zero
    captions = " ".join(c.value for c in at.caption)
    assert "Mapping: DCF upside" in captions and "API cost $0.0000" in captions
