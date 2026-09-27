"""SEC Form N-PORT holdings parser (ETF holdings from EDGAR).

Used for COWZ and CALF, whose issuer site blocks automated downloads. N-PORT
holdings become public about 60 days after the reporting period, so the list's
as-of date is the report period, not the filing date.

Only long common-equity holdings (assetCat EC, payoffProfile Long) with a
ticker are kept; everything else is returned in `skipped` with the reason so
it can be reported, never silently dropped.
"""

from __future__ import annotations

from datetime import date

from lxml import etree
from pydantic import BaseModel, Field


class NportHolding(BaseModel):
    ticker: str  # as filed; normalise with data.universe.normalise_ticker
    name: str
    cusip: str | None = None
    pct_of_fund: float | None = None


class NportResult(BaseModel):
    series_id: str | None = None
    report_period: date | None = None
    holdings: list[NportHolding] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)  # "<name>: <reason>"


def _local(el) -> str:
    return etree.QName(el).localname


def _child(el, name: str):
    for c in el:
        if isinstance(c.tag, str) and _local(c) == name:
            return c
    return None


def _text(el, name: str) -> str | None:
    c = _child(el, name)
    return c.text.strip() if c is not None and c.text and c.text.strip() else None


def _first(root, name: str):
    for el in root.iter():
        if isinstance(el.tag, str) and _local(el) == name:
            return el
    return None


def parse_nport(xml: str | bytes) -> NportResult:
    root = etree.fromstring(xml.encode() if isinstance(xml, str) else xml, parser=etree.XMLParser(recover=True))
    result = NportResult()
    sid = _first(root, "seriesId")
    result.series_id = sid.text.strip() if sid is not None and sid.text else None
    rep = _first(root, "repPdDate")
    if rep is not None and rep.text:
        result.report_period = date.fromisoformat(rep.text.strip()[:10])
    for inv in (el for el in root.iter() if isinstance(el.tag, str) and _local(el) == "invstOrSec"):
        name = _text(inv, "name") or _text(inv, "title") or "(unnamed)"
        asset = _text(inv, "assetCat")
        payoff = _text(inv, "payoffProfile")
        if asset != "EC":
            result.skipped.append(f"{name}: not common equity (assetCat {asset or 'missing'})")
            continue
        if payoff and payoff != "Long":
            result.skipped.append(f"{name}: {payoff} position")
            continue
        ids = _child(inv, "identifiers")
        tk = _child(ids, "ticker") if ids is not None else None
        ticker = (tk.get("value") or "").strip() if tk is not None else ""
        if not ticker:
            result.skipped.append(f"{name}: no ticker in the filing")
            continue
        pct = _text(inv, "pctVal")
        result.holdings.append(NportHolding(ticker=ticker, name=name, cusip=_text(inv, "cusip"),
                                            pct_of_fund=float(pct) if pct else None))
    return result
