"""SEC Form 4 parser.

Counts only open-market purchases (code P) and sales (code S) from the
non-derivative table; grants (A), option exercises (M), tax withholding (F),
gifts (G) and every other code are ignored (and tallied, for transparency).
A sale is marked 10b5-1 when the filing's `aff10b5One` box is checked or a
footnote attached to the transaction mentions a 10b5-1 plan.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date

from lxml import etree
from pydantic import BaseModel, Field

KEPT_CODES = {"P": "buy", "S": "sell"}
RULE_10B5_1 = re.compile(r"10b5[\s\-‐‑–]?1", re.IGNORECASE)


class InsiderTransaction(BaseModel):
    ticker: str
    date: date
    insider: str
    role: str
    type: str  # "buy" | "sell"
    shares: float | None
    price: float | None
    value: float | None
    is_10b5_1: bool = False
    source: str = "Form 4"
    accession: str | None = None


class Form4Result(BaseModel):
    transactions: list[InsiderTransaction] = Field(default_factory=list)
    ignored_codes: dict[str, int] = Field(default_factory=dict)


def _text(el, path: str) -> str | None:
    found = el.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _float(s: str | None) -> float | None:
    try:
        return float(s) if s is not None else None
    except ValueError:
        return None


def _role(owner) -> str:
    rel = owner.find("reportingOwnerRelationship")
    if rel is None:
        return "unknown"
    parts = []
    title = _text(rel, "officerTitle")
    if title:
        parts.append(title)
    if (_text(rel, "isDirector") or "0").lower() in ("1", "true"):
        parts.append("Director")
    if (_text(rel, "isTenPercentOwner") or "0").lower() in ("1", "true"):
        parts.append("10% owner")
    if not parts and (_text(rel, "isOfficer") or "0").lower() in ("1", "true"):
        parts.append("Officer")
    return ", ".join(parts) or "Other"


def parse_form4(xml: str | bytes, ticker: str | None = None, accession: str | None = None) -> Form4Result:
    root = etree.fromstring(xml.encode() if isinstance(xml, str) else xml,
                            parser=etree.XMLParser(recover=True))
    symbol = ticker or _text(root, "issuer/issuerTradingSymbol") or ""
    owners = root.findall("reportingOwner")
    insider = " / ".join(filter(None, (_text(o, "reportingOwnerId/rptOwnerName") for o in owners))) or "unknown"
    role = " / ".join(_role(o) for o in owners) or "unknown"
    doc_10b5 = (_text(root, "aff10b5One") or "0").lower() in ("1", "true")
    footnotes = {fn.get("id"): "".join(fn.itertext()) for fn in root.findall("footnotes/footnote")}

    result = Form4Result()
    ignored: Counter[str] = Counter()
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(tx, "transactionCoding/transactionCode") or "?"
        if code not in KEPT_CODES:
            ignored[code] += 1
            continue
        d = _text(tx, "transactionDate/value")
        shares = _float(_text(tx, "transactionAmounts/transactionShares/value"))
        price = _float(_text(tx, "transactionAmounts/transactionPricePerShare/value"))
        refs = {f.get("id") for f in tx.iter("footnoteId")}
        fn_10b5 = any(RULE_10B5_1.search(footnotes.get(r, "") or "") for r in refs)
        kind = KEPT_CODES[code]
        result.transactions.append(InsiderTransaction(
            ticker=symbol, date=date.fromisoformat(d[:10]) if d else date.min, insider=insider, role=role,
            type=kind, shares=shares, price=price,
            value=shares * price if shares is not None and price is not None else None,
            is_10b5_1=kind == "sell" and (doc_10b5 or fn_10b5), accession=accession,
        ))
    for tx in root.findall("derivativeTable/derivativeTransaction"):
        ignored[_text(tx, "transactionCoding/transactionCode") or "?"] += 1
    result.ignored_codes = dict(ignored)
    return result
