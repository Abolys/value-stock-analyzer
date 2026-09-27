"""Rebuild the screener universe lists from ETF holdings files.

    python scripts/refresh_universe.py [list ...]

Lists: cowz, cash_cows_small (CALF), sp400 (IJH), sp600 (IJR), tsx_composite (XIC).
URLs live in config.UNIVERSE_SOURCES. When a download fails or its format has
changed, a manually downloaded file at data/universe/raw/<list>.csv or .xlsx is
used instead; otherwise the previous CSV is kept and reported as stale.
watchlist.csv and dataroma.csv are hand-maintained and never downloaded
(Dataroma must never be scraped). Run monthly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from data.universe import ensure_manual_templates, refresh_list  # noqa: E402


def main(argv: list[str]) -> int:
    keys = argv or list(config.UNIVERSE_SOURCES)
    config.UNIVERSE_RAW_DIR.mkdir(parents=True, exist_ok=True)
    ensure_manual_templates()
    bad = 0
    for key in keys:
        outcome = refresh_list(key)
        print(f"[{outcome.status.upper():8}] {outcome.message}")
        bad += outcome.status in ("stale", "missing")
    print(f"\n{len(keys) - bad} of {len(keys)} lists current.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
