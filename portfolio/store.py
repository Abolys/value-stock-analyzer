"""SQLite storage for holdings, transactions, theses, triggers, the journal, watchlist
price levels, alerts and alert-check state (tables in storage/db.py).

Paths default to config.RUNS_DB_PATH read at call time, so tests and the app can
redirect the database. Triggers are validated here too, so nothing unvalidated can
be saved from any caller.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from portfolio import metrics as pm
from portfolio.models import (
    SELL, Alert, Holding, JournalEntry, Metric, Reason, Thesis, Transaction, Trigger, WatchLevel,
)
from portfolio.triggers import validate_trigger
from storage.db import connect

RUNNING, COMPLETED = "running", "completed"


def _db(path: Path | str | None) -> Path | str:
    return path if path is not None else config.RUNS_DB_PATH


def _now(now: datetime | None = None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


# --------------------------------------------------------------------------
# Holdings and transactions
# --------------------------------------------------------------------------
def add_holding(ticker: str, account: str, currency: str, first_buy: Transaction, thesis: Thesis,
                snapshot: dict[str, Metric] | None = None, snapshot_analysis_id: int | None = None,
                path: Path | str | None = None) -> int:
    """A new holding with its first buy, thesis (reasons, levels, triggers) and purchase snapshot.
    Every trigger is validated first; a TriggerError leaves nothing saved."""
    if first_buy.side != "buy":
        raise ValueError("a holding starts with a buy")
    triggers = [validate_trigger(t) for t in thesis.triggers]
    _check_txn(first_buy)
    with connect(_db(path)) as conn:
        cur = conn.execute(
            "INSERT INTO holdings (ticker, account, currency, created_at, snapshot_analysis_id, snapshot_json) "
            "VALUES (?,?,?,?,?,?)",
            (ticker.upper(), account.strip() or "Default", currency, _now(), snapshot_analysis_id,
             json.dumps(pm.dump(snapshot or {}))))
        hid = int(cur.lastrowid)
        _insert_txn(conn, hid, first_buy)
        cur = conn.execute("INSERT INTO theses (holding_id, created_at, intrinsic_value, buy_below_price, target_price, "
                           "basis) VALUES (?,?,?,?,?,?)",
                           (hid, _now(), thesis.intrinsic_value, thesis.buy_below_price, thesis.target_price,
                            thesis.basis))
        tid = int(cur.lastrowid)
        for r in thesis.reasons:
            if r.text.strip():
                conn.execute("INSERT INTO thesis_reasons (thesis_id, text) VALUES (?,?)", (tid, r.text.strip()))
        for t in triggers:
            _insert_trigger(conn, tid, t)
    return hid


def _check_txn(t: Transaction) -> None:
    if t.shares <= 0 or t.price <= 0 or t.fees < 0:
        raise ValueError("shares and price must be positive and fees not negative")


def _insert_txn(conn, holding_id: int, t: Transaction) -> int:
    cur = conn.execute("INSERT INTO transactions (holding_id, txn_date, side, shares, price, fees, note) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (holding_id, t.txn_date.isoformat(), t.side, t.shares, t.price, t.fees, t.note))
    return int(cur.lastrowid)


def _insert_trigger(conn, thesis_id: int, t: Trigger) -> int:
    value = {"ref": t.ref} if t.ref is not None else {"literal": t.literal}
    cur = conn.execute("INSERT INTO thesis_triggers (thesis_id, field, op, value_json, created_at) VALUES (?,?,?,?,?)",
                       (thesis_id, t.field, t.op, json.dumps(value), _now()))
    return int(cur.lastrowid)


def add_transaction(holding_id: int, t: Transaction, path: Path | str | None = None) -> int:
    """A buy or a sell. A sell larger than the shares held (before splits) is refused."""
    _check_txn(t)
    h = get_holding(holding_id, path)
    if h is None:
        raise ValueError(f"no holding {holding_id}")
    if t.side == SELL:
        held = sum(x.shares if x.side == "buy" else -x.shares for x in h.transactions if x.txn_date <= t.txn_date)
        if t.shares > held + config.RATIO_COMPARE_TOLERANCE:
            raise ValueError(f"sell of {t.shares:g} exceeds the {held:g} shares held on {t.txn_date} "
                             "(enter shares as traded; later splits are applied automatically)")
    with connect(_db(path)) as conn:
        return _insert_txn(conn, holding_id, t)


def delete_transaction(txn_id: int, path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute("DELETE FROM transactions WHERE txn_id = ?", (txn_id,))


def set_closed(holding_id: int, closed: bool = True, path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute("UPDATE holdings SET closed = ? WHERE holding_id = ?", (int(closed), holding_id))


def _thesis(conn, holding_id: int) -> Thesis | None:
    row = conn.execute("SELECT thesis_id, created_at, intrinsic_value, buy_below_price, target_price, basis "
                       "FROM theses WHERE holding_id = ?", (holding_id,)).fetchone()
    if not row:
        return None
    tid = row[0]
    reasons = [Reason(reason_id=r[0], text=r[1], still_holds=None if r[2] is None else bool(r[2]),
                      reviewed_at=_dt(r[3]))
               for r in conn.execute("SELECT reason_id, text, still_holds, reviewed_at FROM thesis_reasons "
                                     "WHERE thesis_id = ? ORDER BY reason_id", (tid,))]
    triggers = []
    for r in conn.execute("SELECT trigger_id, field, op, value_json, fired_at FROM thesis_triggers "
                          "WHERE thesis_id = ? ORDER BY trigger_id", (tid,)):
        v = json.loads(r[3])
        triggers.append(Trigger(trigger_id=r[0], field=r[1], op=r[2], literal=v.get("literal"), ref=v.get("ref"),
                                fired_at=_dt(r[4])))
    return Thesis(thesis_id=tid, holding_id=holding_id, created_at=_dt(row[1]), intrinsic_value=row[2],
                  buy_below_price=row[3], target_price=row[4], basis=row[5] or "", reasons=reasons, triggers=triggers)


_HCOLS = "holding_id, ticker, account, currency, created_at, snapshot_analysis_id, snapshot_json, closed"


def _holding(conn, r) -> Holding:
    txns = [Transaction(txn_id=t[0], holding_id=r[0], txn_date=t[1], side=t[2], shares=t[3], price=t[4], fees=t[5],
                        note=t[6] or "")
            for t in conn.execute("SELECT txn_id, txn_date, side, shares, price, fees, note FROM transactions "
                                  "WHERE holding_id = ? ORDER BY txn_date, txn_id", (r[0],))]
    return Holding(holding_id=r[0], ticker=r[1], account=r[2], currency=r[3], created_at=_dt(r[4]),
                   snapshot_analysis_id=r[5], snapshot=pm.load(json.loads(r[6]) if r[6] else {}), closed=bool(r[7]),
                   transactions=txns, thesis=_thesis(conn, r[0]))


def get_holding(holding_id: int, path: Path | str | None = None) -> Holding | None:
    with connect(_db(path)) as conn:
        r = conn.execute(f"SELECT {_HCOLS} FROM holdings WHERE holding_id = ?", (holding_id,)).fetchone()
        return _holding(conn, r) if r else None


def list_holdings(include_closed: bool = False, ticker: str | None = None,
                  path: Path | str | None = None) -> list[Holding]:
    q, args = f"SELECT {_HCOLS} FROM holdings WHERE 1=1", []
    if not include_closed:
        q += " AND closed = 0"
    if ticker:
        q, args = q + " AND ticker = ?", [ticker.upper()]
    with connect(_db(path)) as conn:
        return [_holding(conn, r) for r in conn.execute(q + " ORDER BY ticker, account", args).fetchall()]


# --------------------------------------------------------------------------
# Thesis: levels, reasons, triggers
# --------------------------------------------------------------------------
def update_levels(thesis_id: int, intrinsic_value: float | None, buy_below_price: float | None,
                  target_price: float | None, path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute("UPDATE theses SET intrinsic_value = ?, buy_below_price = ?, target_price = ? WHERE thesis_id = ?",
                     (intrinsic_value, buy_below_price, target_price, thesis_id))


def add_trigger(thesis_id: int, t: Trigger, path: Path | str | None = None) -> int:
    t = validate_trigger(t)
    with connect(_db(path)) as conn:
        return _insert_trigger(conn, thesis_id, t)


def delete_trigger(trigger_id: int, path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute("DELETE FROM thesis_triggers WHERE trigger_id = ?", (trigger_id,))


def mark_trigger_fired(trigger_id: int, when: datetime | None = None, path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute("UPDATE thesis_triggers SET fired_at = ? WHERE trigger_id = ?", (_now(when), trigger_id))


def set_reason(reason_id: int, still_holds: bool, holding_id: int | None = None,
               path: Path | str | None = None) -> None:
    """Tick (or untick) "still holds?" on a reason; the change is journaled."""
    with connect(_db(path)) as conn:
        row = conn.execute("SELECT text, still_holds FROM thesis_reasons WHERE reason_id = ?", (reason_id,)).fetchone()
        if not row or (row[1] is not None and bool(row[1]) == still_holds):
            return
        conn.execute("UPDATE thesis_reasons SET still_holds = ?, reviewed_at = ? WHERE reason_id = ?",
                     (int(still_holds), _now(), reason_id))
    if holding_id is not None:
        add_journal(holding_id, f"Reason \"{row[0]}\": {'still holds' if still_holds else 'no longer holds'}",
                    kind="reason", path=path)


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------
def add_journal(holding_id: int, text: str, kind: str = "note", alert_id: int | None = None,
                when: datetime | None = None, path: Path | str | None = None) -> int:
    if not text.strip():
        raise ValueError("empty journal entry")
    with connect(_db(path)) as conn:
        cur = conn.execute("INSERT INTO journal_entries (holding_id, created_at, kind, text, alert_id) VALUES (?,?,?,?,?)",
                           (holding_id, _now(when), kind, text.strip(), alert_id))
        return int(cur.lastrowid)


def journal(holding_id: int, path: Path | str | None = None) -> list[JournalEntry]:
    with connect(_db(path)) as conn:
        rows = conn.execute("SELECT entry_id, holding_id, created_at, kind, text, alert_id FROM journal_entries "
                            "WHERE holding_id = ? ORDER BY created_at DESC, entry_id DESC", (holding_id,)).fetchall()
    return [JournalEntry(entry_id=r[0], holding_id=r[1], created_at=_dt(r[2]), kind=r[3], text=r[4], alert_id=r[5])
            for r in rows]


# --------------------------------------------------------------------------
# Watchlist price levels
# --------------------------------------------------------------------------
def set_watch_level(ticker: str, buy_below_price: float | None, target_price: float | None, basis: str = "",
                    path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute("INSERT OR REPLACE INTO watch_levels VALUES (?,?,?,?,?)",
                     (ticker.upper(), buy_below_price, target_price, basis, _now()))


def watch_levels(path: Path | str | None = None) -> dict[str, WatchLevel]:
    with connect(_db(path)) as conn:
        rows = conn.execute("SELECT ticker, buy_below_price, target_price, basis, updated_at FROM watch_levels").fetchall()
    return {r[0]: WatchLevel(ticker=r[0], buy_below_price=r[1], target_price=r[2], basis=r[3] or "",
                             updated_at=_dt(r[4])) for r in rows}


# --------------------------------------------------------------------------
# Monitor state (baselines and active conditions for fire-once alerts)
# --------------------------------------------------------------------------
def get_state(ticker: str, key: str, default: Any = None, path: Path | str | None = None) -> Any:
    with connect(_db(path)) as conn:
        row = conn.execute("SELECT value_json FROM monitor_state WHERE ticker = ? AND key = ?",
                           (ticker.upper(), key)).fetchone()
    return json.loads(row[0]) if row else default


def set_state(ticker: str, key: str, value: Any, path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute("INSERT OR REPLACE INTO monitor_state VALUES (?,?,?,?)",
                     (ticker.upper(), key, json.dumps(value, default=str), _now()))


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------
_ACOLS = "alert_id, ticker, holding_id, kind, event_key, message, created_at, source, read_at, email_status"


def insert_alert(a: Alert, path: Path | str | None = None) -> int | None:
    """Store an alert unless the same (ticker, kind, event) was already alerted; returns its id or None."""
    with connect(_db(path)) as conn:
        cur = conn.execute("INSERT OR IGNORE INTO alerts (ticker, holding_id, kind, event_key, message, created_at, "
                           "source) VALUES (?,?,?,?,?,?,?)",
                           (a.ticker.upper(), a.holding_id, a.kind, a.event_key, a.message,
                            _now(a.created_at), a.source))
        return int(cur.lastrowid) if cur.rowcount else None


def _alert(r) -> Alert:
    return Alert(**dict(zip([c.strip() for c in _ACOLS.split(",")], r)))


def list_alerts(unread_only: bool = False, ticker: str | None = None, limit: int | None = None,
                path: Path | str | None = None) -> list[Alert]:
    q, args = f"SELECT {_ACOLS} FROM alerts WHERE 1=1", []
    if unread_only:
        q += " AND read_at IS NULL"
    if ticker:
        q, args = q + " AND ticker = ?", [ticker.upper()]
    q += " ORDER BY created_at DESC, alert_id DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    with connect(_db(path)) as conn:
        return [_alert(r) for r in conn.execute(q, args).fetchall()]


def unread_count(ticker: str | None = None, path: Path | str | None = None) -> int:
    q, args = "SELECT COUNT(*) FROM alerts WHERE read_at IS NULL", []
    if ticker:
        q, args = q + " AND ticker = ?", [ticker.upper()]
    with connect(_db(path)) as conn:
        return int(conn.execute(q, args).fetchone()[0])


def mark_read(alert_ids: list[int] | None = None, path: Path | str | None = None) -> None:
    """Mark the given alerts read, or every unread alert when `alert_ids` is None."""
    with connect(_db(path)) as conn:
        if alert_ids is None:
            conn.execute("UPDATE alerts SET read_at = ? WHERE read_at IS NULL", (_now(),))
        else:
            conn.executemany("UPDATE alerts SET read_at = ? WHERE alert_id = ?", [(_now(), i) for i in alert_ids])


def set_email_status(alert_ids: list[int], status: str, path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.executemany("UPDATE alerts SET email_status = ? WHERE alert_id = ?", [(status, i) for i in alert_ids])


# --------------------------------------------------------------------------
# Alert-check log
# --------------------------------------------------------------------------
class CheckRow(dict):
    """One alert_checks row as a dict with attribute access."""

    __getattr__ = dict.get


_CCOLS = ["check_id", "started_at", "ended_at", "source", "status", "tickers", "fired", "errors", "email_status",
          "pid", "log_path"]


def start_check(source: str, pid: int | None = None, log_path: str | None = None,
                path: Path | str | None = None) -> int:
    with connect(_db(path)) as conn:
        cur = conn.execute("INSERT INTO alert_checks (started_at, source, status, pid, log_path) VALUES (?,?,?,?,?)",
                           (_now(), source, RUNNING, pid, log_path))
        return int(cur.lastrowid)


def finish_check(check_id: int, status: str, tickers: int = 0, fired: int = 0, errors: dict | None = None,
                 email_status: str = "", path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute("UPDATE alert_checks SET ended_at = ?, status = ?, tickers = ?, fired = ?, errors = ?, "
                     "email_status = ? WHERE check_id = ?",
                     (_now(), status, tickers, fired, json.dumps(errors or {}), email_status, check_id))


def set_check_pid(check_id: int, pid: int, path: Path | str | None = None) -> None:
    with connect(_db(path)) as conn:
        conn.execute("UPDATE alert_checks SET pid = ? WHERE check_id = ?", (pid, check_id))


def latest_check(path: Path | str | None = None) -> CheckRow | None:
    with connect(_db(path)) as conn:
        r = conn.execute(f"SELECT {', '.join(_CCOLS)} FROM alert_checks ORDER BY check_id DESC LIMIT 1").fetchone()
    if not r:
        return None
    row = CheckRow(zip(_CCOLS, r))
    row["errors"] = json.loads(row["errors"]) if row["errors"] else {}
    row["started_at"], row["ended_at"] = _dt(row["started_at"]), _dt(row["ended_at"])
    return row
