"""Capture offline fixtures for the golden tickers, the canaries and helpers.

Saves the raw yfinance objects (so tests run the real field-map code path)
plus trimmed EDGAR responses into tests/fixtures/<TICKER>/. Run once (live):

    python scripts/capture_fixtures.py [TICKER ...] [--edgar-only]
    python scripts/capture_fixtures.py [TICKER ...] --facts-only   # SEC XBRL company facts only

Tests assert branches, not live numbers, so fixtures don't need refreshing
unless a test needs newer data.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

import config  # noqa: E402
from data import field_map as fm  # noqa: E402
from data.edgar import EdgarClient, Filing, is_exhibit_document  # noqa: E402
from data.fixture_provider import ticker_dir  # noqa: E402
from data.leadership import lookback_start  # noqa: E402

# ABX.TO: a TSX company reporting in USD (currency conversion); USDCAD=X its FX pair;
# ^TNX the US risk-free source; ^GSPTSE the Canadian turnaround benchmark.
EXTRA_FULL = ["ABX.TO"]
PRICE_ONLY = ["USDCAD=X", "^TNX", "^GSPTSE"]
PRICE_COLUMNS = ("Close", "Adj Close", "High", "Low")
MAX_FORM4 = 20
MAX_6K = 10
MAX_DOMESTIC_MARKERS = 2


def _save_frame(df: pd.DataFrame | None, path: Path) -> None:
    if df is not None and not df.empty:
        df.to_csv(path)


def _save_series(s: pd.Series | None, path: Path, name: str) -> None:
    if s is not None and len(s):
        s.rename(name).to_csv(path)


def capture_yf(ticker: str, full: bool = True) -> None:
    import yfinance as yf

    d = ticker_dir(ticker)
    d.mkdir(parents=True, exist_ok=True)
    t = yf.Ticker(ticker)
    hist = t.history(period=config.PRICE_HISTORY_PERIOD if ticker != "HTZ" else "max", auto_adjust=False)
    _save_frame(hist[[c for c in PRICE_COLUMNS if c in hist.columns]], d / "prices.csv")
    meta = {"captured_on": date.today().isoformat(), "yfinance_version": yf.__version__, "ticker": ticker}
    if full:
        (d / "info.json").write_text(json.dumps(t.info, default=str, indent=1))
        for (kind, freq), attr in fm.YF_STATEMENT_ATTRS.items():
            time.sleep(config.YF_MIN_SECONDS_BETWEEN_CALLS)
            _save_frame(getattr(t, attr), d / f"{kind}_{freq}.csv")
        _save_series(t.splits, d / "splits.csv", "Stock Splits")
        _save_series(t.dividends, d / "dividends.csv", "Dividends")
        try:
            _save_series(t.get_shares_full(start="2015-01-01"), d / "shares_full.csv", "Shares")
        except Exception as exc:
            print(f"  {ticker}: shares_full failed: {exc}")
        dates, cal = [], []
        try:
            ed = t.earnings_dates
            if ed is not None:
                dates = sorted({pd.Timestamp(i).date().isoformat() for i in ed.index})
        except Exception as exc:
            print(f"  {ticker}: earnings_dates failed: {exc}")
        try:
            cal = [x.isoformat() for x in (t.calendar or {}).get("Earnings Date", [])]
        except Exception as exc:
            print(f"  {ticker}: calendar failed: {exc}")
        (d / "earnings_dates.json").write_text(json.dumps({"dates": dates, "calendar": cal}, indent=1))
        est = d / "estimates"
        est.mkdir(exist_ok=True)
        for fs in fm.fields_for("estimates"):
            try:
                _save_frame(getattr(t, fs.aliases[0]), est / f"{fs.canonical}.csv")
            except Exception as exc:
                print(f"  {ticker}: {fs.canonical} unavailable: {exc}")
    (d / "meta.json").write_text(json.dumps(meta, indent=1))


class Recorder:
    """Wraps an EdgarClient session, saving every response under a ticker's edgar/ dir."""

    def __init__(self, client: EdgarClient):
        self.client = client
        self.saved: dict[str, dict[str, str]] = {}

    def save(self, ticker_key: str, url: str, content: bytes | str) -> None:
        d = ticker_dir(ticker_key) / "edgar"
        d.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha1(url.encode()).hexdigest()[:16] + ("." + url.rsplit(".", 1)[-1][:4] if "." in url[-6:] else "")
        (d / name).write_bytes(content.encode() if isinstance(content, str) else content)
        self.saved.setdefault(ticker_key, {})[url] = name

    def flush(self) -> None:
        for key, mapping in self.saved.items():
            p = ticker_dir(key) / "edgar" / "index.json"
            existing = json.loads(p.read_text()) if p.exists() else {}
            existing.update(mapping)
            p.write_text(json.dumps(existing, indent=1))


def _trim_recent(recent: dict, keep: set[str]) -> dict:
    idx = [i for i, acc in enumerate(recent["accessionNumber"]) if acc in keep]
    return {k: [v[i] for i in idx] for k, v in recent.items() if isinstance(v, list)}


def capture_edgar(tickers: list[str], rec: Recorder) -> None:
    from data.edgar import SUBMISSIONS_URL, TICKER_MAP_URL

    client = rec.client
    full_map = client._get(TICKER_MAP_URL).json()
    today = date.today()
    lead_start = lookback_start(today)
    ins_start = (pd.Timestamp(today) - pd.DateOffset(months=config.INSIDER_LOOKBACK_MONTHS)).date()
    trimmed_map = {}
    for t in tickers:
        cik = client.lookup_cik(t)
        if cik is None:
            print(f"  {t}: no CIK (non-SEC filer); skipping EDGAR")
            continue
        for k, v in full_map.items():
            if int(v["cik_str"]) == cik:
                trimmed_map[k] = v
        sub = client._get(SUBMISSIONS_URL.format(cik=cik)).json()
        recent = sub["filings"]["recent"]
        filings = [Filing(cik=cik, form=recent["form"][i], filing_date=date.fromisoformat(recent["filingDate"][i]),
                          accession=recent["accessionNumber"][i], primary_document=recent["primaryDocument"][i],
                          items=recent["items"][i] or "", description=recent["primaryDocDescription"][i] or "")
                   for i in range(len(recent["form"]))]
        keep: set[str] = set()
        markers = [f for f in filings if f.form in ("10-K", "10-Q")][:MAX_DOMESTIC_MARKERS]
        keep.update(f.accession for f in markers)
        for f in [f for f in filings if f.form in ("8-K", "8-K/A") and "5.02" in f.items
                  and f.filing_date >= lead_start]:
            rec.save(t, client.document_url(f), client._get(client.document_url(f)).content)
            keep.add(f.accession)
        for f in [f for f in filings if f.form in ("4", "4/A") and f.filing_date >= ins_start][:MAX_FORM4]:
            rec.save(t, client.document_url(f), client._get(client.document_url(f)).content)
            keep.add(f.accession)
        for f in [f for f in filings if f.form in ("6-K", "6-K/A") and f.filing_date >= lead_start][:MAX_6K]:
            rec.save(t, client.document_url(f), client._get(client.document_url(f)).content)
            from data.edgar import INDEX_URL

            idx_url = INDEX_URL.format(cik=f.cik, acc=f.accession_nodash)
            idx = client._get(idx_url)
            rec.save(t, idx_url, idx.content)
            for item in idx.json().get("directory", {}).get("item", []):
                n = item["name"]
                if is_exhibit_document(n, f):
                    u = client.document_url(f, n)
                    rec.save(t, u, client._get(u).content)
            keep.add(f.accession)
        sub["filings"] = {"recent": _trim_recent(recent, keep), "files": []}
        rec.save(t, SUBMISSIONS_URL.format(cik=cik), json.dumps(sub))
        print(f"  {t}: CIK {cik}, kept {len(keep)} filings")
    existing_idx = ticker_dir("_SEC") / "edgar" / "index.json"
    if existing_idx.exists():  # merge with tickers captured earlier
        name = json.loads(existing_idx.read_text()).get(TICKER_MAP_URL)
        if name:
            trimmed_map = {**json.loads((ticker_dir("_SEC") / "edgar" / name).read_text()), **trimmed_map}
    rec.save("_SEC", TICKER_MAP_URL, json.dumps(trimmed_map))


def capture_facts(tickers: list[str], rec: Recorder) -> None:
    """SEC XBRL company facts, trimmed to the concepts in data/xbrl.XBRL_TAGS (the full files are MBs)."""
    from data.xbrl import FACTS_URL, XBRL_TAGS

    client = rec.client
    wanted = {pair for aliases in XBRL_TAGS.values() for pair in aliases}
    for t in tickers:
        cik = client.lookup_cik(t)
        if cik is None:
            print(f"  {t}: no CIK; no XBRL facts")
            continue
        data = client._get(FACTS_URL.format(cik=cik)).json()
        data["facts"] = {tax: {c: v for c, v in concepts.items() if (tax, c) in wanted}
                         for tax, concepts in (data.get("facts") or {}).items()}
        rec.save(t, FACTS_URL.format(cik=cik), json.dumps(data))
        print(f"  {t}: CIK {cik}, {sum(len(v) for v in data['facts'].values())} XBRL concepts kept")


def main(argv: list[str]) -> int:
    if "--facts-only" in argv:
        rec = Recorder(EdgarClient())
        capture_facts([a for a in argv if not a.startswith("--")] or list(config.GOLDEN_TICKERS), rec)
        rec.flush()
        return 0
    edgar_only = "--edgar-only" in argv
    argv = [a for a in argv if not a.startswith("--")]
    full = argv or [*config.GOLDEN_TICKERS, *config.CANARY_TICKERS, *EXTRA_FULL]
    for t in [] if edgar_only else full:
        print(f"yfinance: {t}")
        capture_yf(t, full=True)
    if not argv and not edgar_only:
        for t in PRICE_ONLY:
            print(f"yfinance (prices only): {t}")
            capture_yf(t, full=False)
    rec = Recorder(EdgarClient())
    capture_edgar([t for t in full if t in config.GOLDEN_TICKERS or t in config.CANARY_TICKERS], rec)
    capture_facts([t for t in full if t in config.GOLDEN_TICKERS], rec)
    rec.flush()
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
