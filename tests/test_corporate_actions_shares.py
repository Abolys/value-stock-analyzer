from datetime import date

import numpy as np
import pandas as pd
import pytest

from data import corporate_actions as ca
from data import shares


def quarterly_series(values, start="2021-03-31"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="QE"), dtype=float)


def test_synthetic_reverse_split_is_not_a_share_reduction():
    # 1,000M shares, then a 1-for-10 reverse split: the raw series shows 100M.
    raw = quarterly_series([1000, 1000, 1000, 1000, 100, 100, 100, 100])
    splits = pd.Series([0.1], index=pd.to_datetime(["2022-02-15"]))
    t = shares.share_trend(raw, splits, breaks=[], resample=False)
    assert t.trend_per_year == pytest.approx(0.0, abs=1e-9)
    assert not t.flags and any("1-for-10 reverse" in n for n in t.notes)


def test_already_adjusted_series_left_alone():
    raw = quarterly_series([100, 101, 102, 103])
    splits = pd.Series([2.0], index=pd.to_datetime(["2021-08-15"]))
    t = shares.share_trend(raw, splits, breaks=[], resample=False)
    assert any("already reflected" in n for n in t.notes) and t.trend_per_year > 0


def test_unexplained_share_jump_is_flagged_not_scored():
    raw = quarterly_series([100, 100, 210, 210, 212])
    t = shares.share_trend(raw, None, breaks=[], resample=False)
    assert t.trend_per_year is None and t.flags and "unexplained" in t.flags[0]
    assert t.status.startswith("N/A")


def test_trend_uses_latest_segment_only():
    raw = quarterly_series([100, 100, 300, 290, 280, 270])
    t = shares.share_trend(raw, None, breaks=[date(2021, 7, 1)], resample=False)
    assert t.segment_start == date(2021, 7, 1) and t.span_start >= date(2021, 7, 1)
    assert t.trend_per_year < 0 and not t.flags


def test_lcid_real_reverse_split_adjusted(fx_provider):
    sh = fx_provider.get_shares_history("LCID")
    t = shares.share_trend(sh.series, fx_provider.get_splits("LCID"), [], sh.source)
    assert any("1-for-10 reverse" in n for n in t.notes)
    assert t.trend_per_year is not None and t.trend_per_year > 0  # dilution, not a 90% buyback
    assert shares.dilution_flag(t)


# ---------------------------------------------------------------- corporate actions
def test_heuristic_detects_gap_with_share_change_and_ignores_splits():
    idx = pd.bdate_range("2024-01-01", periods=200)
    px = pd.Series(np.r_[np.full(100, 20.0), np.full(100, 2.0)], index=idx)  # −90% in one day
    sh = pd.Series([100.0, 100.0, 400.0, 400.0], index=pd.to_datetime(["2024-03-01", "2024-05-01",
                                                                       "2024-06-15", "2024-08-01"]))
    breaks = ca.detect_breaks(px, sh)
    assert [b.date for b in breaks] == [idx[100].date()]
    # The same move explained by a 1-for-4... reverse split is not a break.
    split_sh = pd.Series([100.0, 100.0, 25.0, 25.0], index=sh.index)
    splits = pd.Series([0.25], index=[idx[100]])
    assert ca.detect_breaks(px, split_sh, splits) == []
    # A price gap with no share change is not a break.
    assert ca.detect_breaks(px, pd.Series([100.0, 100.0, 101.0], index=sh.index[:3])) == []


def test_htz_break_found_and_merged(fx_provider):
    px = fx_provider.get_price_history("HTZ", adjusted=False)
    sh = fx_provider.get_shares_history("HTZ")
    breaks = ca.corporate_action_breaks("HTZ", px, sh.series, fx_provider.get_splits("HTZ"))
    assert date(2021, 7, 1) in ca.break_dates(breaks)
    assert breaks[0].type == "bankruptcy emergence" and "manual" in breaks[0].sources


def test_htz_share_trend_starts_after_break(fx_provider):
    px = fx_provider.get_price_history("HTZ", adjusted=False)
    sh = fx_provider.get_shares_history("HTZ")
    splits = fx_provider.get_splits("HTZ")
    breaks = ca.break_dates(ca.corporate_action_breaks("HTZ", px, sh.series, splits))
    t = shares.share_trend(sh.series, splits, breaks, sh.source)
    assert t.segment_start == date(2021, 7, 1)
    assert t.span_start > date(2021, 7, 1)
    assert t.trend_per_year is not None and not t.flags  # the 153M → 463M jump is not scored
    no_break = shares.share_trend(sh.series, splits, [], sh.source)
    assert no_break.flags  # without the break, the emergence jump would be flagged


def test_manual_csv_merge_keeps_manual_date():
    m = [ca.Break(date=date(2021, 7, 1), type="bankruptcy emergence", sources=["manual"])]
    h = [ca.Break(date=date(2021, 7, 20), type="heuristic", sources=["heuristic"]),
         ca.Break(date=date(2024, 1, 5), type="heuristic", sources=["heuristic"])]
    merged = ca.merge_breaks(h, m)
    assert [b.date for b in merged] == [date(2021, 7, 1), date(2024, 1, 5)]
    assert merged[0].sources == ["manual", "heuristic"]
