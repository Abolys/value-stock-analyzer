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
from app import alert_jobs, auth, screen_jobs, services, ui  # noqa: E402
from app import screener_view  # noqa: E402
from app.views import accuracy, portfolio, screener, stock  # noqa: E402
from portfolio import store as pf_store  # noqa: E402
from storage import feedback  # noqa: E402

st.set_page_config(page_title="Value Stock Analyzer", layout="wide")


@st.cache_resource
def _provider():
    return services.build_provider()


def _ticker_entered() -> None:
    value = (st.session_state.get("ticker_box") or "").strip().upper()
    if value:
        st.session_state["ticker"] = value
        st.session_state["goto_stock"] = True


TICKER_BOXES = ("ticker_box", "ticker_search")  # the sidebar box and the Stock page's top search box


def _sync_ticker_boxes() -> None:
    """Both ticker inputs mirror the committed ticker. Committing happens in the boxes' on_change
    callbacks: Streamlit runs a changed widget's callback before the script body, so by the time
    this runs, session_state["ticker"] already holds the value the user just entered in either box
    (the callback sets it, with goto_stock). The boxes themselves are never read for a commit:
    after the callback moved the ticker, the box just committed equals it, while the other box
    still mirrors the previous ticker until the re-seed below — treating that stale mirror as a
    fresh commit would undo the user's input. On the Stock page, a ticker the user typed into the
    URL is a commit too; it is acted on only when it differs from what the app last wrote, so the
    URL merely catching up to a fresh commit never undoes that commit. st.switch_page() also resets
    widget values on page change (Streamlit quirk), so re-seed both boxes after every run instead
    of letting them drift."""
    if "pending_ticker_box" in st.session_state:  # set by ui.open_ticker before this run's widget exists
        st.session_state["ticker_box"] = st.session_state.pop("pending_ticker_box")
    committed = ""
    if st.session_state.get("current_page") == "Stock":
        prev = (st.session_state.get("ticker") or "").strip().upper()
        linked = (st.query_params.get("ticker") or "").strip().upper()
        if linked and linked != prev and linked != st.session_state.get("url_ticker"):
            committed = linked  # the URL names its ticker (bookmarkable)
    if committed:
        st.session_state["ticker"] = committed
        st.session_state["goto_stock"] = True  # land on the Stock page if we weren't already there
    ticker = (st.session_state.get("ticker") or "").strip().upper()
    # re-seed unconditionally, not just after a commit: st.switch_page() clears the widget values on
    # the page change, so the run that follows a switch needs this to bring both boxes back
    for key in TICKER_BOXES:
        if ticker and (st.session_state.get(key) or "").strip().upper() != ticker:
            st.session_state[key] = ticker


def sidebar() -> None:
    with st.sidebar:
        st.text_input("Ticker", key="ticker_box", on_change=_ticker_entered,
                      placeholder="e.g. LULU, CNR.TO", help="Any ticker; manual tickers bypass the screen")
        unread = pf_store.unread_count(path=config.RUNS_DB_PATH)
        checking = " · checking alerts…" if alert_jobs.running(config.RUNS_DB_PATH) else ""
        if "portfolio" in ui.PAGES:
            st.page_link(ui.PAGES["portfolio"], label=f"Alerts: {unread} unread{checking}", icon="🔔")
        else:
            st.caption(f"Alerts: {unread} unread{checking}")
        access_and_feedback()


def access_and_feedback() -> None:
    """Who is signed in (for shared use), the feedback box, and, for the owner, the feedback received."""
    if auth.is_remote():
        st.caption("Signed in: " + ("owner (full access)" if auth.is_owner() else
                                    "viewer (read-only: nothing is saved, no new LLM calls)"))
        if st.button("Sign out", key="sign-out"):
            auth.sign_out()
            st.rerun()
    with st.expander("💬 Feedback on the app"):
        key = "feedback-text"
        st.text_area("What works, what's confusing, what's missing?", key=key)
        st.text_input("Your name (optional)", key="feedback-name")

        def send() -> None:
            text = st.session_state.get(key, "")
            if text.strip():
                feedback.add(text, st.session_state.get("feedback-name", ""),
                             page=st.session_state.get("current_page", ""), path=config.RUNS_DB_PATH)
                st.session_state[key] = ""
                st.session_state["feedback-sent"] = True

        st.button("Send feedback", key="feedback-send", on_click=send)
        if st.session_state.pop("feedback-sent", False):
            st.success("Thanks, sent.")
    if auth.is_owner():
        new = feedback.unread(config.RUNS_DB_PATH)
        items = feedback.latest(path=config.RUNS_DB_PATH)
        if items:
            with st.expander(f"📥 Feedback received ({new} new)"):
                for f in items:
                    st.markdown(f"{'🆕 ' if f.read_at is None else ''}**{f.name or 'anonymous'}** · "
                                f"{f.created_at:%Y-%m-%d %H:%M}{' · ' + f.page if f.page else ''}\n\n{f.text}")
                if new and st.button("Mark all read", key="feedback-read"):
                    feedback.mark_all_read(config.RUNS_DB_PATH)
                    st.rerun()


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
    ui.shell_css()  # cap and centre the main column (the mockup's centred-column look)
    auth.gate()  # local use: the owner, no login; through the share link: a password
    provider = _provider()
    report = services.health(provider)
    ui.PAGES.update({
        "screener": st.Page(lambda: screener.render(provider), title="Screener", url_path="screener", default=True,
                            icon=":material/search:"),
        "stock": st.Page(lambda: stock.render(provider), title="Stock", url_path="stock",
                         icon=":material/insights:"),
        "portfolio": st.Page(lambda: portfolio.render(provider), title="Portfolio", url_path="portfolio",
                             icon=":material/account_balance_wallet:"),
        "accuracy": st.Page(lambda: accuracy.render(provider), title="Estimate accuracy", url_path="accuracy",
                            icon=":material/track_changes:"),
    })
    if auth.is_owner():  # a viewer never starts background work
        start_alert_check()
        remind_screen()
    page = st.navigation(list(ui.PAGES.values()))
    st.session_state["current_page"] = page.title
    _sync_ticker_boxes()  # before any ticker widget renders: align both boxes with the committed ticker
    sidebar()
    if st.session_state.pop("goto_stock", False) and page.url_path != "stock":
        st.switch_page(ui.PAGES["stock"])
    ui.banner(report)
    if st.session_state.get("screen_reminder_note"):
        st.info(st.session_state.pop("screen_reminder_note"))
    if st.session_state.get("alert_check_error"):
        st.warning(st.session_state["alert_check_error"])
    page.run()


main()
