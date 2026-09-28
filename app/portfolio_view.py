"""Pure helpers for the Portfolio page (no Streamlit): each holding's position at the actual
latest price with its return vs the benchmark, its thesis check on the latest metrics, and
the table frames and styling the page draws (SPEC "Charts" → Portfolio view)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from pandas.io.formats.style import Styler
from pydantic import BaseModel, ConfigDict

from analysis.turnaround import benchmark_for
from app import theme
from data import currency, prices
from data.provider import ProviderError
from portfolio import alerts as pa
from portfolio import store
from portfolio.models import FIRED, NEAR, UNKNOWN, Holding, Metric, PositionSummary, ThesisCheck
from portfolio.positions import position_summary, with_benchmark
from portfolio.thesis import BETTER, WORSE, thesis_check
from portfolio.triggers import light_label

LIGHT_COLOR = {"red": theme.CRITICAL, "amber": theme.WARNING, "green": theme.GOOD, "none": theme.MUTED}
LIGHT_EMOJI = {"red": "🔴", "amber": "🟡", "green": "🟢", "none": "⚪"}
STATE_EMOJI = {FIRED: "🔴", NEAR: "🟡", UNKNOWN: "🟡", "ok": "🟢"}


class HoldingView(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    holding: Holding
    position: PositionSummary
    check: ThesisCheck
    unread: int = 0
    now_label: str = ""


def position_for(provider, h: Holding) -> PositionSummary:
    price = prices.actual_latest_price(provider, h.ticker)
    try:
        splits = provider.get_splits(h.ticker)
    except ProviderError:
        splits = None
    fx = None
    try:
        trading = provider.get_info(h.ticker).get("currency")
    except ProviderError:
        trading = None
    if trading and trading != h.currency:
        fx = currency.fx_rate(provider, trading, h.currency)
    pos = position_summary(h.transactions, price, splits, fx)
    if trading is None:
        pos.notes.append(f"trading currency unknown; the price is assumed to be in {h.currency}")
    bench = benchmark_for(h.ticker) or ""
    closes, err = None, ""
    if bench:
        try:
            closes = prices.actual_closes(provider, bench)
        except ProviderError as exc:
            err = str(exc)
    else:
        err = "no BENCHMARKS index for this listing country"
    pos = with_benchmark(pos, h.transactions, bench or "benchmark", closes, splits, err)
    if trading and bench and trading != h.currency:
        pos.notes.append(f"{bench} return is in its own currency; the holding's is in {h.currency}")
    return pos


def build_view(provider, h: Holding, db_path=None) -> HoldingView:
    """Position, thesis check and unread alerts for one holding. The thesis check uses the
    same actual latest price as the position (in the trading currency, like the thesis levels)."""
    if h.is_cash:
        return HoldingView(holding=h, position=position_for(provider, h), check=ThesisCheck(holding_id=h.holding_id or 0,
                           ticker=h.ticker, now_source="cash deposit"), unread=store.unread_count(h.ticker, db_path),
                           now_label="cash deposit")
    metrics, label = pa.now_metrics(h.ticker, db_path)
    price = prices.actual_latest_price(provider, h.ticker)
    if price.ok:
        metrics = {**metrics, "price": Metric(value=price.value, display=f"{price.value:,.2f}", as_of=price.period_end,
                                              source="actual latest close")}
    return HoldingView(holding=h, position=position_for(provider, h), check=thesis_check(h, metrics, label),
                       unread=store.unread_count(h.ticker, db_path), now_label=label)


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------
def _money(v: float | None, ccy: str = "") -> str:
    return "N/A" if v is None else f"{v:,.2f} {ccy}".strip()


def _pct(v: float | None, pts: bool = False) -> str:
    if v is None:
        return "N/A"
    return f"{v * 100:+.1f} pts" if pts else f"{v:+.1%}"


def holdings_frame(views: list[HoldingView]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(display frame, state frame: good / bad / na per cell for colouring)."""
    rows, states = [], []
    for v in views:
        h, p, c = v.holding, v.position, v.check
        ok = p.status == "ok"
        rows.append({
            "Ticker": h.ticker, "Type": "Cash deposit" if h.is_cash else "Value stock", "Account": h.account,
            "Shares": f"{p.shares:,.4g}",
            "Avg cost": _money(p.avg_cost, h.currency),
            "Value": _money(p.market_value, h.currency) if ok else p.status,
            "Gain": _pct(p.total_return) if ok else "N/A",
            "Realised": _money(p.realised, h.currency), "Unrealised": _money(p.unrealised, h.currency) if ok else "N/A",
            "vs index": (f"{_pct(p.vs_benchmark, pts=True)} vs {p.benchmark}" if p.vs_benchmark is not None
                          else f"N/A ({p.benchmark})"),
            "Triggers": "— (cash deposit)" if h.is_cash else f"{LIGHT_EMOJI[c.light]} {light_label(c.triggers)}",
            "Alerts": f"{v.unread} new" if v.unread else "—",
        })
        states.append({
            "Gain": "na" if p.total_return is None else "pass" if p.total_return >= 0 else "fail",
            "vs index": "na" if p.vs_benchmark is None else "pass" if p.vs_benchmark >= 0 else "fail",
        })
    df = pd.DataFrame(rows)
    st = pd.DataFrame(states, index=df.index).reindex(columns=df.columns).fillna("")
    return df, st


def style_states(df: pd.DataFrame, states: pd.DataFrame) -> Styler:
    def colour(_df: pd.DataFrame) -> pd.DataFrame:
        return states.map(lambda s: (f"color: {theme.CELL_TEXT[s]}; background-color: {theme.CELL_BG[s]}"
                                     if theme.CELL_TEXT.get(s) else ""))

    return df.style.apply(colour, axis=None)


def totals_by_currency(views: list[HoldingView]) -> pd.DataFrame:
    """Totals per currency and type (value stocks apart from parked cash); amounts in different currencies
    are never added together."""
    agg: dict[tuple[str, str], dict[str, float]] = {}
    for v in views:
        p = v.position
        if p.status != "ok" or p.market_value is None:
            continue
        key = (v.holding.currency, "Cash deposits" if v.holding.is_cash else "Value stocks")
        t = agg.setdefault(key, {"Value": 0.0, "Cost basis": 0.0, "Realised": 0.0, "Unrealised": 0.0})
        t["Value"] += p.market_value
        t["Cost basis"] += p.cost_basis
        t["Realised"] += p.realised
        t["Unrealised"] += p.unrealised or 0.0
    return pd.DataFrame([{"Currency": c, "Type": kind, **{n: f"{x:,.2f}" for n, x in t.items()}}
                         for (c, kind), t in sorted(agg.items())])


CHANGE_STATE = {BETTER: "pass", WORSE: "fail"}


def then_now_frame(check: ThesisCheck) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.DataFrame([{"Metric": r.label, "At purchase": r.then, "Now": r.now, "Change": r.change}
                       for r in check.then_now])
    states = pd.DataFrame([{"Now": CHANGE_STATE.get(r.change, "na" if r.change == "n/a" else "")}
                           for r in check.then_now], index=df.index).reindex(columns=df.columns).fillna("")
    return df, states


def triggers_frame(check: ThesisCheck) -> pd.DataFrame:
    return pd.DataFrame([{
        "": STATE_EMOJI.get(s.state, "⚪"), "Rule": s.trigger.text, "Status": s.state, "Now": s.current,
        "Threshold": s.threshold,
        "Last fired": s.trigger.fired_at.strftime("%Y-%m-%d") if s.trigger.fired_at else "—",
    } for s in check.triggers])


def fmt_when(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""
