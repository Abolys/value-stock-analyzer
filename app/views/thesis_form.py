"""The thesis form ("Add to portfolio" on the Stock page) and the sell-trigger builder.

Triggers are built by picking a field from THESIS_TRIGGER_FIELDS, an operator allowed for
its type and a value (a number, a choice, true/false, or a thesis level such as the
target price). Each rule is validated when it is added and again when the holding is saved;
nothing typed is ever evaluated as text."""

from __future__ import annotations

from datetime import date

import streamlit as st

import config
from analysis.models import AnalysisRun
from portfolio import store
from portfolio.models import Reason, Thesis, Transaction, Trigger
from portfolio.thesis import default_levels, snapshot_of
from portfolio.triggers import TriggerError, allowed_operators, validate_trigger

VALUE_LITERAL, VALUE_LEVEL = "a value", "a thesis level"


def trigger_builder(key: str) -> list[Trigger]:
    """Pick field → operator → value; the rules built so far live in session state under `key`."""
    rules: list[Trigger] = st.session_state.setdefault(key, [])
    fields = list(config.THESIS_TRIGGER_FIELDS)
    c1, c2, c3 = st.columns([3, 1, 3])
    field = c1.selectbox("Field", fields, key=f"{key}-field",
                         format_func=lambda f: f"{f} — {config.THESIS_TRIGGER_FIELDS[f]['label']}")
    spec = config.THESIS_TRIGGER_FIELDS[field]
    op = c2.selectbox("Operator", allowed_operators(field), key=f"{key}-op-{spec['type']}")
    literal, ref = None, None
    with c3:
        if spec["type"] == "number":
            kind = st.radio("Compare with", [VALUE_LITERAL, VALUE_LEVEL], horizontal=True, key=f"{key}-kind")
            if kind == VALUE_LEVEL:
                ref = st.selectbox("Thesis level", config.THESIS_LEVEL_FIELDS, key=f"{key}-ref")
            else:
                literal = st.number_input("Value", value=0.0, format="%.4f", key=f"{key}-num",
                                          help="Ratios and yields as decimals (e.g. 0.05 for 5%)")
        elif spec["type"] == "bool":
            literal = st.selectbox("Value", [True, False], key=f"{key}-bool",
                                   format_func=lambda b: "true" if b else "false")
        else:
            literal = st.selectbox("Value", spec.get("choices", []), key=f"{key}-enum-{field}")
    def add() -> None:
        # Read the widgets' current values from session state (the callback runs before the rerun).
        ss = st.session_state
        f = ss.get(f"{key}-field", field)
        kind = config.THESIS_TRIGGER_FIELDS[f]["type"]
        o = ss.get(f"{key}-op-{kind}", op)
        lit, rf = None, None
        if kind == "number":
            if ss.get(f"{key}-kind") == VALUE_LEVEL:
                rf = ss.get(f"{key}-ref")
            else:
                lit = ss.get(f"{key}-num", 0.0)
        else:
            lit = ss.get(f"{key}-bool" if kind == "bool" else f"{key}-enum-{f}", literal)
        try:
            rules.append(validate_trigger(Trigger(field=f, op=o, literal=lit, ref=rf)))
            st.session_state.pop(f"{key}-error", None)
        except TriggerError as exc:
            st.session_state[f"{key}-error"] = f"Rule not added: {exc}"

    st.button("Add rule", key=f"{key}-add", on_click=add)
    if st.session_state.get(f"{key}-error"):
        st.error(st.session_state[f"{key}-error"])
    for i, t in enumerate(rules):
        a, b = st.columns([6, 1])
        a.markdown(f"`{t.text}`")
        b.button("Remove", key=f"{key}-rm-{i}-{t.text}", on_click=rules.pop, args=(i,))
    if not rules:
        st.caption("No sell triggers yet. Write them now, before you own it long enough to get attached.")
    return rules


def _opt(v: float | None) -> float | None:
    return None if v is None or v <= 0 else float(v)


def _dismissed() -> None:
    st.session_state.pop("add_to_portfolio", None)


@st.dialog("Add to portfolio", width="large", on_dismiss=_dismissed)
def add_holding_dialog(run: AnalysisRun) -> None:
    lv = default_levels(run)
    price = run.screen.price.value if run.screen is not None and run.screen.price.ok else None
    st.caption(f"{run.ticker} · purchase snapshot = analysis {run.analysis_id} ({run.today}): every lens score, "
               f"key metric, signal and the turnaround estimate are frozen with the holding.")
    c1, c2, c3 = st.columns(3)
    account = c1.text_input("Account", value="TFSA", key="th-account")
    when = c2.date_input("Buy date", value=date.today(), key="th-date")
    ccy = c3.text_input("Currency", value=run.currency or "USD", key="th-ccy",
                        help="The currency you paid in; the trading currency by default")
    c1, c2, c3 = st.columns(3)
    shares = c1.number_input("Shares", min_value=0.0, value=0.0, step=1.0, key="th-shares")
    px = c2.number_input("Price paid", min_value=0.0, value=float(price or 0.0), format="%.4f", key="th-price",
                         help="Defaults to the actual latest price")
    fees = c3.number_input("Fees", min_value=0.0, value=0.0, key="th-fees")
    st.markdown("**Thesis**")
    reasons = st.text_area("Reasons (one per line)", key="th-reasons",
                           placeholder="Trades 30% below DCF fair value\nNet cash; buybacks continuing")
    c1, c2, c3 = st.columns(3)
    iv = c1.number_input("Intrinsic value", min_value=0.0, value=float(lv["intrinsic_value"] or 0.0), format="%.2f",
                         key="th-iv")
    bb = c2.number_input("Buy-below", min_value=0.0, value=float(lv["buy_below_price"] or 0.0), format="%.2f",
                         key="th-bb", help=f"Default: intrinsic value × (1 − MIN_MARGIN_OF_SAFETY "
                                           f"{config.MIN_MARGIN_OF_SAFETY:.0%})")
    tp = c3.number_input("Target", min_value=0.0, value=float(lv["target_price"] or 0.0), format="%.2f", key="th-tp",
                         help="Default: the intrinsic value")
    st.caption(f"Intrinsic value basis: {lv['basis']}. Levels are in the trading currency ({run.currency or 'N/A'}). "
               "0 = not set.")
    st.markdown("**Sell triggers**")
    rules = trigger_builder(f"th-rules-{run.ticker}")
    if st.button("Save holding", type="primary", key="th-save"):
        try:
            thesis = Thesis(intrinsic_value=_opt(iv), buy_below_price=_opt(bb), target_price=_opt(tp),
                            basis=lv["basis"] if _opt(iv) == lv["intrinsic_value"] else "entered by hand",
                            reasons=[Reason(text=r) for r in reasons.splitlines() if r.strip()], triggers=rules)
            hid = store.add_holding(run.ticker, account, ccy.strip().upper() or (run.currency or "USD"),
                                    Transaction(txn_date=when, side="buy", shares=shares, price=px, fees=fees),
                                    thesis, snapshot=snapshot_of(run, thesis), snapshot_analysis_id=run.analysis_id)
        except (TriggerError, ValueError) as exc:
            st.error(f"Not saved: {exc}")
            return
        st.session_state.pop(f"th-rules-{run.ticker}", None)
        st.session_state.pop("add_to_portfolio", None)
        store.add_journal(hid, f"Bought {shares:g} at {px:,.2f} {ccy} ({account}). Thesis saved with "
                               f"{len(thesis.reasons)} reason(s) and {len(rules)} sell trigger(s).", kind="note")
        st.session_state["portfolio_selected"] = hid
        st.toast(f"{run.ticker} added to the portfolio ({account}). Follow it on the Portfolio page.")
        st.rerun()
