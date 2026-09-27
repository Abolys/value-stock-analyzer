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


def finish_analysis(analysis_id: int, run, path: Path | str | None = None) -> float:
    """Store the finished AnalysisRun (scores, turnaround estimate, drawdown episode, tokens and
    the full JSON) and return its total LLM cost (summed from its logged calls)."""
    agg = run.aggregate
    scores = {n: (lens.score if lens is not None and lens.ok else None)
              for n, lens in ((n, run.lens(n)) for n in ("quant", "macro", "moat", "devils_advocate"))}
    t = run.turnaround
    cur = t.current if t is not None else None
    episode_key = episode_type = episode_high = None
    if cur is not None and cur.qualifying:
        seg_start = t.segments[-1].start.isoformat() if t.segments else ""
        episode_key = f"{run.ticker}:{seg_start}:{cur.high_date.isoformat()}"
        episode_type, episode_high = cur.episode_type, cur.high_date.isoformat()
    iqr = t.iqr_months if t is not None else None
    with connect(_db(path)) as conn:
        total, tin, tout = conn.execute(
            "SELECT COALESCE(SUM(cost), 0), COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
            "FROM llm_calls WHERE analysis_id = ?", (analysis_id,)).fetchone()
        conn.execute(
            "UPDATE analysis_runs SET finished_at = ?, aggregate_score = ?, verdict = ?, total_cost = ?, "
            "result_json = ?, input_hash = ?, quant_score = ?, macro_score = ?, moat_score = ?, da_score = ?, "
            "lenses_used = ?, turnaround_status = ?, turnaround_median = ?, turnaround_p25 = ?, turnaround_p75 = ?, "
            "turnaround_confidence = ?, episode_key = ?, episode_type = ?, episode_high_date = ?, input_tokens = ?, "
            "output_tokens = ? WHERE analysis_id = ?",
            (_now(), agg.score if agg else None, agg.verdict if agg else "failed to load", total,
             run.model_dump_json(), run.input_hash or None, scores["quant"], scores["macro"], scores["moat"],
             scores["devils_advocate"], agg.lenses_used if agg else 0, t.status if t else None,
             t.median_months if t else None, iqr[0] if iqr else None, iqr[1] if iqr else None,
             t.confidence if t else None, episode_key, episode_type, episode_high, tin, tout, analysis_id))
    return float(total)


def month_analyses(today: date | None = None, path: Path | str | None = None) -> int:
    """Analyses with at least one logged LLM call in the calendar month (the runs behind the spend)."""
    today = today or date.today()
    with connect(_db(path)) as conn:
        return int(conn.execute(
            "SELECT COUNT(DISTINCT analysis_id) FROM llm_calls WHERE analysis_id IS NOT NULL "
            "AND substr(created_at, 1, 7) = ?", (today.strftime("%Y-%m"),)).fetchone()[0])


def analysis_cost(analysis_id: int, path: Path | str | None = None) -> float:
    with connect(_db(path)) as conn:
        return float(conn.execute("SELECT COALESCE(SUM(cost), 0) FROM llm_calls WHERE analysis_id = ?",
                                  (analysis_id,)).fetchone()[0])
