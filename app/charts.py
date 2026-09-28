"""Plotly figure builders for every chart in the app and the exports (SPEC "Charts").

Pure functions: no Streamlit. Each returns a ChartOut whose `excluded` lists
everything left off the chart with its reason (nothing is silently dropped) and
whose `notes` carry the flags and captions shown beside it.

- One y-axis per chart; never a dual axis.
- A ticker keeps its colour everywhere (theme.ticker_color).
- Only price series (stock, benchmark) are ever indexed to 100; profit and every
  other series that can be zero or negative are plotted in their own units.
"""

from __future__ import annotations

import math

from datetime import date, timedelta
from typing import Iterable

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pydantic import BaseModel, ConfigDict, Field

import config
from analysis.models import AggregateResult, FundamentalSeries, LensResult
from analysis.turnaround_models import COMPANY_SPECIFIC, MARKET_DRIVEN, TurnaroundResult, Week52
from app import theme
from data.form4 import InsiderTransaction
from data.values import Datum
from screening.models import SLOT_FCF, SLOT_LEVERAGE, SLOT_MOS, ScreenResult
from signals.asset_floor import AssetFloor
from signals.dcf import ReverseDcf, SensitivityGrid
from signals.dividends import DividendSafety
from signals.trap_scores import AltmanResult, BeneishResult, PiotroskiResult

# No fixed font colour: the Streamlit theme (light or dark) supplies it; exports use plotly_white's ink.
FONT = dict(family="system-ui, -apple-system, Segoe UI, sans-serif", size=12)
FUNDAMENTAL_LABELS = {"total_revenue": "Revenue", "net_income": "Net income", "total_debt": "Total debt"}
SEASONAL_FLOWS = ("total_revenue", "net_income")  # quarterly flows: compared with the same quarter a year earlier


def year_ago_point(pts: list, latest) -> object | None:
    """The quarter about a year before `latest` (4 × QUARTER_GAP_DAYS), or None."""
    lo, hi = (config.TTM_QUARTERS * d for d in config.QUARTER_GAP_DAYS)
    return next((p for p in pts if lo <= (latest.period_end - p.period_end).days <= hi), None)


def yoy_label(latest_value: float, year_ago_value: float) -> str:
    if year_ago_value <= 0:
        return "n/m - year-ago quarter ≤ 0"
    return f"{latest_value / year_ago_value - 1:+.0%}"


class ChartOut(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    fig: go.Figure
    title: str = ""
    excluded: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)

    @property
    def caption(self) -> str:
        parts = list(self.notes)
        if self.excluded:
            parts.append("Not shown: " + "; ".join(self.excluded))
        return " · ".join(parts)


def _base(fig: go.Figure, height: int, **layout) -> go.Figure:
    layout.setdefault("margin", dict(l=10, r=10, t=30, b=10))
    fig.update_layout(template="plotly_white", height=height, font=FONT, paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", hoverlabel=dict(font_size=12), **layout)
    fig.update_xaxes(gridcolor="rgba(195,194,183,0.35)", zerolinecolor=theme.GRID)
    fig.update_yaxes(gridcolor="rgba(195,194,183,0.35)", zerolinecolor=theme.GRID)
    return fig


def _money(v: float, currency: str | None = None) -> str:
    sign = "−" if v < 0 else ""
    a = abs(v)
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}{a / div:,.2f}{suf}" + (f" {currency}" if currency else "")
    return f"{sign}{a:,.2f}" + (f" {currency}" if currency else "")


def _reason(d: Datum) -> str:
    return d.status


# --------------------------------------------------------------------------
# 52-week range bar
# --------------------------------------------------------------------------
def week52_bar(w: Week52) -> ChartOut:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[w.low, w.high], y=[0, 0], mode="lines", line=dict(color=theme.GRID, width=8),
                             name="52-week range", hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=[w.latest], y=[0], mode="markers", name="latest",
                             marker=dict(symbol="line-ns", size=16, line=dict(width=4, color=theme.MARKER)),
                             customdata=[[w.position]],
                             hovertemplate=f"Latest adjusted close {w.latest:,.2f} ({w.as_of})<extra></extra>"))
    fig.add_annotation(x=w.low, y=0, yshift=-22, text=f"52w low {w.low:,.2f}", showarrow=False, xanchor="left")
    fig.add_annotation(x=w.high, y=0, yshift=-22, text=f"52w high {w.high:,.2f}", showarrow=False, xanchor="right")
    fig.add_annotation(x=(w.low + w.high) / 2, y=0, yshift=-22, text=w.label, showarrow=False,
                       font=dict(color=theme.CRITICAL) if w.drawdown > 0 else None)
    pad = (w.high - w.low) * 0.02 or 1.0
    _base(fig, 80, showlegend=False, margin=dict(l=10, r=10, t=5, b=5))
    fig.update_xaxes(visible=False, range=[w.low - pad, w.high + pad])
    fig.update_yaxes(visible=False, range=[-1.4, 0.6])
    return ChartOut(fig=fig, title="52-week range",
                    notes=[f"Adjusted closes, latest segment only; {w.label} (high {w.high:,.2f} on {w.high_date})"])


# --------------------------------------------------------------------------
# Fundamentals over time: small multiples
# --------------------------------------------------------------------------
def _marker_sizes(values: list[float | None]) -> list[float]:
    lo, hi = config.INSIDER_MARKER_SIZE_RANGE
    known = [abs(v) for v in values if v]
    top = max(known) if known else 0
    return [lo + (hi - lo) * (abs(v) / top) if v and top else lo for v in values]


def small_multiples(closes: pd.Series | None, series: FundamentalSeries | None,
                    trades: Iterable[InsiderTransaction] = (), ticker: str = "",
                    price_note: str = "adjusted closes") -> ChartOut:
    series = series or FundamentalSeries()
    titles = ["Price", *FUNDAMENTAL_LABELS.values()]
    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.06, subplot_titles=titles)
    excluded, notes = [], []
    all_dates = [p.period_end for pts in series.points.values() for p in pts]
    start = (min(all_dates) - timedelta(days=92)) if all_dates else None
    colour = theme.ticker_color(ticker) if ticker else theme.PRIMARY
    px = None
    if closes is not None and len(closes):
        px = closes.dropna().sort_index()
        if start is not None:
            px = px[px.index >= pd.Timestamp(start)]
        fig.add_trace(go.Scatter(x=px.index, y=px.values, mode="lines", name="Price", line=dict(color=colour, width=2),
                                 showlegend=False,
                                 hovertemplate="%{x|%Y-%m-%d}: %{y:,.2f}<extra>price</extra>"), row=1, col=1)
    else:
        excluded.append("price: history unavailable")
    trades = [t for t in trades if px is not None and len(px) and pd.Timestamp(t.date) >= px.index[0]]
    for kind, symbol, colour_t in (("buy", "triangle-up", theme.INSIDER_BUY), ("sell", "triangle-down", theme.INSIDER_SELL)):
        for plan in (False, True):
            group = [t for t in trades if t.type == kind and (t.is_10b5_1 == plan)]
            if not group or (kind == "buy" and plan):
                continue
            ys = [float(px.asof(pd.Timestamp(t.date))) for t in group]
            name = {"buy": "Insider buy", "sell": "Insider sale (10b5-1 plan)" if plan else "Insider sale"}[kind]
            fig.add_trace(go.Scatter(
                x=[pd.Timestamp(t.date) for t in group], y=ys, mode="markers", name=name,
                marker=dict(symbol=f"{symbol}-open" if plan else symbol, color=colour_t,
                            size=_marker_sizes([t.value for t in group]), line=dict(width=1.5, color=colour_t)),
                text=[f"{t.insider} ({t.role}): {kind} {t.shares or 0:,.0f} sh"
                      + (f", {_money(t.value)}" if t.value else "") + (" · 10b5-1 plan" if t.is_10b5_1 else "")
                      + f" · {t.source}" for t in group],
                hovertemplate="%{x|%Y-%m-%d}: %{text}<extra></extra>"), row=1, col=1)
    for row, (name, label) in enumerate(FUNDAMENTAL_LABELS.items(), start=2):
        pts = series.points.get(name)
        if not pts:
            reason = series.missing.get(name, "N/A - Data Incomplete")
            excluded.append(f"{label.lower()}: {reason}")
            fig.add_annotation(text=reason, xref="x domain", yref="y domain", x=0.5, y=0.5, showarrow=False,
                               row=row, col=1, font=dict(color=theme.MUTED))
            continue
        freq = series.freqs.get(name, "quarterly")
        fig.add_trace(go.Scatter(
            x=[pd.Timestamp(p.period_end) for p in pts], y=[p.value for p in pts], mode="lines+markers", name=label,
            showlegend=False,
            line=dict(color=colour, width=2), marker=dict(size=8, color=colour),
            text=[_money(p.value, series.currency) for p in pts],
            hovertemplate=f"%{{x|%Y-%m-%d}} ({freq}): %{{text}}<extra>{label}</extra>"), row=row, col=1)
        if any(p.value < 0 for p in pts):
            fig.add_hline(y=0, line=dict(color=theme.GRID, dash="dot", width=1), row=row, col=1)
        if freq == "quarterly" and name in SEASONAL_FLOWS and len(pts) > 1:
            latest = max(pts, key=lambda p: p.period_end)
            ago = year_ago_point(pts, latest)
            if ago is not None:  # seasonal businesses: compare like with like, not with the previous quarter
                fig.add_trace(go.Scatter(
                    x=[pd.Timestamp(ago.period_end)], y=[ago.value], mode="markers", showlegend=False,
                    marker=dict(size=16, color="rgba(0,0,0,0)", line=dict(width=2, color=theme.REF_LINE)),
                    hovertemplate=f"same quarter a year earlier<extra>{label}</extra>"), row=row, col=1)
                fig.add_annotation(x=pd.Timestamp(latest.period_end), y=latest.value, row=row, col=1,
                                   text=f"vs year-ago quarter (○): {yoy_label(latest.value, ago.value)}",
                                   showarrow=True, arrowhead=0, ax=-60, ay=-22, font=dict(size=11))
            else:
                notes.append(f"{label}: no quarter a year before the latest one to compare with")
        if freq != "quarterly":
            notes.append(f"{label}: fiscal-year values (no quarterly data)")
    notes.insert(0, f"Price: {price_note}. Fundamentals at their period ends, each panel on its own scale "
                    f"({series.currency or 'trading currency'}); none indexed. yfinance gives about 5 quarters. "
                    "Quarterly revenue and net income are often seasonal: the latest quarter is compared with "
                    "the same quarter a year earlier (○), not with the one before it.")
    if trades:
        notes.append("▲ open-market buys, ▼ sales (hollow = 10b5-1 plan), sized by value")
    _base(fig, 620, showlegend=bool(trades), legend=dict(orientation="h", y=-0.05))
    for a in fig.layout.annotations[:4]:  # the four subplot titles
        a.update(font=dict(size=12), xanchor="left", x=0)
    return ChartOut(fig=fig, title="Fundamentals over time", excluded=excluded, notes=notes)


# --------------------------------------------------------------------------
# Lens scores: dot strip
# --------------------------------------------------------------------------
def dot_strip(lenses: list[LensResult | None], aggregate: AggregateResult | None,
              labels: list[str] | None = None) -> ChartOut:
    fig = go.Figure()
    excluded = []
    names = labels or [lens.label if lens is not None else "?" for lens in lenses]
    n = len(lenses)
    for i, (lens, name) in enumerate(zip(lenses, names)):
        y = n - 1 - i
        is_da = lens is not None and lens.lens == "devils_advocate"
        if lens is not None and lens.ok:
            fig.add_trace(go.Scatter(
                x=[lens.score], y=[y], mode="markers+text", name=name, text=[f"{lens.score:.1f}"],
                textposition="middle right", marker=dict(size=14, color=theme.DIVERGING_BELOW if is_da else theme.PRIMARY,
                                                         line=dict(width=2, color="white")),
                hovertemplate=f"{name}: %{{x:.1f}}<extra></extra>"))
        else:
            reason = lens.status if lens is not None else "pending"
            excluded.append(f"{name}: {reason}")
            fig.add_trace(go.Scatter(
                x=[(config.SCORE_MIN + config.SCORE_MAX) / 2], y=[y], mode="markers+text", name=name,
                text=[reason if len(reason) <= 40 else reason[:38] + "…"], textposition="middle right", textfont=dict(color=theme.MUTED, size=11),
                marker=dict(size=14, symbol="circle-open", color=theme.MUTED, line=dict(width=2)),
                hovertemplate=f"{name}: {reason}<extra></extra>"))
    if aggregate is not None and aggregate.score is not None:
        fig.add_vline(x=aggregate.score, line=dict(color=theme.MUTED, width=1.5),
                      annotation_text=f"aggregate {aggregate.score:.1f}", annotation_position="top")
        da = next((lens for lens in lenses if lens is not None and lens.lens == "devils_advocate"), None)
        if aggregate.controversy and da is not None and da.ok and aggregate.controversy_gap is not None:
            y = n - 1 - lenses.index(da)
            fig.add_shape(type="rect", x0=da.score, x1=da.score + aggregate.controversy_gap, y0=y - 0.3, y1=y + 0.3,
                          fillcolor="rgba(227,73,72,0.25)", line=dict(width=0), layer="below", name="controversy gap")
    _base(fig, 60 + 42 * n, showlegend=False, margin=dict(l=10, r=40, t=30, b=10))
    fig.update_xaxes(range=[config.SCORE_MIN - 0.3, config.SCORE_MAX + 0.6], dtick=1)
    fig.update_yaxes(tickvals=list(range(n)), ticktext=list(reversed(names)), range=[-0.6, n - 0.4], zeroline=False)
    notes = ["1 = bearish, 10 = bullish; line = aggregate"]
    if aggregate is not None and aggregate.controversy:
        notes.append("shaded = Devil's Advocate gap to the other lenses' mean (high controversy)")
    return ChartOut(fig=fig, title="Lens scores", excluded=excluded, notes=notes)


# --------------------------------------------------------------------------
# Peer strip
# --------------------------------------------------------------------------
PEER_METRICS = [("mos", "Margin of safety", "%"), ("fcf_spread", "FCF yield vs 10-year (pts)", "pts"),
                ("leverage", "Net debt / EBITDA", "x"), ("roic", "ROIC", "%")]


def peer_metric(r: ScreenResult, key: str) -> tuple[float | None, str]:
    """(value, "") or (None, N/A / n/m reason) for one peer-strip metric."""
    if key == "mos":
        m = r.metric(SLOT_MOS)
        return (m.value.value, "") if m and m.value.ok else (None, m.value.status if m else "N/A - Data Incomplete")
    if key == "fcf_spread":
        m = r.metric(SLOT_FCF)
        if m is None:
            return None, "N/A - Data Incomplete"
        if r.treatment != "Standard" and "FCF" not in m.name:
            return None, f"n/m - sector-adjusted ({m.name})"
        if m.fmt == "months":
            return None, "n/m - FCF-negative (cash runway instead)"
        if not m.value.ok:
            return None, m.value.status
        if not r.risk_free.ok:
            return None, f"comparison {r.risk_free.status}"
        return m.value.value - r.risk_free.value, ""
    if key == "leverage":
        m = r.metric(SLOT_LEVERAGE)
        if m is None or not m.name.startswith("Net debt"):
            return None, f"n/m - sector-adjusted ({m.name if m else 'no leverage metric'})"
        return (m.value.value, "") if m.value.ok else (None, m.value.status)
    if key == "roic":
        d = r.inputs.get("ROIC")
        if d is None:
            return None, "N/A - Data Incomplete"
        return (d.value, "") if d.ok else (None, d.status)
    raise KeyError(key)


def _fmt_metric(v: float, unit: str) -> str:
    return {"%": f"{v:+.1%}", "pts": f"{v * 100:+.1f} pts", "x": f"{v:.2f}x"}[unit]


def peer_strip(me: ScreenResult, peers: list[ScreenResult], source_note: str = "") -> ChartOut:
    fig = make_subplots(rows=len(PEER_METRICS), cols=1, vertical_spacing=0.16,
                        subplot_titles=[label for _, label, _ in PEER_METRICS])
    excluded = []
    for row, (key, label, unit) in enumerate(PEER_METRICS, start=1):
        xs, names = [], []
        for p in peers:
            v, why = peer_metric(p, key)
            if v is None:
                excluded.append(f"{p.ticker} {label}: {why}")
            else:
                xs.append(v * (100 if unit == "pts" else 1))
                names.append(f"{p.ticker} — {p.name}: {_fmt_metric(v, unit)}")
        if xs:
            fig.add_trace(go.Scatter(x=xs, y=[0] * len(xs), mode="markers", name="peers", text=names,
                                     marker=dict(size=10, color=theme.PEER_GREY, line=dict(width=1, color="white")),
                                     hovertemplate="%{text}<extra></extra>"), row=row, col=1)
        v, why = peer_metric(me, key)
        if v is None:
            excluded.append(f"{me.ticker} {label}: {why}")
            fig.add_annotation(text=f"{me.ticker}: {why}", xref="x domain", yref="y domain", x=1, y=0.5,
                               showarrow=False, xanchor="right", font=dict(color=theme.MUTED, size=11),
                               row=row, col=1)
        else:
            fig.add_trace(go.Scatter(x=[v * (100 if unit == "pts" else 1)], y=[0], mode="markers+text",
                                     name=me.ticker, text=[_fmt_metric(v, unit)], textposition="top center",
                                     marker=dict(size=15, color=theme.ticker_color(me.ticker),
                                                 line=dict(width=2, color="white")),
                                     hovertemplate=f"{me.ticker}: {_fmt_metric(v, unit)}<extra></extra>"),
                          row=row, col=1)
        fig.update_yaxes(visible=False, range=[-1, 1.2], row=row, col=1)
        if unit == "%":
            fig.update_xaxes(tickformat=".0%", row=row, col=1)
    _base(fig, 330, showlegend=False)
    for a in fig.layout.annotations[:len(PEER_METRICS)]:
        a.update(font=dict(size=12), xanchor="left", x=0)
    notes = [f"{len(peers)} peers (grey), {me.ticker} highlighted; hover for names"]
    if source_note:
        notes.append(source_note)
    return ChartOut(fig=fig, title="Versus peers", excluded=excluded, notes=notes)


# --------------------------------------------------------------------------
# Sensitivity heatmap
# --------------------------------------------------------------------------
def heat_class(value: float, price: float) -> str:
    """'above' / 'below' the actual latest price, or 'neutral' within HEATMAP_NEUTRAL_BAND."""
    rel = value / price - 1
    if abs(rel) <= config.HEATMAP_NEUTRAL_BAND + config.RATIO_COMPARE_TOLERANCE:
        return "neutral"
    return "above" if rel > 0 else "below"


def sensitivity_heatmap(grid: SensitivityGrid | None, price: Datum, reverse: ReverseDcf | None = None) -> ChartOut | None:
    """Fair value per cell with its upside vs the actual latest price. Colour is diverging around the
    price on a log scale (a doubling and a halving are equally strong), stretched to this grid's own
    extremes, so the shades still separate the cells when every one of them sits on the same side."""
    if grid is None or not grid.values or not price.ok:
        return None
    z, text, classes, excluded = [], [], [], []
    for i, rate in enumerate(grid.rates):
        zr, tr, cr = [], [], []
        for j, g in enumerate(grid.growths):
            v = grid.values[i][j]
            if v is None or v <= 0:
                zr.append(None)
                tr.append("N/A" if v is None else f"{v:,.2f}")
                cr.append("n/a")
                excluded.append(f"rate {rate:.1%}, growth {g:+.1%}: "
                                + ("not computable" if v is None else "fair value ≤ 0"))
                continue
            c = heat_class(v, price.value)
            zr.append(0.0 if c == "neutral" else math.log(v / price.value))
            tr.append(f"{v:,.2f}<br>{v / price.value - 1:+.0%}")
            cr.append(c)
        z.append(zr)
        text.append(tr)
        classes.append(cr)
    band = config.HEATMAP_NEUTRAL_BAND
    extent = max([abs(v) for row in z for v in row if v is not None] + [math.log(1 + band) * 2])
    nb = math.log(1 + band) / extent / 2  # the neutral band's half-width on the 0–1 colour scale
    fig = go.Figure(go.Heatmap(
        x=grid.growths, y=grid.rates, z=z, text=text, texttemplate="%{text}", zmin=-extent, zmax=extent, zmid=0,
        colorscale=[[0.0, theme.DIVERGING_BELOW], [0.5 - nb, "#f3c6c5"], [0.5, theme.DIVERGING_MID],
                    [0.5 + nb, "#c6dbf5"], [1.0, theme.DIVERGING_ABOVE]],
        showscale=False, customdata=classes, xgap=2, ygap=2,
        hovertemplate="discount rate %{y:.1%}, growth %{x:+.1%}: fair value %{text} (%{customdata})<extra></extra>"))
    ci, cj = len(grid.rates) // 2, len(grid.growths) // 2
    dx = (grid.growths[1] - grid.growths[0]) / 2 if len(grid.growths) > 1 else 0.01
    dy = (grid.rates[1] - grid.rates[0]) / 2 if len(grid.rates) > 1 else 0.005
    fig.add_shape(type="rect", x0=grid.growths[cj] - dx, x1=grid.growths[cj] + dx, y0=grid.rates[ci] - dy,
                  y1=grid.rates[ci] + dy, line=dict(color="#0b0b0b", width=3), name="base case")
    notes = [f"Fair value per share and upside vs the actual price {price.value:,.2f}; blue above, red below, grey "
             f"within ±{band:.0%}; shade scaled to this grid's range; outlined = base case"]
    if reverse is not None and reverse.status == "ok" and reverse.implied_growth is not None:
        g = reverse.implied_growth
        if grid.growths[0] - dx <= g <= grid.growths[-1] + dx:
            fig.add_vline(x=g, line=dict(color=theme.MARKER, dash="dash", width=1.5),
                          annotation_text=f"price implies {g:+.1%}", annotation_position="top")
        else:
            left = g < grid.growths[0]
            fig.add_annotation(x=grid.growths[0] - dx if left else grid.growths[-1] + dx, y=1.0, yref="paper",
                               xanchor="left" if left else "right", yanchor="bottom", showarrow=False,
                               text=f"{'◀' if left else ''} price implies {g:+.1%}/yr {'' if left else '▶'}",
                               font=dict(color=theme.MARKER))
            notes.append(f"reverse-DCF growth {g:+.1%}/yr lies outside the grid "
                         f"({'below' if left else 'above'} its {'lowest' if left else 'highest'} growth)")
    elif reverse is not None:
        notes.append(f"reverse DCF: {reverse.status}")
    _base(fig, 320, xaxis_title="Stage-1 growth", yaxis_title="Discount rate")
    fig.update_xaxes(tickformat="+.1%", tickvals=grid.growths)
    fig.update_yaxes(tickformat=".0%", tickvals=grid.rates, autorange="reversed")
    return ChartOut(fig=fig, title="Fair value sensitivity", excluded=excluded, notes=notes)


# --------------------------------------------------------------------------
# Asset floor
# --------------------------------------------------------------------------
def asset_floor_panel(af: AssetFloor | None, market_cap: Datum, currency: str | None = None) -> ChartOut | None:
    if af is None:
        return None
    rows = [("Market cap", market_cap), ("Tangible book", af.tbv), ("NCAV", af.ncav), ("NNWC", af.nnwc)]
    names, values, colours, excluded = [], [], [], []
    for name, d in rows:
        if d.ok:
            names.append(name)
            values.append(d.value)
            colours.append(theme.MUTED if name == "Market cap" else
                           ("rgba(42,120,214,0.55)" if d.value < 0 else theme.PRIMARY))
        else:
            excluded.append(f"{name}: {d.status}")
    fig = go.Figure(go.Bar(y=names, x=values, orientation="h", marker=dict(color=colours),
                           text=[_money(v, currency) for v in values], textposition="outside",
                           hovertemplate="%{y}: %{text}<extra></extra>"))
    flags = []
    if market_cap.ok:
        fig.add_vline(x=market_cap.value, line=dict(color=theme.MARKER, width=2),
                      annotation_text="market cap", annotation_position="top")
        if af.ncav.ok and af.ncav.value >= market_cap.value:
            flags.append("Trades below net current assets (net-net)" + (f" — {af.burn_line}" if af.burn_line else ""))
    fig.add_vline(x=0, line=dict(color=theme.GRID, width=1))
    head = (f"{af.coverage.value:.0%} of the price covered by tangible book ({af.coverage_band})" if af.coverage.ok
            else f"Asset coverage: {af.coverage_band} ({af.coverage.status})")
    lo = min([0.0, *values])
    hi = max([0.0, *values])
    pad = (hi - lo) * 0.25 or 1.0
    _base(fig, 230, showlegend=False)
    fig.update_xaxes(range=[lo - (pad if lo < 0 else 0), hi + pad])
    fig.update_yaxes(autorange="reversed")
    notes = [head, f"P/TBV {af.p_tbv.value:.2f}x" if af.p_tbv.ok else f"P/TBV {af.p_tbv.status}",
             "one scale; negative values extend left of zero"]
    return ChartOut(fig=fig, title="Asset floor", excluded=excluded, notes=notes, flags=flags)


# --------------------------------------------------------------------------
# Trap scores
# --------------------------------------------------------------------------
def trap_panel(pio: PiotroskiResult | None, altman: AltmanResult | None, beneish: BeneishResult | None) -> ChartOut:
    fig = make_subplots(rows=3, cols=1, vertical_spacing=0.3,
                        subplot_titles=["Piotroski F-score (0–9)", "Altman Z''", "Beneish M-score"])
    notes, excluded = [], []
    # Piotroski: weak / middle / strong bands shaded like the Altman zones (PIOTROSKI_WEAK / _STRONG)
    weak, strong = config.PIOTROSKI_WEAK, config.PIOTROSKI_STRONG
    for x0, x1, colour in ((0, weak + 0.5, theme.CRITICAL), (weak + 0.5, strong - 0.5, theme.WARNING),
                           (strong - 0.5, 9.9, theme.GOOD)):
        fig.add_shape(type="rect", x0=x0, x1=x1, y0=-0.25, y1=0.25, fillcolor=colour, opacity=0.2,
                      line=dict(width=0), row=1, col=1)
    if pio is not None and pio.status == "ok" and pio.score is not None:
        fig.add_trace(go.Scatter(x=[pio.score], y=[0], mode="markers+text",
                                 text=[f"{pio.score} / 9 ({pio.available} of 9 checks)"],
                                 textposition="middle right" if pio.score < 5 else "middle left",
                                 marker=dict(symbol="line-ns", size=20, line=dict(width=3, color=theme.MARKER)),
                                 hovertemplate="Piotroski %{x} / 9<extra></extra>"), row=1, col=1)
        notes.append(f"Piotroski {pio.score} / 9 ({pio.available} of 9 checks available; weak ≤ {weak}, "
                     f"strong ≥ {strong})")
    else:
        why = pio.status if pio is not None else "N/A - Data Incomplete"
        excluded.append(f"Piotroski: {why}")
        fig.add_annotation(text=why, xref="x domain", yref="y domain", x=0.5, y=0.5, showarrow=False,
                           font=dict(color=theme.MUTED), row=1, col=1)
    fig.update_xaxes(range=[0, 9.9], dtick=1, row=1, col=1)
    # Altman: zones shaded
    zlo, zhi = config.ALTMAN_ZONES["distress_below"], config.ALTMAN_ZONES["safe_above"]
    z = altman.z.value if altman is not None and altman.z.ok else None
    amin, amax = min(0.0, (z or 0) - 0.5), max(4.0, (z or 0) + 0.5)
    for x0, x1, colour in ((amin, zlo, theme.CRITICAL), (zlo, zhi, theme.WARNING), (zhi, amax, theme.GOOD)):
        fig.add_shape(type="rect", x0=x0, x1=x1, y0=-0.25, y1=0.25, fillcolor=colour, opacity=0.35,
                      line=dict(width=0), row=2, col=1)
    if z is not None:
        fig.add_trace(go.Scatter(x=[z], y=[0], mode="markers+text", text=[f"{z:.2f} · {altman.zone}"],
                                 textposition="middle right" if z < (amin + amax) / 2 else "middle left",
                                 marker=dict(symbol="line-ns", size=20,
                                                                        line=dict(width=3, color=theme.MARKER)),
                                 hovertemplate="Altman Z'' %{x:.2f}<extra></extra>"), row=2, col=1)
        notes.append(f"Altman Z'' {z:.2f} ({altman.zone}; distress < {zlo}, safe > {zhi})")
    else:
        why = altman.status if altman is not None else "N/A - Data Incomplete"
        excluded.append(f"Altman Z'': {why}")
        fig.add_annotation(text=why, xref="x domain", yref="y domain", x=0.5, y=0.9, showarrow=False,
                           font=dict(color=theme.MUTED), row=2, col=1)
    fig.update_xaxes(range=[amin, amax], row=2, col=1)
    # Beneish: threshold marked
    thr = config.BENEISH_THRESHOLD
    m = beneish.m.value if beneish is not None and beneish.m.ok else None
    bmin, bmax = min(-4.0, (m or 0) - 0.5), max(0.0, (m or 0) + 0.5)
    fig.add_shape(type="rect", x0=thr, x1=bmax, y0=-0.25, y1=0.25, fillcolor=theme.CRITICAL, opacity=0.2,
                  line=dict(width=0), row=3, col=1)  # the "possible manipulation" side
    below = m is None or m < thr  # put the two labels on opposite sides of the threshold line
    fig.add_vline(x=thr, line=dict(color=theme.CRITICAL, width=2), row=3, col=1, exclude_empty_subplots=False,
                  annotation_text=f"flag above {thr}", annotation_position="top right" if below else "top left")
    if m is not None:
        fig.add_trace(go.Scatter(x=[m], y=[0], mode="markers+text",
                                 text=[f"{m:.2f} · {'flag' if beneish.flag else 'no flag'}"],
                                 textposition="middle left" if below else "middle right",
                                 marker=dict(symbol="line-ns", size=20, line=dict(width=3, color=theme.MARKER)),
                                 hovertemplate="Beneish M %{x:.2f}<extra></extra>"), row=3, col=1)
        notes.append(f"Beneish M {m:.2f} (flag above {thr}; " + ("flagged" if beneish.flag else "no flag") + ")")
    else:
        why = beneish.status if beneish is not None else "N/A - Data Incomplete"
        excluded.append(f"Beneish: {why}")
        fig.add_annotation(text=why, xref="x domain", yref="y domain", x=0.5, y=0.9, showarrow=False,
                           font=dict(color=theme.MUTED), row=3, col=1)
    fig.update_xaxes(range=[bmin, bmax], row=3, col=1)
    notes.append("Beneish is probabilistic; false positives happen.")
    for r in (1, 2, 3):
        fig.update_yaxes(visible=False, range=[-0.6, 0.8], row=r, col=1)
        fig.update_xaxes(zeroline=False, row=r, col=1)
    _base(fig, 300, showlegend=False)
    for a in fig.layout.annotations[:3]:
        a.update(font=dict(size=12), xanchor="left", x=0)
    return ChartOut(fig=fig, title="Value-trap scores", excluded=excluded, notes=notes)


# --------------------------------------------------------------------------
# Dividend panel (payers only)
# --------------------------------------------------------------------------
def dividend_panel(div: DividendSafety | None) -> tuple[ChartOut, ChartOut | None] | None:
    """(per-share bars, payout line) for payers; None for non-payers (the panel is hidden). The payout
    line is dividends ÷ FCF, or ÷ net income for financials and REITs (FCF is not meaningful for them);
    with no year to plot it is left off and the reasons go in the bars' caption."""
    if div is None or not div.payer:
        return None
    cut_years = {c.year for c in div.cuts}
    years = sorted(div.annual_per_share)
    bars = go.Figure(go.Bar(
        x=[str(y) for y in years], y=[div.annual_per_share[y] for y in years],
        marker=dict(color=[theme.DIVERGING_BELOW if y in cut_years else theme.PRIMARY for y in years]),
        hovertemplate="%{x}: %{y:,.4f} per share<extra></extra>"))
    _base(bars, 200, showlegend=False, yaxis_title="Dividend per share")
    bar_notes = [f"Calendar-year totals (split-adjusted); red = cut of more than {config.DIVIDEND_CUT_THRESHOLD:.0%}"]
    if div.payout_basis == "earnings":
        history, name, cap = div.earnings_payout_history, "Earnings payout", config.DIVIDEND_EARNINGS_PAYOUT_MAX_FINANCIALS
        note = "Dividends paid ÷ net income per fiscal year (financials and REITs: FCF is not meaningful)"
    else:
        history, name, cap = div.fcf_payout_history, "FCF payout", config.DIVIDEND_FCF_PAYOUT_MAX
        note = "Dividends paid ÷ raw FCF per fiscal year"
    ok = {k: d for k, d in history.items() if d.ok}
    excluded = [f"{k}: {d.status}" for k, d in history.items() if not d.ok]
    bars_out = ChartOut(fig=bars, title="Dividend per share", notes=bar_notes)
    if not ok:  # nothing to plot: never draw an empty frame
        bars_out.excluded.append(f"{name} chart: no fiscal year with a value"
                                 + (f" ({'; '.join(excluded)})" if excluded else " (dividends paid not reported)"))
        return bars_out, None
    line = go.Figure(go.Scatter(x=list(ok), y=[d.value for d in ok.values()], mode="lines+markers",
                                line=dict(color=theme.PRIMARY, width=2), marker=dict(size=8),
                                hovertemplate=f"%{{x}}: %{{y:.0%}}<extra>{name}</extra>"))
    line.add_hline(y=cap, line=dict(color=theme.CRITICAL, dash="dash", width=1), annotation_text=f"{cap:.0%}")
    _base(line, 170, showlegend=False, yaxis_title=name)
    line.update_yaxes(tickformat=".0%")
    return bars_out, ChartOut(fig=line, title=f"{name} by fiscal year", excluded=excluded, notes=[note])


# --------------------------------------------------------------------------
# Turnaround
# --------------------------------------------------------------------------
def turnaround_range_bar(t: TurnaroundResult | None) -> ChartOut | None:
    if t is None or not t.has_range or t.median_months is None:
        return None
    lo, hi = t.iqr_months
    top = max(24.0, hi * 1.3)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=[0, top], y=[0, 0], mode="lines", line=dict(color=theme.GRID, width=10),
                             hoverinfo="skip"))
    fig.add_trace(go.Scatter(x=[lo, hi], y=[0, 0], mode="lines", line=dict(color="rgba(42,120,214,0.45)", width=10),
                             hovertemplate=f"interquartile range {lo:.0f}–{hi:.0f} months<extra></extra>"))
    fig.add_trace(go.Scatter(x=[t.median_months], y=[0], mode="markers",
                             marker=dict(symbol="line-ns", size=24, line=dict(width=3, color=theme.PRIMARY)),
                             hovertemplate=f"median {t.median_months:.0f} months<extra></extra>"))
    _base(fig, 80, showlegend=False, margin=dict(l=10, r=10, t=5, b=25))
    fig.update_xaxes(range=[0, top], title=None, ticksuffix=" mo")
    fig.update_yaxes(visible=False, range=[-0.5, 0.5])
    return ChartOut(fig=fig, title="Turnaround range",
                    notes=[f"Bar = interquartile range {lo:.0f}–{hi:.0f} months; line = median "
                           f"{t.median_months:.0f}"])


def indexed(series: pd.Series, start: pd.Timestamp) -> pd.Series:
    """Price series indexed to 100 at `start`. Prices only: they stay positive."""
    s = series.dropna().sort_index()
    s = s[s.index >= start]
    if s.empty or s.iloc[0] <= 0:
        raise ValueError("indexing needs a positive first value")
    return s / s.iloc[0] * 100


def drawdown_history(closes: pd.Series | None, bench: pd.Series | None, t: TurnaroundResult,
                     ticker: str) -> ChartOut | None:
    if closes is None or closes.dropna().empty:
        return None
    closes = closes.dropna().sort_index()
    excluded = []
    start = closes.index[0]
    if bench is not None and not bench.dropna().empty:
        start = max(start, bench.dropna().index[0])
    else:
        excluded.append(f"benchmark {t.benchmark or '(none)'}: history unavailable")
    fig = go.Figure()
    fills = {MARKET_DRIVEN: theme.MARKET_DRIVEN_FILL, COMPANY_SPECIFIC: theme.COMPANY_SPECIFIC_FILL}
    end = closes.index[-1]
    shaded = set()
    seg_end = {sg.index: pd.Timestamp(sg.end) for sg in t.segments}
    for e in t.episodes:
        # An unrecovered episode ends at its own segment's end (a break cut it off) or today.
        x1 = pd.Timestamp(e.recovery_date) if e.recovery_date else seg_end.get(e.segment, end)
        if x1 < start:
            continue
        fig.add_vrect(x0=max(pd.Timestamp(e.peak_date), start), x1=x1, line_width=0, layer="below",
                      fillcolor=fills.get(e.episode_type, theme.UNCLASSIFIED_FILL))
        shaded.add(e.episode_type)
    for typ in (MARKET_DRIVEN, COMPANY_SPECIFIC, "unclassified"):
        if typ in shaded:
            fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=f"{typ} drop",
                                     marker=dict(symbol="square", size=12,
                                                 color=fills.get(typ, theme.UNCLASSIFIED_FILL))))
    if bench is not None and not bench.dropna().empty:
        b = indexed(bench, start)
        fig.add_trace(go.Scatter(x=b.index, y=b.values, mode="lines", name=t.benchmark or "benchmark",
                                 line=dict(color=theme.MUTED, width=1.5, dash="dash"),
                                 hovertemplate="%{x|%Y-%m-%d}: %{y:.1f}<extra>" + (t.benchmark or "") + "</extra>"))
    s = indexed(closes, start)
    fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines", name=ticker,
                             line=dict(color=theme.ticker_color(ticker), width=2),
                             hovertemplate="%{x|%Y-%m-%d}: %{y:.1f}<extra>" + ticker + "</extra>"))
    for br in t.breaks:
        fig.add_vline(x=pd.Timestamp(br.date), line=dict(color=theme.MARKER, width=1.5, dash="dot"),
                      annotation_text=f"break: {br.type}", annotation_position="top left")
    _base(fig, 320, legend=dict(orientation="h", y=-0.15), yaxis_title=f"Indexed (100 = {start.date()})")
    fig.update_xaxes(type="date", range=[start, end])
    fig.update_yaxes(zeroline=False)
    notes = [f"{ticker} and {t.benchmark or 'benchmark'} adjusted closes, both indexed to 100 at {start.date()}; "
             "shaded = past drawdown episodes by type" + ("; dotted lines = corporate-action breaks" if t.breaks else "")]
    return ChartOut(fig=fig, title="Price history and past drops", excluded=excluded, notes=notes)


# --------------------------------------------------------------------------
# Screener scatter
# --------------------------------------------------------------------------
class ScatterPoint(BaseModel):
    ticker: str
    name: str = ""
    mos: float
    quality: float
    quality_display: str = ""
    label: bool = False
    status: str = "Pass"  # screen status, shown by the marker's shape (colour stays the ticker's)


# Marker shape per screen status (colour is the ticker's identity colour everywhere, SPEC "Charts").
STATUS_SYMBOLS = {"Pass": "circle", "Incomplete": "circle-open", "Fail": "x-thin-open"}
STATUS_SYMBOL_LEGEND = "● Pass · ○ Incomplete · ✕ Fail"


def group_excluded(excluded: list[str], limit: int = config.CHANGES_LIST_MAX) -> list[str]:
    """"TICKER: reason" items grouped by reason, each with up to `limit` tickers and "+n more", so a
    caption stays readable when hundreds are left off (the page lists every one in full)."""
    groups: dict[str, list[str]] = {}
    for item in excluded:
        ticker, _, reason = item.partition(": ")
        groups.setdefault(reason or "no reason recorded", []).append(ticker)
    out = []
    for reason, tickers in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        names = ", ".join(tickers[:limit]) + (f" +{len(tickers) - limit} more" if len(tickers) > limit else "")
        out.append(f"{len(tickers)} {reason}: {names}")
    return out


def screener_scatter(points: list[ScatterPoint], excluded: list[str]) -> ChartOut:
    fig = go.Figure()
    if points:
        # Labels alternate above and below in margin-of-safety order, so neighbours don't print over each other.
        order = {id(p): k for k, p in enumerate(sorted(points, key=lambda p: p.mos))}
        fig.add_trace(go.Scatter(
            x=[p.mos for p in points], y=[p.quality for p in points], mode="markers+text",
            text=[p.ticker if p.label else "" for p in points],
            textposition=["top center" if order[id(p)] % 2 == 0 else "bottom center" for p in points],
            customdata=[[p.ticker, p.name, p.quality_display, p.status] for p in points],
            marker=dict(size=11, color=[theme.ticker_color(p.ticker) for p in points],
                        symbol=[STATUS_SYMBOLS.get(p.status, "circle") for p in points],
                        opacity=[1.0 if p.status == "Pass" else 0.55 for p in points],
                        line=dict(width=2, color=[theme.ticker_color(p.ticker) if p.status != "Pass" else "white"
                                                  for p in points])),
            hovertemplate="%{customdata[0]} — %{customdata[1]}<br>Margin of safety %{x:+.0%}"
                          "<br>Quality %{customdata[2]}<br>%{customdata[3]}<extra></extra>"))
    fig.add_vline(x=0, line=dict(color=theme.GRID, dash="dash", width=1))
    hurdle = config.MIN_MARGIN_OF_SAFETY
    fig.add_vline(x=hurdle, line=dict(color=theme.REF_LINE, dash="dot", width=1.5),
                  annotation_text=f"pass ≥ {hurdle:.0%}", annotation_position="top right")
    _base(fig, 400, showlegend=False, xaxis_title="Margin of safety (Graham Number vs price)",
          yaxis_title="Quality score (0–10)")
    fig.update_xaxes(tickformat=".0%")
    fig.update_yaxes(range=[0, 11], tickvals=list(range(0, 11, 2)))  # headroom so labels at 10 aren't clipped
    notes = [f"{len(points)} tickers; labels on the top {config.SCATTER_LABEL_TOP_N} by quality rank + margin-of-safety "
             f"rank; {STATUS_SYMBOL_LEGEND}; dotted line = the margin-of-safety pass mark "
             f"(MIN_MARGIN_OF_SAFETY {hurdle:.0%}); click a point to open it"]
    return ChartOut(fig=fig, title="Margin of safety vs quality", excluded=group_excluded(excluded), notes=notes)


# --------------------------------------------------------------------------
# History and estimate accuracy
# --------------------------------------------------------------------------
def verdict_history(points: list[tuple[date, float | None, str]]) -> ChartOut:
    """(run date, aggregate score, label) per run; runs without a score are listed, not plotted."""
    ok = [(d, s, lab) for d, s, lab in points if s is not None]
    excluded = [f"{d}: {lab}" for d, s, lab in points if s is None]
    fig = go.Figure(go.Scatter(x=[d for d, _, _ in ok], y=[s for _, s, _ in ok], mode="lines+markers",
                               text=[lab for _, _, lab in ok], line=dict(color=theme.PRIMARY, width=2),
                               marker=dict(size=9), hovertemplate="%{x|%Y-%m-%d}: %{y:.1f} (%{text})<extra></extra>"))
    _base(fig, 240, showlegend=False, yaxis_title="Aggregate (1–10)")
    fig.update_yaxes(range=[config.SCORE_MIN - 0.5, config.SCORE_MAX + 0.5])
    return ChartOut(fig=fig, title="Aggregate verdict over time", excluded=excluded)


def accuracy_bars(by_type: list) -> ChartOut:
    """One stacked horizontal bar per episode type: recovered in window / still waiting / missed."""
    labels = [a.episode_type for a in by_type]
    fig = go.Figure()
    for attr, name, colour in (("recovered", "Recovered in window", theme.ACC_RECOVERED),
                               ("waiting", "Still waiting", theme.ACC_WAITING),
                               ("missed", "Missed", theme.ACC_MISSED)):
        fig.add_trace(go.Bar(y=labels, x=[getattr(a, attr) for a in by_type], orientation="h", name=name,
                             marker=dict(color=colour, line=dict(width=2, color="white")),
                             hovertemplate="%{y}: %{x} " + name.lower() + "<extra></extra>"))
    for a in by_type:
        fig.add_annotation(x=a.scored, y=a.episode_type, text=f"{a.recovered}/{a.scored} hit", showarrow=False,
                           xanchor="left", xshift=6)
    _base(fig, 90 + 50 * max(1, len(by_type)), barmode="stack", legend=dict(orientation="h", y=-0.3),
          xaxis_title="Scored estimates (first per drawdown episode)")
    return ChartOut(fig=fig, title="Estimate accuracy")
