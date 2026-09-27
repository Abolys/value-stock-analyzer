"""Permanent disk cache for LLM responses (CLAUDE.md Rule 4).

Keyed by (ticker, lens, hash of the input payload, prompt version), so the
same inputs give the same answer across refreshes; changing a prompt template
bumps its version and misses the cache. The model name is part of the hashed
input, so switching ANTHROPIC_MODEL never serves another model's answer.
The 6-K departure check uses the filing accession number in the hash slot.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
    ticker TEXT NOT NULL,
    lens TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (ticker, lens, payload_hash, prompt_version)
)
"""


def payload_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


class LLMCache:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path if path is not None else config.LLM_CACHE_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self, ticker: str, lens: str, key_hash: str, prompt_version: str) -> str | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT response_json FROM llm_cache WHERE ticker=? AND lens=? AND payload_hash=? "
                               "AND prompt_version=?", (ticker, lens, key_hash, prompt_version)).fetchone()
        return row[0] if row else None

    def put(self, ticker: str, lens: str, key_hash: str, prompt_version: str, model: str, response_json: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?,?,?)",
                         (ticker, lens, key_hash, prompt_version, model, response_json,
                          datetime.now().isoformat(timespec="seconds")))
