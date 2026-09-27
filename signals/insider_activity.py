"""Insider activity summary (SPEC "Insider activity").

Over INSIDER_LOOKBACK_MONTHS: distinct insiders buying and selling (open-market
P and S only; the parser already drops every other code), net shares and value,
10b5-1 plan sales counted separately from discretionary sales, and the cluster-buy
flag when at least INSIDER_CLUSTER_MIN distinct insiders bought within any
INSIDER_CLUSTER_DAYS window. Coverage is always carried.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from pydantic import BaseModel, Field

import config
from data.form4 import InsiderTransaction
from data.insiders import COVERAGE_NONE, InsiderData


class InsiderSummary(BaseModel):
    coverage: str = COVERAGE_NONE
    since: date | None = None
    buyers: int = 0
    sellers_discretionary: int = 0
    sellers_10b5_1: int = 0
    net_shares: float = 0.0
    net_value: float = 0.0
    cluster_buy: bool = False
    cluster_window: tuple[date, date] | None = None
    buys: list[InsiderTransaction] = Field(default_factory=list)
    # Every open-market buy loaded (INSIDER_FETCH_MONTHS), for checks that reach past the
    # summary window, such as a cluster buy during the current drawdown.
    history_since: date | None = None
    history_buys: list[InsiderTransaction] = Field(default_factory=list)
    # Every open-market buy and sale loaded (INSIDER_FETCH_MONTHS), for the price-panel markers.
    history_trades: list[InsiderTransaction] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def available(self) -> bool:
        return not self.coverage.startswith(COVERAGE_NONE)

    @property
    def sellers(self) -> int:
        return self.sellers_discretionary + self.sellers_10b5_1


def lookback_start(today: date) -> date:
    return (pd.Timestamp(today) - pd.DateOffset(months=config.INSIDER_LOOKBACK_MONTHS)).date()


def fetch_start(today: date) -> date:
    """How far back insider trades are loaded (the longer of the two windows)."""
    months = max(config.INSIDER_FETCH_MONTHS, config.INSIDER_LOOKBACK_MONTHS)
    return (pd.Timestamp(today) - pd.DateOffset(months=months)).date()


def cluster_buy(buys: list[InsiderTransaction]) -> tuple[date, date] | None:
    """First window of INSIDER_CLUSTER_DAYS containing INSIDER_CLUSTER_MIN distinct buyers."""
    buys = sorted(buys, key=lambda t: t.date)
    for i, start in enumerate(buys):
        end = start.date + timedelta(days=config.INSIDER_CLUSTER_DAYS)
        names = {t.insider for t in buys[i:] if t.date <= end}
        if len(names) >= config.INSIDER_CLUSTER_MIN:
            last = max(t.date for t in buys[i:] if t.date <= end)
            return start.date, last
    return None


def insider_summary(data: InsiderData | None, today: date) -> InsiderSummary:
    if data is None:
        return InsiderSummary(coverage=f"{COVERAGE_NONE} (EDGAR not loaded)")
    since = lookback_start(today)
    txs = [t for t in data.transactions if since <= t.date <= today]
    buys = [t for t in txs if t.type == "buy"]
    sells = [t for t in txs if t.type == "sell"]
    sign = {"buy": 1, "sell": -1}
    window = cluster_buy(buys)
    return InsiderSummary(
        coverage=data.coverage_label, since=since, buyers=len({t.insider for t in buys}),
        sellers_discretionary=len({t.insider for t in sells if not t.is_10b5_1}),
        sellers_10b5_1=len({t.insider for t in sells if t.is_10b5_1}),
        net_shares=sum(sign[t.type] * (t.shares or 0.0) for t in txs if t.shares is not None),
        net_value=sum(sign[t.type] * (t.value or 0.0) for t in txs if t.value is not None),
        cluster_buy=window is not None, cluster_window=window, buys=buys,
        history_since=data.since or since,
        history_buys=[t for t in data.transactions if t.type == "buy" and t.date <= today],
        history_trades=sorted((t for t in data.transactions if t.date <= today), key=lambda t: t.date),
        errors=list(data.errors))
