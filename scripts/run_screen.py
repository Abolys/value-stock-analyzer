"""Run the value screener over universe lists, outside Streamlit.

    python scripts/run_screen.py --lists cowz,tsx_composite
    python scripts/run_screen.py --resume          # continue an interrupted or stopped run

Writes to the screen_runs / screen_results tables in storage/runs.db. Exit
codes: 0 completed, 1 nothing to run / bad arguments, 2 blocked by the health
check, 3 stopped by the circuit breaker (resumable). VSA_DATA_SOURCE=fixtures runs it offline from tests/fixtures.
A completed run then checks alerts for holdings and the watchlist (scripts/check_alerts.py).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from app import services  # noqa: E402
from data.universe import list_labels  # noqa: E402
from screening.engine import ScreenContext  # noqa: E402
from screening.runner import run_screen  # noqa: E402
from storage import screen_store as store  # noqa: E402

EXIT = {store.COMPLETED: 0, store.BLOCKED: 2, store.STOPPED: 3}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lists", default="", help=f"comma-separated list keys: {', '.join(list_labels())}")
    ap.add_argument("--resume", action="store_true", help="continue the latest interrupted or stopped run")
    ap.add_argument("--db", default=str(config.RUNS_DB_PATH), help="runs database path")
    ap.add_argument("--log", default=None, help="log file (the app passes one when it launches a run)")
    ap.add_argument("--run-id", type=int, default=0,
                    help="run row the app created on launch (0: create a new row, the CLI behaviour)")
    ap.add_argument("--universe-dir", default=str(config.UNIVERSE_DIR), help="folder with the universe CSVs")
    args = ap.parse_args(argv)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    lists = [k.strip() for k in args.lists.split(",") if k.strip()]
    unknown = [k for k in lists if k not in list_labels()]
    if unknown:
        ap.error(f"unknown list(s): {', '.join(unknown)}")
    if args.run_id and args.resume:
        ap.error("--run-id and --resume are mutually exclusive")
    if not lists and not args.resume:
        ap.error("give --lists or --resume")

    provider = services.build_provider()
    ctx = ScreenContext(provider=provider, db_path=args.db, today=date.today(), valet_fetch=services.valet_fetch())
    try:
        run = run_screen(ctx, lists or None, resume=args.resume, log_path=args.log, progress=logging.info,
                         universe_dir=Path(args.universe_dir), run_id=args.run_id)
    except (RuntimeError, ValueError) as exc:
        logging.error("%s", exc)
        return 1
    logging.info("run %s finished with status %r: %s attempted, %s passed stage 1, %s Pass, %s failed to load",
                 run.run_id, run.status, run.attempted, run.passed_stage1, run.passed_stage2, run.failed_to_load)
    if run.status == store.COMPLETED:
        _check_alerts(provider, args.db)
    return EXIT.get(run.status, 1)


def _check_alerts(provider, db_path) -> None:
    """Alerts for holdings and the watchlist at the end of every completed screen. A failure is
    logged and never changes the screen's exit code."""
    from scripts.check_alerts import run_check

    try:
        rep = run_check(provider, db_path, "screen")
        logging.info("alert check %s: %d tickers, %d new alerts, %d errors, email %s", rep.check_id,
                     len(rep.tickers), len(rep.fired), len(rep.errors), rep.email_status)
    except Exception as exc:  # the screen itself completed; the alert check is reported separately
        logging.exception("alert check after the screen failed: %s", exc)


if __name__ == "__main__":
    raise SystemExit(main())
