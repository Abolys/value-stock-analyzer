"""Sharing the app with a link: remote visitors must sign in; the viewer password is read-only
(no edits, no screens or alert checks, no LLM calls, nothing stored); feedback reaches the owner."""

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
