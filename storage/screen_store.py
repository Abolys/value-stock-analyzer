"""Reads and writes for the screen_runs, screen_results and screen_divergences
tables (schema in storage/db.py). Every write commits, so an interrupted run
keeps every finished ticker and can continue with --resume."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

import config
from screening.models import STATUS_FAILED_TO_LOAD, STATUS_PASS, ScreenResult
from storage.db import connect

RUNNING = "running"
COMPLETED = "completed"
BLOCKED = "blocked: health check failed"
STOPPED = "stopped: source failing"
RESUMABLE = (RUNNING, STOPPED)


class ScreenRun(BaseModel):
    run_id: int
    started_at: datetime
    ended_at: datetime | None = None
    lists: list[str] = Field(default_factory=list)
    status: str
    health_failures: list[str] = Field(default_factory=list)
    total: int = 0
    attempted: int = 0
    passed_stage1: int = 0
    passed_stage2: int = 0
    failed_to_load: int = 0
    refetched_reported: int = 0
    served_from_cache: int = 0
    field_na: dict[str, dict[str, int]] = Field(default_factory=dict)
    flagged_fields: list[str] = Field(default_factory=list)
    pid: int | None = None
    log_path: str | None = None
    note: str | None = None


_COLS = ["run_id", "started_at", "ended_at", "lists", "status", "health_failures", "total", "attempted",
         "passed_stage1", "passed_stage2", "failed_to_load", "refetched_reported", "served_from_cache",
         "field_na", "flagged_fields", "pid", "log_path", "note"]
_JSON = {"lists": list, "health_failures": list, "field_na": dict, "flagged_fields": list}


def _row_to_run(row) -> ScreenRun:
    d = dict(zip(_COLS, row))
    for k, typ in _JSON.items():
        d[k] = json.loads(d[k]) if d[k] else typ()
    return ScreenRun(**d)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_run(lists: list[str], status: str = RUNNING, health_failures: list[str] | None = None,
               total: int = 0, pid: int | None = None, log_path: str | None = None,
               path: Path | str = config.RUNS_DB_PATH) -> int:
    ended = _now() if status != RUNNING else None
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO screen_runs (started_at, ended_at, lists, status, health_failures, total, pid, log_path) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (_now(), ended, json.dumps(lists), status, json.dumps(health_failures or []), total, pid, log_path))
        return int(cur.lastrowid)


def update_run(run_id: int, path: Path | str = config.RUNS_DB_PATH, **fields: Any) -> None:
    if not fields:
        return
    sets, vals = [], []
    for k, v in fields.items():
        if k not in _COLS or k == "run_id":
            raise KeyError(k)
        sets.append(f"{k}=?")
        vals.append(json.dumps(v) if k in _JSON else v)
    with connect(path) as conn:
        conn.execute(f"UPDATE screen_runs SET {', '.join(sets)} WHERE run_id=?", (*vals, run_id))


def get_run(run_id: int, path: Path | str = config.RUNS_DB_PATH) -> ScreenRun | None:
    with connect(path) as conn:
        row = conn.execute(f"SELECT {', '.join(_COLS)} FROM screen_runs WHERE run_id=?", (run_id,)).fetchone()
    return _row_to_run(row) if row else None


def list_runs(path: Path | str = config.RUNS_DB_PATH, limit: int = 20) -> list[ScreenRun]:
    with connect(path) as conn:
        rows = conn.execute(f"SELECT {', '.join(_COLS)} FROM screen_runs ORDER BY run_id DESC LIMIT ?",
                            (limit,)).fetchall()
    return [_row_to_run(r) for r in rows]


def latest_run(path: Path | str = config.RUNS_DB_PATH, statuses: tuple[str, ...] | None = None) -> ScreenRun | None:
    for run in list_runs(path, limit=1000):
        if statuses is None or run.status in statuses:
            return run
    return None


def latest_completed_run(path: Path | str = config.RUNS_DB_PATH) -> ScreenRun | None:
    return latest_run(path, (COMPLETED,))


def latest_resumable_run(path: Path | str = config.RUNS_DB_PATH) -> ScreenRun | None:
    return latest_run(path, RESUMABLE)


def write_result(run_id: int, r: ScreenResult, path: Path | str = config.RUNS_DB_PATH) -> None:
    ey = r.earnings_yield.value.value if r.earnings_yield and r.earnings_yield.value.ok else None
    with connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO screen_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, r.ticker, r.sources, r.status, r.decided_at_stage, r.load_error,
             r.quality.score if r.quality else None, ey, int(r.stale),
             r.fundamentals_as_of.isoformat() if r.fundamentals_as_of else None,
             r.model_dump_json(), _now()))
        conn.execute("DELETE FROM screen_divergences WHERE run_id=? AND ticker=?", (run_id, r.ticker))
        conn.executemany("INSERT INTO screen_divergences VALUES (?,?,?,?,?,?)",
                         [(run_id, r.ticker, d.metric, d.stage1, d.stage2, d.rel_diff) for d in r.divergences])


def finished_tickers(run_id: int, path: Path | str = config.RUNS_DB_PATH, include_failed: bool = True) -> set[str]:
    sql, params = "SELECT ticker FROM screen_results WHERE run_id=?", [run_id]
    if not include_failed:
        sql += " AND status != ?"
        params.append(STATUS_FAILED_TO_LOAD)
    with connect(path) as conn:
        return {row[0] for row in conn.execute(sql, params)}


def load_results(run_id: int, path: Path | str = config.RUNS_DB_PATH) -> list[ScreenResult]:
    with connect(path) as conn:
        rows = conn.execute("SELECT result_json FROM screen_results WHERE run_id=? ORDER BY ticker",
                            (run_id,)).fetchall()
    return [ScreenResult.model_validate_json(r[0]) for r in rows]


def load_divergences(run_id: int, path: Path | str = config.RUNS_DB_PATH) -> list[dict[str, Any]]:
    with connect(path) as conn:
        rows = conn.execute("SELECT ticker, metric, stage1, stage2, rel_diff FROM screen_divergences "
                            "WHERE run_id=? ORDER BY ticker", (run_id,)).fetchall()
    return [dict(zip(["ticker", "metric", "stage1", "stage2", "rel_diff"], r)) for r in rows]


class RunCounts(BaseModel):
    done: int = 0
    passed_stage1: int = 0
    passed_stage2: int = 0
    failed_to_load: int = 0


def count_results(run_id: int, path: Path | str = config.RUNS_DB_PATH) -> RunCounts:
    """Counts recomputed from the stored rows, so a resumed run's summary covers every session."""
    with connect(path) as conn:
        rows = conn.execute("SELECT status, decided_at_stage FROM screen_results WHERE run_id=?",
                            (run_id,)).fetchall()
    return RunCounts(done=len(rows),
                     passed_stage1=sum(1 for s, st in rows if st == 2),
                     passed_stage2=sum(1 for s, st in rows if s == STATUS_PASS),
                     failed_to_load=sum(1 for s, _ in rows if s == STATUS_FAILED_TO_LOAD))
