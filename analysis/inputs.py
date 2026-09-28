"""Everything the four lenses read for one ticker, loaded once.

The stage-2 screen evaluation (manual path, so the screen never gates it)
supplies price, market cap, risk-free rate, the trap scores, the asset floor
and the stale flag; this adds dividends, insiders, leadership, context fields
and cyclicality. Each lens is then a pure function of `AnalysisInputs`, which
is what lets Quant, Macro and Moat run concurrently.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

import pandas as pd
from pydantic import BaseModel, ConfigDict

from data.edgar import EdgarClient
from analysis.models import FundamentalSeries, SeriesPoint
from data import field_map as fm
from data.fundamentals import Fundamentals
from data.insiders import collect_insider_data
from data.xbrl import CompanyFacts
from data.leadership import FilingDoc, LeadershipResult, LLMConfirmation, leadership_flag
from data.provider import AnalystEstimates, InfoResult, ProviderError
from data.sector import SectorRoute, route
from data.values import NA_INCOMPLETE, Datum, na_field_not_found
from screening.engine import MANUAL_SOURCE, ScreenContext, screen_ticker_full
from screening.models import STATUS_FAILED_TO_LOAD, ScreenResult
from signals.context import ContextFields, context_fields
from signals.cyclicality import Cyclicality, cyclicality
from signals.dividends import DividendSafety, dividend_safety
from signals.insider_activity import InsiderSummary, fetch_start, insider_summary


class AnalysisLoadError(Exception):
    """The ticker could not be loaded (info, price or statements)."""


class AnalysisInputs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: str
    today: date
    info: InfoResult
    route: SectorRoute
    f: Fundamentals
    screen: ScreenResult
    cyclicality: Cyclicality
    dividends: DividendSafety
    insiders: InsiderSummary
    leadership: LeadershipResult | None = None
    context: ContextFields
    notes: list[str] = []
    xbrl: CompanyFacts | None = None  # SEC EDGAR XBRL facts (long history; US and other SEC filers)
    xbrl_status: str = "N/A - SEC EDGAR not loaded"

    # Convenience accessors onto the screen result's inputs (already in the trading currency).
    @property
    def price(self) -> Datum:
        return self.screen.price

    @property
    def market_cap(self) -> Datum:
        return self.screen.market_cap

    def input(self, name: str) -> Datum:
        return self.screen.inputs.get(name, Datum.missing())

    @property
    def business_summary(self) -> str:
        return self.info.get("business_summary") or ""

    @property
    def company(self) -> str:
        return self.info.get("long_name") or self.ticker


SERIES_FIELDS = ("total_revenue", "net_income", "total_debt")


def fundamental_series(f: Fundamentals, currency: str | None) -> FundamentalSeries:
    """Revenue, net income and total debt at their own period ends for the small multiples:
    quarterly values when the provider has quarters, else fiscal years (recorded per metric).
    Flows are single-quarter values here, never TTM sums, so each point is one period."""
    out = FundamentalSeries(currency=currency, provider=f.provider)
    for name in SERIES_FIELDS:
        src = fm.spec(name).source
        for freq in ("quarterly", "annual"):
            stmt = f.stmt(src, freq)
            series = stmt.series(name) if stmt is not None else {}
            if series:
                out.points[name] = [SeriesPoint(period_end=d, value=v) for d, v in sorted(series.items())]
                out.freqs[name] = freq
                break
        else:
            annual = f.stmt(src, "annual")
            out.missing[name] = (na_field_not_found(name) if annual is not None and name in annual.not_found
                                 else NA_INCOMPLETE)
    return out


Confirm = Callable[[FilingDoc, str], LLMConfirmation]


def load_xbrl(edgar: EdgarClient | None, ticker: str) -> tuple[CompanyFacts | None, str]:
    """(facts, status). Only SEC filers have them; TSX-only companies never do (SEDAR+ isn't automated)."""
    if edgar is None:
        return None, "N/A - SEC EDGAR not loaded"
    try:
        cik = edgar.lookup_cik(ticker)
        if cik is None:
            return None, "N/A - not an SEC filer (no XBRL history; e.g. TSX-only)"
        return edgar.company_facts(cik), "ok"
    except ProviderError as exc:
        return None, f"N/A - SEC XBRL facts unavailable ({exc})"


def load_inputs(ctx: ScreenContext, ticker: str, edgar: EdgarClient | None = None,
                confirm: Confirm | None = None) -> AnalysisInputs:
    screen, f, info = screen_ticker_full(ctx, ticker, sources=MANUAL_SOURCE, force_stage2=True)
    if screen.status == STATUS_FAILED_TO_LOAD or f is None or info is None:
        raise AnalysisLoadError(screen.load_error or "statements not loaded")
    notes: list[str] = []
    try:
        divs = ctx.provider.get_dividends(ticker)
    except ProviderError as exc:
        divs = pd.Series(dtype=float)
        notes.append(f"dividend history unavailable: {exc}")
    try:
        estimates: AnalystEstimates | None = ctx.provider.get_analyst_estimates(ticker)
    except ProviderError as exc:
        estimates = None
        notes.append(f"analyst estimates unavailable: {exc}")
    # Officer snapshots and the manual CSV work without EDGAR; 8-K / 6-K / Form 4 need it.
    kw = {"confirm": confirm} if confirm is not None else {}
    lead = leadership_flag(ticker, today=ctx.today, edgar=edgar, db_path=ctx.db_path, **kw)
    ins = collect_insider_data(ticker, edgar, fetch_start(ctx.today))
    if edgar is None:
        notes.append("EDGAR not loaded: leadership from officer snapshots and the manual CSV only; "
                     "insiders from the manual CSV only")
    xbrl, xbrl_status = load_xbrl(edgar, ticker)
    return AnalysisInputs(
        ticker=ticker, today=ctx.today, info=info, route=route(info), f=f, screen=screen, xbrl=xbrl,
        xbrl_status=xbrl_status,
        cyclicality=cyclicality(info.get("sector"), info.get("industry")),
        dividends=dividend_safety(divs, f, screen.price, ctx.today, route(info).sector_adjusted),
        insiders=insider_summary(ins, ctx.today), leadership=lead,
        context=context_fields(info, estimates), notes=notes)
