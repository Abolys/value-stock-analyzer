"""LLM call log and analysis runs (CLAUDE.md Rule 4: every call records its
tokens and estimated cost with the run; cache hits are logged at zero cost).

Paths default to config.RUNS_DB_PATH read at call time, so tests and the app
can redirect the database.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel

import config
from storage.db import connect


class LLMCallRecord(BaseModel):
    ticker: str
    lens: str
    model: str
    prompt_version: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    cache_hit: bool = False
    attempt: int = 1
    outcome: str = "ok"
    analysis_id: int | None = None
    created_at: datetime | None = None
    backend: str = "api"  # api | claude_code
    list_price_cost: float = 0.0  # estimated at API list prices (what the call would cost on the API)


def _db(path: Path | str | None) -> Path | str:
    return path if path is not None else config.RUNS_DB_PATH


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_call(rec: LLMCallRecord, path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute(
            "INSERT INTO llm_calls (analysis_id, ticker, lens, model, prompt_version, input_tokens, output_tokens, "
            "cost, cache_hit, attempt, outcome, created_at, backend, list_price_cost) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.analysis_id, rec.ticker, rec.lens, rec.model, rec.prompt_version, rec.input_tokens,
             rec.output_tokens, rec.cost, int(rec.cache_hit), rec.attempt, rec.outcome,
             (rec.created_at or datetime.now()).isoformat(timespec="seconds"), rec.backend, rec.list_price_cost))


def calls(analysis_id: int | None = None, path: Path | str | None = None) -> list[LLMCallRecord]:
    q = "SELECT analysis_id, ticker, lens, model, prompt_version, input_tokens, output_tokens, cost, cache_hit, " \
        "attempt, outcome, created_at, backend, list_price_cost FROM llm_calls"
    args: tuple = ()
    if analysis_id is not None:
        q, args = q + " WHERE analysis_id = ?", (analysis_id,)
    with connect(_db(path)) as conn:
        rows = conn.execute(q + " ORDER BY call_id", args).fetchall()
    return [LLMCallRecord(analysis_id=r[0], ticker=r[1], lens=r[2], model=r[3], prompt_version=r[4],
                          input_tokens=r[5], output_tokens=r[6], cost=r[7], cache_hit=bool(r[8]), attempt=r[9],
                          outcome=r[10], created_at=datetime.fromisoformat(r[11]), backend=r[12],
                          list_price_cost=r[13]) for r in rows]


def month_spend(today: date | None = None, path: Path | str | None = None) -> tuple[float, int, int]:
    """(cost, calls, cache hits) for the calendar month containing `today`."""
    today = today or date.today()
    prefix = today.strftime("%Y-%m")
    with connect(_db(path)) as conn:
        cost, n, hits = conn.execute(
            "SELECT COALESCE(SUM(cost), 0), COUNT(*), COALESCE(SUM(cache_hit), 0) FROM llm_calls "
            "WHERE substr(created_at, 1, 7) = ?", (prefix,)).fetchone()
    return float(cost), int(n), int(hits)


def month_claude_code(today: date | None = None, path: Path | str | None = None) -> tuple[int, float]:
    """(calls, list-price equivalent) for Claude Code subscription calls in the calendar month (cache hits excluded)."""
    today = today or date.today()
    with connect(_db(path)) as conn:
        n, est = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(list_price_cost), 0) FROM llm_calls WHERE backend = 'claude_code' "
            "AND cache_hit = 0 AND substr(created_at, 1, 7) = ?", (today.strftime("%Y-%m"),)).fetchone()
    return int(n), float(est)


def create_analysis(ticker: str, path: Path | str | None = None) -> int:
    with connect(_db(path)) as conn:
        cur = conn.execute("INSERT INTO analysis_runs (ticker, created_at) VALUES (?, ?)", (ticker, _now()))
        return int(cur.lastrowid)


def finish_analysis(analysis_id: int, aggregate_score: float | None, verdict: str, result_json: str,
                    path: Path | str | None = None) -> float:
    """Store the result and return the analysis's total LLM cost (summed from its logged calls)."""
    with connect(_db(path)) as conn:
        total = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM llm_calls WHERE analysis_id = ?",
                             (analysis_id,)).fetchone()[0]
        conn.execute("UPDATE analysis_runs SET finished_at = ?, aggregate_score = ?, verdict = ?, total_cost = ?, "
                     "result_json = ? WHERE analysis_id = ?",
                     (_now(), aggregate_score, verdict, total, result_json, analysis_id))
    return float(total)


def analysis_cost(analysis_id: int, path: Path | str | None = None) -> float:
    with connect(_db(path)) as conn:
        return float(conn.execute("SELECT COALESCE(SUM(cost), 0) FROM llm_calls WHERE analysis_id = ?",
                                  (analysis_id,)).fetchone()[0])
