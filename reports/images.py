"""Chart PNGs for exports via kaleido (which drives a headless Chrome; install it
once with `plotly_get_chrome`). A chart that can't be rendered is never fatal:
the renderer returns the reason and the report lists the chart as not rendered."""

from __future__ import annotations

from typing import Callable

import plotly.graph_objects as go

EXPORT_WIDTH, EXPORT_SCALE = 900, 2

Renderer = Callable[[go.Figure], "tuple[bytes | None, str]"]


def render_png(fig: go.Figure) -> tuple[bytes | None, str]:
    try:
        export = go.Figure(fig)
        export.update_layout(paper_bgcolor="white", plot_bgcolor="white")
        height = export.layout.height or 400
        return export.to_image(format="png", width=EXPORT_WIDTH, height=height, scale=EXPORT_SCALE), ""
    except Exception as exc:  # kaleido or Chrome missing, or a rendering failure
        return None, f"{type(exc).__name__}: {exc}".splitlines()[0][:200]
