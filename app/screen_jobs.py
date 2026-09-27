"""Launching screen runs from the app. The Streamlit page never screens in the
request: it starts scripts/run_screen.py as a background process and polls the
run's progress from the database."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import config
from storage import screen_store as store

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_screen.py"


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def active_run(db_path=config.RUNS_DB_PATH) -> store.ScreenRun | None:
    """The latest run still marked running whose process is alive."""
    run = store.latest_run(db_path)
    if run and run.status == store.RUNNING and pid_alive(run.pid):
        return run
    return None


def interrupted_run(db_path=config.RUNS_DB_PATH) -> store.ScreenRun | None:
    """A resumable run: stopped by the circuit breaker, or 'running' with a dead process."""
    run = store.latest_resumable_run(db_path)
    if run and (run.status == store.STOPPED or not pid_alive(run.pid)):
        return run
    return None


def launch(lists: list[str] | None = None, resume: bool = False, db_path=config.RUNS_DB_PATH) -> Path:
    if active_run(db_path):
        raise RuntimeError("a screen run is already in progress")
    log = Path(config.SCREEN_LOG_DIR) / f"screen_{datetime.now():%Y%m%d_%H%M%S}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), "--log", str(log)]
    cmd += ["--resume"] if resume else ["--lists", ",".join(lists or [])]
    subprocess.Popen(cmd, cwd=ROOT, start_new_session=True, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=os.environ.copy())
    return log
