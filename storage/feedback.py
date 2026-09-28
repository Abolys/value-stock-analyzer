"""Feedback left through the app's sidebar (typically by a friend using the shared link)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

import config
from storage.db import connect



class Feedback(BaseModel):
    feedback_id: int
    created_at: datetime
    name: str = ""
    page: str = ""
    text: str
    read_at: datetime | None = None


def _db(path: Path | str | None) -> Path | str:
    return path if path is not None else config.RUNS_DB_PATH


def add(text: str, name: str = "", page: str = "", path: Path | str | None = None) -> int:
    text = text.strip()
    if not text:
        raise ValueError("empty feedback")
    with connect(_db(path)) as conn:
        cur = conn.execute("INSERT INTO feedback (created_at, name, page, text) VALUES (?,?,?,?)",
                           (datetime.now().isoformat(timespec="seconds"), name.strip()[:100], page[:100],
                            text[:config.FEEDBACK_MAX_CHARS]))
        return int(cur.lastrowid)


def latest(limit: int = config.FEEDBACK_SHOWN, path: Path | str | None = None) -> list[Feedback]:
    with connect(_db(path)) as conn:
        rows = conn.execute("SELECT feedback_id, created_at, name, page, text, read_at FROM feedback "
                            "ORDER BY feedback_id DESC LIMIT ?", (limit,)).fetchall()
    return [Feedback(feedback_id=r[0], created_at=r[1], name=r[2] or "", page=r[3] or "", text=r[4], read_at=r[5])
            for r in rows]


def unread(path: Path | str | None = None) -> int:
    with connect(_db(path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM feedback WHERE read_at IS NULL").fetchone()[0])


def mark_all_read(path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute("UPDATE feedback SET read_at = ? WHERE read_at IS NULL", (datetime.now().isoformat(timespec="seconds"),))
