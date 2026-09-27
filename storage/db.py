"""SQLite app database (runs.db). Phase 1 created officer_snapshots; Phase 2
adds the screen_runs / screen_results / screen_divergences tables (helpers in
storage/screen_store.py); later phases add run history and portfolio tables.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS officer_snapshots (
    ticker TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    ceo TEXT NOT NULL,          -- JSON list of names
    cfo TEXT NOT NULL,          -- JSON list of names
    officers_json TEXT NOT NULL,
    source TEXT,
    PRIMARY KEY (ticker, snapshot_date)
);
CREATE TABLE IF NOT EXISTS screen_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    lists TEXT NOT NULL,              -- JSON list of universe list keys
    status TEXT NOT NULL,             -- running | completed | blocked: health check failed | stopped: source failing
    health_failures TEXT,             -- JSON list
    total INTEGER DEFAULT 0,
    attempted INTEGER DEFAULT 0,
    passed_stage1 INTEGER DEFAULT 0,
    passed_stage2 INTEGER DEFAULT 0,
    failed_to_load INTEGER DEFAULT 0,
    refetched_reported INTEGER DEFAULT 0,
    served_from_cache INTEGER DEFAULT 0,
    field_na TEXT,                    -- JSON {field: {"na": n, "of": m}}
    flagged_fields TEXT,              -- JSON list of "likely renamed upstream" fields
    pid INTEGER,
    log_path TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS screen_results (
    run_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    sources TEXT,
    status TEXT NOT NULL,
    decided_at_stage INTEGER,
    load_error TEXT,
    quality REAL,
    earnings_yield REAL,
    stale INTEGER,
    fundamentals_as_of TEXT,
    result_json TEXT NOT NULL,
    written_at TEXT NOT NULL,
    PRIMARY KEY (run_id, ticker)
);
CREATE TABLE IF NOT EXISTS screen_divergences (
    run_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    metric TEXT NOT NULL,
    stage1 REAL,
    stage2 REAL,
    rel_diff REAL
);
"""


def connect(path: Path | str = config.RUNS_DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


class OfficerSnapshot(BaseModel):
    ticker: str
    snapshot_date: date
    ceo: list[str] = Field(default_factory=list)
    cfo: list[str] = Field(default_factory=list)
    officers: list[dict[str, Any]] = Field(default_factory=list)
    source: str | None = None


def _norm_name(name: str) -> str:
    return " ".join(name.split())


def officers_by_role(officers: list[dict[str, Any]]) -> dict[str, list[str]]:
    """CEO/CFO names from a yfinance companyOfficers list, matched by title terms."""
    out: dict[str, list[str]] = {role: [] for role in config.OFFICER_ROLE_TITLE_TERMS}
    for o in officers or []:
        title = f" {(o.get('title') or '').lower()} "
        for role, terms in config.OFFICER_ROLE_TITLE_TERMS.items():
            if any(_title_has(title, t) for t in terms):
                name = _norm_name(o.get("name") or "")
                if name and name not in out[role]:
                    out[role].append(name)
    return {k: sorted(v) for k, v in out.items()}


def _title_has(title: str, term: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", title) is not None


def save_officer_snapshot(ticker: str, info: Any, when: date | None = None,
                          path: Path | str = config.RUNS_DB_PATH) -> OfficerSnapshot:
    """Store the CEO/CFO names from an already-fetched info (InfoResult or raw dict).

    Called by stage 1 of the screener (Phase 2) on every screened ticker and by
    manual analyses, so snapshots cost no extra requests.
    """
    raw = info.raw if hasattr(info, "raw") else (info or {})
    officers = raw.get("companyOfficers") or []
    roles = officers_by_role(officers)
    snap = OfficerSnapshot(ticker=ticker.upper(), snapshot_date=when or date.today(), ceo=roles["CEO"],
                           cfo=roles["CFO"], officers=officers, source=getattr(info, "provider", None))
    with connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO officer_snapshots VALUES (?,?,?,?,?,?)",
                     (snap.ticker, snap.snapshot_date.isoformat(), json.dumps(snap.ceo), json.dumps(snap.cfo),
                      json.dumps(officers, default=str), snap.source))
    return snap


def get_officer_snapshots(ticker: str, path: Path | str = config.RUNS_DB_PATH) -> list[OfficerSnapshot]:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT ticker, snapshot_date, ceo, cfo, officers_json, source FROM officer_snapshots "
            "WHERE ticker=? ORDER BY snapshot_date", (ticker.upper(),)).fetchall()
    return [OfficerSnapshot(ticker=r[0], snapshot_date=date.fromisoformat(r[1]), ceo=json.loads(r[2]),
                            cfo=json.loads(r[3]), officers=json.loads(r[4]), source=r[5]) for r in rows]


def snapshotted_tickers_on(day: date, path: Path | str = config.RUNS_DB_PATH) -> set[str]:
    with connect(path) as conn:
        rows = conn.execute("SELECT ticker FROM officer_snapshots WHERE snapshot_date=?",
                            (day.isoformat(),)).fetchall()
    return {r[0] for r in rows}
