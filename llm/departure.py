"""LLM confirmation of 6-K leadership keyword hits (SPEC "Leadership-turnover
flag", layer 2). Only filings that passed the keyword pre-filter reach this.
The filing text goes inside a <filing_text> data block; the answer is JSON
{departure, role, person, effective_date}, cached by filing accession number."""

from __future__ import annotations

import threading
from datetime import date

import config
from data.leadership import FilingDoc, LLMConfirmation
from llm import prompts
from llm.client import LLMClient
from llm.schemas import DepartureResponse

LENS = "departure"
_default: LLMClient | None = None
_default_lock = threading.Lock()


def confirm_departure(doc: FilingDoc, ticker: str, llm: LLMClient) -> LLMConfirmation:
    res = llm.run(ticker=ticker, lens=LENS, prompt_version=prompts.PROMPT_VERSIONS[LENS],
                  system=prompts.DEPARTURE_SYSTEM,
                  user=prompts.departure_user(ticker, doc.form, doc.filing_date.isoformat(), doc.text),
                  schema=DepartureResponse, cache_key=f"accession:{doc.accession}")
    if not res.ok:
        return LLMConfirmation(status="unconfirmed", note=res.status)
    d = DepartureResponse.model_validate(res.data)
    if not (d.departure and d.role in config.LEADERSHIP_ROLE_TERMS):
        return LLMConfirmation(status="rejected", departure=False, role=d.role, person=d.person,
                               note=d.explanation)
    try:
        effective = date.fromisoformat(d.effective_date) if d.effective_date else None
    except ValueError:
        effective = None
    return LLMConfirmation(status="confirmed", departure=True, role=d.role, person=d.person,
                           effective_date=effective, note=d.explanation)


def default_confirm(doc: FilingDoc, ticker: str) -> LLMConfirmation:
    """The confirm used when none is passed: the configured backend (API key, else Claude Code),
    or "unconfirmed" when neither is available."""
    global _default
    with _default_lock:
        llm = _default if _default is not None else LLMClient()
        if llm.configured:
            _default = llm
    if not llm.configured:
        return LLMConfirmation(status="unconfirmed", note="LLM not configured (no API key and no Claude Code CLI)")
    return confirm_departure(doc, ticker, llm)


def confirm_with(llm: LLMClient):
    """A confirm callable bound to a specific client (e.g. one that logs to an analysis run)."""
    return lambda doc, ticker: confirm_departure(doc, ticker, llm)
