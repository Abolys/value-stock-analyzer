"""App-start reminder: ask to run a screen when the last completed one is older than
SCREEN_REMIND_DAYS (or none has completed); offer Resume for an interrupted run; stay quiet
while a run is active or the last screen is recent."""

import os
from datetime import datetime, timedelta

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import config
from app import screen_jobs
from storage import screen_store as store

NOW = datetime(2026, 9, 27, 9, 0)


def _completed(days_ago: float, lists=("cowz", "tsx_composite")) -> int:
    rid = store.create_run(list(lists), status=store.COMPLETED, path=config.RUNS_DB_PATH)
    store.update_run(rid, path=config.RUNS_DB_PATH,
                     started_at=(NOW - timedelta(days=days_ago)).isoformat(timespec="seconds"))
    return rid


def test_no_screen_ever_asks_to_run():
    p = screen_jobs.screen_prompt(now=NOW)
    assert p.kind == "run" and "No screen has completed yet" in p.message and p.lists == []


def test_recent_screen_does_not_ask():
    _completed(config.SCREEN_REMIND_DAYS - 1)
    assert screen_jobs.screen_prompt(now=NOW) is None


def test_week_old_screen_asks_with_its_lists():
    _completed(config.SCREEN_REMIND_DAYS + 1)
    p = screen_jobs.screen_prompt(now=NOW)
    assert p.kind == "run" and p.lists == ["cowz", "tsx_composite"]
    assert f"({config.SCREEN_REMIND_DAYS + 1} days ago)" in p.message


def test_active_run_does_not_ask():
    _completed(30)
    store.create_run(["cowz"], status=store.RUNNING, pid=os.getpid(), path=config.RUNS_DB_PATH)
    assert screen_jobs.screen_prompt(now=NOW) is None


def test_interrupted_run_offers_resume():
    _completed(30)
    rid = store.create_run(["cowz"], status=store.STOPPED, total=100, path=config.RUNS_DB_PATH)
    store.update_run(rid, path=config.RUNS_DB_PATH, attempted=40)
    p = screen_jobs.screen_prompt(now=NOW)
    assert p.kind == "resume" and "40 of 100" in p.message


@pytest.fixture
def offline_app(monkeypatch, tmp_path):
    from app import services
    from data.cache import CachedProvider, DiskCache
    from data.fixture_provider import fixture_provider
    from data.health import HealthReport

    st.cache_resource.clear()
    provider = CachedProvider(fixture_provider(), DiskCache(tmp_path / "cache.db"))
    monkeypatch.setattr(services, "build_provider", lambda: provider)
    monkeypatch.setattr(services, "health", lambda p: HealthReport(ok=True, failures=[], checked_at=datetime(2026, 9, 1)))
    yield
    st.cache_resource.clear()


def test_app_start_asks_and_runs_with_the_last_lists(offline_app, _no_background_screens):
    _completed(10, lists=("cowz",))  # relative to NOW; the app uses the real clock, so this is well over a week
    at = AppTest.from_file("../app/main.py", default_timeout=60)
    at.run()
    assert not at.exception
    assert any("Run a new screen now?" in m.value for m in at.markdown)
    at.button(key="remind-run").click().run()
    assert not at.exception
    assert _no_background_screens == [(["cowz"], False)]
    assert any("Screen started in the background" in i.value for i in at.info)
    at.run()  # asked once per session
    assert not any("Run a new screen now?" in m.value for m in at.markdown)


def test_app_start_stays_quiet_after_a_recent_screen(offline_app, _no_background_screens):
    rid = store.create_run(["cowz"], status=store.COMPLETED, path=config.RUNS_DB_PATH)  # started now
    at = AppTest.from_file("../app/main.py", default_timeout=60)
    at.run()
    assert not at.exception and rid
    assert not any("Run a new screen now?" in m.value for m in at.markdown)
    assert _no_background_screens == []
