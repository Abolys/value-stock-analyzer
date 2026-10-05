"""The `info` fallback: rebuild a ticker's company info from other sources when Yahoo refuses `info`
requests (common on shared cloud hosts, while its price and statement requests still work).

Sources, in order, each value labelled with where it came from (`InfoResult.sources`):

- Yahoo chart data (`get_quote_profile`): trading currency, exchange, quote type, name, shares,
  market cap.
- The quarterly and annual statements: trailing EPS and the other stage-1 money fields (TTM or
  latest quarter, Rule 3b), book value per share, the latest fiscal year and quarter ends.
- Dividend history: trailing-twelve-month dividend rate and the latest ex-dividend date.
- SEC EDGAR (US filers and the TSX names mapped in sec_cik_overrides.csv): SIC code → sector and
  industry (SIC_SECTOR_RULES), reporting currency from the XBRL units, company name.
- Finnhub's free tier (optional, FINNHUB_API_KEY): sector and industry (FINNHUB_INDUSTRY_SECTORS)
  where SEC has none, reporting currency, country, name.

What none of them has (business summary, officers, ownership, short interest, the next earnings date)
stays N/A with the reason (Rule 2). The result's provider is FALLBACK_PROVIDER, so every consumer can
say the info is rebuilt. A reporting currency no source gives is assumed equal to the trading currency
and labelled as such: without it the statements would be used unconverted anyway, silently.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable

import config
from data import field_map as fm
from data import periods
from data.provider import FALLBACK_PROVIDER, DataProvider, InfoResult, ProviderError, Statement
from data.values import OK

log = logging.getLogger(__name__)

UNAVAILABLE = "N/A - Yahoo info unavailable; no fallback source has {what}"

# Money fields rebuilt from the statements: info canonical → (statement canonical, flow?).
STATEMENT_FIELDS = {
    "trailing_eps": ("diluted_eps", True),
    "info_free_cashflow": ("free_cash_flow", True),
    "info_ebitda": ("ebitda", True),
    "info_total_revenue": ("total_revenue", True),
    "info_total_debt": ("total_debt", False),
    "info_total_cash": ("cash_and_short_term_investments", False),
}
CASH_FALLBACK = "cash_and_equivalents"  # when the statements carry no cash + short-term investments row


def sector_from_sic(sic: Any, description: str | None) -> tuple[str, str | None] | None:
    """(yfinance sector, industry) for an SEC SIC code, the SIC description standing in for the industry
    when SIC_SECTOR_RULES names none; None for a code no rule covers."""
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return None
    for lo, hi, sector, industry in config.SIC_SECTOR_RULES:
        if lo <= code <= hi:
            return sector, industry or (description.title() if description else None)
    return None


def sector_from_finnhub(industry: str | None) -> tuple[str, str | None] | None:
    if not industry:
        return None
    hit = config.FINNHUB_INDUSTRY_SECTORS.get(industry)
    if hit is None:
        log.warning("Unmapped Finnhub industry %r; add it to FINNHUB_INDUSTRY_SECTORS", industry)
        return None
    sector, mapped = hit
    return sector, mapped or industry


def _epoch(d: date) -> int:
    return int(datetime.combine(d, time(), tzinfo=timezone.utc).timestamp())


class InfoFallback:
    """Builds a fallback InfoResult. `provider` serves statements, dividends and (when it has one)
    `get_quote_profile`; `edgar` and `finnhub` are optional clients."""

    def __init__(self, provider: DataProvider, edgar: Any = None, finnhub: Any = None,
                 today: Callable[[], date] = date.today):
        self.provider = provider
        self.edgar = edgar
        self.finnhub = finnhub
        self.today = today

    def build(self, ticker: str, why: str = "") -> InfoResult:
        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        statuses: dict[str, str] = {}
        notes: list[str] = []

        def put(canonical: str, value: Any, source: str) -> None:
            if value is None or canonical in values:
                return
            values[canonical] = value
            statuses[canonical] = OK
            sources[canonical] = source

        # 1. Yahoo chart data
        profile_fn = getattr(self.provider, "get_quote_profile", None)
        if profile_fn is not None:
            try:
                for k, v in profile_fn(ticker).items():
                    put(k, v, "Yahoo chart data")
            except ProviderError as exc:
                notes.append(f"Yahoo chart data: {exc}")
        is_fund = values.get("quote_type") in config.FUND_QUOTE_TYPES

        # 2. SEC EDGAR, 3. Finnhub
        if not is_fund:
            self._edgar(ticker, put, notes)
            self._finnhub(ticker, put, notes)

        trade_ccy = values.get("currency")
        if trade_ccy and "financial_currency" not in values:
            put("financial_currency", trade_ccy, "assumed = trading currency (no source gives the reporting currency)")

        # 4. Statements (reporting currency, like Yahoo's own info fields)
        if not is_fund:
            self._statements(ticker, put, notes)

        # 5. Dividends (trading currency): only where no conversion applies
        self._dividends(ticker, put, notes, values)

        if not values:
            raise ProviderError(f"no fallback source has data for {ticker}" + (f" ({why})" if why else ""))
        for fs in fm.fields_for("info"):
            if fs.canonical not in values:
                statuses[fs.canonical] = UNAVAILABLE.format(what=fs.canonical.replace("_", " "))
        raw = {"_fallback": True, "_why": why, "_notes": notes}
        return InfoResult(ticker=ticker, values=values, statuses=statuses, raw=raw, provider=FALLBACK_PROVIDER,
                          sources=sources)

    # -- sources ---------------------------------------------------------------------------------
    def _edgar(self, ticker: str, put, notes: list[str]) -> None:
        if self.edgar is None:
            return
        try:
            cik = self.edgar.lookup_cik(ticker)
        except ProviderError as exc:
            notes.append(f"SEC EDGAR: {exc}")
            return
        if cik is None:
            notes.append("SEC EDGAR: not an SEC filer")
            return
        try:
            sub = self.edgar.submissions(cik)
        except ProviderError as exc:
            notes.append(f"SEC EDGAR: {exc}")
            return
        sic, desc = sub.get("sic"), sub.get("sicDescription")
        mapped = sector_from_sic(sic, desc)
        if mapped is not None:
            label = f"SEC EDGAR SIC {sic}" + (f" ({desc})" if desc else "")
            put("sector", mapped[0], label)
            put("industry", mapped[1], label)
        put("long_name", sub.get("name"), "SEC EDGAR")
        try:
            facts = self.edgar.company_facts(cik)
        except ProviderError as exc:
            notes.append(f"SEC XBRL: {exc}")
            return
        for canonical in ("net_income", "equity"):
            c = facts.concepts.get(canonical)
            if c is not None and c.unit and c.unit != "shares" and "/" not in c.unit:
                put("financial_currency", c.unit, f"SEC XBRL units ({c.tag})")
                break

    def _finnhub(self, ticker: str, put, notes: list[str]) -> None:
        if self.finnhub is None:
            return
        try:
            prof = self.finnhub.profile(ticker)
        except ProviderError as exc:
            notes.append(f"Finnhub: {exc}")
            return
        mapped = sector_from_finnhub(prof.get("finnhubIndustry"))
        if mapped is not None:
            label = f"Finnhub ({prof.get('finnhubIndustry')})"
            put("sector", mapped[0], label)
            put("industry", mapped[1], label)
        put("financial_currency", prof.get("currency") or None, "Finnhub")
        put("country", prof.get("country") or None, "Finnhub")
        put("long_name", prof.get("name") or None, "Finnhub")

    def _statements(self, ticker: str, put, notes: list[str]) -> None:
        stmts: dict[tuple[str, str], Statement | None] = {}

        def stmt(kind: str, freq: str) -> Statement | None:
            if (kind, freq) not in stmts:
                try:
                    stmts[(kind, freq)] = self.provider.get_statement(ticker, kind, freq)
                except ProviderError as exc:
                    notes.append(f"{freq} {kind} statement: {exc}")
                    stmts[(kind, freq)] = None
            return stmts[(kind, freq)]

        def datum(canonical: str, flow: bool):
            src = fm.spec(canonical).source
            q, a = stmt(src, "quarterly"), stmt(src, "annual")
            return periods.ttm(q, a, canonical) if flow else periods.latest_balance(q, a, canonical)

        for info_name, (canonical, flow) in STATEMENT_FIELDS.items():
            d = datum(canonical, flow)
            if not d.ok and info_name == "info_total_cash":
                d = datum(CASH_FALLBACK, False)
            if d.ok:
                put(info_name, d.value, f"statements ({d.period_label or d.period_end})")
        equity, shares = datum("stockholders_equity", False), datum("ordinary_shares", False)
        if equity.ok and shares.ok and shares.value > 0:
            # Book value per common share, like Yahoo's: preferred equity belongs to other holders.
            preferred = datum("preferred_stock", False)
            common = equity.value - (preferred.value if preferred.ok else 0.0)
            what = "common equity ÷ shares" if preferred.ok and preferred.value else "equity ÷ shares"
            put("book_value_per_share", common / shares.value,
                f"statements ({what}, {equity.period_label or equity.period_end})")
        if shares.ok:
            put("shares_outstanding", shares.value, f"statements ({shares.period_label or shares.period_end})")
        for canonical, freq in (("last_fiscal_year_end", "annual"), ("most_recent_quarter", "quarterly")):
            ends = [s.periods[0] for kind in ("income", "balance") if (s := stmt(kind, freq)) is not None and s.periods]
            if ends:
                put(canonical, _epoch(max(ends)), f"{freq} statements")

    def _dividends(self, ticker: str, put, notes: list[str], values: dict[str, Any]) -> None:
        try:
            divs = self.provider.get_dividends(ticker)
        except ProviderError as exc:
            notes.append(f"dividends: {exc}")
            return
        if divs is None or len(divs) == 0:
            return
        last = divs.index[-1].date()
        put("ex_dividend_date", _epoch(last), "dividend history (latest ex-date)")
        # Dividend history is in the trading currency; the info field is treated as reporting currency
        # (converted on a mismatch), so it is only filled when the two agree.
        if values.get("financial_currency") == values.get("currency"):
            cutoff = self.today() - timedelta(days=365)
            ttm = float(divs[[d.date() > cutoff for d in divs.index]].sum())
            put("dividend_rate", ttm, f"dividend history (12 months to {last})")


def fallback_reason(info: InfoResult) -> str:
    """One line for the UI: which values were rebuilt and from where."""
    if not info.is_fallback:
        return ""
    by_source: dict[str, list[str]] = {}
    for canonical, src in info.sources.items():
        by_source.setdefault(src, []).append(canonical.replace("_", " "))
    parts = [f"{', '.join(names)} from {src}" for src, names in by_source.items()]
    missing = [fs.canonical.replace("_", " ") for fs in fm.fields_for("info") if info.status(fs.canonical) != OK]
    line = "Yahoo company info unavailable; rebuilt from fallback sources: " + "; ".join(parts) + "."
    if missing:
        line += " Not available: " + ", ".join(missing) + "."
    return line


__all__ = ["InfoFallback", "fallback_reason", "sector_from_sic", "sector_from_finnhub"]
