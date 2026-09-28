"""Value Stock Analyzer — app shell: shared sidebar, the data-source banner and
four pages (Screener, Stock, Portfolio, Estimate accuracy), following docs/ui-mockup.html.
On start it launches the background alert check for holdings and the watchlist when one is due.

    streamlit run app/main.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

import config  # noqa: E402
from app import alert_jobs, screen_jobs, services, ui  # noqa: E402
from app import screener_view  # noqa: E402
from app.views import accuracy, portfolio, screener, stock  # noqa: E402
from portfolio import store as pf_store  # noqa: E402
from storage import llm_store  # noqa: E402

st.set_page_config(page_title="Value Stock Analyzer", layout="wide")


@st.cache_resource
def _provider():
    return services.build_provider()


def _ticker_entered() -> None:
    value = (st.session_state.get("ticker_box") or "").strip().upper()
    if value:
        st.session_state["ticker"] = value
        st.session_state["goto_stock"] = True


def sidebar(report) -> None:
    if "pending_ticker_box" in st.session_state:  # set by ui.open_ticker before this run's widget exists
        st.session_state["ticker_box"] = st.session_state.pop("pending_ticker_box")
    with st.sidebar:
        st.text_input("Ticker", key="ticker_box", on_change=_ticker_entered,
                      placeholder="e.g. LULU, CNR.TO", help="Any ticker; manual tickers bypass the screen")
        spend, calls, hits = llm_store.month_spend(path=config.RUNS_DB_PATH)
        analyses = llm_store.month_analyses(path=config.RUNS_DB_PATH)
        st.caption(f"API spend this month: \\${spend:.2f} · {analyses} analyses ({calls} LLM calls, {hits} cache hits)")
        cc_calls, cc_est = llm_store.month_claude_code(path=config.RUNS_DB_PATH)
        if cc_calls:
            st.caption(f"Claude Code (subscription) this month: {cc_calls} calls, ≈\\${cc_est:.2f} at API list prices")
        unread = pf_store.unread_count(path=config.RUNS_DB_PATH)
        checking = " · checking alerts…" if alert_jobs.running(config.RUNS_DB_PATH) else ""
        if "portfolio" in ui.PAGES:
            st.page_link(ui.PAGES["portfolio"], label=f"Alerts: {unread} unread{checking}", icon="🔔")
        else:
            st.caption(f"Alerts: {unread} unread{checking}")
        st.caption(f"Data source check: {'OK' if report.ok else 'FAILED'} at {report.checked_at:%Y-%m-%d %H:%M}")


def _close_reminder() -> None:
    st.session_state.pop("screen_reminder", None)


@st.dialog("Screen out of date", on_dismiss=_close_reminder)
def screen_reminder(prompt: screen_jobs.ScreenPrompt) -> None:
    st.write(prompt.message)
    rows = {r["key"]: r for r in screener_view.list_picker_rows()}
    lists = [k for k in prompt.lists if k in rows] or [k for k, r in rows.items() if r["count"]]
    if prompt.kind == "run":
        st.caption("Lists: " + ", ".join(f"{rows[k]['label']} ({rows[k]['count']:,})" for k in lists)
                   + ". Choose different lists on the Screener page.")
    c1, c2 = st.columns(2)
    label = "Resume screen" if prompt.kind == "resume" else "Run screen now"
    if c1.button(label, type="primary", key="remind-run", disabled=prompt.kind == "run" and not lists):
        try:
            if prompt.kind == "resume":
                screen_jobs.launch(resume=True, db_path=config.RUNS_DB_PATH)
            else:
                screen_jobs.launch(lists, db_path=config.RUNS_DB_PATH)
            st.session_state["screen_reminder_note"] = "Screen started in the background; progress is on the Screener page."
        except RuntimeError as exc:
            st.session_state["screen_reminder_note"] = f"Screen not started: {exc}"
        _close_reminder()
        st.rerun()
    if c2.button("Not now", key="remind-skip"):
        _close_reminder()
        st.rerun()


def remind_screen() -> None:
    """Once per session: ask to run a screen when the last completed one is older than SCREEN_REMIND_DAYS."""
    if not st.session_state.get("screen_reminder_checked"):
        st.session_state["screen_reminder_checked"] = True
        prompt = screen_jobs.screen_prompt(config.RUNS_DB_PATH)
        if prompt is not None:
            st.session_state["screen_reminder"] = prompt  # kept until answered or dismissed
    if st.session_state.get("screen_reminder") is not None:
        screen_reminder(st.session_state["screen_reminder"])


def start_alert_check() -> None:
    """Once per session: launch the background alert check when one is due (never blocks the page)."""
    if st.session_state.get("alert_check_started"):
        return
    st.session_state["alert_check_started"] = True
    try:
        alert_jobs.maybe_start(config.RUNS_DB_PATH)
    except Exception as exc:  # the app must open even if the check can't start; say so
        st.session_state["alert_check_error"] = f"Alert check not started: {type(exc).__name__}: {exc}"


def main() -> None:
    provider = _provider()
    report = services.health(provider)
    ui.PAGES.update({
        "screener": st.Page(lambda: screener.render(provider), title="Screener", url_path="screener", default=True),
        "stock": st.Page(lambda: stock.render(provider), title="Stock", url_path="stock"),
        "portfolio": st.Page(lambda: portfolio.render(provider), title="Portfolio", url_path="portfolio"),
        "accuracy": st.Page(lambda: accuracy.render(provider), title="Estimate accuracy", url_path="accuracy"),
    })
    start_alert_check()
    remind_screen()
    page = st.navigation(list(ui.PAGES.values()))
    sidebar(report)
    if st.session_state.pop("goto_stock", False) and page.url_path != "stock":
        st.switch_page(ui.PAGES["stock"])
    ui.banner(report)
    if st.session_state.get("screen_reminder_note"):
        st.info(st.session_state.pop("screen_reminder_note"))
    if st.session_state.get("alert_check_error"):
        st.warning(st.session_state["alert_check_error"])
    page.run()


main()
