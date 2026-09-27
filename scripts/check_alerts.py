"""Evaluate alerts for every holding and watchlist ticker, outside Streamlit.

    python scripts/check_alerts.py
    python scripts/check_alerts.py --tickers LULU,CNR.TO

The app launches this in the background on start (app/alert_jobs.py), at most every
ALERT_CHECK_MIN_INTERVAL_MINUTES; scripts/run_screen.py runs the same check at the end
of every screen. VSA_DATA_SOURCE=fixtures runs it offline. Exit codes: 0 completed,
1 the check failed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from app import services  # noqa: E402
from llm.client import LLMClient  # noqa: E402
from portfolio import store  # noqa: E402
from portfolio.alerts import check_alerts  # noqa: E402
from screening.engine import ScreenContext  # noqa: E402


def run_check(provider, db_path, source: str, tickers: list[str] | None = None, check_id: int | None = None,
              today: date | None = None):
    """The check with the app's provider stack, EDGAR and (cached) LLM for 6-K confirmations."""
    ctx = ScreenContext(provider=provider, db_path=db_path, today=today or date.today(),
                        valet_fetch=services.valet_fetch(), save_snapshots=True)
    llm = LLMClient(db_path=db_path)
    return check_alerts(ctx, source, llm=llm, edgar=services.build_edgar(provider), tickers=tickers,
                        check_id=check_id)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tickers", default="", help="comma-separated tickers (default: holdings + watchlist)")
    ap.add_argument("--db", default=str(config.RUNS_DB_PATH), help="runs database path")
    ap.add_argument("--source", default="manual", help="recorded on each alert: app_start | screen | manual")
    ap.add_argument("--check-id", type=int, default=None, help="an alert_checks row the app already created")
    ap.add_argument("--log", default=None, help="log file")
    args = ap.parse_args(argv)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if args.check_id is not None:
        store.set_check_pid(args.check_id, os.getpid(), args.db)
    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
    try:
        rep = run_check(services.build_provider(), args.db, args.source, tickers, args.check_id)
    except Exception as exc:
        logging.exception("alert check failed: %s", exc)
        return 1
    logging.info("alert check %s: %d tickers, %d new alerts, %d errors, email %s", rep.check_id, len(rep.tickers),
                 len(rep.fired), len(rep.errors), rep.email_status)
    for a in rep.fired:
        logging.info("  %s %s: %s", a.ticker, a.kind, a.message)
    for t, e in rep.errors.items():
        logging.warning("  %s: %s", t, e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
