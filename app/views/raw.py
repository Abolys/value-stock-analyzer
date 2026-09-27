"""The Stock page's "Raw data" tab: the provider output behind the analysis, with
every value's period, provider and N/A reason (the Phase 1–4 debug view, kept)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from data import corporate_actions as ca
from data import currency, periods, prices, shares
from data.provider import ProviderError
from data.risk_free import risk_free_for
from data.values import Datum

INCOME_FLOWS = ["total_revenue", "gross_profit", "operating_income", "ebit", "ebitda", "net_income", "interest_expense"]
CASHFLOW_FLOWS = ["operating_cash_flow", "capital_expenditure", "free_cash_flow", "stock_based_compensation",
                  "dividends_paid"]
BALANCE = ["cash_and_equivalents", "short_term_investments", "total_debt", "current_debt", "long_term_debt",
           "total_assets", "total_liabilities", "stockholders_equity", "ordinary_shares"]


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


def raw_data(provider, ticker: str) -> None:
    try:
        info = provider.get_info(ticker)
    except ProviderError as exc:
        st.error(f"Could not load {ticker}: {exc}")
        return
    st.caption(f"info: {cache_note(provider, 'get_info', ticker)}")
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
    st.markdown("**Key values (trading currency, with periods)**")
    items: dict[str, Datum] = {"actual latest price": price}
    items.update({f"TTM {f}": periods.ttm(q_inc, a_inc, f) for f in INCOME_FLOWS})
    items.update({f"TTM {f}": periods.ttm(q_cf, a_cf, f) for f in CASHFLOW_FLOWS})
    items.update({f: periods.latest_balance(q_bal, a_bal, f) for f in BALANCE})
    items["10-year risk-free"] = risk_free_for(info, provider, provider.cache)
    st.dataframe(datum_rows(items), hide_index=True, width="stretch")
    st.markdown("**Stage-1 info fields**")
    st.dataframe(datum_rows(currency.info_datums(info, fx)), hide_index=True, width="stretch")
    st.markdown("**Corporate actions and share count**")
    try:
        px = prices.actual_closes(provider, ticker)
        splits = provider.get_splits(ticker)
        sh = provider.get_shares_history(ticker)
        breaks = ca.corporate_action_breaks(ticker, px, sh.series, splits)
        trend = shares.share_trend(sh.series, splits, ca.break_dates(breaks), sh.source)
        st.write("Breaks: " + (", ".join(f"{b.date} ({b.type}; {', '.join(b.sources)})" for b in breaks)
                               or "none detected"))
        st.write(f"Share-count trend: {trend.trend_per_year:+.2%}/yr over {trend.span_label} (source: {trend.source})"
                 if trend.trend_per_year is not None else f"Share-count trend: {trend.status}")
        for line in trend.flags + trend.notes:
            st.caption(line)
    except ProviderError as exc:
        st.warning(f"Price/share history unavailable: {exc}")
    for (kind, freq), stmt in stmts.items():
        st.markdown(f"**{kind} · {freq}** — provider {stmt.provider}; "
                    f"fields not found: {', '.join(stmt.not_found) or 'none'}")
        st.dataframe(statement_frame(stmt), width="stretch")
    st.markdown("**Raw info**")
    st.json(info.raw, expanded=False)
    st.caption(f"Viewed {date.today()}")
