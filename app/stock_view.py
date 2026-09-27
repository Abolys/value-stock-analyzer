"""Stock page view model (pure; no Streamlit), shared by the page and the exports:
header tags, the verdict badge and as-of line, the price bundle behind the
charts, every chart for one AnalysisRun, and the list of data gaps."""

from __future__ import annotations

from datetime import date

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

import config
from analysis.models import LENSES, AggregateResult, AnalysisRun, QuantResult
from analysis.turnaround import Benchmarks, benchmark_for, listing_country, select_peers
from app import charts
from app.charts import ChartOut
from data import prices
from data.insiders import COVERAGE_NONE
from data.leadership import NO_SOURCE
from data.provider import DataProvider, ProviderError
from screening.models import ScreenResult
from storage import screen_store

LENS_SHORT = {"quant": "Quant fundamental", "macro": "Macro & balance sheet", "moat": "Business moat",
              "devils_advocate": "Devil's advocate"}


class Tag(BaseModel):
    text: str
    kind: str = "accent"  # warning | accent | danger | success
    tip: str = ""


def verdict_badge(agg: AggregateResult | None) -> str:
    if agg is None:
        return "Verdict pending"
    if agg.score is None:
        return f"Insufficient data — 0 of {agg.lenses_total} lenses"
    return f"{agg.score:.1f} / 10 — {agg.verdict} · {agg.lenses_used} of {agg.lenses_total} lenses"


def asof_line(run: AnalysisRun) -> str:
    kind = "annual, not TTM" if "annual" in (run.fundamentals_label or "") else "TTM"
    fund = run.fundamentals_as_of.isoformat() if run.fundamentals_as_of else "N/A"
    price = run.price_as_of.isoformat() if run.price_as_of else "N/A"
    return f"Price as of {price} · Fundamentals as of {fund} ({kind})"


def leadership_tag(run: AnalysisRun) -> Tag:
    lead = run.leadership
    if lead is None:
        return Tag(text="Leadership: N/A - not loaded", kind="accent", tip="leadership flag not evaluated")
    if lead.flag == NO_SOURCE:
        return Tag(text=f"Leadership: {NO_SOURCE}", kind="accent", tip="no 8-K, 6-K, officer snapshot or manual layer")
    tip = "; ".join(f"{e.date} {e.role} {e.person or '(name not parsed)'} — {', '.join(e.sources) or e.layer}"
                    for e in lead.events) or "no CEO/CFO departures found in the coverage window"
    if lead.partial_coverage:
        tip += " · partial coverage: absence of events is 'unknown', not 'clean'"
    kind = "warning" if lead.departures else "accent"
    return Tag(text=f"Leadership: {lead.summary}", kind=kind, tip=tip)


def header_tags(run: AnalysisRun) -> list[Tag]:
    tags: list[Tag] = []
    agg = run.aggregate
    if agg is not None and agg.controversy:
        tags.append(Tag(text="High controversy", kind="warning",
                        tip=f"Devil's Advocate {agg.controversy_gap:.1f} points below the other lenses' mean "
                            f"(threshold {config.CONTROVERSY_GAP:g})"))
    s = run.screen
    if s is not None and s.trap_risk:
        zone = f"Altman {s.altman.zone}" if s.altman and s.altman.zone else "; ".join(s.trap_risk_reasons)
        tags.append(Tag(text=f"Trap risk: {zone}", kind="warning", tip="; ".join(s.trap_risk_reasons)))
    if run.insiders is not None and run.insiders.cluster_buy:
        w = run.insiders.cluster_window
        tags.append(Tag(text="Insider cluster buy", kind="accent",
                        tip=f"{run.insiders.buyers} insiders buying" + (f", {w[0]} to {w[1]}" if w else "")))
    tags.append(leadership_tag(run))
    if run.sector_adjusted:
        tags.append(Tag(text=run.treatment or "Sector-adjusted", kind="accent",
                        tip="Financials and REITs: EBITDA, net debt/EBITDA and FCF are not meaningful; "
                            "sector-adjusted metrics are used"))
    if run.stale:
        tags.append(Tag(text="Fundamentals may be stale", kind="warning", tip=run.stale_label))
    if s is not None and s.asset_floor is not None and s.asset_floor.net_net:
        tags.append(Tag(text="Net-net", kind="success", tip=s.asset_floor.burn_line or "NCAV ≥ market cap"))
    return tags


def held_tags(ticker: str, db_path=None) -> list[Tag]:
    """A "Held (account)" tag per open holding of the ticker (Phase 6)."""
    from portfolio import store

    out = []
    for h in store.list_holdings(ticker=ticker, path=db_path):
        unread = store.unread_count(ticker, db_path)
        out.append(Tag(text=f"Held ({h.account})", kind="success",
                       tip=f"first bought {h.first_buy}; {unread} unread alert(s); see the Portfolio page"))
    return out


# --------------------------------------------------------------------------
# Prices and peers behind the charts
# --------------------------------------------------------------------------
class PriceBundle(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    closes: pd.Series | None = None
    bench: pd.Series | None = None
    peers: list[ScreenResult] = Field(default_factory=list)
    peer_note: str = ""
    errors: list[str] = Field(default_factory=list)


def load_bundle(provider: DataProvider, run: AnalysisRun, db_path=None) -> PriceBundle:
    db_path = db_path or config.RUNS_DB_PATH
    b = PriceBundle()
    try:
        b.closes = prices.adjusted_closes(provider, run.ticker).dropna()
    except ProviderError as exc:
        b.errors.append(f"price history unavailable: {exc}")
    symbol = benchmark_for(run.ticker)
    b.bench, why = Benchmarks(provider).get(symbol, listing_country(run.ticker))
    if why:
        b.errors.append(f"benchmark: {why}")
    s = run.screen
    mcap = s.market_cap.value if s is not None and s.market_cap.ok else None
    sel = select_peers(run.ticker, run.industry, mcap, db_path)
    if sel.ok and sel.screen_run_id is not None:
        wanted = {p.ticker for p in sel.peers}
        b.peers = [r for r in screen_store.load_results(sel.screen_run_id, db_path) if r.ticker in wanted]
        b.peer_note = (f"{len(b.peers)} peers from your universe lists (same industry, nearest market cap; "
                       f"screen run {sel.screen_run_id}, {sel.screen_run_date})")
    else:
        b.peer_note = sel.status if sel.status != "ok" else "no peers found in the universe lists"
    return b


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------
def lens_list(run: AnalysisRun) -> list:
    return [run.lens(n) for n in LENSES]


def build_charts(run: AnalysisRun, bundle: PriceBundle) -> dict[str, ChartOut | None]:
    s = run.screen
    q = run.quant
    out: dict[str, ChartOut | None] = {
        "week52": charts.week52_bar(run.week52) if run.week52 else None,
        "small_multiples": charts.small_multiples(
            bundle.closes, run.series, run.insiders.history_trades if run.insiders else [], run.ticker),
        "dot_strip": charts.dot_strip(lens_list(run), run.aggregate, [LENS_SHORT[n] for n in LENSES]),
        "peers": charts.peer_strip(s, bundle.peers, bundle.peer_note) if s is not None and bundle.peers else None,
        "heatmap": charts.sensitivity_heatmap(q.grid, s.price, q.reverse_dcf)
        if isinstance(q, QuantResult) and s is not None else None,
        "asset_floor": charts.asset_floor_panel(s.asset_floor, s.market_cap, run.currency) if s is not None else None,
        "trap": charts.trap_panel(s.piotroski, s.altman, s.beneish) if s is not None else None,
        "turnaround_range": charts.turnaround_range_bar(run.turnaround),
        "drawdown": charts.drawdown_history(bundle.closes, bundle.bench, run.turnaround, run.ticker)
        if run.turnaround is not None else None,
    }
    div = charts.dividend_panel(run.dividends)
    out["dividend_bars"], out["dividend_payout"] = div if div else (None, None)
    return out


# --------------------------------------------------------------------------
# Text blocks
# --------------------------------------------------------------------------
def months(d) -> str:
    return f"{d.value:.1f} months" if d.ok else d.status


def valuation_lines(q: QuantResult | None, currency: str | None = None) -> list[str]:
    if q is None:
        return ["Quant lens pending"]
    if not q.ok and q.method not in ("runway",):
        lines = [f"Valuation: {q.status}"]
    else:
        lines = []
    if q.method == "runway":
        lines.append(f"{q.fcf_negative}: cash runway {months(q.runway_months)} replaces the DCF fair value")
    if q.dcf is not None and q.dcf.ok:
        label = "normalised base-case fair value" if q.peak and q.peak.flagged else "Base-case fair value"
        lines.append(f"{label}: {q.dcf.fair_value:,.2f} {currency or ''} ({q.dcf.upside:+.0%} vs price)".strip())
        if q.peak and q.peak.flagged and q.dcf_raw is not None and q.dcf_raw.ok:
            lines.append(f"Raw fair value {q.dcf_raw.fair_value:,.2f} vs normalised {q.dcf.fair_value:,.2f} — "
                         f"possibly peak earnings: {q.peak.detail}")
    elif q.dcf is not None:
        lines.append(f"DCF: {q.dcf.status}")
    if q.grid is not None:
        lines.append(f"Fair-value range (central 3 × 3): {q.grid.range_display}")
    if q.reverse_dcf is not None:
        lines.append(q.reverse_dcf.display)
    if q.graham.ok:
        lines.append(f"Graham Number {q.graham.value:,.2f}" + (f" ({q.graham_upside:+.0%} vs price)"
                                                               if q.graham_upside is not None else ""))
    for c in q.confidence_reasons:
        lines.append(f"Low confidence: {c}")
    return lines


def insider_line(run: AnalysisRun) -> str:
    i = run.insiders
    if i is None:
        return f"Insiders: {COVERAGE_NONE}"
    if not i.available:
        return f"Insiders: {i.coverage}"
    return (f"Insiders since {i.since}: {i.buyers} buying, {i.sellers_discretionary} selling "
            f"(+{i.sellers_10b5_1} under 10b5-1 plans); net {i.net_shares:+,.0f} shares"
            + (" · cluster buy" if i.cluster_buy else "") + f" · coverage: {i.coverage}")


def ev_line(run: AnalysisRun) -> str:
    s = run.screen
    if s is None or s.earnings_yield is None:
        return "EV/EBIT earnings yield: N/A - Data Incomplete"
    ey = s.earnings_yield
    if ey.net_cash_flag:
        return f"EV/EBIT earnings yield: {ey.value.status} — net cash exceeds market cap (a flag worth a look)"
    return f"EV/EBIT earnings yield: {ey.display}"


def context_items(run: AnalysisRun) -> list[tuple[str, str]]:
    c = run.context
    if c is None:
        return [("Context", "N/A - Data Incomplete")]
    io = f"{c.insider_ownership.value:.1%}" if c.insider_ownership.ok else c.insider_ownership.status
    si = f"{c.short_interest.value:.1%} of float" if c.short_interest.ok else c.short_interest.status
    rev = c.revisions_90d if c.revisions_90d.startswith("N/A") else f"revised {c.revisions_90d} (90 days)"
    return [("Insider ownership", io), ("Short interest", si), ("Analyst EPS estimates", rev)]


def dividend_line(run: AnalysisRun) -> str:
    d = run.dividends
    if d is None:
        return "Dividend: N/A - Data Incomplete"
    if not d.payer:
        return f"Dividend: {d.status}"
    y = f"{d.trailing_yield.value:.1%}" if d.trailing_yield.ok else d.trailing_yield.status
    p = f"{d.fcf_payout.value:.0%}" if d.fcf_payout.ok else d.fcf_payout.status
    e = f"{d.earnings_payout.value:.0%}" if d.earnings_payout.ok else d.earnings_payout.status
    return (f"Yield {y} · FCF payout {p} · earnings payout {e} · {len(d.cuts)} cut(s) in {d.history_span or 'N/A'}"
            f" · {d.uninterrupted_years} uninterrupted years" + (f" · AT RISK: {d.at_risk_reason}" if d.at_risk else ""))


def data_gaps(run: AnalysisRun, chart_map: dict[str, ChartOut | None] | None = None) -> list[tuple[str, str]]:
    """Every N/A and n/m the view shows, with its reason (Rule 2: gaps are visible)."""
    gaps: list[tuple[str, str]] = []
    s = run.screen
    if run.stale:
        gaps.append(("Fundamentals", run.stale_label))
    if s is not None:
        for m in s.metrics:
            if not m.value.ok:
                gaps.append((m.name, m.value.status))
            elif m.na_reason:
                gaps.append((m.name, m.na_reason))
        for name, d in s.inputs.items():
            if not d.ok:
                gaps.append((f"input: {name}", d.status))
    for n in LENSES:
        lens = run.lens(n)
        if lens is None:
            gaps.append((LENS_SHORT[n], "not run"))
        elif not lens.ok:
            gaps.append((lens.label, lens.status))
        elif lens.completeness:
            gaps.append((f"{lens.label} completeness", lens.completeness))
    for label, value in context_items(run):
        if value.startswith(("N/A", "n/m")):
            gaps.append((label, value))
    lead = run.leadership
    if lead is None or lead.flag == NO_SOURCE:
        gaps.append(("Leadership", NO_SOURCE if lead else "not loaded"))
    elif lead.partial_coverage:
        gaps.append(("Leadership", f"partial coverage ({lead.coverage_label})"))
    if run.insiders is not None and not run.insiders.available:
        gaps.append(("Insider activity", run.insiders.coverage))
    t = run.turnaround
    if t is not None:
        if t.status.startswith("Insufficient"):
            gaps.append(("Turnaround", t.status))
        gaps.append(("Valuation-based recovery", t.valuation_recovery))
        gaps.extend(("Turnaround note", n) for n in t.notes)
    gaps.extend(("Note", n) for n in run.notes)
    for key, c in (chart_map or {}).items():
        if c is not None:
            gaps.extend((f"{c.title} chart", e) for e in c.excluded)
    seen, out = set(), []
    for g in gaps:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


def today_or(run: AnalysisRun) -> date:
    return run.today or date.today()
