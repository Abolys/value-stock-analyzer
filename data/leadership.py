"""Leadership-turnover flag from four layered sources (SPEC "Data sources").

1. 8-K Item 5.02 (US domestic filers) — "8-K, full history"
2. 6-K filings: keyword pre-filter, then an LLM confirmation — "6-K, keyword + LLM check"
3. Officer snapshots (yfinance companyOfficers) — "officer tracking since <date>"
4. Manual events in /data/leadership_events.csv — "manual"

Events are merged and deduplicated by person (or role) and month. Coverage
shorter than LEADERSHIP_LOOKBACK_MONTHS is reported as "partial coverage" and
never as a clean "no turnover".

SEDAR+ is never accessed programmatically; Canadian filings reach the app only
through the manual CSV.
"""

from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from typing import Callable

import pandas as pd
from pydantic import BaseModel, Field

import config
from data.edgar import EdgarClient, Filing, html_to_text
from data.provider import ProviderError
from storage import db

LAYER_8K = "8-K, full history"
LAYER_6K = "6-K, keyword + LLM check"
LAYER_OFFICERS = "officer tracking since {d}"
LAYER_MANUAL = "manual"
NO_SOURCE = "N/A - no leadership data source"
MANUAL_COLUMNS = ["ticker", "date", "role", "person", "note", "source"]


class LeadershipEvent(BaseModel):
    ticker: str
    date: date
    role: str  # "CEO" | "CFO"
    person: str | None = None
    layer: str
    detail: str = ""
    accession: str | None = None
    sources: list[str] = Field(default_factory=list)


class LLMConfirmation(BaseModel):
    status: str  # "confirmed" | "rejected" | "unconfirmed"
    departure: bool | None = None
    role: str | None = None
    person: str | None = None
    effective_date: date | None = None


class FilingDoc(BaseModel):
    """A filing's text, already fetched (lets the parsers run offline)."""

    form: str
    filing_date: date
    accession: str
    text: str
    exhibit_descriptions: list[str] = Field(default_factory=list)


class LayerResult(BaseModel):
    name: str  # coverage label
    coverage_start: date | None  # None → coverage window unknown (manual)
    coverage_end: date
    events: list[LeadershipEvent] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)  # unconfirmed 6-K keyword hits
    systematic: bool = True


class LeadershipResult(BaseModel):
    ticker: str
    flag: str  # "none" | "flagged" | "high" | NO_SOURCE
    departures: int
    events: list[LeadershipEvent] = Field(default_factory=list)
    layers_used: list[str] = Field(default_factory=list)
    coverage_start: date | None = None
    coverage_end: date | None = None
    partial_coverage: bool = False
    coverage_label: str = ""
    unconfirmed_candidates: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.flag == NO_SOURCE:
            return NO_SOURCE
        if self.departures == 0:
            base = "No departures found"
        else:
            base = f"{self.departures} departure{'s' if self.departures != 1 else ''}"
        return f"{base} · {self.coverage_label}"


def lookback_start(today: date) -> date:
    return (pd.Timestamp(today) - pd.DateOffset(months=config.LEADERSHIP_LOOKBACK_MONTHS)).date()


# --------------------------------------------------------------------------
# Layer 1: 8-K Item 5.02
# --------------------------------------------------------------------------
ITEM_502_CAPTION = re.compile(
    r"Departure of Directors or (Certain|Principal) Officers;.*?Compensatory Arrangements of Certain Officers\.?",
    re.IGNORECASE | re.DOTALL)
NEXT_ITEM = re.compile(r"\bItem\s+\d\.\d\d\b|\bSIGNATURES?\b", re.IGNORECASE)
PERSON_HONORIFIC = re.compile(r"\b(?:Mr|Ms|Mrs|Dr)\.?\s+((?:[A-Z][\w'\-]+\.?\s+){0,2}[A-Z][\w'\-]+)")
# Dots after honorifics and middle initials would split a name across sentences.
HONORIFIC_DOT = re.compile(r"\b(Mr|Ms|Mrs|Dr|[A-Z])\.(?=\s+[A-Z])")
COMPANY_WORDS = ("the company", "the firm", "the corporation", "the registrant", "the bank", "the issuer")
PERSON_BEFORE_VERB = re.compile(
    r"\b([A-Z][a-z'\-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-z'\-]+){1,2})(?:,\s[^,]{1,80},)?\s+(?:has\s+)?"
    r"(?:notified|informed|announced|resigned|will resign|retired|will retire|will step down|stepped down|decided)")


def _item_502_section(text: str) -> str:
    m = re.search(r"\bItem\s+5\.02\b", text, re.IGNORECASE)
    if not m:
        return ""
    rest = text[m.end():]
    rest = ITEM_502_CAPTION.sub(" ", rest, count=1)
    nxt = NEXT_ITEM.search(rest)
    return rest[: nxt.start()] if nxt else rest


def _has_term(sentence_lower: str, term: str) -> bool:
    return re.search(rf"(?<![a-z]){re.escape(term)}", sentence_lower) is not None


def _company_level(after: str, company_name: str | None) -> bool:
    """False for divisional titles such as "CEO of CCB" or "Co-CEOs of the Commercial Bank".

    A role followed by "of <X>" is company-level only when X is the company itself
    ("the Company", "the Firm", or the issuer's own name).
    """
    m = re.match(r"s?\s+of\s+(.{0,60})", after)
    if not m:
        return True
    target = m.group(1)
    if target.startswith(COMPANY_WORDS):
        return True
    first_word = (company_name or "").lower().split()[:1]
    return bool(first_word) and target.startswith(first_word[0])


def _roles_in(sentence_lower: str, company_name: str | None = None) -> list[str]:
    roles = []
    for role, terms in config.LEADERSHIP_ROLE_TERMS.items():
        for t in terms:
            for m in re.finditer(rf"(?<![a-z]){re.escape(t)}(?![a-rt-z])", sentence_lower):
                if _company_level(sentence_lower[m.end():], company_name):
                    roles.append(role)
                    break
            if role in roles:
                break
    return roles


def _person_in(sentence: str) -> str | None:
    m = PERSON_HONORIFIC.search(sentence) or PERSON_BEFORE_VERB.search(sentence)
    return " ".join(m.group(1).split()) if m else None


def parse_8k_item_502(doc: FilingDoc, ticker: str, company_name: str | None = None) -> list[LeadershipEvent]:
    """CEO/CFO departures in an 8-K's Item 5.02 section.

    A departure needs a role term and a departure term in the same sentence;
    the standard Item caption (which itself says "Departure of ... Officers")
    is removed first, and divisional titles ("CEO of CCB") are not counted.
    """
    text = html_to_text(doc.text) if "<" in doc.text else doc.text
    section = HONORIFIC_DOT.sub(r"\1", _item_502_section(text))  # keep "Mr. X" / "Jane Q. Smith" together
    events: list[LeadershipEvent] = []
    seen: set[str] = set()
    for sentence in re.split(r"(?<=[.;])\s+(?=[A-Z])", section):
        low = sentence.lower()
        if not any(_has_term(low, t) for t in config.DEPARTURE_TERMS):
            continue
        for role in _roles_in(low, company_name):
            if role in seen:
                continue
            seen.add(role)
            events.append(LeadershipEvent(ticker=ticker, date=doc.filing_date, role=role,
                                          person=_person_in(sentence), layer=LAYER_8K,
                                          detail=sentence.strip()[:300], accession=doc.accession))
    return events


def layer_8k(ticker: str, docs: list[FilingDoc], start: date, today: date,
             company_name: str | None = None) -> LayerResult:
    events = []
    for d in docs:
        if d.filing_date >= start:
            events.extend(parse_8k_item_502(d, ticker, company_name))
    return LayerResult(name=LAYER_8K, coverage_start=start, coverage_end=today, events=events)


# --------------------------------------------------------------------------
# Layer 2: 6-K keyword pre-filter + LLM confirmation
# --------------------------------------------------------------------------
def _kw_found(text: str, kw: str) -> bool:
    flags = 0 if kw.isupper() else re.IGNORECASE  # "CEO"/"CFO" must be upper case
    return re.search(rf"(?<![A-Za-z]){re.escape(kw)}", text, flags) is not None


def keyword_hits(text: str, descriptions: list[str] | None = None) -> list[str]:
    """Keywords found; empty unless at least one role AND one action keyword occur."""
    blob = " ".join([text, *(descriptions or [])])
    roles = [k for k in config.LEADERSHIP_KEYWORDS["role"] if _kw_found(blob, k)]
    actions = [k for k in config.LEADERSHIP_KEYWORDS["action"] if _kw_found(blob, k)]
    return roles + actions if roles and actions else []


# ============================ PHASE 3 STUB ================================
def confirm_departure_llm(doc: FilingDoc, ticker: str) -> LLMConfirmation:
    """PHASE 3 STUB — the LLM confirmation of a 6-K keyword hit.

    Phase 3 replaces this with an Anthropic call returning JSON
    {departure, role, person, effective_date}, cached by accession number,
    with the filing text inside a delimited <filing_text> data block.
    Until then every keyword hit stays "unconfirmed" and is never counted.
    """
    return LLMConfirmation(status="unconfirmed")
# ==========================================================================


def layer_6k(ticker: str, docs: list[FilingDoc], start: date, today: date,
             confirm: Callable[[FilingDoc, str], LLMConfirmation] = confirm_departure_llm) -> LayerResult:
    layer = LayerResult(name=LAYER_6K, coverage_start=start, coverage_end=today)
    for d in docs:
        if d.filing_date < start:
            continue
        text = html_to_text(d.text) if "<" in d.text else d.text
        hits = keyword_hits(text, d.exhibit_descriptions)
        if not hits:
            continue
        c = confirm(d.model_copy(update={"text": text}), ticker)
        if c.status == "confirmed" and c.departure and c.role in config.LEADERSHIP_ROLE_TERMS:
            layer.events.append(LeadershipEvent(
                ticker=ticker, date=c.effective_date or d.filing_date, role=c.role, person=c.person,
                layer=LAYER_6K, detail=f"6-K keywords: {', '.join(hits)}", accession=d.accession))
        elif c.status == "unconfirmed":
            layer.candidates.append({"accession": d.accession, "filing_date": d.filing_date.isoformat(),
                                     "keywords": hits, "status": "unconfirmed"})
    return layer


# --------------------------------------------------------------------------
# Layer 3: officer snapshots
# --------------------------------------------------------------------------
def officer_change_events(snapshots: list[db.OfficerSnapshot]) -> list[LeadershipEvent]:
    """A CEO/CFO name that disappears between consecutive snapshots is a departure,
    dated to the later snapshot."""
    events = []
    snaps = sorted(snapshots, key=lambda s: s.snapshot_date)
    for prev, cur in zip(snaps, snaps[1:]):
        for role, attr in (("CEO", "ceo"), ("CFO", "cfo")):
            before, after = set(getattr(prev, attr)), set(getattr(cur, attr))
            if not before or not after:
                continue  # a role missing from one snapshot is a data gap, not a departure
            for person in sorted(before - after):
                events.append(LeadershipEvent(
                    ticker=cur.ticker, date=cur.snapshot_date, role=role, person=person,
                    layer="officer snapshots",
                    detail=f"{role} changed between {prev.snapshot_date} and {cur.snapshot_date}: "
                           f"{', '.join(sorted(before))} → {', '.join(sorted(after))}"))
    return events


def layer_officers(snapshots: list[db.OfficerSnapshot], today: date) -> LayerResult | None:
    if not snapshots:
        return None
    first = min(s.snapshot_date for s in snapshots)
    return LayerResult(name=LAYER_OFFICERS.format(d=first.isoformat()), coverage_start=first,
                       coverage_end=today, events=officer_change_events(snapshots))


# --------------------------------------------------------------------------
# Layer 4: manual CSV
# --------------------------------------------------------------------------
def load_manual_events(ticker: str, path: Path = config.LEADERSHIP_EVENTS_CSV) -> list[LeadershipEvent]:
    if not Path(path).exists():
        return []
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("ticker") or "").strip().upper() != ticker.upper():
                continue
            out.append(LeadershipEvent(
                ticker=ticker, date=date.fromisoformat(r["date"].strip()), role=(r.get("role") or "").strip().upper(),
                person=(r.get("person") or "").strip() or None, layer=LAYER_MANUAL,
                detail=" · ".join(filter(None, [r.get("note"), r.get("source")]))))
    return out


def layer_manual(ticker: str, today: date, path: Path = config.LEADERSHIP_EVENTS_CSV) -> LayerResult | None:
    events = load_manual_events(ticker, path)
    if not events:
        return None
    return LayerResult(name=LAYER_MANUAL, coverage_start=None, coverage_end=today, events=events, systematic=False)


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------
def _dedupe_key(e: LeadershipEvent) -> tuple:
    who = (e.person or "").lower()
    who = re.sub(r"\b(mr|ms|mrs|dr)\.?\s+", "", who).strip()
    return (who or e.role, e.date.strftime("%Y-%m"))


def merge_leadership(ticker: str, layers: list[LayerResult], today: date,
                     errors: list[str] | None = None) -> LeadershipResult:
    start = lookback_start(today)
    if not layers:
        return LeadershipResult(ticker=ticker, flag=NO_SOURCE, departures=0, coverage_label=NO_SOURCE,
                                partial_coverage=True, errors=errors or [])
    merged: dict[tuple, LeadershipEvent] = {}
    for layer in layers:
        for e in layer.events:
            if e.date < start or e.date > today:
                continue
            k = _dedupe_key(e)
            if k in merged:
                if layer.name not in merged[k].sources:
                    merged[k].sources.append(layer.name)
            else:
                merged[k] = e.model_copy(update={"sources": [layer.name]})
    events = sorted(merged.values(), key=lambda e: e.date, reverse=True)
    systematic = [l for l in layers if l.systematic and l.coverage_start is not None]
    cov_start = min((l.coverage_start for l in systematic), default=None)
    partial = cov_start is None or cov_start > start
    label = "; ".join(l.name for l in layers)
    if partial:
        label += " · partial coverage"
    n = len(events)
    flag = "high" if n >= config.LEADERSHIP_HIGH_COUNT else "flagged" if n >= 1 else "none"
    return LeadershipResult(
        ticker=ticker, flag=flag, departures=n, events=events, layers_used=[l.name for l in layers],
        coverage_start=max(cov_start, start) if cov_start else None, coverage_end=today,
        partial_coverage=partial, coverage_label=label,
        unconfirmed_candidates=[c for l in layers for c in l.candidates], errors=errors or [])


def leadership_flag(ticker: str, today: date | None = None, edgar: EdgarClient | None = None,
                    db_path: Path | str = config.RUNS_DB_PATH, manual_path: Path = config.LEADERSHIP_EVENTS_CSV,
                    confirm: Callable[[FilingDoc, str], LLMConfirmation] = confirm_departure_llm) -> LeadershipResult:
    """Evaluate every layer that applies to the ticker and merge the results."""
    today = today or date.today()
    start = lookback_start(today)
    layers: list[LayerResult] = []
    errors: list[str] = []
    if edgar is not None:
        try:
            cik = edgar.lookup_cik(ticker)
            if cik is not None:
                if edgar.is_domestic_filer(cik):
                    docs = [_doc(edgar, f) for f in edgar.filings(cik, {"8-K", "8-K/A"}, since=start)
                            if "5.02" in f.items]
                    name = edgar.submissions(cik).get("name")
                    layers.append(layer_8k(ticker, [d for d in docs if d], start, today, name))
                if edgar.files_6k(cik):
                    docs = [_doc(edgar, f, exhibits=True) for f in edgar.filings(cik, {"6-K", "6-K/A"}, since=start)]
                    layers.append(layer_6k(ticker, [d for d in docs if d], start, today, confirm))
        except ProviderError as exc:
            errors.append(f"EDGAR: {exc}")
    officers = layer_officers(db.get_officer_snapshots(ticker, db_path), today)
    if officers:
        layers.append(officers)
    manual = layer_manual(ticker, today, manual_path)
    if manual:
        layers.append(manual)
    return merge_leadership(ticker, layers, today, errors)


def _doc(edgar: EdgarClient, f: Filing, exhibits: bool = False) -> FilingDoc | None:
    try:
        text = html_to_text(edgar.primary_text(f))
    except ProviderError:
        return None
    descriptions = [f.description] if f.description else []
    if exhibits:
        for name, ex_text in edgar.exhibit_texts(f):
            descriptions.append(name)
            text += " " + ex_text
    return FilingDoc(form=f.form, filing_date=f.filing_date, accession=f.accession, text=text,
                     exhibit_descriptions=descriptions)
