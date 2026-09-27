"""The six statements of one ticker, already in its trading currency, with the
Rule 3b accessors used by the screener, the signals and (later) the lenses:

- `ttm(field)`: flows, trailing twelve months (or "annual, not TTM");
- `bal(field)`: balance-sheet items, latest quarter with its date;
- `fy(field, period_end)`: one fiscal year's value, labelled by the company's own year end.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from data import currency, periods
from data import field_map as fm
from data.provider import DataProvider, EarningsDates, InfoResult, Statement
from data.values import Datum, NA_INCOMPLETE, na_field_not_found

KINDS = ("income", "balance", "cashflow")
FREQS = ("annual", "quarterly")


def _key(kind: str, freq: str) -> str:
    return f"{kind}_{freq}"


class Fundamentals(BaseModel):
    ticker: str
    statements: dict[str, Statement] = Field(default_factory=dict)
    earnings: EarningsDates | None = None

    def stmt(self, kind: str, freq: str) -> Statement | None:
        return self.statements.get(_key(kind, freq))

    def _pair(self, canonical: str) -> tuple[Statement | None, Statement | None]:
        src = fm.spec(canonical).source
        return self.stmt(src, "quarterly"), self.stmt(src, "annual")

    def ttm(self, canonical: str) -> Datum:
        return periods.ttm(*self._pair(canonical), canonical)

    def bal(self, canonical: str) -> Datum:
        return periods.latest_balance(*self._pair(canonical), canonical)

    def fiscal_year_ends(self) -> list[date]:
        """Fiscal year ends, newest first (from the annual income statement, else the balance sheet)."""
        for kind in ("income", "balance", "cashflow"):
            s = self.stmt(kind, "annual")
            if s is not None and not s.empty:
                return s.periods
        return []

    def fy(self, canonical: str, period_end: date) -> Datum:
        annual = self.stmt(fm.spec(canonical).source, "annual")
        if annual is None:
            return Datum.missing(NA_INCOMPLETE)
        series = annual.series(canonical)
        if period_end in series:
            return Datum(value=series[period_end], period_end=period_end,
                         period_label=periods.fiscal_year_label(period_end), provider=annual.provider,
                         currency=annual.currency, notes=list(annual.notes))
        if canonical in annual.not_found:
            return Datum.missing(na_field_not_found(canonical))
        return Datum.missing(NA_INCOMPLETE, period_end=period_end)

    def annual_values(self, canonical: str) -> list[Datum]:
        s = self.stmt(fm.spec(canonical).source, "annual")
        return periods.fiscal_years(s, canonical)

    def latest_period_end(self) -> date | None:
        return periods.latest_period_end(*self.statements.values())

    @property
    def provider(self) -> str:
        return ", ".join(sorted({s.provider for s in self.statements.values() if s.provider}))


def load_fundamentals(provider: DataProvider, info: InfoResult, ticker: str) -> Fundamentals:
    """Fetch all six statements (ProviderError propagates) and convert them to the trading currency."""
    stmts = {}
    for kind in KINDS:
        for freq in FREQS:
            stmts[_key(kind, freq)] = currency.to_trading_currency(
                provider, info, provider.get_statement(ticker, kind, freq))
    return Fundamentals(ticker=ticker, statements=stmts)
