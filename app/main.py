"""Value Stock Analyzer — Phase 1 shell.

A sidebar with a ticker input and a "Run screener" option, and a page showing
the raw provider output for a ticker with every value's period, provider and
N/A reason. Later phases replace this with the real views.

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
                st.caption(f"{len(lead.unconfirmed_candidates)} 6-K keyword hit(s) awaiting LLM confirmation (Phase 3); not counted.")
            since = (pd.Timestamp(date.today()) - pd.DateOffset(months=config.INSIDER_LOOKBACK_MONTHS)).date()
            ins = collect_insider_data(ticker, edgar, since)
            st.write(f"**Insider transactions since {since}:** {len(ins.transactions)} — coverage: {ins.coverage_label}")
            if ins.transactions:
                st.dataframe(pd.DataFrame([t.model_dump() for t in ins.transactions]), hide_index=True)

    with st.expander("Raw info"):
        st.json(info.raw, expanded=False)


def screener_page() -> None:
    st.header("Screener")
    st.info("The value screener arrives in Phase 2. Universe lists available now:")
    rows = []
    for key, label in list_labels().items():
        df = load_list(key)
        rows.append({"list": label, "file": f"{key}.csv", "tickers": len(df),
                     "as of": ", ".join(sorted(set(df["as_of"]))) if len(df) else "empty"})
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption("Refresh with `python scripts/refresh_universe.py`; failed downloads keep the previous list.")


def main() -> None:
    provider = _provider()
    report = services.health(provider)
    with st.sidebar:
        st.title("Value Stock Analyzer")
        ticker = st.text_input("Ticker", key="ticker").strip().upper()
        page = st.radio("Page", ["Ticker data", "Run screener"], key="page")
        st.caption(f"Data source check: {'OK' if report.ok else 'FAILED'} at {report.checked_at:%Y-%m-%d %H:%M}")
    if not report.ok:
        st.error(BANNER + "\n\n" + "\n".join(f"- {f}" for f in report.failures)
                 + "\n\nCached data is still shown, with its age.")
    if page == "Run screener":
        screener_page()
    elif ticker:
        ticker_page(provider, ticker)
    else:
        st.write("Enter a ticker in the sidebar to see the raw provider output.")


main()
