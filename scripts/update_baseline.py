"""Refresh the committed baseline/ snapshot from the current local runtime files.

The baseline (storage/runs.db -> baseline/runs.db, data/cache/*.db ->
baseline/*.db) is what every Streamlit Cloud deploy starts from: the Cloud app
copies it into place on start (storage/baseline.py) and never writes to the
committed files.

    python scripts/update_baseline.py

Run it after a screen run or portfolio change, with the app closed so the
SQLite files are quiescent, then commit and push:

    git add baseline
    git commit -m "chore(data): refresh deploy baseline"
    git push origin main
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

PAIRS = (
    ("runs.db", config.RUNS_DB_PATH),
    ("cache.db", config.CACHE_DB_PATH),
    ("llm_cache.db", config.LLM_CACHE_DB_PATH),
)


def main() -> int:
    base = ROOT / "baseline"
    base.mkdir(exist_ok=True)
    copied = []
    for name, src in PAIRS:
        src = Path(src)
        if not src.exists():
            print(f"skip {name}: {src} does not exist locally")
            continue
        try:
            con = sqlite3.connect(src)
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
        except sqlite3.Error as exc:
            print(f"warn: could not checkpoint {name}: {exc}")
        shutil.copyfile(src, base / name)
        copied.append(name)
    print("Copied: " + (", ".join(copied) or "nothing"))
    print("Next: git add baseline && git commit -m 'chore(data): refresh deploy baseline' && git push origin main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())