"""Analysis run history and turnaround-estimate scoring (SPEC "Turnaround estimate
integrity"; config ESTIMATE_SCORING_EDGE).

Only the first estimate made during each drawdown episode is scored, measured from
that run's date; later runs in the same episode are kept but marked "same episode",
so one drop can't be counted many times. An estimate's window runs from the run
date to run date + ESTIMATE_SCORING_EDGE of its range (p75 or median months).

- recovered: a close after the run date, inside the window, back within
  RECOVERY_BAND of the episode's prior high;
- missed: the window ended first;
- not yet: the window is still open.

The prior high is re-read from the current adjusted series at the episode's
52-week-high date, since adjusted closes are revised as dividends are paid.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field

import config
from storage.db import connect

RECOVERED, NOT_YET, MISSED = "recovered", "not yet", "missed"
SAME_EPISODE = "same episode"
NO_ESTIMATE = "no estimate"
UNSCORABLE = "unscorable"
EDGE_FIELDS = {"p75": "turnaround_p75", "median": "turnaround_median"}

_COLS = ["analysis_id", "ticker", "created_at", "finished_at", "aggregate_score", "verdict", "total_cost",
         "input_hash", "quant_score", "macro_score", "moat_score", "da_score", "lenses_used", "turnaround_status",
         "turnaround_median", "turnaround_p25", "turnaround_p75", "turnaround_confidence", "episode_key",
         "episode_type", "episode_high_date", "input_tokens", "output_tokens"]


class HistoryRow(BaseModel):
    analysis_id: int
    ticker: str
    created_at: datetime
    finished_at: datetime | None = None
    aggregate_score: float | None = None
    verdict: str | None = None
    total_cost: float = 0.0
    input_hash: str | None = None
    quant_score: float | None = None
    macro_score: float | None = None
    moat_score: float | None = None
    da_score: float | None = None
    lenses_used: int | None = None
    turnaround_status: str | None = None
    turnaround_median: float | None = None
    turnaround_p25: float | None = None
    turnaround_p75: float | None = None
    turnaround_confidence: str | None = None
    episode_key: str | None = None
    episode_type: str | None = None
    episode_high_date: date | None = None
    input_tokens: int | None = 0
    output_tokens: int | None = 0

    @property
    def run_date(self) -> date:
        return self.created_at.date()

    @property
    def has_estimate(self) -> bool:
        return self.turnaround_p25 is not None and self.turnaround_p75 is not None

    @property
    def window_months(self) -> float | None:
        return getattr(self, EDGE_FIELDS[config.ESTIMATE_SCORING_EDGE])


class ScoredEstimate(BaseModel):
    row: HistoryRow
    status: str  # recovered | not yet | missed | same episode | no estimate | unscorable
    scored: bool = False  # first estimate of its episode
    window_end: date | None = None
    recovered_on: date | None = None
    target: float | None = None
    detail: str = ""


class TypeAccuracy(BaseModel):
    episode_type: str
    recovered: int = 0
    waiting: int = 0
    missed: int = 0
    unscorable: int = 0

    @property
    def scored(self) -> int:
        return self.recovered + self.waiting + self.missed


class AccuracySummary(BaseModel):
    by_type: list[TypeAccuracy] = Field(default_factory=list)
    total_scored: int = 0
    enough: bool = False
    notes: list[str] = Field(default_factory=list)


def _db(path: Path | str | None) -> Path | str:
    return path if path is not None else config.RUNS_DB_PATH


def _load(where: str, args: tuple, path: Path | str | None) -> list[HistoryRow]:
    with connect(_db(path)) as conn:
        rows = conn.execute(f"SELECT {', '.join(_COLS)} FROM analysis_runs WHERE finished_at IS NOT NULL {where} "
                            "ORDER BY created_at, analysis_id", args).fetchall()
    out = []
    for r in rows:
        d = dict(zip(_COLS, r))
        d["total_cost"] = d["total_cost"] or 0.0
        out.append(HistoryRow(**d))
    return out


def load_history(ticker: str, path: Path | str | None = None) -> list[HistoryRow]:
    return _load("AND ticker = ?", (ticker.upper(),), path)


def load_all_estimates(path: Path | str | None = None) -> list[HistoryRow]:
    """Every finished run that carries a turnaround range, across all tickers."""
    return _load("AND turnaround_p75 IS NOT NULL", (), path)


def window_end(row: HistoryRow) -> date | None:
    months = row.window_months
    if months is None:
        return None
    return row.run_date + timedelta(days=round(months * config.DAYS_PER_MONTH))


def _score_one(row: HistoryRow, closes: pd.Series | None, today: date) -> ScoredEstimate:
    end = window_end(row)
    out = ScoredEstimate(row=row, status=NOT_YET, scored=True, window_end=end)
    if closes is None or closes.empty or row.episode_high_date is None:
        out.status, out.detail = UNSCORABLE, "price history unavailable" if row.episode_high_date else "no episode high"
        return out
    closes = closes.dropna().sort_index()
    high = closes.asof(pd.Timestamp(row.episode_high_date))
    if pd.isna(high):
        out.status, out.detail = UNSCORABLE, f"no close on or before the episode high ({row.episode_high_date})"
        return out
    out.target = float((1 - config.RECOVERY_BAND) * high)
    after = closes[(closes.index > pd.Timestamp(row.run_date)) & (closes.index <= pd.Timestamp(end))]
    hit = after[after >= out.target]
    if len(hit):
        out.status, out.recovered_on = RECOVERED, hit.index[0].date()
        out.detail = f"back within {config.RECOVERY_BAND:.0%} of the prior high on {out.recovered_on}"
    elif today > end:
        out.status, out.detail = MISSED, f"window ended {end} without a recovery"
    else:
        out.detail = f"window open until {end}"
    return out


def score_estimates(rows: list[HistoryRow], closes_by_ticker: dict[str, pd.Series | None],
                    today: date) -> list[ScoredEstimate]:
    """One ScoredEstimate per row, in run order; only the first estimate per episode is scored."""
    seen: set[str] = set()
    out = []
    for row in sorted(rows, key=lambda r: (r.created_at, r.analysis_id)):
        if not row.has_estimate:
            out.append(ScoredEstimate(row=row, status=NO_ESTIMATE, detail=row.turnaround_status or "no turnaround"))
            continue
        key = row.episode_key or f"{row.ticker}:run{row.analysis_id}"
        if key in seen:
            out.append(ScoredEstimate(row=row, status=SAME_EPISODE, window_end=window_end(row),
                                      detail="an earlier run already estimated this drop"))
            continue
        seen.add(key)
        out.append(_score_one(row, closes_by_ticker.get(row.ticker), today))
    return out


def accuracy_summary(scored: list[ScoredEstimate]) -> AccuracySummary:
    by_type: dict[str, TypeAccuracy] = {}
    for s in scored:
        if not s.scored:
            continue
        typ = s.row.episode_type or "unclassified"
        acc = by_type.setdefault(typ, TypeAccuracy(episode_type=typ))
        if s.status == RECOVERED:
            acc.recovered += 1
        elif s.status == MISSED:
            acc.missed += 1
        elif s.status == NOT_YET:
            acc.waiting += 1
        else:
            acc.unscorable += 1
    order = {"market-driven": 0, "company-specific": 1}
    types = sorted(by_type.values(), key=lambda a: (order.get(a.episode_type, 2), a.episode_type))
    total = sum(a.scored for a in types)
    summary = AccuracySummary(by_type=types, total_scored=total, enough=total >= config.ACCURACY_MIN_SCORED)
    unscorable = sum(a.unscorable for a in types)
    if unscorable:
        summary.notes.append(f"{unscorable} first estimate(s) could not be scored (price history unavailable); "
                             "not counted")
    return summary
