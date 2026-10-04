"""Estimate accuracy page: across all tickers, the first turnaround estimate of each
drawdown episode scored as recovered within its window / still waiting / missed,
one stacked bar per episode type. The check on whether the turnaround estimate
is worth trusting."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

import config
from app import charts, ui
from data import prices
from data.provider import ProviderError
from storage import history


def closes_for(provider, tickers: set[str]) -> dict[str, pd.Series | None]:
    out = {}
    for t in sorted(tickers):
        try:
            out[t] = prices.adjusted_closes(provider, t)
        except ProviderError:
            out[t] = None
    return out


def render(provider) -> None:
    st.title("Estimate accuracy")
    edge = {"p75": "the upper end of its interquartile range", "median": "its median"}[config.ESTIMATE_SCORING_EDGE]
    st.caption(f"Only the first estimate made during each drawdown episode is scored, measured from that run's date; "
               f"its window ends at {edge}. Recovered = a close back within {config.RECOVERY_BAND:.0%} of the "
               "episode's prior high inside the window.")
    rows = history.load_all_estimates()
    with st.spinner("Loading prices for the scored tickers…"):
        closes = closes_for(provider, {r.ticker for r in rows})
    scored = history.score_estimates(rows, closes, date.today())
    summary = history.accuracy_summary(scored)
    if not summary.enough:
        st.info(f"Not enough scored estimates yet ({summary.total_scored} of {config.ACCURACY_MIN_SCORED} needed).")
    if summary.by_type:
        ui.chart(charts.accuracy_bars(summary.by_type), key="accuracy-bars")
        st.caption(" · ".join(f"{a.episode_type}: {a.scored} scored ({a.recovered} recovered, {a.waiting} waiting, "
                              f"{a.missed} missed)" for a in summary.by_type))
    for n in summary.notes:
        st.caption(n)
    firsts = [s for s in scored if s.scored]
    if firsts:
        st.space(12)
        with st.expander(f"Scored estimates ({len(firsts)})"):
            st.dataframe(pd.DataFrame([{
                "ticker": s.row.ticker, "run date": s.row.run_date, "type": s.row.episode_type,
                "estimate (months)": f"{s.row.turnaround_p25:.0f}–{s.row.turnaround_p75:.0f}",
                "window ends": s.window_end, "outcome": s.status, "detail": s.detail} for s in firsts]),
                hide_index=True, width="stretch",
                column_config={"ticker": st.column_config.Column(width=90),
                                "run date": st.column_config.Column(width=110),
                                "type": st.column_config.Column(width=140),
                                "estimate (months)": st.column_config.Column(width=150),
                                "window ends": st.column_config.Column(width=110),
                                "outcome": st.column_config.Column(width=100)})
