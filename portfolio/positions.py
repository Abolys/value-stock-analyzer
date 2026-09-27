"""Position maths for one holding, as pure functions.

- Average cost (config.COST_BASIS_METHOD = "average", also Canada's adjusted-cost-base rule):
  a buy adds its cash (shares × price + fees) to the cost base; a sell realises
  shares × (price − average cost) − fees and removes average cost × shares from the base.
- Value and unrealised gain use the ACTUAL latest price (CLAUDE.md Rule 5), converted to the
  holding's currency when the two differ.
- Splits: the price source is split-adjusted, so a transaction entered before a split is
  restated (shares × ratio, price ÷ ratio) for every split dated after it.
- Return vs benchmark, index-equivalent: every transaction's cash flow is mirrored into the
  ticker's BENCHMARKS index on the same date (a buy's cash buys index units at that day's
  close; a sell sells the same fraction of the index units). Both sides are price returns on
  actual closes: dividends are excluded on both sides, and the label says so.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

import config
from data.values import Datum
from portfolio.models import BUY, SELL, PositionSummary, Transaction

RETURN_BASIS = "price return, dividends excluded on both sides"
_EPS = config.RATIO_COMPARE_TOLERANCE  # float slack when a sell empties the position


def split_factor(splits: pd.Series | None, after: date, until: date | None = None) -> float:
    """Product of split ratios dated after `after` (and on or before `until`)."""
    if splits is None or len(splits) == 0:
        return 1.0
    f = 1.0
    for ts, ratio in splits.items():
        d = pd.Timestamp(ts).date()
        if d > after and (until is None or d <= until) and ratio and ratio > 0:
            f *= float(ratio)
    return f


def restate(txns: list[Transaction], splits: pd.Series | None, until: date | None = None) -> tuple[list[Transaction], list[str]]:
    """Transactions in date order, restated for later splits."""
    notes, out = [], []
    for t in sorted(txns, key=lambda t: (t.txn_date, t.txn_id or 0)):
        f = split_factor(splits, t.txn_date, until)
        if f != 1.0:
            notes.append(f"{t.side} of {t.txn_date} restated for splits after it (×{f:g} shares)")
            t = t.model_copy(update={"shares": t.shares * f, "price": t.price / f})
        out.append(t)
    return out, notes


def position_summary(txns: list[Transaction], price: Datum, splits: pd.Series | None = None,
                     fx: Datum | None = None) -> PositionSummary:
    """Average-cost position at the actual latest price. `fx` converts the trading currency
    into the holding's currency (None when they are the same)."""
    if config.COST_BASIS_METHOD not in config.COST_BASIS_METHODS:
        raise ValueError(f"unknown COST_BASIS_METHOD {config.COST_BASIS_METHOD!r}")
    rows, notes = restate(txns, splits, price.period_end)
    out = PositionSummary(notes=notes)
    cost = 0.0
    proceeds = 0.0
    for t in rows:
        if t.side == BUY:
            cost += t.shares * t.price + t.fees
            out.shares += t.shares
            out.invested += t.shares * t.price + t.fees
        elif t.side == SELL:
            if t.shares > out.shares + _EPS:
                out.status = f"N/A - sell of {t.shares:g} shares on {t.txn_date} exceeds the {out.shares:g} held"
                return out
            avg = cost / out.shares
            out.realised += t.shares * (t.price - avg) - t.fees
            cost -= avg * t.shares
            out.shares -= t.shares
            proceeds += t.shares * t.price - t.fees
            if out.shares < _EPS:
                out.shares, cost = 0.0, 0.0
    out.cost_basis = cost
    out.avg_cost = cost / out.shares if out.shares > 0 else None
    if not price.ok:
        out.status = price.status
        return out
    px = price.value
    if fx is not None:
        if not fx.ok:
            out.status = f"N/A - currency conversion {fx.status}"
            return out
        px *= fx.value
        out.notes.append(f"latest price converted at {fx.value:.4f} ({fx.period_label or 'FX'})")
    out.price, out.price_as_of = px, price.period_end
    out.market_value = out.shares * px
    out.unrealised = out.market_value - cost
    out.total_gain = out.realised + out.unrealised
    if out.invested > 0:
        out.total_return = (out.market_value + proceeds - out.invested) / out.invested
    return out


def _close_on(closes: pd.Series, d: date) -> float | None:
    """The close on `d`, or the last one before it (weekends, holidays)."""
    s = closes[closes.index <= pd.Timestamp(d)]
    return float(s.iloc[-1]) if len(s) else None


def benchmark_return(txns: list[Transaction], bench_closes: pd.Series, as_of: date,
                     splits: pd.Series | None = None) -> tuple[float | None, str]:
    """Index-equivalent return of the holding's cash flows in the benchmark, to `as_of`.
    Returns (return, reason when N/A)."""
    closes = bench_closes.dropna()
    if closes.empty:
        return None, "N/A - no benchmark history"
    closes.index = pd.to_datetime(closes.index)
    rows, _ = restate(txns, splits, as_of)
    units = held = invested = proceeds = 0.0
    for t in rows:
        b = _close_on(closes, t.txn_date)
        if b is None or b <= 0:
            return None, f"N/A - benchmark history starts after {t.txn_date}"
        if t.side == BUY:
            cash = t.shares * t.price + t.fees
            units += cash / b
            invested += cash
            held += t.shares
        else:
            if held <= 0:
                return None, "N/A - sell before any buy"
            sold = units * min(t.shares / held, 1.0)
            units -= sold
            proceeds += sold * b
            held -= t.shares
    b_now = _close_on(closes, as_of)
    if b_now is None or invested <= 0:
        return None, "N/A - nothing invested"
    return (units * b_now + proceeds - invested) / invested, ""


def with_benchmark(summary: PositionSummary, txns: list[Transaction], bench: str, bench_closes: pd.Series | None,
                   splits: pd.Series | None = None, bench_error: str = "") -> PositionSummary:
    out = summary.model_copy(update={"benchmark": bench})
    if bench_closes is None:
        out.notes.append(f"vs {bench}: N/A - {bench_error or 'benchmark prices unavailable'}")
        return out
    as_of = summary.price_as_of or date.today()
    r, reason = benchmark_return(txns, bench_closes, as_of, splits)
    out.benchmark_return = r
    if r is None:
        out.notes.append(f"vs {bench}: {reason}")
    elif summary.total_return is not None:
        out.vs_benchmark = summary.total_return - r
    out.notes.append(f"Return vs {bench}: {RETURN_BASIS}; the index mirrors each buy and sell on the same date")
    return out
