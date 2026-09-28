"""Stock page (docs/ui-mockup.html → Stock page).

Opening a ticker runs the full analysis once per session (memoised in
st.session_state; "Re-run analysis" forces a fresh run). Loading is
progressive: every section is a placeholder that says what it is waiting on;
the header, 52-week bar and charts fill in as soon as the inputs are loaded,
each lens as it finishes (Quant, Macro and Moat in parallel, the Devil's
Advocate last), then the aggregate and the turnaround.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st
from pydantic import BaseModel, ConfigDict

import config
from analysis.models import LENSES, AnalysisRun, DevilsAdvocateResult, LensResult, LLMLensResult, MoatResult, QuantResult
from analysis.pipeline import run_analysis
from analysis.turnaround import clock_label
from analysis.turnaround_models import STATUS_WITHHELD
from app import charts, services, ui
from app import stock_view as sv
from app.charts import ChartOut
from app.views.raw import raw_data
from app.views.thesis_form import add_holding_dialog
from llm.client import LLMClient
from portfolio.alerts import journal_for
from reports.build import build_report
from reports.docx_export import to_docx
from reports.markdown import to_markdown
from screening.engine import ScreenContext
from storage import history

LENS_TAB = {"quant": "Quant", "macro": "Macro", "moat": "Moat", "devils_advocate": ":orange[⚠ Devil's Advocate]"}
WAITING = {
    "header": "Loading statements, screen metrics, insiders and leadership…",
    "dots": "Waiting on: the four lenses",
    "fundamentals": "Waiting on: statements and price history",
    "peers": "Waiting on: screen metrics and peer selection",
    "asset_floor": "Waiting on: balance sheet",
    "valuation": "Waiting on: Quant lens (DCF, sensitivity grid, reverse DCF)",
    "trap": "Waiting on: statements (Piotroski, Altman, Beneish) and Form 4",
    "turnaround": "Waiting on: all four lenses, then the price history and episodes",
    "dividend": "Waiting on: dividend history",
    "quant": "Waiting on: Quant lens (running in parallel with Macro and Moat)",
    "macro": "Waiting on: Macro lens (running in parallel with Quant and Moat)",
    "moat": "Waiting on: Moat lens (LLM; running in parallel with Quant and Macro)",
    "devils_advocate": "Waiting on: Quant, Macro and Moat — the Devil's Advocate runs after them",
    "turnaround_tab": "Waiting on: the turnaround estimate",
    "history": "Waiting on: this run to finish",
    "export": "Waiting on: the full analysis",
}


class Entry(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run: AnalysisRun
    bundle: sv.PriceBundle | None = None
    charts: dict[str, ChartOut | None] = {}
    stale_note: str = ""  # data served from cache because the live source failed, with its age


class Layout:
    """Placeholders in the mockup's order; each shows what it is waiting on until filled."""

    def __init__(self):
        self.slots: dict[str, st.delta_generator.DeltaGenerator] = {}
        self.slots["header"] = st.empty()
        st.subheader("Lens scores")
        self.slots["dots"] = st.empty()
        st.subheader("Fundamentals over time")
        self.slots["fundamentals"] = st.empty()
        st.subheader("Versus peers")
        self.slots["peers"] = st.empty()
        st.subheader("Asset floor · if the earnings case fails")
        self.slots["asset_floor"] = st.empty()
        left, right = st.columns(2)
        with left:
            st.subheader("Valuation")
            self.slots["valuation"] = st.empty()
        with right:
            st.subheader("Value-trap scores")
            self.slots["trap"] = st.empty()
        st.subheader("Turnaround outlook")
        self.slots["turnaround"] = st.empty()
        st.subheader("Dividend and context")
        self.slots["dividend"] = st.empty()
        tabs = st.tabs([LENS_TAB[n] for n in LENSES] + ["Turnaround details", "History", "Raw data"])
        for name, tab in zip([*LENSES, "turnaround_tab", "history", "raw"], tabs):
            with tab:
                self.slots[name] = st.empty()
        st.subheader("Export")
        self.slots["export"] = st.empty()
        for name, msg in WAITING.items():
            self.slots[name].caption(f"⏳ {msg}")

    def __getitem__(self, name: str):
        return self.slots[name]


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
def draw_header(slot, run: AnalysisRun, ch: dict[str, ChartOut | None]) -> None:
    s = run.screen
    with slot.container():
        left, right = st.columns([3, 2])
        price = f"{s.price.value:,.2f} {run.currency or ''}" if s is not None and s.price.ok else "price N/A"
        left.markdown(f"## {run.company or run.ticker} <span style='font-size:0.9rem;color:#898781'>"
                      f"{run.ticker} · {price}</span>", unsafe_allow_html=True)
        kind = "accent" if run.aggregate is None or run.aggregate.score is None else (
            "success" if run.aggregate.score >= 6 else "warning" if run.aggregate.score >= 4 else "danger")
        right.markdown(f"<div style='text-align:right;padding-top:1rem'>"
                       f"{ui.badge_html(sv.verdict_badge(run.aggregate), kind)}</div>", unsafe_allow_html=True)
        st.caption(sv.asof_line(run) + f" · {run.sector or 'N/A'} / {run.industry or 'N/A'} · {run.treatment}")
        st.markdown(ui.tags_html(sv.header_tags(run) + sv.held_tags(run.ticker, config.RUNS_DB_PATH)),
                    unsafe_allow_html=True)
        if ch.get("week52") is not None:
            ui.chart(ch["week52"])
        for n in run.notes:
            st.caption(f"Note: {n}")


def draw_dots(slot, run: AnalysisRun) -> None:
    with slot.container():
        ui.chart(charts.dot_strip(sv.lens_list(run), run.aggregate, [sv.LENS_SHORT[n] for n in LENSES]),
)


def draw_valuation(slot, run: AnalysisRun, ch: dict[str, ChartOut | None]) -> None:
    q = run.quant
    with slot.container():
        for line in sv.valuation_lines(q if isinstance(q, QuantResult) else None, run.currency):
            st.markdown(("⚠️ " if line.startswith("Low confidence") else "") + line)
        if ch.get("heatmap") is not None:
            ui.chart(ch["heatmap"])


def draw_trap(slot, run: AnalysisRun, ch: dict[str, ChartOut | None]) -> None:
    with slot.container():
        ui.chart(ch.get("trap"))
        st.markdown(sv.ev_line(run))
        st.markdown(sv.insider_line(run))


def draw_turnaround(slot, run: AnalysisRun, ch: dict[str, ChartOut | None]) -> None:
    t = run.turnaround
    with slot.container():
        if t is None:
            st.caption("Turnaround not run.")
            return
        with st.container(border=True):
            conf = [sv.Tag(text=f"{t.confidence} confidence", kind="warning")] if t.confidence else []
            st.markdown(f"**{t.headline or t.status}** " + ui.tags_html(conf), unsafe_allow_html=True)
            if t.status == STATUS_WITHHELD:
                st.warning(t.structural_note)
            ui.chart(ch.get("turnaround_range"))
            st.caption(t.survivorship_caveat)
            if t.secondary_line:
                st.caption(t.secondary_line + " (same episodes, a different start for the clock)")
            active = t.active_signals
            st.markdown("**Near-term signals:** " + ("; ".join(f"{x.name} — {x.detail}" for x in active)
                                                      if active else "none active"))
            st.markdown("**Catalysts:** " + "; ".join(x.text + (f" _({x.source})_" if x.source else "")
                                                       for x in t.catalysts))
            st.caption(t.asset_floor_line + " (context only; never changes the range or confidence)")
        ui.chart(ch.get("drawdown"))


def draw_dividend(slot, run: AnalysisRun, ch: dict[str, ChartOut | None]) -> None:
    with slot.container():
        d = run.dividends
        if d is not None and d.payer:
            left, right = st.columns([3, 2])
            with left:
                ui.chart(ch.get("dividend_bars"))
                ui.chart(ch.get("dividend_payout"))
            with right:
                if d.at_risk:
                    st.markdown(ui.tags_html([sv.Tag(text="Dividend at risk", kind="danger", tip=d.at_risk_reason)]),
                                unsafe_allow_html=True)
                st.markdown(sv.dividend_line(run))
        else:
            st.caption(sv.dividend_line(run) + " — dividend panel hidden for non-payers")
        cols = st.columns(3)
        for col, (label, value) in zip(cols, sv.context_items(run)):
            col.markdown(f"<span style='font-size:0.8rem;color:#898781'>{label}</span><br>{value}",
                         unsafe_allow_html=True)


def draw_lens(slot, lens: LensResult | None) -> None:
    with slot.container():
        if lens is None:
            st.caption("Not run.")
            return
        box = st.warning if isinstance(lens, DevilsAdvocateResult) else st.info
        box(f"**{lens.label}: {lens.display}**" + (f" · confidence {lens.confidence}" if lens.ok else ""))
        if lens.status.startswith("Insufficient") and "error" in lens.status:
            st.error(lens.status)
        if lens.stale:
            st.warning(lens.stale_label)
        for c in lens.confidence_reasons:
            st.markdown(f"⚠️ **Low confidence:** {c}")
        if lens.mapping_steps:
            st.markdown("**How this score was built:** " + " → ".join(s.line for s in lens.mapping_steps))
        elif lens.mapping_line:
            st.markdown(f"**How this score was built:** {lens.mapping_line}")
        if lens.completeness:
            st.caption(f"Data completeness: {lens.completeness}")
        if isinstance(lens, QuantResult) and lens.method == "runway":
            st.markdown(f"FCF-negative: cash runway **{sv.months(lens.runway_months)}** replaces the DCF "
                        "fair value.")
        if isinstance(lens, MoatResult) and lens.sector_threat:
            st.markdown(f"**Sector threat:** {lens.sector_threat}")
        if isinstance(lens, LLMLensResult) and lens.evidence:
            st.markdown("**Evidence:**\n" + "\n".join(f"- `{e.field}` = {e.value} — {e.why}" for e in lens.evidence))
        if isinstance(lens, DevilsAdvocateResult):
            for label, value in (("Weakest valuation assumption", lens.weakest_valuation_assumption),
                                 ("Weakest moat point", lens.weakest_moat_point),
                                 ("Accounting red flags", lens.accounting_red_flags),
                                 ("Leadership", lens.leadership_turnover),
                                 ("Impairment", f"{lens.impairment_type} — {lens.impairment_reasoning}")):
                if value:
                    st.markdown(f"**{label}:** {value}")
        if lens.key_figures:
            st.dataframe(pd.DataFrame([{"figure": k, "value": v} for k, v in lens.key_figures.items()]),
                         hide_index=True, width="stretch")
        with st.expander("Rationale and assumptions"):
            st.markdown(lens.rationale)
            st.json({k: (v if isinstance(v, (int, float, str, bool, list, dict)) or v is None else str(v))
                     for k, v in lens.assumptions.items()}, expanded=False)
            if getattr(lens, "payload", None):
                st.caption("LLM payload (compact JSON sent to the model)")
                st.json(lens.payload, expanded=False)


def draw_turnaround_details(slot, run: AnalysisRun) -> None:
    t = run.turnaround
    with slot.container():
        if t is None:
            st.caption("Turnaround not run.")
            return
        if t.confidence:
            st.markdown(f"**Confidence: {t.confidence}** — " + "; ".join(t.confidence_reasons))
        st.caption(f"Confidence rule: {t.confidence_rule}")
        if t.basis_note:
            st.caption(f"Basis: {t.basis or 'none'} — {t.basis_note}")
        if t.current is not None:
            c = t.current
            st.markdown(f"Current drop: {c.label} (52-week high {c.high_price:,.2f} on {c.high_date}; adjusted closes)"
                        + (f" · type **{c.episode_type}**" if c.qualifying else ""))
            if c.note:
                st.caption(c.note)
        st.caption(f"Structural check: {t.structural_note}")
        st.markdown(f"Valuation-based recovery: {t.valuation_recovery}")
        if t.peer is not None:
            st.markdown("**Peers:** " + (", ".join(p.ticker for p in t.peer.peers) or "none") + " — "
                        + (t.peer.source_note or t.peer.status))
        if t.episodes:
            st.dataframe(pd.DataFrame([{
                "peak": e.peak_date.isoformat(), "trough": e.trough_date.isoformat(),
                "recovered": e.recovery_date.isoformat() if e.recovery_date else "—",
                "drop": f"{-e.drop:.0%}",
                **{f"months {clock_label(c)}": round(e.months_from[c], 1) if c in e.months_from else None
                   for c in (t.clock, t.secondary_clock) if c},
                "type": e.episode_type, "status": "recovered" if e.recovered else f"unrecovered: {e.unrecovered_reason}",
            } for e in t.episodes]), hide_index=True, width="stretch")
        with st.expander("All signals checked"):
            st.dataframe(pd.DataFrame([{"signal": x.name, "active": x.active, "detail": x.detail} for x in t.signals]),
                         hide_index=True, width="stretch")
        for n in t.notes:
            st.caption(f"Note: {n}")
        with st.expander("Turnaround rationale and assumptions"):
            st.markdown(t.rationale)
            st.json(t.assumptions, expanded=False)


def draw_history(slot, run: AnalysisRun, bundle: sv.PriceBundle | None) -> None:
    with slot.container():
        rows = history.load_history(run.ticker)
        if not rows:
            st.caption("No stored runs yet.")
            return
        ui.chart(charts.verdict_history([(r.run_date, r.aggregate_score, r.verdict or "") for r in rows]),
                 key=f"hist-{run.ticker}")
        scored = history.score_estimates(rows, {run.ticker: bundle.closes if bundle else None}, date.today())
        df = pd.DataFrame([{
            "run": s.row.created_at.strftime("%Y-%m-%d %H:%M"), "aggregate": s.row.aggregate_score,
            "verdict": s.row.verdict, "lenses": s.row.lenses_used,
            "estimate": (f"{s.row.turnaround_p25:.0f}–{s.row.turnaround_p75:.0f} mo "
                         f"({s.row.turnaround_confidence})") if s.row.has_estimate else (s.row.turnaround_status or ""),
            "episode": s.row.episode_type or "", "outcome": s.status, "detail": s.detail,
            "cost": f"${s.row.total_cost:.4f}"} for s in reversed(scored)])
        grey = {history.SAME_EPISODE, history.NO_ESTIMATE}
        st.dataframe(df.style.apply(lambda r: ["color: #b4b2a9" if r["outcome"] in grey else ""] * len(r), axis=1),
                     hide_index=True, width="stretch")
        st.caption("Only the first estimate in each drawdown episode is scored, from that run's date; later runs in "
                   "the same episode are greyed as 'same episode'.")


def _journal(ticker: str):
    """The thesis journal for the export when the ticker is (or was) held; None otherwise."""
    return journal_for(ticker, config.RUNS_DB_PATH) or None


def draw_export(slot, run: AnalysisRun, ch: dict[str, ChartOut | None]) -> None:
    with slot.container():
        name = f"{run.ticker}_{run.today or date.today()}"
        c1, c2 = st.columns(2)
        c1.download_button("Export Markdown", data=lambda: to_markdown(build_report(run, ch, journal=_journal(run.ticker))),
                           file_name=f"{name}.md", mime="text/markdown", key=f"md-{run.ticker}", on_click="ignore")
        c2.download_button("Export Word (.docx)", data=lambda: to_docx(build_report(run, ch, journal=_journal(run.ticker))),
                           file_name=f"{name}.docx", key=f"docx-{run.ticker}", on_click="ignore",
                           mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        st.caption("Charts are embedded as PNGs with every assumption, data gap and the footer on each page; "
                   "a held ticker's export includes its thesis journal.")
        st.caption(f"Analysis {run.analysis_id}: API cost \\${run.total_cost:.4f} (cache hits cost \\$0) · "
                   f"input hash {run.input_hash[:12] if run.input_hash else 'N/A'}")


def draw_raw(slot, provider, ticker: str) -> None:
    with slot.container():
        if st.toggle("Show raw provider data", key=f"raw-{ticker}"):
            raw_data(provider, ticker)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def draw_inputs(lay: Layout, e: Entry) -> None:
    run, ch = e.run, e.charts
    draw_header(lay["header"], run, ch)
    draw_dots(lay["dots"], run)
    with lay["fundamentals"].container():
        ui.chart(ch.get("small_multiples"))
    with lay["peers"].container():
        if ch.get("peers") is not None:
            ui.chart(ch["peers"])
        else:
            st.caption(f"Peer strip unavailable: {e.bundle.peer_note if e.bundle else 'no peers'}")
    with lay["asset_floor"].container():
        if ch.get("asset_floor") is not None:
            ui.chart(ch["asset_floor"])
        else:
            st.caption("Asset floor: N/A - Data Incomplete")
    draw_trap(lay["trap"], run, ch)
    draw_dividend(lay["dividend"], run, ch)


def draw_all(lay: Layout, e: Entry, provider) -> None:
    run = e.run
    draw_inputs(lay, e)
    draw_valuation(lay["valuation"], run, e.charts)
    for n in LENSES:
        draw_lens(lay[n], run.lens(n))
    draw_turnaround(lay["turnaround"], run, e.charts)
    draw_turnaround_details(lay["turnaround_tab"], run)
    draw_history(lay["history"], run, e.bundle)
    draw_raw(lay["raw"], provider, run.ticker)
    draw_export(lay["export"], run, e.charts)


def llm_notice() -> LLMClient:
    llm = LLMClient(db_path=config.RUNS_DB_PATH)
    if llm.backend == "claude_code":
        st.caption("LLM lenses run through the Claude Code CLI on your Claude subscription (no ANTHROPIC_API_KEY "
                   "set): no API charge; the list-price equivalent is logged.")
    elif not llm.configured:
        st.info("No ANTHROPIC_API_KEY and no Claude Code CLI found: the Moat and Devil's Advocate lenses will "
                "return \"Insufficient data - LLM not configured\" (cached answers are still used). "
                "Set CLAUDE_CODE_CLI in .env if the CLI is installed somewhere unusual.")
    return llm


def stale_cache_note(provider, since: int) -> str:
    """Cached data served during this run because the live source failed, with its age."""
    stale = [ev for ev in provider.cache.events_since(since) if ev.outcome == "stale"]
    if not stale:
        return ""
    dated = [ev for ev in stale if ev.fetched_at is not None]
    oldest = min(dated, key=lambda ev: ev.fetched_at) if dated else stale[0]
    return (f"⚠️ {len(stale)} item(s) served from cache, {oldest.age} old — live source failed "
            f"({oldest.error}). Numbers are as of the cached fetch.")


def run_progressive(provider, ticker: str, lay: Layout, llm: LLMClient, use_edgar: bool) -> Entry | None:
    state: dict = {}
    events_before = provider.cache.event_count

    def refresh_charts(event: str = "inputs") -> None:
        e = state["entry"]
        e.charts = sv.build_charts(e.run, e.bundle, only=sv.CHARTS_FOR_EVENT.get(event), current=e.charts)

    def on_result(name: str, result) -> None:
        if name == "inputs":
            with lay["header"].container():
                with st.spinner("Loading price history, benchmark and peers…"):
                    bundle = sv.load_bundle(provider, result)
            state["entry"] = Entry(run=result, bundle=bundle)
            refresh_charts()
            draw_inputs(lay, state["entry"])
            return
        e = state["entry"]
        setattr(e.run, name, result)
        refresh_charts(name)
        if name in LENSES:
            draw_lens(lay[name], result)
            if name == "quant":
                draw_valuation(lay["valuation"], e.run, e.charts)
            draw_dots(lay["dots"], e.run)
        elif name == "aggregate":
            draw_header(lay["header"], e.run, e.charts)
            draw_dots(lay["dots"], e.run)
            lay["turnaround"].caption("⏳ Waiting on: price history, episodes and peers for the turnaround estimate")
        elif name == "turnaround":
            draw_turnaround(lay["turnaround"], e.run, e.charts)
            draw_turnaround_details(lay["turnaround_tab"], e.run)

    ctx = ScreenContext(provider=provider, db_path=config.RUNS_DB_PATH, today=date.today(),
                        valet_fetch=services.valet_fetch())
    with lay["header"].container():
        with st.spinner(f"Loading {ticker}: statements, screen metrics, insiders and leadership…"):
            pass
    run = run_analysis(ctx, ticker, llm=llm, edgar=services.build_edgar(provider) if use_edgar else None,
                       on_result=on_result)
    note = stale_cache_note(provider, events_before)
    if run.load_error:
        return Entry(run=run, stale_note=note)
    e = state.get("entry") or Entry(run=run)
    e.run, e.stale_note = run, note
    if note:
        st.warning(note)
    for err in run.errors:
        st.error(f"Lens error: {err}")
    draw_history(lay["history"], run, e.bundle)
    draw_raw(lay["raw"], provider, ticker)
    draw_export(lay["export"], run, e.charts)
    return e


def render(provider) -> None:
    linked = (st.query_params.get("ticker") or "").strip().upper()
    if linked and linked != st.session_state.get("ticker"):
        st.session_state["ticker"] = linked
    ticker = st.session_state.get("ticker")
    if ticker:
        st.query_params["ticker"] = ticker  # the page URL names its ticker (bookmarkable)
    if not ticker:
        st.title("Stock")
        st.info("Enter a ticker in the sidebar, or click a row or point on the Screener.")
        return
    llm = llm_notice()
    top = st.columns([4, 2, 1])
    use_edgar = top[1].checkbox("Include SEC EDGAR", value=True, key=f"edgar-{ticker}",
                                help="Leadership 8-K/6-K and Form 4 insiders")
    rerun = top[2].button("Re-run analysis", key=f"rerun-{ticker}")
    store: dict[str, Entry] = st.session_state.setdefault("analyses", {})
    entry = None if rerun else store.get(ticker)
    if entry is not None and entry.run.load_error:
        show_load_error(ticker, entry.run.load_error)
        if entry.stale_note:
            st.warning(entry.stale_note)
        return
    if entry is not None and entry.stale_note:
        st.warning(entry.stale_note)
    lay = Layout()
    if entry is not None:
        draw_all(lay, entry, provider)
        for err in entry.run.errors:
            st.error(f"Lens error: {err}")
    else:
        entry = run_progressive(provider, ticker, lay, llm, use_edgar)
        store[ticker] = entry
        if entry.run.load_error:
            st.rerun()
    with top[0]:
        portfolio_button(entry.run)


def portfolio_button(run: AnalysisRun) -> None:
    """"Add to portfolio": the thesis form pre-filled from this analysis (its purchase snapshot). The form
    stays open while st.session_state["add_to_portfolio"] names this ticker (set by the button or by the
    Portfolio page; cleared on save or when the dialog is dismissed)."""
    if run.load_error or run.analysis_id is None:
        return
    st.button("Add to portfolio", key=f"add-pf-{run.ticker}",
              help="Record a buy with its thesis, levels and sell triggers; this analysis is frozen as the "
                   "purchase snapshot",
              on_click=lambda: st.session_state.__setitem__("add_to_portfolio", run.ticker))
    if st.session_state.get("add_to_portfolio") == run.ticker:
        add_holding_dialog(run)


def show_load_error(ticker: str, error: str) -> None:
    st.title(ticker)
    hint = " TSX tickers need the .TO suffix (e.g. CNR.TO)." if "." not in ticker else ""
    st.error(f"Could not load **{ticker}**: {error}.{hint} Check the symbol, or try again later if the data "
             "source is failing (see the banner).")
