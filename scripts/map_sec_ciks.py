"""Map non-US universe tickers (e.g. .TO) to the SEC filer of the same company, by name.

    python scripts/map_sec_ciks.py            # write matches into data/sec_cik_overrides.csv
    python scripts/map_sec_ciks.py --dry-run  # only print them

Cross-listed Canadian companies file with the SEC under their US ticker (CNR.TO → CNI), so their
SEC XBRL history (valuation-based recovery, debt maturities) and 6-K leadership check need this
mapping. Matched rows are marked "auto:" in the note; hand-written rows are never changed, and a
ticker you delete a row for will be re-added on the next run unless you add it back by hand with
a different CIK (or a note without "auto:"). Unmatched names are listed so you can add the ones
that do file with the SEC by hand (e.g. names spelled differently on each exchange).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from data.edgar import FOREIGN_SUFFIX, EdgarClient  # noqa: E402
from data.sec_names import AUTO_NOTE, TICKER_EXCHANGE_URL, match_names  # noqa: E402
from data.universe import list_labels, load_list  # noqa: E402

FIELDS = ["ticker", "cik", "note"]


def listings() -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for key in list_labels():
        df = load_list(key)
        for t, n in zip(df["ticker"], df["name"]):
            if isinstance(t, str) and FOREIGN_SUFFIX.search(t.upper()) and t.upper() not in seen:
                seen[t.upper()] = n or ""
    return sorted(seen.items())


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    path = config.SEC_CIK_OVERRIDES_CSV
    existing = list(csv.DictReader(open(path, newline=""))) if path.exists() else []
    manual = {r["ticker"].upper(): r for r in existing if not (r.get("note") or "").startswith(AUTO_NOTE)}
    data = EdgarClient()._get(TICKER_EXCHANGE_URL).json()
    sec_rows = [dict(zip(data["fields"], r)) for r in data["data"]]
    todo = [(t, n) for t, n in listings() if t not in manual]
    matches, unmatched = match_names(todo, sec_rows)
    for m in matches:
        print(f"{m.ticker:10} → CIK {m.cik:<8} {m.kind:6} {m.listing_name!r} = {m.sec_name!r} ({', '.join(m.sec_tickers[:3])})")
    print(f"\n{len(matches)} matched ({sum(m.kind == 'prefix' for m in matches)} by prefix: check those first); "
          f"{len(manual)} hand-written rows kept; {len(unmatched)} unmatched (not SEC filers, or named differently):")
    print("  " + "; ".join(f"{t} {n}" for t, n in unmatched))
    if dry:
        return 0
    rows = list(manual.values()) + [{"ticker": m.ticker, "cik": m.cik, "note": m.note} for m in matches]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(({k: r.get(k, "") for k in FIELDS} for r in rows), key=lambda r: r["ticker"]))
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
