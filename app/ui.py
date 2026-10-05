"""Small shared Streamlit pieces: page registry and navigation to a ticker, tags,
the app-shell styles (content width), the data-source banner and chart display with
its caption."""

from __future__ import annotations

import html
import itertools
from typing import Any

import streamlit as st

from app.charts import ChartOut
from app.stock_view import Tag
from data.health import BANNER, INFO_BLOCKED_BANNER, HealthReport

_chart_ids = itertools.count()
PAGES: dict[str, Any] = {}  # filled by app/main.py: "screener" | "stock" | "accuracy" → st.Page

# The shell styles. Streamlit's "wide" layout is full-bleed, so on large monitors the charts and
# tables stretch into flat slabs; the mockup is a centred column. 1400px is the widest the main
# content gets (the sidebar keeps its own width). The type scale follows the mockup's editorial
# target: one calm page title and quiet section labels — Streamlit's default ~700-weight headings
# make every section shout at the same volume.
SHELL_CSS = """
<style>
.block-container {
    max-width: 1400px;
    margin: 0 auto;
}
.stApp h1 { font-size: 1.5rem; font-weight: 600; letter-spacing: -0.01em; }
.stApp h2 { font-size: 1.0625rem; font-weight: 500; }
.stApp h3 { font-size: 1rem; font-weight: 500; }
/* Align digits in columns everywhere in markdown/HTML text (the mockup's tabular-nums rule). */
.stApp [data-testid="stMarkdownContainer"] { font-variant-numeric: tabular-nums; }
</style>
"""


def shell_css() -> None:
    """Inject the app-shell styles; called once per run from app/main.py."""
    st.markdown(SHELL_CSS, unsafe_allow_html=True)

TAG_STYLE = {
    "warning": "background:rgba(250,178,25,0.20);color:#b36b00",
    "accent": "background:rgba(42,120,214,0.14);color:#2a78d6",
    "danger": "background:rgba(208,59,59,0.14);color:#d03b3b",
    "success": "background:rgba(12,163,12,0.14);color:#1a8f1a",
}


def open_ticker(ticker: str) -> None:
    """Open a ticker's Stock page (from a table row, a scatter point or a changes card)."""
    st.session_state["ticker"] = ticker.upper()
    st.session_state["pending_ticker_box"] = ticker.upper()
    if "stock" in PAGES:
        st.switch_page(PAGES["stock"])


def tags_html(tags: list[Tag]) -> str:
    spans = [f'<span title="{html.escape(t.tip)}" style="{TAG_STYLE.get(t.kind, TAG_STYLE["accent"])};'
             f'font-size:0.8rem;padding:2px 8px;border-radius:8px;margin:0 6px 6px 0;display:inline-block;'
             f'white-space:nowrap">{html.escape(t.text)}</span>' for t in tags]
    return "".join(spans)


def badge_html(text: str, kind: str = "accent") -> str:
    return (f'<span style="{TAG_STYLE[kind]};font-size:1rem;padding:4px 12px;border-radius:8px;'
            f'font-weight:500">{html.escape(text)}</span>')


def steps_html(steps: list[str]) -> str:
    """Mapping steps as a chain of chips with arrows between them (the mockup's mapping line),
    instead of one long inline bold sentence."""
    chips = [f'<span style="{TAG_STYLE["accent"]};font-size:0.78rem;padding:2px 8px;border-radius:8px;'
             f'white-space:nowrap">{html.escape(s, quote=False)}</span>' for s in steps]
    return " <span style='color:#6e6d68'>→</span> ".join(chips)


def banner(report: HealthReport) -> None:
    if report.ok:
        return
    if report.only_info_blocked:  # prices and statements work: a notice, not an error
        st.warning("**" + INFO_BLOCKED_BANNER + "**\n\n" + "\n".join(f"- {f}" for f in report.failures)
                   + f"\n\nChecked {report.checked_at:%Y-%m-%d %H:%M}.")
        return
    st.error("**" + BANNER + "**\n\n" + "\n".join(f"- {f}" for f in report.failures)
             + f"\n\nChecked {report.checked_at:%Y-%m-%d %H:%M}. Cached results are still shown, each with its age.")


def chart(c: ChartOut | None, key: str | None = None, on_select: Any = "ignore", **kw) -> Any:
    """Show a chart with its caption (notes, flags and everything left off with the reason)."""
    if c is None:
        return None
    # Progressive loading redraws a chart into the same placeholder; a fresh key per draw avoids
    # duplicate element IDs. Charts that report selections (the screener scatter) pass a fixed key.
    key = key or f"chart-{next(_chart_ids)}"
    event = st.plotly_chart(c.fig, key=key, on_select=on_select, config={"displayModeBar": False},
                            width="stretch", **kw)
    for f in c.flags:
        st.markdown(tags_html([Tag(text=f, kind="success")]), unsafe_allow_html=True)
    if c.caption:
        st.caption(c.caption)
    return event
