"""Insider transactions: SEC Form 4 for US filers, /data/insider_events.csv for
everyone else (TSX-only names, whose filings are on SEDI — never automated).

Coverage labels: "Form 4, full history", "manual", or
"N/A - no insider data source" when neither applies.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

import config
from data.edgar import EdgarClient
from data.form4 import InsiderTransaction, parse_form4
from data.provider import ProviderError

COVERAGE_FORM4 = "Form 4, full history"
COVERAGE_MANUAL = "manual"
COVERAGE_NONE = "N/A - no insider data source"
MANUAL_COLUMNS = ["ticker", "date", "insider", "role", "type", "shares", "price", "source"]


class InsiderData(BaseModel):
    ticker: str
    transactions: list[InsiderTransaction] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    ignored_codes: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)

    @property
    def coverage_label(self) -> str:
        return "; ".join(self.coverage) if self.coverage else COVERAGE_NONE


def _num(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def load_manual_insider_events(ticker: str, path: Path = config.INSIDER_EVENTS_CSV) -> list[InsiderTransaction]:
    if not Path(path).exists():
        return []
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("ticker") or "").strip().upper() != ticker.upper():
                continue
            shares, price = _num(r.get("shares")), _num(r.get("price"))
            out.append(InsiderTransaction(
                ticker=ticker, date=date.fromisoformat(r["date"].strip()), insider=r.get("insider", ""),
                role=r.get("role", ""), type=(r.get("type") or "").strip().lower(), shares=shares, price=price,
                value=shares * price if shares is not None and price is not None else None,
                source=r.get("source") or COVERAGE_MANUAL,
            ))
    return out


def collect_insider_data(ticker: str, edgar: EdgarClient | None, since: date,
                         manual_path: Path = config.INSIDER_EVENTS_CSV) -> InsiderData:
    data = InsiderData(ticker=ticker)
    if edgar is not None:
        try:
            cik = edgar.lookup_cik(ticker)
            if cik is not None and edgar.is_domestic_filer(cik):
                data.coverage.append(COVERAGE_FORM4)
                ignored: dict[str, int] = {}
                for f in edgar.filings(cik, {"4", "4/A"}, since=since):
                    try:
                        res = parse_form4(edgar.get_text(edgar.document_url(f)), ticker=ticker, accession=f.accession)
                    except ProviderError as exc:
                        data.errors.append(f"{f.accession}: {exc}")
                        continue
                    data.transactions.extend(t for t in res.transactions if t.date >= since)
                    for k, v in res.ignored_codes.items():
                        ignored[k] = ignored.get(k, 0) + v
                data.ignored_codes = ignored
        except ProviderError as exc:
            data.errors.append(str(exc))
    manual = [t for t in load_manual_insider_events(ticker, manual_path) if t.date >= since]
    if manual:
        data.coverage.append(COVERAGE_MANUAL)
        data.transactions.extend(manual)
    data.transactions.sort(key=lambda t: t.date, reverse=True)
    return data
