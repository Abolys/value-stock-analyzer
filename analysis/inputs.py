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
from data.fundamentals import Fundamentals
from data.insiders import collect_insider_data
from data.leadership import FilingDoc, LeadershipResult, LLMConfirmation, leadership_flag
from data.provider import AnalystEstimates, InfoResult, ProviderError
from data.sector import SectorRoute, route
from data.values import Datum
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


Confirm = Callable[[FilingDoc, str], LLMConfirmation]


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
    return AnalysisInputs(
        ticker=ticker, today=ctx.today, info=info, route=route(info), f=f, screen=screen,
        cyclicality=cyclicality(info.get("sector"), info.get("industry")),
        dividends=dividend_safety(divs, f, screen.price, ctx.today, route(info).sector_adjusted),
        insiders=insider_summary(ins, ctx.today), leadership=lead,
        context=context_fields(info, estimates), notes=notes)
