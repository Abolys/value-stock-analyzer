"""Chart PNGs for exports via kaleido, which drives a headless Chrome. Chrome is
looked up in this order: the BROWSER_PATH environment variable, the project's
config.CHROME_DIR (install with `python scripts/get_chrome.py`), then kaleido's own
defaults (PATH and its download folder). A chart that can't be rendered is never
fatal: the renderer returns the reason and the report lists it as not rendered."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import plotly.graph_objects as go

import config

EXPORT_WIDTH, EXPORT_SCALE = 900, 2

Renderer = Callable[[go.Figure], "tuple[bytes | None, str]"]


# The Chrome executable inside a Chrome-for-Testing download, per platform.
CHROME_EXECUTABLES = ["chrome-linux64/chrome", "chrome-win64/chrome.exe", "chrome-win32/chrome.exe",
                      "chrome-mac-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"]


def project_chrome(root: Path | None = None) -> Path | None:
    root = root or config.CHROME_DIR
    for pattern in CHROME_EXECUTABLES:
        for exe in sorted(root.glob(pattern)):
            if exe.is_file():
                return exe
    return None


def use_project_chrome() -> str:
    """Point kaleido at the project's Chrome unless BROWSER_PATH is already set; returns the path used."""
    if os.environ.get("BROWSER_PATH"):
        return os.environ["BROWSER_PATH"]
    exe = project_chrome()
    if exe is not None:
        os.environ["BROWSER_PATH"] = str(exe)
        return str(exe)
    return ""


def render_png(fig: go.Figure) -> tuple[bytes | None, str]:
    chrome = use_project_chrome()
    try:
        export = go.Figure(fig)
        export.update_layout(paper_bgcolor="white", plot_bgcolor="white")
        height = export.layout.height or 400
        return export.to_image(format="png", width=EXPORT_WIDTH, height=height, scale=EXPORT_SCALE), ""
    except Exception as exc:  # kaleido or Chrome missing, or a rendering failure
        reason = f"{type(exc).__name__}: {exc}".splitlines()[0][:200]
        if not chrome:
            reason += " (no Chrome in the project; run `python scripts/get_chrome.py`)"
        return None, reason
