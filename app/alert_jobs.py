"""The app-start alert check. The page never checks in the request: it starts
scripts/check_alerts.py as a background process (like the screener's Run button), at most
once per ALERT_CHECK_MIN_INTERVAL_MINUTES, and never while another check is running."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import config
from app.screen_jobs import pid_alive
from portfolio import store

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_alerts.py"


def running(db_path=None) -> store.CheckRow | None:
    """The latest check if it is still running (its process alive)."""
    row = store.latest_check(db_path)
    if row and row.status == store.RUNNING:
        if row.pid is None and row.started_at and datetime.now() - row.started_at < timedelta(seconds=config.ALERT_CHECK_LAUNCH_GRACE_SECONDS):
            return row  # just launched; the script records its pid when it starts
        if pid_alive(row.pid):
            return row
    return None


def due(db_path=None, now: datetime | None = None) -> bool:
    row = store.latest_check(db_path)
    if row is None:
        return True
    if running(db_path):
        return False
    return (now or datetime.now()) - row.started_at >= timedelta(minutes=config.ALERT_CHECK_MIN_INTERVAL_MINUTES)


def launch(source: str = "app_start", tickers: list[str] | None = None, db_path=None) -> int:
    db_path = db_path or config.RUNS_DB_PATH
    if running(db_path):
        raise RuntimeError("an alert check is already running")
    log = Path(config.ALERT_LOG_DIR) / f"alerts_{datetime.now():%Y%m%d_%H%M%S}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    check_id = store.start_check(source, log_path=str(log), path=db_path)
    cmd = [sys.executable, str(SCRIPT), "--db", str(db_path), "--source", source, "--check-id", str(check_id),
           "--log", str(log)]
    if tickers:
        cmd += ["--tickers", ",".join(tickers)]
    proc = subprocess.Popen(cmd, cwd=ROOT, start_new_session=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, env=os.environ.copy())
    store.set_check_pid(check_id, proc.pid, db_path)
    return check_id


def anything_to_check(db_path=None) -> bool:
    from portfolio.alerts import watchlist_tickers

    stocks = [h for h in store.list_holdings(path=db_path) if not h.is_cash]
    return bool(stocks or store.watch_levels(db_path) or watchlist_tickers())


def maybe_start(db_path=None) -> bool:
    """Launch the app-start check when one is due and there is something to check; True when launched."""
    if not due(db_path) or not anything_to_check(db_path):
        return False
    launch("app_start", db_path=db_path)
    return True
