"""Screener universe lists (SPEC "Screener universe").

One CSV per list in /data/universe/ with columns ticker, name, source, as_of.
ETF holdings files are parsed by locating the header row by column-name
aliases, so iShares CSVs, Pacer CSV/XLSX downloads and manually saved files
all work. Non-equity lines (cash, futures, money market, FX) are dropped and
tickers are normalised to yfinance format (TSX → ".TO").

Hand-maintained lists (watchlist, dataroma) are never downloaded. Dataroma
has no API and must never be scraped.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import pandas as pd
from pydantic import BaseModel

import config

COLUMNS = ["ticker", "name", "source", "as_of"]
TICKER_ALIASES = ["ticker", "stockticker", "symbol", "issuer ticker", "ticker symbol"]
NAME_ALIASES = ["name", "securityname", "security name", "description", "holding", "company"]
ASSET_CLASS_ALIASES = ["asset class", "assetclass", "security type", "type", "sectype"]
MONEY_MARKET_FLAG_ALIASES = ["moneymarketflag"]
EQUITY_ASSET_CLASSES = {"equity", "common stock", "stock", "equities"}
# Used only when a file has no asset-class column. Deliberately narrow, so that
# companies like "FirstCash Holdings" or "Treasury Wine" are not dropped.
NON_EQUITY_PATTERN = re.compile(
    r"^(cash|us dollar|canadian dollar)\b|money market|govt oblig|treasury (bill|obligation|sl agency)"
    r"|\bfutures?\b|\b(emini|e-mini)\b|cash collateral|\bmargin\b",
    re.IGNORECASE)
VALID_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}(-[A-Z0-9]{1,3})?$")
NON_TICKERS = {"", "-", "--", "CASH", "USD", "CAD", "N/A", "NA"}


class RefreshOutcome(BaseModel):
    key: str
    status: str  # "updated" | "raw file" | "stale" | "missing"
    as_of: str | None = None
    count: int = 0
    message: str = ""


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def normalise_ticker(raw: str, exchange: str) -> str | None:
    t = (raw or "").strip().upper()
    if t in NON_TICKERS:
        return None
    t = re.sub(r"[./ ]+", "-", t).strip("-")
    if not VALID_TICKER.match(t):
        return None
    if exchange == "TSX":
        return f"{t}.TO"
    return t


def _find_col(cols: list[str], aliases: list[str]) -> str | None:
    low = {c.strip().lower(): c for c in cols}
    for a in aliases:
        if a in low:
            return low[a]
    return None


def _as_of_from_preamble(lines: list[str]) -> str | None:
    for line in lines:
        m = re.search(r"as of[^A-Za-z0-9]*\"?([A-Za-z]{3,9}\.? \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})",
                      line, re.IGNORECASE)
        if m:
            try:
                return pd.Timestamp(m.group(1)).date().isoformat()
            except ValueError:
                continue
    return None


def _table_from_rows(rows: list[list[str]]) -> tuple[pd.DataFrame, list[str]]:
    for i, row in enumerate(rows):
        cells = [str(c).strip().lower() for c in row]
        if any(a in cells for a in TICKER_ALIASES) and any(a in cells for a in NAME_ALIASES):
            header = [str(c).strip() for c in row]
            body = [r + [""] * (len(header) - len(r)) for r in rows[i + 1:] if any(str(c).strip() for c in r)]
            return pd.DataFrame([r[: len(header)] for r in body], columns=header), [",".join(map(str, r)) for r in rows[:i]]
    raise ValueError("holdings header row not found (format changed?)")


def read_holdings_table(content: bytes, filename: str = "") -> tuple[pd.DataFrame, str | None]:
    """The raw holdings table and the as-of date found in any preamble."""
    if filename.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(content), header=None, dtype=str).fillna("")
        rows = df.values.tolist()
    else:
        text = content.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
    table, preamble = _table_from_rows(rows)
    return table, _as_of_from_preamble(preamble)


def parse_holdings(content: bytes, label: str, exchange: str, filename: str = "",
                   as_of: str | None = None) -> pd.DataFrame:
    """Equity holdings as a universe frame (ticker, name, source, as_of)."""
    table, found_as_of = read_holdings_table(content, filename)
    as_of = as_of or found_as_of or date.today().isoformat()
    tcol = _find_col(list(table.columns), TICKER_ALIASES)
    ncol = _find_col(list(table.columns), NAME_ALIASES)
    acol = _find_col(list(table.columns), ASSET_CLASS_ALIASES)
    mmcol = _find_col(list(table.columns), MONEY_MARKET_FLAG_ALIASES)
    out = []
    for _, r in table.iterrows():
        name = str(r[ncol]).strip()
        if acol and str(r[acol]).strip():
            if str(r[acol]).strip().lower() not in EQUITY_ASSET_CLASSES:
                continue
        elif NON_EQUITY_PATTERN.search(name):
            continue
        if mmcol and str(r[mmcol]).strip().upper() in ("Y", "YES", "TRUE", "1"):
            continue
        t = normalise_ticker(str(r[tcol]), exchange)
        if t is None:
            continue
        out.append({"ticker": t, "name": name, "source": label, "as_of": as_of})
    df = pd.DataFrame(out, columns=COLUMNS).drop_duplicates("ticker")
    if df.empty:
        raise ValueError("no equity holdings parsed (format changed?)")
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------
# Lists on disk
# --------------------------------------------------------------------------
def list_path(key: str, directory: Path = config.UNIVERSE_DIR) -> Path:
    return Path(directory) / f"{key}.csv"


def load_list(key: str, directory: Path = config.UNIVERSE_DIR) -> pd.DataFrame:
    p = list_path(key, directory)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype=str, keep_default_na=False)


def list_labels() -> dict[str, str]:
    labels = {k: v["label"] for k, v in config.UNIVERSE_SOURCES.items()}
    labels.update(config.MANUAL_UNIVERSE_LISTS)
    return labels


def merge_lists(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Remove duplicate tickers but keep every source ("COWZ, Dataroma")."""
    merged: dict[str, dict] = {}
    for df in frames:
        for _, r in df.iterrows():
            t = r["ticker"]
            if t not in merged:
                merged[t] = {"ticker": t, "name": r.get("name", ""), "sources": [], "as_of": []}
            for src in str(r.get("source", "")).split(", "):
                if src and src not in merged[t]["sources"]:
                    merged[t]["sources"].append(src)
            if r.get("as_of") and r["as_of"] not in merged[t]["as_of"]:
                merged[t]["as_of"].append(r["as_of"])
    rows = [{"ticker": m["ticker"], "name": m["name"], "source": ", ".join(m["sources"]),
             "as_of": ", ".join(m["as_of"])} for m in merged.values()]
    return pd.DataFrame(rows, columns=COLUMNS)


def load_universe(keys: list[str], directory: Path = config.UNIVERSE_DIR) -> pd.DataFrame:
    return merge_lists([load_list(k, directory) for k in keys])


# --------------------------------------------------------------------------
# Refresh (used by scripts/refresh_universe.py)
# --------------------------------------------------------------------------
def _raw_file(key: str, raw_dir: Path) -> Path | None:
    for ext in (".csv", ".xlsx", ".xls"):
        p = Path(raw_dir) / f"{key}{ext}"
        if p.exists():
            return p
    return None


def _default_get(url: str) -> bytes:
    import requests

    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (value-stock-analyzer)"}, timeout=60)
    r.raise_for_status()
    return r.content


def refresh_list(key: str, http_get: Callable[[str], bytes] = _default_get,
                 directory: Path = config.UNIVERSE_DIR, raw_dir: Path = config.UNIVERSE_RAW_DIR) -> RefreshOutcome:
    src = config.UNIVERSE_SOURCES[key]
    label, exchange = src["label"], src["exchange"]
    errors = []
    try:
        df = parse_holdings(http_get(src["url"]), label, exchange, filename=src["url"].split("?")[0])
        df.to_csv(list_path(key, directory), index=False)
        return RefreshOutcome(key=key, status="updated", as_of=df["as_of"].iloc[0], count=len(df),
                              message=f"{label}: {len(df)} tickers downloaded")
    except Exception as exc:
        errors.append(f"download failed: {exc}")
    raw = _raw_file(key, raw_dir)
    if raw is not None:
        try:
            mtime = datetime.fromtimestamp(raw.stat().st_mtime).date().isoformat()
            df = parse_holdings(raw.read_bytes(), label, exchange, filename=raw.name)
            if df["as_of"].iloc[0] == date.today().isoformat():
                df["as_of"] = mtime  # no date inside the file: use when it was saved
            df.to_csv(list_path(key, directory), index=False)
            return RefreshOutcome(key=key, status="raw file", as_of=df["as_of"].iloc[0], count=len(df),
                                  message=f"{label}: {len(df)} tickers from manual file {raw.name} ({'; '.join(errors)})")
        except Exception as exc:
            errors.append(f"manual file {raw.name} unreadable: {exc}")
    existing = load_list(key, directory)
    if not existing.empty:
        as_of = existing["as_of"].iloc[0]
        return RefreshOutcome(key=key, status="stale", as_of=as_of, count=len(existing),
                              message=f"STALE: {label} kept previous list, stale since {as_of} ({'; '.join(errors)})")
    return RefreshOutcome(key=key, status="missing", count=0,
                          message=f"MISSING: {label} has no list yet; download the holdings file by hand into "
                                  f"{raw_dir}/{key}.csv or .xlsx ({'; '.join(errors)})")


def ensure_manual_templates(directory: Path = config.UNIVERSE_DIR) -> None:
    for key in config.MANUAL_UNIVERSE_LISTS:
        p = list_path(key, directory)
        if not p.exists():
            p.write_text(",".join(COLUMNS) + "\n")
