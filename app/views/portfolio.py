"""Portfolio page (docs/ui-mockup.html → Portfolio; SPEC "Portfolio and thesis tracking").

Holdings table (value, gain, return vs benchmark, trigger traffic light, unread alerts);
per holding the "then vs now" comparison, the sell triggers with their current status,
the thesis reasons with a "still holds?" tick, the journal and the transactions; then
the alerts inbox and the watchlist price levels. Every refresh of the page re-evaluates
the triggers on the latest metrics; fresh data comes from the alert check (run in the
background on app start, after each screen, or with "Check alerts now")."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

import config
from app import alert_jobs, auth, ui
from app import portfolio_view as pv
from app.views.thesis_form import trigger_builder
from portfolio import store
from portfolio.alerts import watchlist_tickers
from portfolio.models import BUY, KIND_CASH, KIND_STOCK, SELL, Transaction
from portfolio.triggers import TriggerError
from storage import history


def check_status() -> None:
    db = config.RUNS_DB_PATH
    row = store.latest_check(db)
    c1, c2 = st.columns([5, 1])
    if alert_jobs.running(db):
        c1.caption(f"⏳ Alert check running (started {pv.fmt_when(row.started_at)}): fresh prices, statements, "
                   "leadership and insiders for every holding and watchlist name. Refresh to see new alerts.")
    elif row is None:
        c1.caption("No alert check has run yet. Checks run on app start, after each screen run, or on demand.")
    else:
        errs = row.errors or {}
        c1.caption(f"Last alert check {pv.fmt_when(row.started_at)} ({row.source}): {row.status}, {row.tickers} "
                   f"ticker(s), {row.fired} new alert(s), email {row.email_status or 'N/A'}"
                   + (f"; {len(errs)} ticker(s) failed — " + "; ".join(f"{t}: {e}" for t, e in errs.items())
                      if errs else ""))
    if auth.is_owner() and c2.button("Check alerts now", key="pf-check", disabled=alert_jobs.running(db) is not None):
        try:
            alert_jobs.launch("manual", db_path=db)
            st.rerun()
        except RuntimeError as exc:
            st.warning(str(exc))


def holdings_table(views: list[pv.HoldingView]) -> None:
    df, states = pv.holdings_frame(views)
    st.dataframe(pv.style_states(df, states), hide_index=True, width="stretch")
    totals = pv.totals_by_currency(views)
    if not totals.empty:
        st.caption("Totals per currency (amounts in different currencies are never added together):")
        st.dataframe(totals, hide_index=True)
    notes = sorted({n for v in views for n in v.position.notes})
    for n in notes:
        st.caption(n)


def then_now(v: pv.HoldingView) -> None:
    h = v.holding
    st.markdown(f"**{h.ticker} · then vs now** · bought {h.first_buy or 'N/A'}")
    df, states = pv.then_now_frame(v.check)
    if df.empty:
        st.caption("Nothing to compare.")
    else:
        st.dataframe(pv.style_states(df, states), hide_index=True, width="stretch")
    st.caption(f"At purchase: analysis {h.snapshot_analysis_id or 'N/A'}. Now: {v.now_label}; the Moat, Devil's "
               "Advocate and aggregate come from the latest full analysis (re-run it on the Stock page). "
               "Green = better, red = worse for that metric.")
    for n in v.check.notes:
        st.caption(n)


def triggers(v: pv.HoldingView) -> None:
    st.markdown(f"**Sell triggers** {pv.LIGHT_EMOJI[v.check.light]}")
    tf = pv.triggers_frame(v.check)
    if tf.empty:
        st.caption("No sell triggers.")
    else:
        st.dataframe(tf, hide_index=True, width="stretch")
        st.caption(f"🟡 near = within {config.TRIGGER_NEAR_BAND:.0%} of the threshold, or the value is N/A / n/m "
                   "(a trigger that can't be evaluated never fires silently).")
    th = v.holding.thesis
    if th is None or not auth.is_owner():
        return
    with st.expander("Edit triggers and levels"):
        rules = trigger_builder(f"pf-rules-{v.holding.holding_id}")
        if rules and st.button("Save new rules", key=f"pf-save-rules-{v.holding.holding_id}"):
            try:
                for t in rules:
                    store.add_trigger(th.thesis_id, t)
                store.add_journal(v.holding.holding_id, "Sell triggers added: " + ", ".join(t.text for t in rules))
                st.session_state.pop(f"pf-rules-{v.holding.holding_id}", None)
                st.rerun()
            except TriggerError as exc:
                st.error(f"Not saved: {exc}")
        for t in th.triggers:
            if st.button(f"Delete `{t.text}`", key=f"pf-del-trig-{t.trigger_id}"):
                store.delete_trigger(t.trigger_id)
                store.add_journal(v.holding.holding_id, f"Sell trigger removed: {t.text}")
                st.rerun()
        c1, c2, c3 = st.columns(3)
        iv = c1.number_input("Intrinsic value", min_value=0.0, value=float(th.intrinsic_value or 0.0),
                             key=f"pf-iv-{th.thesis_id}")
        bb = c2.number_input("Buy-below", min_value=0.0, value=float(th.buy_below_price or 0.0),
                             key=f"pf-bb-{th.thesis_id}")
        tp = c3.number_input("Target", min_value=0.0, value=float(th.target_price or 0.0), key=f"pf-tp-{th.thesis_id}")
        if st.button("Save levels", key=f"pf-lv-{th.thesis_id}"):
            store.update_levels(th.thesis_id, iv or None, bb or None, tp or None)
            store.add_journal(v.holding.holding_id, f"Levels changed: intrinsic {iv:,.2f}, buy-below {bb:,.2f}, "
                                                    f"target {tp:,.2f}")
            st.rerun()


def reasons(v: pv.HoldingView) -> None:
    th = v.holding.thesis
    st.markdown("**Reasons at purchase · still hold?**")
    if th is None or not th.reasons:
        st.caption("No reasons recorded.")
        return
    for r in th.reasons:
        key = f"pf-reason-{r.reason_id}"
        st.checkbox(r.text, value=bool(r.still_holds), key=key, disabled=not auth.is_owner(),
                    help="Not reviewed yet" if r.still_holds is None else f"Reviewed {pv.fmt_when(r.reviewed_at)}",
                    on_change=lambda rid=r.reason_id, k=key: store.set_reason(rid, bool(st.session_state[k]),
                                                                             v.holding.holding_id))
    st.caption("Each tick or untick is dated in the journal.")


def journal(v: pv.HoldingView) -> None:
    hid = v.holding.holding_id
    st.markdown("**Journal**")
    key = f"pf-journal-{hid}"
    if not auth.is_owner():
        _journal_entries(hid)
        return
    st.text_area("New entry", key=key, label_visibility="collapsed",
                 placeholder="What changed, what you read, what you decided…")

    def add() -> None:
        text = st.session_state.get(key, "")
        if text.strip():
            store.add_journal(hid, text)
            st.session_state[key] = ""

    st.button("Add entry", key=f"pf-journal-add-{hid}", on_click=add)
    _journal_entries(hid)


def _journal_entries(hid: int) -> None:
    entries = store.journal(hid)
    if not entries:
        st.caption("No entries yet.")
    icon = {"note": "📝", "trigger": "🔴", "alert": "🔔", "reason": "☑️"}
    for e in entries:
        st.markdown(f"{icon.get(e.kind, '•')} **{pv.fmt_when(e.created_at)}** · {e.kind} — {e.text}")


def transactions(v: pv.HoldingView) -> None:
    h = v.holding
    st.markdown("**Transactions**")
    st.dataframe(pd.DataFrame([{"id": t.txn_id, "date": t.txn_date, "side": t.side, "shares": t.shares,
                                "price": t.price, "fees": t.fees, "note": t.note} for t in h.transactions]),
                 hide_index=True, width="stretch")
    if not auth.is_owner():
        return
    with st.form(f"pf-txn-{h.holding_id}", clear_on_submit=True):
        c = st.columns(5)
        side = c[0].selectbox("Side", [BUY, SELL])
        when = c[1].date_input("Date", value=date.today())
        shares = c[2].number_input("Shares", min_value=0.0, step=1.0)
        price = c[3].number_input("Price", min_value=0.0, format="%.4f")
        fees = c[4].number_input("Fees", min_value=0.0)
        note = st.text_input("Note")
        if st.form_submit_button("Add transaction"):
            try:
                store.add_transaction(h.holding_id, Transaction(txn_date=when, side=side, shares=shares, price=price,
                                                                fees=fees, note=note))
                store.add_journal(h.holding_id, f"{side.capitalize()} {shares:g} at {price:,.2f} {h.currency} on {when}")
                st.rerun()
            except ValueError as exc:
                st.error(f"Not saved: {exc}")
    c1, c2, c3 = st.columns(3)
    ids = [t.txn_id for t in h.transactions]
    victim = c1.selectbox("Delete transaction", [None, *ids], key=f"pf-del-txn-sel-{h.holding_id}",
                          format_func=lambda i: "—" if i is None else f"#{i}")
    if victim is not None and c1.button("Delete", key=f"pf-del-txn-{h.holding_id}"):
        store.delete_transaction(victim)
        st.rerun()
    if c2.button("Open Stock page", key=f"pf-open-{h.holding_id}", help="Re-run the full analysis there"):
        ui.open_ticker(h.ticker)
    label = "Reopen holding" if h.closed else "Close holding"
    if c3.button(label, key=f"pf-close-{h.holding_id}", help="Closed holdings leave the table and alerts; "
                                                             "their journal is kept"):
        store.set_closed(h.holding_id, not h.closed)
        st.rerun()


def kind_toggle(v: pv.HoldingView) -> None:
    h = v.holding
    key = f"pf-cash-{h.holding_id}"
    st.toggle("Cash deposit", value=h.is_cash, key=key, disabled=not auth.is_owner(),
              help="Where cash is parked until an opportunity comes: performance still tracked and totalled apart from "
                   "the value stocks; turning it on removes the analysis snapshot, levels and sell triggers",
              on_change=lambda: store.set_kind(h.holding_id, KIND_CASH if st.session_state[key] else KIND_STOCK))


def inbox() -> None:
    st.subheader("Alerts")
    unread = store.unread_count()
    alerts = store.list_alerts(limit=config.ALERT_INBOX_MAX)
    c1, c2 = st.columns([5, 1])
    c1.caption(f"{unread} unread · each alert fires once per event; newest {config.ALERT_INBOX_MAX} shown")
    if auth.is_owner() and c2.button("Mark all read", key="pf-read-all", disabled=unread == 0):
        store.mark_read(None)
        st.rerun()
    if not alerts:
        st.caption("No alerts yet.")
        return
    for a in alerts:
        new = a.read_at is None
        c1, c2 = st.columns([8, 1])
        c1.markdown(f"{'🔔 **' if new else '• '}{a.ticker}: {config.ALERT_KINDS.get(a.kind, a.kind)}"
                    f"{'**' if new else ''} — {a.message} "
                    f"<span style='color:#898781;font-size:0.8rem'>{pv.fmt_when(a.created_at)} · {a.source}"
                    f"{' · email ' + a.email_status if a.email_status else ''}</span>", unsafe_allow_html=True)
        if new and auth.is_owner() and c2.button("Read", key=f"pf-read-{a.alert_id}"):
            store.mark_read([a.alert_id])
            st.rerun()


def watchlist_levels() -> None:
    st.subheader("Watchlist price levels")
    levels = store.watch_levels()
    tickers = sorted(set(watchlist_tickers()) | set(levels))
    if not tickers:
        st.caption("No watchlist tickers: add them to data/universe/watchlist.csv.")
        return
    rows = []
    for t in tickers:
        lv = levels.get(t)
        full = history.latest_full_run(t)
        fv = full.quant.dcf.fair_value if full and full.quant and full.quant.dcf and full.quant.dcf.ok else None
        rows.append({"ticker": t, "buy_below": lv.buy_below_price if lv else None,
                     "target": lv.target_price if lv else None,
                     "fair value (latest analysis)": fv,
                     "suggested buy-below": fv * (1 - config.MIN_MARGIN_OF_SAFETY) if fv else None})
    df = pd.DataFrame(rows)
    if not auth.is_owner():
        st.dataframe(df, hide_index=True, width="stretch")
        return
    edited = st.data_editor(df, hide_index=True, width="stretch", key="pf-watch",
                            disabled=["ticker", "fair value (latest analysis)", "suggested buy-below"],
                            column_config={"buy_below": st.column_config.NumberColumn("Buy-below", min_value=0.0),
                                           "target": st.column_config.NumberColumn("Target", min_value=0.0)})
    st.caption(f"Suggested buy-below = the latest analysis's DCF fair value × (1 − MIN_MARGIN_OF_SAFETY "
               f"{config.MIN_MARGIN_OF_SAFETY:.0%}); blank = no fair value (never run, or no DCF). Alerts fire when "
               "the price reaches a level.")
    c1, c2 = st.columns(2)
    if c1.button("Save levels", key="pf-watch-save"):
        for _, r in edited.iterrows():
            bb = r["buy_below"] if pd.notna(r["buy_below"]) and r["buy_below"] > 0 else None
            tp = r["target"] if pd.notna(r["target"]) and r["target"] > 0 else None
            store.set_watch_level(r["ticker"], bb, tp, "entered on the Portfolio page")
        st.success("Saved.")
    if c2.button("Fill blanks from fair value", key="pf-watch-fill"):
        for _, r in df.iterrows():
            fv = r["fair value (latest analysis)"]
            if pd.notna(fv) and pd.isna(r["buy_below"]) and pd.isna(r["target"]):
                store.set_watch_level(r["ticker"], fv * (1 - config.MIN_MARGIN_OF_SAFETY), fv,
                                      "pre-filled from the latest analysis fair value")
        st.rerun()


def render(provider) -> None:
    st.title("Portfolio")
    check_status()
    holdings = store.list_holdings(include_closed=st.toggle("Show closed holdings", key="pf-closed"))
    if not holdings:
        st.info("No holdings yet. Open a ticker's Stock page and click **Add to portfolio**: the analysis you are "
                "looking at is frozen as the purchase snapshot, with your reasons, levels and sell triggers.")
        if not auth.is_owner():
            return
        t = st.text_input("Ticker to add", key="pf-add-ticker", placeholder="e.g. LULU")
        if st.button("Open its Stock page", key="pf-add-go") and t.strip():
            st.session_state["add_to_portfolio"] = t.strip().upper()
            ui.open_ticker(t.strip())
    else:
        with st.spinner("Prices and benchmarks…"):
            views = [pv.build_view(provider, h) for h in holdings]
        holdings_table(views)
        labels = {v.holding.holding_id: f"{v.holding.ticker} · {v.holding.account}" for v in views}
        ids = list(labels)
        sel = st.session_state.get("portfolio_selected")
        chosen = st.selectbox("Holding", ids, index=ids.index(sel) if sel in ids else 0, format_func=labels.get,
                              key="pf-select")
        st.session_state["portfolio_selected"] = chosen
        v = next(x for x in views if x.holding.holding_id == chosen)
        kind_toggle(v)
        if v.holding.is_cash:
            st.caption(f"{v.holding.ticker} is parked cash, totalled apart from your value stocks. Its value, gain/loss "
                       "and return vs the index are tracked above; open its Stock page for the price-based analysis. "
                       "No thesis, sell triggers or alerts.")
            journal(v)
        else:
            left, right = st.columns(2)
            with left:
                then_now(v)
                reasons(v)
            with right:
                triggers(v)
                journal(v)
        transactions(v)
        if auth.is_owner():
            with st.expander("Add another holding"):
                t = st.text_input("Ticker", key="pf-add-ticker-2")
                if st.button("Open its Stock page", key="pf-add-go-2") and t.strip():
                    st.session_state["add_to_portfolio"] = t.strip().upper()
                    ui.open_ticker(t.strip())
    inbox()
    watchlist_levels()
