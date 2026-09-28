"""One ticker through the two-stage screen (SPEC "Screener execution").

- Info load failure, or no price, → "failed to load" (never a screen Fail).
- Every loaded ticker gets an officer snapshot from the same info call.
- Stage 1 decides whether the full statements are fetched; a ticker cut there
  is recorded as Fail with decided_at_stage=1 and the stage-1 reasons.
- Manual tickers (`analyse_manual`) always go through stage 2: the screener is
  for discovery, not a gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import config
from data import corporate_actions as ca
from data import currency, periods, prices, sector
from data.fundamentals import Fundamentals, load_fundamentals
from data.provider import DataProvider, EarningsDates, InfoResult, ProviderError
from data.risk_free import fetch_boc_valet, risk_free_for
from data.shares import ShareTrend, share_trend
from data.values import Datum
from screening.models import STATUS_FAIL, STATUS_FAILED_TO_LOAD, ScreenResult
from screening.stage1 import stage1
from screening.stage2 import evaluate
from storage.db import save_officer_snapshot

log = logging.getLogger(__name__)
MANUAL_SOURCE = "manual"


@dataclass
class ScreenContext:
    provider: DataProvider
    db_path: Path | str = config.RUNS_DB_PATH
    today: date = field(default_factory=date.today)
    valet_fetch: Callable[[str], tuple[float, date]] = fetch_boc_valet
    save_snapshots: bool = True

    @property
    def cache(self):
        return getattr(self.provider, "cache", None)


def failed_to_load(ticker: str, sources: str, reason: str) -> ScreenResult:
    return ScreenResult(ticker=ticker, sources=sources, status=STATUS_FAILED_TO_LOAD, load_error=reason,
                        decided_at_stage=0, status_reasons=[reason])


def info_as_of(info: InfoResult) -> date | None:
    """Info's mostRecentQuarter (epoch seconds in yfinance) as a date."""
    return info.date("most_recent_quarter")


STAGE1_STALENESS_NOTE = ("staleness checked by age only at stage 1: the earnings history is fetched only for "
                         "tickers that reach stage 2")


def stage1_info_values(ctx: ScreenContext, info: InfoResult) -> dict[str, Datum]:
    mismatch = currency.detect_mismatch(info)
    fx = currency.fx_rate(ctx.provider, *mismatch) if mismatch else None
    return currency.info_datums(info, fx, period_end=info_as_of(info))


def _earnings(ctx: ScreenContext, ticker: str) -> EarningsDates | None:
    try:
        return ctx.provider.get_earnings_dates(ticker)
    except ProviderError:
        return None


def adjusted_share_trend(ctx: ScreenContext, ticker: str) -> ShareTrend:
    """Split-adjusted share-count trend on the latest segment after any corporate-action break."""
    try:
        closes = prices.actual_closes(ctx.provider, ticker)
        splits = ctx.provider.get_splits(ticker)
        sh = ctx.provider.get_shares_history(ticker)
    except ProviderError as exc:
        return ShareTrend(status=f"N/A - share history unavailable ({exc})")
    breaks = ca.corporate_action_breaks(ticker, closes, sh.series, splits)
    return share_trend(sh.series, splits, ca.break_dates(breaks), sh.source)


def stage1_cut_result(ticker: str, sources: str, info: InfoResult, route, s1, price: Datum,
                      today: date) -> ScreenResult:
    as_of = info_as_of(info)
    stale = periods.staleness(as_of, None, today)  # age only: no earnings history for stage-1 cuts
    res = ScreenResult(ticker=ticker, name=info.get("long_name") or "", sources=sources, status=STATUS_FAIL,
                       decided_at_stage=1, status_reasons=[f"cut at stage 1: {r}" for r in s1.cut_reasons],
                       sector=route.sector, industry=route.industry, treatment=route.label,
                       currency=info.get("currency"), price=price, market_cap=s1.market_cap, stage1=s1,
                       metrics=s1.metrics, metrics_available=sum(m.available for m in s1.metrics),
                       fundamentals_as_of=as_of, stale=stale.stale, stale_label=stale.label,
                       field_statuses=dict(s1.field_statuses), notes=[*s1.notes, STAGE1_STALENESS_NOTE])
    lines = [f"**{ticker}** — {res.display_status}: cut at stage 1 (info fields, thresholds loosened by "
             f"STAGE1_SLACK {config.STAGE1_SLACK:.0%}); statements not fetched.",
             *[f"- {r}" for r in s1.cut_reasons],
             f"Fundamentals as of {as_of or 'N/A'} (info mostRecentQuarter)."]
    if stale.stale:
        lines.append(f"⚠️ {stale.label}")
    res.rationale = "\n".join(lines)
    return res


def screen_ticker(ctx: ScreenContext, ticker: str, sources: str = "", price: Datum | None = None,
                  force_stage2: bool = False) -> ScreenResult:
    return screen_ticker_full(ctx, ticker, sources, price, force_stage2)[0]


def screen_ticker_full(ctx: ScreenContext, ticker: str, sources: str = "", price: Datum | None = None,
                       force_stage2: bool = False) -> tuple[ScreenResult, Fundamentals | None, InfoResult | None]:
    """The screen result plus the fetched statements and info (None where not fetched),
    so per-ticker analysis reuses them instead of fetching twice."""
    try:
        info = ctx.provider.get_info(ticker)
    except ProviderError as exc:
        return failed_to_load(ticker, sources, f"info: {exc}"), None, None
    if ctx.save_snapshots:
        save_officer_snapshot(ticker, info, when=ctx.today, path=ctx.db_path)
    if price is None:
        price = prices.actual_latest_price(ctx.provider, ticker)
    if not price.ok:
        return failed_to_load(ticker, sources, f"price: {price.status}"), None, info
    price = price.model_copy(update={"currency": info.get("currency")})
    route = sector.route(info)
    info_values = stage1_info_values(ctx, info)
    rf = risk_free_for(info, ctx.provider, ctx.cache, ctx.valet_fetch)
    s1 = stage1(info_values, price, rf, route, ticker)
    if not s1.survives and not force_stage2:  # one yfinance call (info) for a stage-1 cut
        return stage1_cut_result(ticker, sources, info, route, s1, price, ctx.today), None, info
    earnings = _earnings(ctx, ticker)
    try:
        f = load_fundamentals(ctx.provider, info, ticker)
    except ProviderError as exc:
        return failed_to_load(ticker, sources, f"statements: {exc}"), None, info
    f.earnings = earnings
    trend = adjusted_share_trend(ctx, ticker)
    res = evaluate(ticker, info, info_values, route, f, trend, price, rf, ctx.today, stage1=s1,
                   earnings=earnings, sources=sources)
    if force_stage2 and not s1.survives:
        res.notes.append("stage 1 would have cut this ticker (" + "; ".join(s1.cut_reasons)
                         + "); analysed in full because it was entered manually")
    return res, f, info


def analyse_manual(ctx: ScreenContext, ticker: str) -> ScreenResult:
    """Manual tickers bypass the screen and always get the full stage-2 analysis."""
    return screen_ticker(ctx, ticker, sources=MANUAL_SOURCE, force_stage2=True)
