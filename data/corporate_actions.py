"""Corporate-action breaks (bankruptcy emergence, mergers into a new equity).

yfinance records splits but not these. Heuristic: a single-day price move of at
least CORP_ACTION_PRICE_GAP plus a reported share-count change of at least
CORP_ACTION_SHARE_CHANGE within CORP_ACTION_SHARE_WINDOW_DAYS, not explained by
a recorded split. Merged with the manual list in /data/corporate_actions.csv.
No drawdown, rolling high or share-count trend may be computed across a break.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from pydantic import BaseModel, Field

import config

COLUMNS = ["ticker", "date", "type", "note"]


class Break(BaseModel):
    date: date
    type: str
    note: str = ""
    sources: list[str] = Field(default_factory=list)  # "manual" and/or "heuristic"


def load_manual_breaks(ticker: str, path: Path = config.CORPORATE_ACTIONS_CSV) -> list[Break]:
    if not Path(path).exists():
        return []
    with open(path, newline="") as f:
        return [Break(date=date.fromisoformat(r["date"].strip()), type=r.get("type", ""), note=r.get("note", ""),
                      sources=["manual"])
                for r in csv.DictReader(f) if (r.get("ticker") or "").strip().upper() == ticker.upper()]


def _split_explains(d: pd.Timestamp, ratio_observed: float, splits: pd.Series | None, window: timedelta) -> bool:
    if splits is None or len(splits) == 0:
        return False
    near = splits[(splits.index >= d - window) & (splits.index <= d + window)]
    tol = config.SPLIT_MATCH_TOLERANCE
    return any(abs(ratio_observed / r - 1) <= tol for r in near if r > 0)


def detect_breaks(prices: pd.Series, shares: pd.Series | None, splits: pd.Series | None = None) -> list[Break]:
    """Heuristic breaks from actual (split-adjusted, not dividend-adjusted) closes and raw share counts."""
    if prices is None or len(prices) < 2 or shares is None or len(shares) == 0:
        return []
    window = timedelta(days=config.CORP_ACTION_SHARE_WINDOW_DAYS)
    moves = prices.pct_change().abs()
    breaks: list[Break] = []
    for d, move in moves[moves >= config.CORP_ACTION_PRICE_GAP].items():
        before = shares[(shares.index >= d - window) & (shares.index < d)]
        after = shares[(shares.index > d) & (shares.index <= d + window)]
        if before.empty or after.empty or before.iloc[-1] <= 0:
            continue
        ratio = float(after.median() / before.median())
        if abs(ratio - 1) < config.CORP_ACTION_SHARE_CHANGE:
            continue
        if _split_explains(d, ratio, splits, window):
            continue
        breaks.append(Break(date=d.date(), type="heuristic",
                            note=f"price move {move:+.0%} with share count ×{ratio:.2f}", sources=["heuristic"]))
    return breaks


def merge_breaks(heuristic: list[Break], manual: list[Break]) -> list[Break]:
    """Manual dates win; a heuristic break within the window of a manual one is the same event."""
    window = timedelta(days=config.CORP_ACTION_SHARE_WINDOW_DAYS)
    merged = [b.model_copy(deep=True) for b in manual]
    for h in heuristic:
        match = next((m for m in merged if abs(m.date - h.date) <= window), None)
        if match:
            if "heuristic" not in match.sources:
                match.sources.append("heuristic")
        else:
            merged.append(h)
    return sorted(merged, key=lambda b: b.date)


def corporate_action_breaks(ticker: str, prices: pd.Series, shares: pd.Series | None,
                            splits: pd.Series | None = None,
                            manual_path: Path = config.CORPORATE_ACTIONS_CSV) -> list[Break]:
    return merge_breaks(detect_breaks(prices, shares, splits), load_manual_breaks(ticker, manual_path))


def break_dates(breaks: list[Break]) -> list[date]:
    return [b.date for b in breaks]
