"""Install the headless Chrome that kaleido needs for the export PNGs into the
project (config.CHROME_DIR, gitignored), so exports find it however the app is
started (VS Code, a terminal, cron). About 350 MB; run once, or again with
--force to update.

    python scripts/get_chrome.py [--force]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kaleido  # noqa: E402

import config  # noqa: E402
from reports.images import project_chrome, render_png  # noqa: E402


def main(argv: list[str]) -> int:
    force = "--force" in argv
    existing = project_chrome()
    if existing and not force:
        print(f"Chrome already installed: {existing}")
    else:
        config.CHROME_DIR.mkdir(parents=True, exist_ok=True)
        kaleido.get_chrome_sync(path=config.CHROME_DIR, force=force)
        existing = project_chrome()
        if existing is None:
            print(f"FAIL: download finished but no Chrome executable found under {config.CHROME_DIR}")
            return 1
        print(f"Chrome installed: {existing}")
    import plotly.graph_objects as go

    png, why = render_png(go.Figure(go.Bar(x=[1, 2], y=[3, 4])))
    print("PNG test render: " + (f"OK ({len(png):,} bytes)" if png else f"FAILED — {why}"))
    return 0 if png else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
