"""SEC EDGAR client: CIK lookup, filing lists, filing documents.

Every request sends SEC_USER_AGENT and respects EDGAR_MAX_REQUESTS_PER_SECOND.
EDGAR only. SEDAR+ and SEDI must never be accessed programmatically (their
terms prohibit it); Canadian-only filings come in through the manual CSVs.
"""

from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path
from typing import Any

import requests
from pydantic import BaseModel

import config
from data.cache import DiskCache
from data.provider import ProviderError
from data.throttle import EDGAR_LIMITER, RateLimiter

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"

# Tickers with an exchange suffix are never looked up by stripping it: the
# bare symbol can belong to a different US company (CNR.TO is Canadian
# National, but "CNR" on EDGAR is another issuer). Use the overrides CSV.
FOREIGN_SUFFIX = re.compile(r"\.[A-Z]{1,3}$")
DOMESTIC_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}


class Filing(BaseModel):
    cik: int
    form: str
    filing_date: date
    accession: str
    primary_document: str
    items: str = ""
    description: str = ""

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")


def html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&#160;|&nbsp;|&#xa0;", " ", text)
    text = (text.replace("&#8220;", '"').replace("&#8221;", '"').replace("&#8217;", "'")
            .replace("&#8211;", "-").replace("&#8212;", "-").replace("&amp;", "&"))
    text = re.sub(r"&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_cik_overrides(path: Path = config.SEC_CIK_OVERRIDES_CSV) -> dict[str, int]:
    if not Path(path).exists():
        return {}
    with open(path, newline="") as f:
        return {r["ticker"].strip().upper(): int(r["cik"]) for r in csv.DictReader(f)
                if r.get("ticker") and r.get("cik")}


class EdgarClient:
    def __init__(self, user_agent: str | None = None, session: Any = None,
                 limiter: RateLimiter = EDGAR_LIMITER, cache: DiskCache | None = None,
                 overrides_path: Path = config.SEC_CIK_OVERRIDES_CSV):
        self.user_agent = user_agent if user_agent is not None else config.SEC_USER_AGENT
        self.session = session or requests.Session()
        self.limiter = limiter
        self.cache = cache
        self.overrides_path = overrides_path

    # -- transport --------------------------------------------------------
    def _get(self, url: str) -> requests.Response:
        if not self.user_agent.strip():
            raise ProviderError("SEC_USER_AGENT is not set; EDGAR requires a name and contact email")
        self.limiter.wait()
        try:
            r = self.session.get(url, headers={"User-Agent": self.user_agent}, timeout=30)
            r.raise_for_status()
            return r
        except Exception as exc:
            raise ProviderError(f"EDGAR request failed for {url}: {exc}") from exc

    def _json(self, url: str, kind: str = "prices") -> Any:
        if self.cache is None:
            return self._get(url).json()
        return self.cache.fetch(f"edgar|{url}", kind, lambda: self._get(url).json())

    def get_text(self, url: str) -> str:
        """A filing document (immutable once filed, so cached under the long rule)."""
        if self.cache is None:
            return self._get(url).text
        return self.cache.fetch(f"edgar|{url}", "fundamentals", lambda: self._get(url).text)

    # -- lookups ----------------------------------------------------------
    def ticker_map(self) -> dict[str, int]:
        data = self._json(TICKER_MAP_URL)
        return {v["ticker"].upper(): int(v["cik_str"]) for v in data.values()}

    def lookup_cik(self, ticker: str) -> int | None:
        t = ticker.upper()
        overrides = load_cik_overrides(self.overrides_path)
        if t in overrides:
            return overrides[t]
        if FOREIGN_SUFFIX.search(t):
            return None
        m = self.ticker_map()
        return m.get(t) or m.get(t.replace("-", "."))

    def submissions(self, cik: int) -> dict:
        return self._json(SUBMISSIONS_URL.format(cik=cik))

    def filings(self, cik: int, forms: set[str], since: date | None = None) -> list[Filing]:
        """Filings of the given forms, newest first, including older pages when needed."""
        sub = self.submissions(cik)
        out = _rows_to_filings(cik, sub.get("filings", {}).get("recent", {}), forms, since)
        for page in sub.get("filings", {}).get("files", []):
            if since and page.get("filingTo") and date.fromisoformat(page["filingTo"]) < since:
                continue
            data = self._json(SUBMISSIONS_PAGE_URL.format(name=page["name"]))
            out.extend(_rows_to_filings(cik, data, forms, since))
        return sorted(out, key=lambda f: f.filing_date, reverse=True)

    def forms_filed(self, cik: int) -> set[str]:
        return set(self.submissions(cik).get("filings", {}).get("recent", {}).get("form", []))

    def is_domestic_filer(self, cik: int) -> bool:
        """Files 10-K/10-Q (8-K Item 5.02 and Form 4 apply)."""
        return bool(self.forms_filed(cik) & DOMESTIC_FORMS)

    def files_6k(self, cik: int) -> bool:
        return bool(self.forms_filed(cik) & {"6-K", "6-K/A"})

    # -- documents --------------------------------------------------------
    def document_url(self, f: Filing, doc: str | None = None) -> str:
        doc = doc or f.primary_document
        if f.form in ("4", "4/A"):
            doc = doc.split("/")[-1]  # strip the "xslF345X06/" rendering prefix → raw XML
        return ARCHIVE_URL.format(cik=f.cik, acc=f.accession_nodash, doc=doc)

    def primary_text(self, f: Filing) -> str:
        return self.get_text(self.document_url(f))

    def exhibit_texts(self, f: Filing, limit: int = 3) -> list[tuple[str, str]]:
        """(file name, text) of the filing's other .htm/.txt documents (6-K press releases live here)."""
        try:
            idx = self._json(INDEX_URL.format(cik=f.cik, acc=f.accession_nodash), kind="fundamentals")
        except ProviderError:
            return []
        names = [i["name"] for i in idx.get("directory", {}).get("item", []) if is_exhibit_document(i["name"], f)]
        out = []
        for name in names[:limit]:
            try:
                out.append((name, html_to_text(self.get_text(self.document_url(f, name)))))
            except ProviderError:
                continue
        return out


def is_exhibit_document(name: str, f: Filing) -> bool:
    """An exhibit worth reading: an .htm/.txt document other than the primary one,
    excluding index pages and the complete-submission file (<accession>.txt),
    which bundles every document including encoded binaries."""
    low = name.lower()
    return (low.endswith((".htm", ".html", ".txt")) and name != f.primary_document
            and "index" not in low and not low.startswith(f.accession.lower()))


def _rows_to_filings(cik: int, rows: dict, forms: set[str], since: date | None) -> list[Filing]:
    out = []
    n = len(rows.get("form", []))
    for i in range(n):
        form = rows["form"][i]
        if form not in forms:
            continue
        fd = date.fromisoformat(rows["filingDate"][i])
        if since and fd < since:
            continue
        out.append(Filing(
            cik=cik, form=form, filing_date=fd, accession=rows["accessionNumber"][i],
            primary_document=rows["primaryDocument"][i],
            items=(rows.get("items") or [""] * n)[i] or "",
            description=(rows.get("primaryDocDescription") or [""] * n)[i] or "",
        ))
    return out
