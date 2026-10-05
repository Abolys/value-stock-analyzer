"""Sharing the app with a link: remote visitors must sign in; the viewer password is read-only
(no edits, no screens or alert checks, nothing stored; LLM lenses run on the free tier only -
never the owner's API key or Claude subscription); feedback reaches the owner."""

import os
from datetime import date, datetime

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import config
from data.cache import CachedProvider, DiskCache
from data.fixture_provider import fixture_edgar, fixture_provider
from data.health import HealthReport
from portfolio import store
from portfolio.models import Thesis, Transaction, Trigger
from storage import feedback


@pytest.fixture(autouse=True)
def _app(monkeypatch, tmp_path):
    from app import auth, services
    from portfolio import alerts as pa

    st.cache_resource.clear()
    provider = CachedProvider(fixture_provider(), DiskCache(tmp_path / "cache.db"))
    monkeypatch.setattr(services, "build_provider", lambda: provider)
    monkeypatch.setattr(services, "health", lambda p: HealthReport(ok=True, failures=[], checked_at=datetime(2026, 9, 1)))
    monkeypatch.setattr(services, "build_edgar", lambda p: fixture_edgar())
    monkeypatch.setattr(pa, "watchlist_tickers", lambda: [])
    monkeypatch.setattr(auth, "is_remote", lambda: True)  # every request comes through the share tunnel
    yield provider
    st.cache_resource.clear()


def _passwords(monkeypatch):
    monkeypatch.setenv("APP_OWNER_PASSWORD", "owner-secret")
    monkeypatch.setenv("APP_VIEWER_PASSWORD", "friend-secret")


def _sign_in(at, password):
    at.text_input[0].set_value(password)
    at.button[0].click().run()
    return at


def test_remote_access_is_refused_without_passwords(monkeypatch):
    monkeypatch.delenv("APP_OWNER_PASSWORD", raising=False)
    monkeypatch.delenv("APP_VIEWER_PASSWORD", raising=False)
    at = AppTest.from_file("../app/main.py", default_timeout=60)
    at.run()
    assert any("remote access is refused" in e.value for e in at.error)
    assert not any(t.value == "Screener" for t in at.title)


def test_streamlit_cloud_secrets_supply_the_passwords(monkeypatch):
    # A Cloud deploy has no .env: the passwords arrive through st.secrets (settings → secrets)
    monkeypatch.delenv("APP_OWNER_PASSWORD", raising=False)
    monkeypatch.delenv("APP_VIEWER_PASSWORD", raising=False)
    monkeypatch.setattr(st, "secrets", {"APP_VIEWER_PASSWORD": "friend-secret"})
    try:
        at = AppTest.from_file("../app/main.py", default_timeout=60)
        at.run()
        _sign_in(at, "friend-secret")
        assert not at.exception
        assert any(t.value == "Screener" for t in at.title)
    finally:
        os.environ.pop("APP_OWNER_PASSWORD", None)
        os.environ.pop("APP_VIEWER_PASSWORD", None)  # the mirror writes through, undo it for the suite


def test_env_passwords_win_over_cloud_secrets(monkeypatch):
    _passwords(monkeypatch)  # owner-secret / friend-secret in the environment
    monkeypatch.setattr(st, "secrets", {"APP_VIEWER_PASSWORD": "cloud-secret"})
    at = AppTest.from_file("../app/main.py", default_timeout=60)
    at.run()
    _sign_in(at, "cloud-secret")
    assert any("Wrong password" in e.value for e in at.error)  # env value, not the secret, is checked
    _sign_in(at, "friend-secret")
    assert not at.exception
    assert any(t.value == "Screener" for t in at.title)


def test_cloud_secrets_are_mirrored_into_the_environment(monkeypatch):
    # config snapshots its variables at import time, so on Cloud the only way its .env variables
    # (SEC_USER_AGENT, ANTHROPIC_API_KEY, …) can arrive is the st.secrets → os.environ mirror at
    # the top of app/main.py; verify it writes (config is already imported in this process, so
    # only the environment write is observable here)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setattr(st, "secrets", {"SEC_USER_AGENT": "cloud-agent <x@example.com>"})
    try:
        at = AppTest.from_file("../app/main.py", default_timeout=60)
        at.run()
        assert os.environ.get("SEC_USER_AGENT") == "cloud-agent <x@example.com>"
    finally:
        os.environ.pop("SEC_USER_AGENT", None)  # the mirror writes through, undo it for the suite


def test_wrong_password_stays_on_the_login(monkeypatch):
    _passwords(monkeypatch)
    at = AppTest.from_file("../app/main.py", default_timeout=60)
    at.run()
    _sign_in(at, "guess")
    assert any("Wrong password" in e.value for e in at.error)
    assert not any(t.value == "Screener" for t in at.title)


def test_viewer_is_read_only_and_starts_nothing(monkeypatch, _no_background_checks, _no_background_screens):
    _passwords(monkeypatch)
    hid = store.add_holding("LULU", "Main", "USD", Transaction(txn_date=date(2026, 5, 20), side="buy", shares=10,
                                                              price=120.38),
                            Thesis(reasons=[{"text": "Brand intact"}], triggers=[Trigger(field="piotroski", op="<",
                                                                                          literal=5)]))
    store.add_journal(hid, "Holding up")
    at = AppTest.from_file("../app/main.py", default_timeout=60)
    at.run()
    _sign_in(at, "friend-secret")
    assert not at.exception
    assert any(t.value == "Screener" for t in at.title)
    assert any("viewer (read-only" in c.value for c in at.sidebar.caption)
    assert _no_background_checks == [] and _no_background_screens == []  # a viewer starts no background work
    assert not any(b.label == "Run new screen" for b in at.button)
    # the Portfolio page, read-only
    from app import auth

    def page(provider):
        from app.views import portfolio

        portfolio.render(provider)

    view = AppTest.from_function(page, args=(_app_provider(),), default_timeout=60)
    view.session_state[auth.ROLE_KEY] = auth.VIEWER
    view.run()
    assert not view.exception
    labels = {b.label for b in view.button}
    assert not labels & {"Check alerts now", "Add entry", "Add transaction", "Close holding", "Mark all read"}
    assert all(c.disabled for c in view.checkbox if c.label == "Brand intact")
    assert "Holding up" in " ".join(m.value for m in view.markdown)  # the journal is shown, not editable
    assert not view.text_area  # no journal box


def _app_provider():
    return CachedProvider(fixture_provider(), DiskCache(config.RUNS_DB_PATH.with_name("view_cache.db")))


def test_viewer_analysis_makes_no_llm_calls_and_is_not_stored(monkeypatch):
    from app import auth
    from storage import history

    def page(provider):
        import streamlit as st

        from app.views import stock

        st.session_state.setdefault("ticker", "LULU")
        stock.render(provider)

    at = AppTest.from_function(page, args=(_app_provider(),), default_timeout=180)
    at.session_state[auth.ROLE_KEY] = auth.VIEWER
    at.run()
    assert not at.exception
    assert any("cached answers only" in c.value for c in at.caption)
    assert not any(b.label in ("Re-run analysis", "Add to portfolio") for b in at.button)
    assert history.load_history("LULU") == []  # nothing stored in the owner's run history


def test_viewer_llm_client_runs_on_the_free_tier_only(monkeypatch):
    """With both keys set, a viewer's LLM client is the free tier with the paid fallback off, so a
    viewer can use the free model but never spends the owner's API key."""
    from app import auth
    from app.views import stock

    monkeypatch.setenv("FREE_LLM_API_KEY", "sk-free")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setattr(config, "LLM_BACKEND", "auto")

    def page():
        import streamlit as st
        from app import auth
        from app.views import stock

        st.session_state[auth.ROLE_KEY] = auth.VIEWER
        st.session_state["llm_client"] = stock.llm_notice()

    at = AppTest.from_function(page, default_timeout=60)
    at.run()
    assert not at.exception
    client = at.session_state["llm_client"]
    assert client.backend == "free" and client._fallback is None
    assert any("run on the free tier" in c.value for c in at.caption)


def test_viewer_without_a_free_key_stays_cache_only(monkeypatch):
    """No FREE_LLM_API_KEY: the viewer's lenses are cached answers only (no LLM calls at all)."""
    from app import auth
    from app.views import stock

    monkeypatch.setenv("FREE_LLM_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")

    def page():
        import streamlit as st
        from app import auth
        from app.views import stock

        st.session_state[auth.ROLE_KEY] = auth.VIEWER
        st.session_state["llm_client"] = stock.llm_notice()

    at = AppTest.from_function(page, default_timeout=60)
    at.run()
    assert not at.exception
    client = at.session_state["llm_client"]
    assert client.backend == "none" and not client.configured
    assert any("cached answers only" in c.value for c in at.caption)


def test_owner_llm_client_keeps_the_paid_fallback(monkeypatch):
    """The owner's client keeps the free-tier → billed-API fallback (CLAUDE.md: only the owner's
    sessions may spend the API key)."""
    from app import auth
    from app.views import stock

    monkeypatch.setenv("FREE_LLM_API_KEY", "sk-free")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setattr(config, "LLM_BACKEND", "auto")

    def page():
        import streamlit as st
        from app import auth
        from app.views import stock

        st.session_state[auth.ROLE_KEY] = auth.OWNER
        st.session_state["llm_client"] = stock.llm_notice()

    at = AppTest.from_function(page, default_timeout=60)
    at.run()
    assert not at.exception
    client = at.session_state["llm_client"]
    assert client.backend == "free" and client._fallback is not None
    assert any("falls back to the Anthropic API" in c.value for c in at.caption)


def test_owner_password_gives_full_access_and_sees_feedback(monkeypatch):
    _passwords(monkeypatch)
    feedback.add("The scatter is great; the heatmap needs a legend", "Sam", "Screener")
    at = AppTest.from_file("../app/main.py", default_timeout=60)
    at.run()
    _sign_in(at, "owner-secret")
    assert not at.exception
    assert any("owner (full access)" in c.value for c in at.sidebar.caption)
    assert any(e.label.startswith("📥 Feedback received (1 new)") for e in at.sidebar.expander)
    assert "the heatmap needs a legend" in " ".join(m.value for m in at.sidebar.markdown)


def test_feedback_box_stores_an_entry():
    fid = feedback.add("  Loads slowly on first open  ", "Alex", "Stock")
    [f] = feedback.latest()
    assert f.feedback_id == fid and f.text == "Loads slowly on first open" and f.read_at is None
    assert feedback.unread() == 1
    feedback.mark_all_read()
    assert feedback.unread() == 0
    with pytest.raises(ValueError):
        feedback.add("   ")
