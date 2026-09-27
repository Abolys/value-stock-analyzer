"""The Screener page renders the latest completed run; the launcher starts the
script in the background and refuses a second concurrent run."""

import os
from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

import config
from app import screen_jobs, services
from data.cache import CachedProvider, DiskCache
from data.fixture_provider import fixture_provider
from data.health import HealthReport
from storage import screen_store as store
from tests.screen_helpers import run_eval


def test_screener_page_shows_latest_completed_run(monkeypatch, tmp_path):
    db = config.RUNS_DB_PATH  # redirected to tmp by conftest
    run_id = store.create_run(["cowz"], total=1, path=db)
    res = run_eval()
    res.sources = "COWZ"
    store.write_result(run_id, res, db)
    store.update_run(run_id, db, status=store.COMPLETED, attempted=1, passed_stage1=1, passed_stage2=1,
                     refetched_reported=0, served_from_cache=1, flagged_fields=["ttm.ebitda"])
    provider = CachedProvider(fixture_provider(), DiskCache(tmp_path / "c.db"))
    monkeypatch.setattr(services, "build_provider", lambda: provider)
    monkeypatch.setattr(services, "health", lambda p: HealthReport(ok=True, checked_at=datetime.now()))

    at = AppTest.from_file("../app/main.py", default_timeout=60)
    at.run()
    at.sidebar.radio(key="page").set_value("Screener").run()
    assert not at.exception
    assert any("Latest completed run" in s.value for s in at.subheader)
    assert "likely renamed upstream" in " ".join(w.value for w in at.warning)
    table = at.dataframe[1].value  # [0] is the universe-lists table
    row = table.iloc[0]
    assert row["ticker"] == "TEST" and row["status"] == "Pass" and row["source"] == "COWZ"
    assert row["quality"].endswith("(4 of 4)")


def test_launch_starts_script_in_background(monkeypatch, tmp_path):
    started = {}
    monkeypatch.setattr(config, "SCREEN_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(screen_jobs.subprocess, "Popen", lambda cmd, **kw: started.update(cmd=cmd, kw=kw))
    log = screen_jobs.launch(["cowz", "tsx_composite"], db_path=tmp_path / "r.db")
    assert started["cmd"][1].endswith("scripts/run_screen.py")
    assert started["cmd"][-2:] == ["--lists", "cowz,tsx_composite"]
    assert started["kw"]["start_new_session"] and log.parent == tmp_path / "logs"
    screen_jobs.launch(resume=True, db_path=tmp_path / "r.db")
    assert started["cmd"][-1] == "--resume"


def test_launch_refused_while_a_run_is_active(tmp_path):
    db = tmp_path / "r.db"
    store.create_run(["cowz"], pid=os.getpid(), path=db)
    assert screen_jobs.active_run(db) is not None
    with pytest.raises(RuntimeError):
        screen_jobs.launch(["cowz"], db_path=db)


def test_dead_running_run_counts_as_interrupted(tmp_path):
    db = tmp_path / "r.db"
    store.create_run(["cowz"], pid=2 ** 22 + 12345, path=db)  # no such process
    assert screen_jobs.active_run(db) is None
    assert screen_jobs.interrupted_run(db).status == store.RUNNING
