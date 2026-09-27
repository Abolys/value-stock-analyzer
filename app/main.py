"""Value Stock Analyzer — app shell: shared sidebar, the data-source banner and
three pages (Screener, Stock, Estimate accuracy), following docs/ui-mockup.html.

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
from app import services, ui  # noqa: E402
from app.views import accuracy, screener, stock  # noqa: E402
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
        st.caption(f"Data source check: {'OK' if report.ok else 'FAILED'} at {report.checked_at:%Y-%m-%d %H:%M}")


def main() -> None:
    provider = _provider()
    report = services.health(provider)
    ui.PAGES.update({
        "screener": st.Page(lambda: screener.render(provider), title="Screener", url_path="screener", default=True),
        "stock": st.Page(lambda: stock.render(provider), title="Stock", url_path="stock"),
        "accuracy": st.Page(lambda: accuracy.render(provider), title="Estimate accuracy", url_path="accuracy"),
    })
    page = st.navigation(list(ui.PAGES.values()))
    sidebar(report)
    if st.session_state.pop("goto_stock", False) and page.url_path != "stock":
        st.switch_page(ui.PAGES["stock"])
    ui.banner(report)
    page.run()


main()
