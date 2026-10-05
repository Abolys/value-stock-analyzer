"""Seed the runtime databases from the committed ``baseline/`` directory.

Streamlit Cloud checks out git-tracked files read-only, so the app must never
write to them. The committed baseline therefore ships the latest screen
results, portfolio and caches as a read-only snapshot; on start the app copies
each file into place and works on its own writable copy from then on.

A file is copied when the live copy is missing, or when the committed baseline
has changed since it was last copied (a hash is kept next to the live file), so
every push of a new baseline reaches the site even if the container survives
the redeploy. A live file that is not writable (a deploy that still has it under
git) is replaced by a writable copy of itself.

Refresh the baseline from a working local install with
``python scripts/update_baseline.py``, then commit ``baseline/`` and push.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Iterable

import config

MARKER_SUFFIX = ".baseline-sha"
_HASH_CHUNK = 1 << 20

_done = False  # main.py runs on every Streamlit rerun; seed once per process


def _pairs() -> tuple[tuple[str, Path], ...]:
    """(baseline filename, live destination) for every runtime database."""
    return (
        ("runs.db", config.RUNS_DB_PATH),
        ("cache.db", config.CACHE_DB_PATH),
        ("llm_cache.db", config.LLM_CACHE_DB_PATH),
    )


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _marker(dst: Path) -> Path:
    return dst.with_name(dst.name + MARKER_SUFFIX)


def bootstrap_baseline(baseline_dir: Path | str | None = None,
                       pairs: Iterable[tuple[str, Path]] | None = None) -> list[str]:
    """Copy missing or outdated runtime files from the committed baseline.

    A live file is replaced only when it is missing or the baseline differs from
    the one last copied to it; a local install whose marker matches (or that has
    no marker and an existing file, i.e. the machine the baseline is built on)
    keeps its own data. A live file the app cannot write is replaced by a
    writable copy of itself. Never raises: a missing baseline (fresh clone,
    fixtures mode) is a no-op. Returns the names of the files copied.
    """
    base = Path(baseline_dir) if baseline_dir is not None else config.ROOT / "baseline"
    copied: list[str] = []
    for name, dst in (list(pairs) if pairs is not None else _pairs()):
        src, dst = base / name, Path(dst)
        try:
            if src.exists():
                marker = _marker(dst)
                sha = _sha(src)
                seeded = marker.exists()
                if not dst.exists() or (seeded and marker.read_text().strip() != sha):
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _copy_into_place(src, dst)
                    marker.write_text(sha)
                    copied.append(name)
                    continue
            if dst.exists() and not os.access(dst, os.W_OK):
                _replace_with_writable_copy(dst)
                copied.append(name)
        except OSError:
            continue  # the app still opens; the database error, if any, surfaces where it is used
    return copied


def bootstrap_once() -> list[str]:
    """bootstrap_baseline() at most once per process (the app calls this on every rerun)."""
    global _done
    if _done:
        return []
    _done = True
    return bootstrap_baseline()


def _copy_into_place(src: Path, dst: Path) -> None:
    """Copy via a temp file and an atomic rename, dropping any stale SQLite side files."""
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    for side in ("-journal", "-wal", "-shm"):
        dst.with_name(dst.name + side).unlink(missing_ok=True)
    os.replace(tmp, dst)


def _replace_with_writable_copy(path: Path) -> None:
    """Swap a read-only file for a writable copy of its own content (needs the directory writable)."""
    tmp = path.with_name(path.name + ".tmp")
    shutil.copyfile(path, tmp)  # a new file: owned by this process, so writable
    os.replace(tmp, path)
