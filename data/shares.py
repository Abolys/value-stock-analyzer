"""Adjusted share-count history (SPEC "Data sources": share-count history).

1. Adjust for splits and reverse splits, so a 1-for-10 reverse split is never
   read as a 90% buyback. yfinance's get_shares_full() is NOT split-adjusted
   (verified on LCID 2025-09 1:10), while annual diluted shares are restated;
   so a series is treated as unadjusted only when it jumps by the split ratio
   at the split date.
2. Cut the series at corporate-action breaks and compute the trend only on
   the latest segment, stating its span.
3. A single-period change above CORP_ACTION_SHARE_CHANGE not explained by a
   split is flagged and the trend is not scored.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from pydantic import BaseModel, Field

import config
from data.values import NA_INCOMPLETE, OK


class ShareTrend(BaseModel):
    trend_per_year: float | None = None  # annualised change; negative = shrinking
    status: str = OK
    span_start: date | None = None
    span_end: date | None = None
    segment_start: date | None = None  # the break the latest segment starts after
    points: int = 0
    flags: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    source: str = ""

    @property
    def span_label(self) -> str:
        if not self.span_start or not self.span_end:
            return ""
        years = (self.span_end - self.span_start).days / 365.25
        return f"{self.span_start.isoformat()} to {self.span_end.isoformat()} ({years:.1f} yrs)"


def quarterly(series: pd.Series) -> pd.Series:
    """Quarter-end medians (daily share data from yfinance contains stray spikes)."""
    s = series.dropna()
    s = s[s > 0]
    if s.empty:
        return s
    return s.resample("QE").median().dropna()


def adjust_for_splits(series: pd.Series, splits: pd.Series | None) -> tuple[pd.Series, list[str]]:
    """Restate pre-split values into current shares when the series is unadjusted."""
    s = series.sort_index().astype(float).copy()
    notes: list[str] = []
    if splits is None or len(splits) == 0 or s.empty:
        return s, notes
    tol = config.SPLIT_MATCH_TOLERANCE
    window = pd.Timedelta(days=config.CORP_ACTION_SHARE_WINDOW_DAYS)
    for d, ratio in splits.sort_index().items():
        if ratio <= 0 or ratio == 1:
            continue
        before = s[(s.index < d) & (s.index >= d - window)]
        after = s[(s.index >= d) & (s.index <= d + window)]
        if before.empty or after.empty:
            notes.append(f"split {ratio:g} on {d.date()}: no data around it; series assumed already adjusted")
            continue
        observed = float(after.iloc[0] / before.iloc[-1])
        if abs(observed / ratio - 1) <= tol:
            s.loc[s.index < d] *= ratio
            notes.append(f"adjusted for {_split_label(ratio)} split on {d.date()}")
        else:
            notes.append(f"{_split_label(ratio)} split on {d.date()} already reflected in the series")
    return s, notes


def _split_label(ratio: float) -> str:
    return f"{ratio:g}-for-1" if ratio >= 1 else f"1-for-{1 / ratio:g} reverse"


def _split_explains(prev_d, cur_d, change_ratio: float, splits: pd.Series | None) -> bool:
    if splits is None or len(splits) == 0:
        return False
    within = splits[(splits.index > prev_d) & (splits.index <= cur_d)]
    return any(abs(change_ratio / r - 1) <= config.SPLIT_MATCH_TOLERANCE for r in within if r > 0)


def share_trend(raw: pd.Series | None, splits: pd.Series | None, breaks: list[date],
                source: str = "", resample: bool = True) -> ShareTrend:
    """Annualised share-count change over the latest segment after the last break."""
    if raw is None or len(raw) == 0:
        return ShareTrend(status=NA_INCOMPLETE, source=source, notes=["no share-count history"])
    s = pd.Series(raw).copy()
    s.index = pd.DatetimeIndex(s.index)
    s = s.dropna()
    if s.index.tz is not None:
        s.index = s.index.tz_localize(None)
    adjusted, notes = adjust_for_splits(s, splits)
    series = quarterly(adjusted) if resample else adjusted[adjusted > 0].sort_index()
    seg_start = None
    past_breaks = [b for b in sorted(breaks) if pd.Timestamp(b) <= series.index.max()] if len(series) else []
    if past_breaks:
        seg_start = past_breaks[-1]
        series = series[series.index >= pd.Timestamp(seg_start)]
        notes.append(f"series cut at corporate-action break {seg_start.isoformat()}; trend uses the latest segment only")
    result = ShareTrend(source=source, notes=notes, segment_start=seg_start, points=len(series))
    if len(series) < 2:
        result.status = f"{NA_INCOMPLETE} (fewer than 2 share-count points in the latest segment)"
        return result
    result.span_start, result.span_end = series.index[0].date(), series.index[-1].date()
    for (d0, v0), (d1, v1) in zip(series.items(), list(series.items())[1:]):
        change = v1 / v0
        if abs(change - 1) > config.CORP_ACTION_SHARE_CHANGE and not _split_explains(d0, d1, change, splits):
            result.flags.append(f"unexplained share-count jump {change - 1:+.0%} between {d0.date()} and {d1.date()}")
    years = (series.index[-1] - series.index[0]).days / 365.25
    if years <= 0:
        result.status = f"{NA_INCOMPLETE} (zero-length span)"
        return result
    trend = (series.iloc[-1] / series.iloc[0]) ** (1 / years) - 1
    if result.flags:
        result.status = "N/A - unexplained share-count jump; not scored"
        result.notes.append(f"raw annualised change {trend:+.1%}/yr shown for reference only")
        return result
    result.trend_per_year = float(trend)
    return result


def dilution_flag(trend: ShareTrend) -> bool:
    return trend.trend_per_year is not None and trend.trend_per_year > config.DILUTION_FLAG_PER_YEAR

