"""Snapshot CEO/CFO names for tickers that no screen covers.

Screen runs already snapshot every screened ticker from their stage-1 info
call, so this only covers watchlist/dataroma names that aren't in any
ETF-derived list. Throttled through the cached provider; resumable — tickers
already snapshotted today are skipped when the script is rerun.

    python scripts/snapshot_officers.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from data.cache import CachedProvider  # noqa: E402
from data.provider import DataProvider, ProviderError  # noqa: E402
from data.universe import load_list  # noqa: E402
from data.yfinance_provider import YFinanceProvider  # noqa: E402
from storage.db import save_officer_snapshot, snapshotted_tickers_on  # noqa: E402


def unscreened_tickers() -> list[str]:
    screened = {t for key in config.UNIVERSE_SOURCES for t in load_list(key)["ticker"]}
    manual = [t for key in config.MANUAL_UNIVERSE_LISTS for t in load_list(key)["ticker"]]
    return sorted({t for t in manual if t and t not in screened})


def run(provider: DataProvider, tickers: list[str], today: date | None = None,
        db_path=config.RUNS_DB_PATH) -> dict[str, str]:
    today = today or date.today()
    done = snapshotted_tickers_on(today, db_path)
    results = {}
    for t in tickers:
        if t.upper() in done:
            results[t] = "skipped (already snapshotted today)"
            continue
        try:
            snap = save_officer_snapshot(t, provider.get_info(t), today, db_path)
            results[t] = f"CEO {snap.ceo or '—'}; CFO {snap.cfo or '—'}"
        except ProviderError as exc:
            results[t] = f"failed: {exc}"
    return results


def main() -> int:
    tickers = unscreened_tickers()
    if not tickers:
        print("No unscreened watchlist/dataroma tickers.")
        return 0
    provider = CachedProvider(YFinanceProvider())
    for t, msg in run(provider, tickers).items():
        print(f"{t}: {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
