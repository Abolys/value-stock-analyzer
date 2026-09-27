"""Value Stock Analyzer — plain shell (styling arrives in Phase 5).

- Ticker page: the four-lens analysis (Phase 3; each lens shown as it arrives,
  then the aggregate and the analysis cost), the raw provider output with every
  value's period, provider and N/A reason, and the screen metrics for that
  ticker (information only: manual tickers bypass the screen).
- Screener page: the latest completed screen run with its summary and a
  sortable results table; "Run new screen" launches scripts/run_screen.py in
  the background and shows its progress from the database.

    streamlit run app/main.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

import config  # noqa: E402
from app import services  # noqa: E402
from data import corporate_actions as ca  # noqa: E402
from data import currency, periods, prices, sector, shares  # noqa: E402
from data.health import BANNER  # noqa: E402
from data.insiders import collect_insider_data  # noqa: E402
from data.leadership import leadership_flag  # noqa: E402
from data.provider import ProviderError  # noqa: E402
from data.risk_free import risk_free_for  # noqa: E402
from data.universe import list_labels, load_list  # noqa: E402
from data.values import Datum  # noqa: E402
from app import screen_jobs  # noqa: E402
from analysis.models import LENS_LABELS, LENSES, AggregateResult, LensResult, QuantResult  # noqa: E402
from analysis.pipeline import run_analysis  # noqa: E402
from llm.client import LLMClient  # noqa: E402
from storage import llm_store  # noqa: E402
from screening.engine import ScreenContext, analyse_manual  # noqa: E402
from screening.table import result_row  # noqa: E402
from storage import screen_store as store  # noqa: E402

INCOME_FLOWS = ["total_revenue", "gross_profit", "operating_income", "ebit", "ebitda", "net_income", "interest_expense"]
CASHFLOW_FLOWS = ["operating_cash_flow", "capital_expenditure", "free_cash_flow", "stock_based_compensation",
                  "dividends_paid"]
BALANCE = ["cash_and_equivalents", "short_term_investments", "total_debt", "current_debt", "long_term_debt",
           "total_assets", "total_liabilities", "stockholders_equity", "ordinary_shares"]

st.set_page_config(page_title="Value Stock Analyzer", layout="wide")


@st.cache_resource
def _provider():
    return services.build_provider()


def datum_rows(items: dict[str, Datum]) -> pd.DataFrame:
    return pd.DataFrame([{"field": k, "value": d.display(), "period": d.period_label,
                          "period end": d.period_end.isoformat() if d.period_end else "",
                          "provider": d.provider, "notes": "; ".join(d.notes)} for k, d in items.items()])


def cache_note(provider, method: str, ticker: str, *args) -> str:
    ev = provider.cache.last_event(provider.key_for(method, ticker, *args))
    if ev is None:
        return ""
    if ev.outcome == "stale":
        return f"⚠️ served from cache, {ev.age} old — live source failed ({ev.error})"
    if ev.outcome == "hit":
        return f"cache hit ({ev.age} old; expires by rule: {ev.reason})"
    return "fetched live" + (f" (previous entry expired: {ev.reason})" if ev.reason else "")


def statement_frame(stmt) -> pd.DataFrame:
    if stmt.empty:
        return pd.DataFrame()
    return pd.DataFrame(stmt.values).T.sort_index(axis=1, ascending=False)


def show_lens(r: LensResult) -> None:
    st.markdown(f"#### {r.label}: {r.display}" + (f" · confidence {r.confidence}" if r.ok else ""))
    if r.stale:
        st.warning(r.stale_label)
    st.caption(f"Mapping: {r.mapping_line}")
    if r.completeness:
        st.caption(f"Data completeness: {r.completeness}")
    for c in r.confidence_reasons:
        st.caption(f"Low confidence: {c}")
    if r.key_figures:
        st.dataframe(pd.DataFrame([{"figure": k, "value": v} for k, v in r.key_figures.items()]),
                     hide_index=True, width="stretch")
    if isinstance(r, QuantResult) and r.grid is not None and r.grid.values:
        st.caption("Sensitivity grid (fair value per share; rows = discount rate, columns = stage-1 growth; "
                   "the score uses the centre cell)")
        st.dataframe(pd.DataFrame([[f"{v:,.2f}" if v is not None else "N/A" for v in row] for row in r.grid.values],
                                  index=[f"{x:.1%}" for x in r.grid.rates],
                                  columns=[f"{g:+.1%}" for g in r.grid.growths]), width="stretch")
    with st.expander("Rationale, assumptions"):
        st.markdown(r.rationale)
        st.json({k: (v if isinstance(v, (int, float, str, bool, list, dict)) or v is None else str(v))
                 for k, v in r.assumptions.items()}, expanded=False)
        if getattr(r, "payload", None):
            st.caption("LLM payload (compact JSON sent to the model)")
            st.json(r.payload, expanded=False)


def show_aggregate(a: AggregateResult) -> None:
    st.markdown(f"#### Aggregate: {a.display} — {a.verdict}")
    if a.controversy:
        st.warning(f"High controversy: the Devil's Advocate is {a.controversy_gap:.1f} points below the other "
                   f"lenses' mean (threshold {config.CONTROVERSY_GAP:g}).")
    st.markdown(a.rationale)


def analysis_section(provider, ticker: str) -> None:
    st.subheader("Four-lens analysis")
    llm = LLMClient(db_path=config.RUNS_DB_PATH)
    if llm.backend == "claude_code":
        st.caption("LLM lenses run through the Claude Code CLI on your Claude subscription (no ANTHROPIC_API_KEY "
                   "set): no API charge; the list-price equivalent is logged.")
    elif not llm.configured:
        st.info("No ANTHROPIC_API_KEY and no Claude Code CLI found: the Moat and Devil's Advocate lenses will "
                "return \"Insufficient data - LLM not configured\" (cached answers are still used). "
                "Set CLAUDE_CODE_CLI in .env if the CLI is installed somewhere unusual.")
    use_edgar = st.checkbox("Include SEC EDGAR (leadership 8-K/6-K, Form 4 insiders)", value=True,
                            key=f"an-edgar-{ticker}")
    if not st.button("Run analysis", key=f"run-analysis-{ticker}",
                     help="Moat and Devil's Advocate call the LLM (API or Claude Code; cached by input)."):
        return
    slots = {name: st.empty() for name in (*LENSES, "aggregate")}
    for name in LENSES:
        slots[name].info(f"{LENS_LABELS[name]}: running…")

    def on_result(name: str, result) -> None:
        with slots[name].container():
            (show_aggregate if name == "aggregate" else show_lens)(result)

    ctx = ScreenContext(provider=provider, db_path=config.RUNS_DB_PATH, today=date.today(),
                        valet_fetch=services.valet_fetch())
    run = run_analysis(ctx, ticker, llm=llm, edgar=services.build_edgar(provider) if use_edgar else None,
                       on_result=on_result)
    if run.load_error:
        st.error(f"Analysis unavailable: {run.load_error}")
        return
    for e in run.errors:
        st.error(f"Lens error: {e}")
    list_price = sum(getattr(run.lens(n), "list_price_cost", 0.0) or 0.0 for n in ("moat", "devils_advocate"))
    st.caption(f"Analysis {run.analysis_id} via {llm.backend}: API cost ${run.total_cost:.4f} (cache hits cost $0)"
               + (f"; list-price equivalent ${list_price:.4f} on the subscription" if llm.backend == "claude_code"
                  else "") + f". Fundamentals as of {run.fundamentals_as_of or 'N/A'}.")


def ticker_page(provider, ticker: str) -> None:
    try:
        info = provider.get_info(ticker)
    except ProviderError as exc:
        st.error(f"Could not load {ticker}: {exc}")
        return
    st.caption(cache_note(provider, "get_info", ticker))
    route = sector.route(info)
    st.header(f"{ticker} — {info.get('long_name') or ''}")
    st.write(f"**Sector:** {info.get('sector') or 'N/A'} · **Industry:** {info.get('industry') or 'N/A'} · "
             f"**Treatment:** {route.label} · **Trading currency:** {info.get('currency') or 'N/A'} · "
             f"**Reporting currency:** {info.get('financial_currency') or 'N/A'}")
    if route.unmatched_industry:
        st.warning(f"Industry {route.industry!r} matches no SUBSECTOR_RULES entry; default financial treatment used.")

    analysis_section(provider, ticker)

    stmts = {}
    for kind in ("income", "balance", "cashflow"):
        for freq in ("annual", "quarterly"):
            try:
                stmts[(kind, freq)] = currency.to_trading_currency(provider, info, provider.get_statement(ticker, kind, freq))
            except ProviderError as exc:
                st.warning(f"{freq} {kind} statement unavailable: {exc}")
    mismatch = currency.detect_mismatch(info)
    fx = currency.fx_rate(provider, *mismatch) if mismatch else None
    if mismatch:
        st.info(f"Reports in {mismatch[0]}, trades in {mismatch[1]}: financials converted — "
                f"{currency.conversion_note(*mismatch, fx) if fx and fx.ok else fx.status}")

    price = prices.actual_latest_price(provider, ticker)
    q_inc, q_cf, q_bal = (stmts.get((k, "quarterly")) for k in ("income", "cashflow", "balance"))
    a_inc, a_cf, a_bal = (stmts.get((k, "annual")) for k in ("income", "cashflow", "balance"))
    latest = periods.latest_period_end(q_inc, q_bal, a_inc)
    try:
        earnings = provider.get_earnings_dates(ticker)
    except ProviderError:
        earnings = None
    stale = periods.staleness(latest, earnings, date.today())
    st.write(f"Price as of {price.period_end or 'N/A'} · Fundamentals as of {latest or 'N/A'} · "
             f"Next earnings: {earnings.next if earnings and earnings.next else 'N/A'}")
    if stale.stale:
        st.warning(stale.label)

    st.subheader("Key values (trading currency, with periods)")
    items: dict[str, Datum] = {"actual latest price": price}
    items.update({f"TTM {f}": periods.ttm(q_inc, a_inc, f) for f in INCOME_FLOWS})
    items.update({f"TTM {f}": periods.ttm(q_cf, a_cf, f) for f in CASHFLOW_FLOWS})
    items.update({f: periods.latest_balance(q_bal, a_bal, f) for f in BALANCE})
    items["10-year risk-free"] = risk_free_for(info, provider, provider.cache)
    st.dataframe(datum_rows(items), hide_index=True, width="stretch")

    st.subheader("Stage-1 info fields")
    st.dataframe(datum_rows(currency.info_datums(info, fx)), hide_index=True, width="stretch")

    st.subheader("Corporate actions and share count")
    try:
        px = prices.actual_closes(provider, ticker)
        splits = provider.get_splits(ticker)
        sh = provider.get_shares_history(ticker)
        breaks = ca.corporate_action_breaks(ticker, px, sh.series, splits)
        trend = shares.share_trend(sh.series, splits, ca.break_dates(breaks), sh.source)
        st.write("Breaks: " + (", ".join(f"{b.date} ({b.type}; {', '.join(b.sources)})" for b in breaks) or "none detected"))
        if trend.trend_per_year is not None:
            st.write(f"Share-count trend: {trend.trend_per_year:+.2%}/yr over {trend.span_label} (source: {trend.source})"
                     + (" — dilution flag" if shares.dilution_flag(trend) else ""))
        else:
            st.write(f"Share-count trend: {trend.status}")
        for line in trend.flags + trend.notes:
            st.caption(line)
        adj = prices.adjusted_closes(provider, ticker)
        st.write(f"Prices: actual latest {price.display()} vs adjusted-close series "
                 f"(first {adj.index[0].date()}: adjusted {adj.iloc[0]:.2f} vs actual {px.iloc[0]:.2f})")
    except ProviderError as exc:
        st.warning(f"Price/share history unavailable: {exc}")

    st.subheader("Screen metrics (information only — manual tickers bypass the screen)")
    ctx = ScreenContext(provider=provider, db_path=config.RUNS_DB_PATH, today=date.today(),
                        valet_fetch=services.valet_fetch())
    result = analyse_manual(ctx, ticker)
    if result.status == "failed to load":
        st.warning(f"Screen metrics unavailable: {result.load_error}")
    else:
        st.write(f"**Screen status:** {result.display_status} · **Quality:** "
                 f"{result.quality.display if result.quality else 'N/A'}")
        st.markdown(result.rationale)

    with st.expander("Statements (canonical fields)"):
        for (kind, freq), stmt in stmts.items():
            st.markdown(f"**{kind} · {freq}** — provider {stmt.provider}; "
                        f"fields not found: {', '.join(stmt.not_found) or 'none'}")
            for n in stmt.notes:
                st.caption(n)
            st.dataframe(statement_frame(stmt), width="stretch")

    with st.expander("Analyst estimates, dividends, ownership"):
        try:
            est = provider.get_analyst_estimates(ticker)
            for name, table in est.tables.items():
                st.markdown(f"**{name}** — {est.statuses[name]}")
                if table is not None:
                    st.dataframe(table, width="stretch")
        except ProviderError as exc:
            st.write(f"Analyst estimates unavailable: {exc}")
        try:
            div = provider.get_dividends(ticker)
            st.write(f"Dividend history: {len(div)} payments" + (f", latest {div.index[-1].date()}" if len(div) else " (N/A - no dividend)"))
        except ProviderError as exc:
            st.write(f"Dividends unavailable: {exc}")
        for f in ("held_percent_insiders", "short_percent_of_float"):
            v = info.get(f)
            st.write(f"{f}: {f'{v:.2%}' if v is not None else info.status(f)}")

    with st.expander("Leadership and insider activity (SEC EDGAR, officer snapshots, manual CSVs)"):
        if st.toggle("Load from EDGAR", key=f"edgar-{ticker}"):
            edgar = services.build_edgar(provider)
            lead = leadership_flag(ticker, edgar=edgar)
            st.write(f"**Leadership flag:** {lead.flag} — {lead.summary}")
            for e in lead.events:
                st.write(f"- {e.date} {e.role} {e.person or '(name not parsed)'} — {', '.join(e.sources)}: {e.detail}")
            if lead.unconfirmed_candidates:
                st.caption(f"{len(lead.unconfirmed_candidates)} 6-K keyword hit(s) not confirmed by the LLM check "
                           f"({lead.unconfirmed_candidates[0].get('note') or 'unconfirmed'}); not counted.")
            since = (pd.Timestamp(date.today()) - pd.DateOffset(months=config.INSIDER_LOOKBACK_MONTHS)).date()
            ins = collect_insider_data(ticker, edgar, since)
            st.write(f"**Insider transactions since {since}:** {len(ins.transactions)} — coverage: {ins.coverage_label}")
            if ins.transactions:
                st.dataframe(pd.DataFrame([t.model_dump() for t in ins.transactions]), hide_index=True)

    with st.expander("Raw info"):
        st.json(info.raw, expanded=False)


def run_summary(run: store.ScreenRun) -> None:
    labels = list_labels()
    st.write(f"**Run {run.run_id}** · {run.status} · started {run.started_at:%Y-%m-%d %H:%M}"
             + (f", ended {run.ended_at:%Y-%m-%d %H:%M}" if run.ended_at else "")
             + f" · lists: {', '.join(labels.get(k, k) for k in run.lists)}")
    cols = st.columns(6)
    cols[0].metric("Tickers", run.total)
    cols[1].metric("Attempted", run.attempted)
    cols[2].metric("Passed stage 1", run.passed_stage1)
    cols[3].metric("Pass (stage 2)", run.passed_stage2)
    cols[4].metric("Failed to load", run.failed_to_load)
    cols[5].metric("Refetched (reported)", run.refetched_reported)
    st.caption(f"{run.refetched_reported} tickers refetched because they reported since the last fetch; "
               f"{run.served_from_cache} served from cache. Failed to load is never counted as a Fail.")
    if run.flagged_fields:
        st.warning("Fields N/A for more than "
                   f"{config.FIELD_NA_SPIKE:.0%} of tickers — likely renamed upstream: " + ", ".join(run.flagged_fields))
    if run.note:
        st.caption(run.note)


@st.fragment(run_every=config.SCREEN_PROGRESS_POLL_SECONDS)
def run_progress() -> None:
    run = screen_jobs.active_run(config.RUNS_DB_PATH)
    if run is None:
        return
    done = run.attempted
    st.info(f"Screen run {run.run_id} in progress: {done} of {run.total} tickers "
            f"({run.passed_stage1} passed stage 1, {run.failed_to_load} failed to load).")
    st.progress(done / run.total if run.total else 0.0)


def screener_page() -> None:
    st.header("Screener")
    labels = list_labels()
    rows = []
    for key, label in labels.items():
        df = load_list(key)
        rows.append({"list": label, "key": key, "tickers": len(df),
                     "as of": ", ".join(sorted(set(df["as_of"]))) if len(df) else "empty"})
    with st.expander("Universe lists"):
        st.dataframe(pd.DataFrame(rows), hide_index=True)
        st.caption("Refresh with `python scripts/refresh_universe.py`; failed downloads keep the previous list.")

    chosen = st.multiselect("Lists to screen", list(labels), default=["cowz"], format_func=labels.get)
    db = config.RUNS_DB_PATH
    busy = screen_jobs.active_run(db) is not None
    if st.button("Run new screen", disabled=busy or not chosen):
        try:
            log = screen_jobs.launch(chosen, db_path=db)
            st.success(f"Screen started in the background (log: {log}).")
        except RuntimeError as exc:
            st.warning(str(exc))
    run_progress()

    paused = screen_jobs.interrupted_run(db)
    if paused and not busy:
        st.warning(f"Run {paused.run_id} is '{paused.status}' after {paused.attempted} of {paused.total} tickers. "
                   f"{paused.note or ''} Continue with `python scripts/run_screen.py --resume`.")
        if st.button("Resume run"):
            screen_jobs.launch(resume=True, db_path=db)
            st.rerun()
    last = store.latest_run(db)
    if last and last.status == store.BLOCKED:
        st.error(f"Run {last.run_id} was blocked: health check failed — " + "; ".join(last.health_failures))

    run = store.latest_completed_run(db)
    if run is None:
        st.info("No completed screen run yet.")
        return
    st.subheader("Latest completed run")
    run_summary(run)
    results = store.load_results(run.run_id, db)
    table = pd.DataFrame([result_row(r) for r in results])
    statuses = sorted(table["status"].unique()) if len(table) else []
    shown = st.multiselect("Status", statuses, default=statuses)
    st.dataframe(table[table["status"].isin(shown)], hide_index=True, width="stretch")
    st.caption("Metric cells show the value and outcome, or the N/A / n/m reason. n/m counts as failing; "
               "N/A counts as unavailable. Trap-risk and asset-floor flags are shown, not scored.")

    failed = [r for r in results if r.status == "failed to load"]
    if failed:
        with st.expander(f"Failed to load ({len(failed)})"):
            st.dataframe(pd.DataFrame([{"ticker": r.ticker, "reason": r.load_error} for r in failed]), hide_index=True)
    div = store.load_divergences(run.run_id, db)
    if div:
        with st.expander(f"Stage-1 / stage-2 divergences ({len(div)})"):
            st.dataframe(pd.DataFrame(div), hide_index=True)
    if run.field_na:
        with st.expander("Per-field N/A counts"):
            st.dataframe(pd.DataFrame([{"field": k, "N/A": v["na"], "of": v["of"]} for k, v in run.field_na.items()]),
                         hide_index=True)
    pick = st.selectbox("Rationale for", [r.ticker for r in results if r.rationale])
    if pick:
        st.markdown(next(r.rationale for r in results if r.ticker == pick))


def main() -> None:
    provider = _provider()
    report = services.health(provider)
    with st.sidebar:
        st.title("Value Stock Analyzer")
        ticker = st.text_input("Ticker", key="ticker").strip().upper()
        page = st.radio("Page", ["Ticker data", "Screener"], key="page")
        st.caption(f"Data source check: {'OK' if report.ok else 'FAILED'} at {report.checked_at:%Y-%m-%d %H:%M}")
        spend, calls, hits = llm_store.month_spend(path=config.RUNS_DB_PATH)
        st.caption(f"API spend this month: ${spend:.2f} ({calls} LLM calls, {hits} cache hits)")
        cc_calls, cc_est = llm_store.month_claude_code(path=config.RUNS_DB_PATH)
        if cc_calls:
            st.caption(f"Claude Code (subscription) this month: {cc_calls} calls, ≈${cc_est:.2f} at API list prices")
    if not report.ok:
        st.error(BANNER + "\n\n" + "\n".join(f"- {f}" for f in report.failures)
                 + "\n\nCached data is still shown, with its age.")
    if page == "Screener":
        screener_page()
    elif ticker:
        ticker_page(provider, ticker)
    else:
        st.write("Enter a ticker in the sidebar to see the raw provider output.")


main()
