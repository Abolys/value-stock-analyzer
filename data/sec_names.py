"""Matching non-US listings (e.g. TSX tickers ending .TO) to the SEC filer of the same company, by
name, so cross-listed companies get their EDGAR data (XBRL history, 6-K leadership check).

A TSX ticker and its US ticker differ (CNR.TO is CNI on the NYSE), so the SEC's own ticker map
can't be used directly. Names are normalised (case, punctuation, legal suffixes, share-class words,
SEC state tags like "/CAN/") and a match is kept only when it points to exactly one SEC filer:
- "exact": the normalised names are equal;
- "prefix": the listing's (often truncated) name is the start of exactly one filer's name.
SEC names ending "/ADR" are depositary-bank registrations (Form F-6) for unsponsored ADRs, not the
company, and never file financials, so they are skipped. Matches are written to data/sec_cik_overrides.csv for review by scripts/map_sec_ciks.py; rows
written by hand are never touched.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

TICKER_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
STOP_WORDS = {"INC", "CORP", "CORPORATION", "LTD", "LIMITED", "CO", "COMPANY", "THE", "PLC", "TRUST", "LP", "L P",
              "HOLDINGS", "HOLDING", "GROUP", "INCORPORATED", "SUBORDINATE", "SUB", "VOTING", "VOT", "CLASS",
              "SHARES", "NON", "A", "B", "UNITS", "UNIT", "CAN", "NEW"}
MIN_PREFIX_CHARS = 6  # a shorter normalised name is too generic to prefix-match safely
AUTO_NOTE = "auto:"  # marks rows written by the matcher (hand-written rows never start with it)


class SecMatch(BaseModel):
    ticker: str
    listing_name: str
    cik: int
    sec_name: str
    sec_tickers: list[str]
    kind: str  # exact | prefix

    @property
    def note(self) -> str:
        return (f"{AUTO_NOTE} {self.kind} name match: '{self.listing_name}' = SEC '{self.sec_name}' "
                f"({', '.join(self.sec_tickers[:3])}); review")


def normalise(name: str) -> str:
    n = re.sub(r"/[A-Z ]+/?", " ", name.upper()).replace("&", " AND ")
    n = re.sub(r"[^A-Z0-9 ]", " ", n)
    return " ".join(w for w in n.split() if w not in STOP_WORDS)


def match_names(listings: list[tuple[str, str]], sec_rows: list[dict]) -> tuple[list[SecMatch], list[tuple[str, str]]]:
    """(matches, unmatched) for (ticker, name) listings against SEC rows {cik, name, ticker, exchange}."""
    by: dict[str, dict[int, dict]] = {}
    for r in sec_rows:
        if "/ADR" in r["name"].upper():  # depositary registrations for unsponsored ADRs: no financials filed
            continue
        by.setdefault(normalise(r["name"]), {}).setdefault(int(r["cik"]), {"name": r["name"], "tickers": []})[
            "tickers"].append(r["ticker"])
    keys = list(by)
    matches, unmatched = [], []
    for ticker, name in listings:
        n = normalise(name)
        kind, found = "exact", by.get(n, {})
        if len(found) != 1 and len(n) >= MIN_PREFIX_CHARS:
            kind, found = "prefix", {}
            for k in keys:
                if k.startswith(n + " "):
                    found.update(by[k])
        if len(found) == 1:
            cik, info = next(iter(found.items()))
            matches.append(SecMatch(ticker=ticker, listing_name=name, cik=cik, sec_name=info["name"],
                                    sec_tickers=info["tickers"], kind=kind))
        else:
            unmatched.append((ticker, name))
    return matches, unmatched
