"""Chart and table colours (SPEC "Charts"). The categorical order, the diverging
pair and the status colours are the validated reference palette the UI mockup
uses; they are identity and state colours, not thresholds."""

from __future__ import annotations

import zlib

# Categorical hues in fixed order (identity). A ticker's slot comes from a hash of the
# ticker, so sorting or filtering never repaints it.
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

# Diverging (sensitivity heatmap): blue above the price, red below, grey midpoint.
DIVERGING_ABOVE = "#2a78d6"
DIVERGING_BELOW = "#e34948"
DIVERGING_MID = "#d9d8d3"

# Status (reserved: never used as a series colour).
GOOD = "#0ca30c"
WARNING = "#fab219"
CRITICAL = "#d03b3b"

INK = "#52514e"  # secondary text on charts
MUTED = "#898781"
GRID = "#c3c2b7"
PEER_GREY = "#b4b2a9"
PRIMARY = CATEGORICAL[0]
MARKER = "#e34948"  # the "you are here" marker and reference lines: visible on light and dark themes
REF_LINE = "#898781"

# Episode shading (drawdown history) and insider markers.
MARKET_DRIVEN_FILL = "rgba(42,120,214,0.18)"
COMPANY_SPECIFIC_FILL = "rgba(235,104,52,0.20)"
UNCLASSIFIED_FILL = "rgba(137,135,129,0.18)"
INSIDER_BUY = "#1baf7a"
INSIDER_SELL = "#e34948"

# Estimate accuracy stacks.
ACC_RECOVERED = "#1baf7a"
ACC_WAITING = "#b4b2a9"
ACC_MISSED = "#e34948"

# Screener cell states (text colour; readable on light and dark Streamlit themes).
CELL_TEXT = {"pass": "#1a8f1a", "fail": "#d03b3b", "na": "#898781", "nm": "#c98500", "info": ""}
CELL_BG = {"pass": "rgba(12,163,12,0.10)", "fail": "rgba(208,59,59,0.10)", "na": "rgba(137,135,129,0.12)",
           "nm": "rgba(250,178,25,0.16)", "info": ""}


def ticker_color(ticker: str) -> str:
    return CATEGORICAL[zlib.crc32(ticker.upper().encode()) % len(CATEGORICAL)]
