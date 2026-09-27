"""SQLite app database (runs.db). Phase 1 created officer_snapshots; Phase 2
adds the screen_runs / screen_results / screen_divergences tables (helpers in
storage/screen_store.py); Phase 3 adds analysis_runs and llm_calls (helpers in
storage/llm_store.py); Phase 5 adds the run-history columns on analysis_runs
(helpers in storage/history.py); Phase 6 adds the portfolio,
journal and alert tables (helpers in portfolio/store.py).
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
CREATE TABLE IF NOT EXISTS analysis_runs (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    aggregate_score REAL,
    verdict TEXT,
    total_cost REAL DEFAULT 0,
    result_json TEXT              -- AnalysisRun model dump
);
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER,          -- NULL for calls outside an analysis (e.g. a 6-K check in a screen)
    ticker TEXT NOT NULL,
    lens TEXT NOT NULL,           -- moat | devils_advocate | departure
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost REAL NOT NULL,           -- billed API cost ($0 on the Claude Code subscription backend)
    cache_hit INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    outcome TEXT NOT NULL,        -- ok | invalid: <reason> | error: <reason>
    created_at TEXT NOT NULL,
    backend TEXT NOT NULL DEFAULT 'api',     -- api | claude_code
    list_price_cost REAL NOT NULL DEFAULT 0  -- estimated cost at API list prices
);
-- Phase 6: portfolio, thesis journal and alerts (helpers in portfolio/store.py)
CREATE TABLE IF NOT EXISTS holdings (
    holding_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    account TEXT NOT NULL,             -- free-text label, e.g. "TFSA"
    currency TEXT NOT NULL,            -- currency the transactions are in
    created_at TEXT NOT NULL,
    snapshot_analysis_id INTEGER,      -- the full analysis run frozen at purchase
    snapshot_json TEXT,                -- flattened metrics at purchase (portfolio/metrics.py)
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transactions (
    txn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    holding_id INTEGER NOT NULL,
    txn_date TEXT NOT NULL,
    side TEXT NOT NULL,                -- buy | sell
    shares REAL NOT NULL,
    price REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    note TEXT
);
CREATE TABLE IF NOT EXISTS theses (
    thesis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    holding_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    intrinsic_value REAL,
    buy_below_price REAL,
    target_price REAL,
    basis TEXT                         -- where the intrinsic value came from
);
CREATE TABLE IF NOT EXISTS thesis_reasons (
    reason_id INTEGER PRIMARY KEY AUTOINCREMENT,
    thesis_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    still_holds INTEGER,               -- NULL = not reviewed yet
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS thesis_triggers (
    trigger_id INTEGER PRIMARY KEY AUTOINCREMENT,
    thesis_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    op TEXT NOT NULL,
    value_json TEXT NOT NULL,          -- {"literal": x} or {"ref": "target_price"}
    created_at TEXT NOT NULL,
    fired_at TEXT                      -- last time it fired (alerted)
);
CREATE TABLE IF NOT EXISTS journal_entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    holding_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,                -- note | trigger | alert | reason
    text TEXT NOT NULL,
    alert_id INTEGER
);
CREATE TABLE IF NOT EXISTS watch_levels (
    ticker TEXT PRIMARY KEY,
    buy_below_price REAL,
    target_price REAL,
    basis TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    holding_id INTEGER,
    kind TEXT NOT NULL,                -- a config.ALERT_KINDS key
    event_key TEXT NOT NULL,           -- what makes this event unique (fires once per event)
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,              -- screen | app_start | manual
    read_at TEXT,
    email_status TEXT,
    UNIQUE (ticker, kind, event_key)
);
CREATE TABLE IF NOT EXISTS monitor_state (
    ticker TEXT NOT NULL,
    key TEXT NOT NULL,                 -- e.g. fundamentals_as_of, piotroski_baseline, active:<rule>
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (ticker, key)
);
CREATE TABLE IF NOT EXISTS alert_checks (
    check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    source TEXT NOT NULL,
    status TEXT NOT NULL,              -- running | completed | failed: <reason>
    tickers INTEGER DEFAULT 0,
    fired INTEGER DEFAULT 0,
    errors TEXT,                       -- JSON {ticker: reason}
    email_status TEXT,
    pid INTEGER,
    log_path TEXT
);
"""


# Columns added after a table was first created: (table, column, definition).
MIGRATIONS = [
    ("llm_calls", "backend", "TEXT NOT NULL DEFAULT 'api'"),
    ("llm_calls", "list_price_cost", "REAL NOT NULL DEFAULT 0"),
    # Phase 5 run history: one row per analysis with its scores, estimate and episode.
    ("analysis_runs", "input_hash", "TEXT"),
    ("analysis_runs", "quant_score", "REAL"),
    ("analysis_runs", "macro_score", "REAL"),
    ("analysis_runs", "moat_score", "REAL"),
    ("analysis_runs", "da_score", "REAL"),
    ("analysis_runs", "lenses_used", "INTEGER"),
    ("analysis_runs", "turnaround_status", "TEXT"),
    ("analysis_runs", "turnaround_median", "REAL"),
    ("analysis_runs", "turnaround_p25", "REAL"),
    ("analysis_runs", "turnaround_p75", "REAL"),
    ("analysis_runs", "turnaround_confidence", "TEXT"),
    ("analysis_runs", "episode_key", "TEXT"),  # ticker:segment start:52-week-high date of the current drop
    ("analysis_runs", "episode_type", "TEXT"),
    ("analysis_runs", "episode_high_date", "TEXT"),
    ("analysis_runs", "input_tokens", "INTEGER DEFAULT 0"),
    ("analysis_runs", "output_tokens", "INTEGER DEFAULT 0"),
]


def connect(path: Path | str = config.RUNS_DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    for table, column, definition in MIGRATIONS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
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
