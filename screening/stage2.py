"""Stage 2: exact metrics from full statements, for stage-1 survivors (and
manual tickers). The final screen status always comes from here.

`evaluate` is pure: it takes already-fetched, currency-converted data, so the
engine does the fetching and tests can feed synthetic statements.
"""

from __future__ import annotations

import logging
from datetime import date

import config
from data import periods
from data.fundamentals import Fundamentals
from data.provider import EarningsDates, InfoResult
from data.ratios import market_cap, safe_ratio, sum_datums
from data.sector import SectorRoute
from data.shares import ShareTrend, dilution_flag
from data.values import Datum
from screening import metrics as mx
from screening.models import (
    SLOT_FCF, SLOT_LEVERAGE, SLOT_MOS, Divergence, MetricResult, ScreenResult, Stage1Result,
)
from screening.quality import QualityInputs, quality_score
from screening.status import min_metrics_for_pass, screen_status
from signals.asset_floor import NET_NET_LABEL, asset_floor
from signals.returns import ffo, roe, roic
from signals.trap_scores import altman_z2, beneish, piotroski
from signals.valuation import ev_ebit_yield, liquid_cash, optional_part

log = logging.getLogger(__name__)

# Canonical statement fields whose statuses feed the per-field N/A report.
TRACKED_TTM = ["total_revenue", "ebit", "ebitda", "net_income", "diluted_eps", "free_cash_flow",
               "operating_cash_flow", "stock_based_compensation"]
TRACKED_BAL = ["total_assets", "total_liabilities", "stockholders_equity", "total_debt", "cash_and_equivalents",
               "current_assets", "current_liabilities", "ordinary_shares", "invested_capital", "retained_earnings"]


def _rel_diff(a: float, b: float) -> float | None:
    return None if a == 0 else abs(b - a) / abs(a)


def divergences(stage1: Stage1Result | None, pairs: dict[str, tuple[str, str, Datum]],
                ticker: str) -> list[Divergence]:
    """Stage-2 metrics that differ from their stage-1 estimate by more than STAGE_DIVERGENCE.

    pairs: name → (slot, fmt, stage-2 value); a stage-1 metric in a different
    unit (estimated runway vs FCF yield) is not compared.
    """
    out: list[Divergence] = []
    if stage1 is None:
        return out
    for name, (slot, fmt, s2) in pairs.items():
        m1 = stage1.metric(slot)
        if m1 is None or m1.fmt != fmt or not m1.value.ok or not s2.ok:
            continue
        rel = _rel_diff(m1.value.value, s2.value)
        if rel is not None and rel > config.STAGE_DIVERGENCE:
            out.append(Divergence(metric=name, stage1=m1.value.value, stage2=s2.value, rel_diff=rel))
            log.info("%s: stage-1/stage-2 divergence on %s: %.4g vs %.4g (%.0f%%)",
                     ticker, name, m1.value.value, s2.value, rel * 100)
    return out


def _shares_outstanding(f: Fundamentals, info_values: dict[str, Datum]) -> Datum:
    sh = f.bal("ordinary_shares")
    if sh.ok and sh.value > 0:
        return sh
    fallback = info_values.get("shares_outstanding", Datum.missing())
    if fallback.ok:
        return fallback.model_copy(update={"notes": [*fallback.notes, "balance-sheet share count missing; info used"]})
    return sh


def ttm_eps(f: Fundamentals) -> Datum:
    """Diluted EPS, TTM. When a quarterly EPS cell is missing but four quarters
    of net income exist, TTM net income / latest diluted shares keeps it TTM
    rather than falling back to the fiscal year (Rule 3b)."""
    eps = f.ttm("diluted_eps")
    if eps.ok and eps.period_label.startswith("TTM"):
        return eps
    ni = f.ttm("net_income")
    shares = periods.latest_balance(f.stmt("income", "quarterly"), f.stmt("income", "annual"), "diluted_shares")
    if ni.ok and ni.period_label.startswith("TTM"):
        derived = safe_ratio(ni, shares, name="diluted EPS (TTM)", nonpositive_reason="no diluted shares",
                             num_name="net income", den_name="diluted shares")
        if derived.ok:
            derived.period_label = f"TTM net income / latest diluted shares ({ni.period_label})"
            derived.notes.append("quarterly diluted EPS incomplete; derived from TTM net income")
            return derived
    return eps


def evaluate(ticker: str, info: InfoResult, info_values: dict[str, Datum], route: SectorRoute, f: Fundamentals,
             trend: ShareTrend, price: Datum, rf: Datum, today: date, stage1: Stage1Result | None = None,
             earnings: EarningsDates | None = None, sources: str = "") -> ScreenResult:
    sa = route.sector_adjusted
    shares = _shares_outstanding(f, info_values)
    mcap = market_cap(shares, price, ticker)
    equity = f.bal("stockholders_equity")
    pref, pref_note = optional_part(f.bal("preferred_stock"), "preferred equity")
    common_equity = sum_datums({"equity": equity, "preferred": pref}, "common equity", signs={"preferred": -1})
    bvps = safe_ratio(common_equity, shares, name="book value per share", nonpositive_reason="no shares outstanding",
                      num_name="common equity", den_name="shares")
    eps = ttm_eps(f)
    fcf = mx.ttm_fcf(f)
    sbc = f.ttm("stock_based_compensation")
    cash = liquid_cash(f)
    debt = f.bal("total_debt")
    ebitda = f.ttm("ebitda")
    revenue = f.ttm("total_revenue")

    res = ScreenResult(ticker=ticker, name=info.get("long_name") or "", sources=sources,
                       sector=route.sector, industry=route.industry, treatment=route.label,
                       currency=info.get("currency"), price=price, market_cap=mcap, risk_free=rf, stage1=stage1,
                       decided_at_stage=2)
    res.inputs = {"shares outstanding": shares, "EPS (TTM, diluted)": eps, "book value per share": bvps,
                  "FCF (TTM, raw)": fcf, "SBC (TTM)": sbc, "cash + short-term investments": cash,
                  "total debt": debt, "EBITDA (TTM)": ebitda, "revenue (TTM)": revenue, "equity": equity}
    if pref_note:
        res.notes.append(f"book value per share: {pref_note}")
    if route.unmatched_industry:
        res.notes.append(f"industry {route.industry!r} matches no SUBSECTOR_RULES entry; default financial treatment")

    # -- the four screen metrics ------------------------------------------
    mos = mx.margin_of_safety(eps, bvps, price)
    runway = mx.cash_runway(cash, fcf)
    fcf_test = mx.fcf_negative_test(mx.annual_fcf(f))
    res.fcf_negative = fcf_test.status
    shares_m = mx.share_trend_metric(trend)
    lev_ratio = Datum.missing()
    floor = asset_floor(f, mcap, fcf, sector_adjusted=sa)
    if not sa:
        if fcf_test.status == mx.FCF_NEGATIVE and not runway.is_nm:
            fcf_m = mx.runway_metric(runway, cash, fcf)
            fcf_m.notes.append(f"structurally FCF-negative ({fcf_test.detail}): cash runway replaces FCF yield")
            if caveat := mx.fleet_runway_caveat(route.industry):
                fcf_m.notes.append(caveat)
        else:
            fcf_m = mx.fcf_yield(fcf, sbc, mcap, rf)
            if fcf_test.status == mx.FCF_NEGATIVE:
                fcf_m.notes.append(f"structurally FCF-negative ({fcf_test.detail}) but not burning cash in the "
                                   f"TTM (runway {runway.status}); FCF yield used")
            elif fcf_test.status == mx.TOO_LITTLE_HISTORY:
                fcf_m.notes.append(f"FCF-negative test: {fcf_test.status} ({fcf_test.detail}); FCF yield used")
        lev_m = mx.leverage_metric(debt, cash, ebitda)
        lev_ratio = lev_m.value
        metrics = [mos, fcf_m, lev_m, shares_m]
    else:
        roe_v = roe(f)
        if route.subsector == "reit":
            fcf_m = mx.ffo_yield_metric(ffo(f), mcap, rf)
            lev_m = mx.not_applicable(SLOT_LEVERAGE, "Book-value slot", "not applicable to REITs (P/FFO used)")
        else:
            fcf_m = mx.roe_spread_metric(roe_v)
            lev_m = (mx.p_tbv_metric(floor.p_tbv) if route.subsector == "bank"
                     else mx.p_b_metric(mcap, equity))
        metrics = [mos, fcf_m, lev_m, shares_m]
        res.notes.append(f"{route.label}: EBITDA, net debt/EBITDA and FCF are not meaningful; "
                         "sector-adjusted metrics used")

    # -- signals ------------------------------------------------------------
    res.piotroski = piotroski(f, sa)
    res.altman = altman_z2(f, sa)
    res.beneish = beneish(f, sa)
    res.earnings_yield = ev_ebit_yield(f, mcap, sa)
    res.asset_floor = floor
    res.net_cash_flag = res.earnings_yield.net_cash_flag
    if config.USE_EARNINGS_YIELD_IN_SCREEN and not sa:
        metrics.append(mx.earnings_yield_metric(res.earnings_yield))
    if res.piotroski.score is not None and res.piotroski.score <= config.PIOTROSKI_WEAK:
        res.trap_risk_reasons.append(f"Piotroski {res.piotroski.display} ≤ {config.PIOTROSKI_WEAK}")
    if res.altman.zone == "distress":
        res.trap_risk_reasons.append(f"Altman Z'' {res.altman.display}")
    res.trap_risk = bool(res.trap_risk_reasons)

    res.metrics = metrics
    res.metrics_available = sum(m.available for m in metrics)
    res.min_metrics_for_pass = min_metrics_for_pass(metrics)
    res.status, res.status_reasons = screen_status(metrics, res.trap_risk)
    res.dilution_flag = dilution_flag(trend)
    res.share_trend_span = trend.span_label

    # -- quality (nothing divided by price) ------------------------------------
    if sa:
        roe_v = roe(f)
        spread = roe_v.model_copy(update={"value": roe_v.value - config.COST_OF_CAPITAL}) if roe_v.ok else roe_v
        q_in = QualityInputs(returns_spread=spread, returns_label="ROE spread", share_trend=shares_m.value,
                             sector_adjusted=True)
    else:
        r = roic(f)
        spread = r.value.model_copy(update={"value": r.value.value - config.COST_OF_CAPITAL}) if r.value.ok else r.value
        adj = sum_datums({"FCF": fcf, "SBC": sbc}, "FCF − SBC", signs={"SBC": -1}) if sbc.ok else fcf
        margin = safe_ratio(adj, revenue, name="FCF margin", nonpositive_reason="revenue ≤ 0")
        if not sbc.ok and margin.ok:
            margin.notes.append(mx.SBC_NOT_REPORTED.replace("FCF yield", "FCF margin"))
        q_in = QualityInputs(returns_spread=spread,
                             returns_label="ROIC spread" if not r.is_substitute else "ROA spread (ROIC n/m)",
                             fcf_margin=margin, net_debt=mx.net_debt(debt, cash), ebitda=ebitda,
                             net_debt_ebitda=lev_ratio, runway_months=runway, share_trend=shares_m.value)
        res.inputs["ROIC"] = r.value
        res.notes += [f"ROIC: {n}" for n in r.notes]
    res.quality = quality_score(q_in)

    # -- periods, staleness, divergence, field statuses ----------------------
    res.fundamentals_as_of = f.latest_period_end()
    stale = periods.staleness(res.fundamentals_as_of, earnings, today)
    res.stale, res.stale_label = stale.stale, stale.label
    raw_yield = safe_ratio(fcf, mcap, name="raw FCF yield", nonpositive_reason="market cap ≤ 0")
    res.divergences = divergences(stage1, {"margin of safety": (SLOT_MOS, "pct", mos.value),
                                           "FCF yield (raw)": (SLOT_FCF, "pct", raw_yield),
                                           "net debt / EBITDA": (SLOT_LEVERAGE, "ratio", lev_ratio)}, ticker)
    res.field_statuses = {**(stage1.field_statuses if stage1 else {}),
                          **{f"ttm.{k}": f.ttm(k).status for k in TRACKED_TTM},
                          **{f"bal.{k}": f.bal(k).status for k in TRACKED_BAL}}
    res.assumptions = {"COST_OF_CAPITAL": config.COST_OF_CAPITAL, "MIN_MARGIN_OF_SAFETY": config.MIN_MARGIN_OF_SAFETY,
                       "MIN_FCF_SPREAD_OVER_10Y": config.MIN_FCF_SPREAD_OVER_10Y,
                       "MAX_NET_DEBT_EBITDA": config.MAX_NET_DEBT_EBITDA,
                       "MAX_SHARE_GROWTH_PER_YEAR": config.MAX_SHARE_GROWTH_PER_YEAR,
                       "risk_free": rf.value if rf.ok else rf.status}
    res.rationale = rationale(res)
    return res


def _metric_line(m: MetricResult) -> str:
    line = f"- **{m.name}**: {m.display} → {m.outcome}" + (f" (needs {m.threshold})" if m.threshold else "")
    return "\n".join([line, *[f"  - {n}" for n in m.notes]])


def rationale(r: ScreenResult) -> str:
    lines = [f"**{r.ticker}** — {r.display_status} ({'; '.join(r.status_reasons)})",
             f"Treatment: {r.treatment}. Fundamentals as of {r.fundamentals_as_of or 'N/A'}; "
             f"price {r.price.display()} ({r.price.period_end or 'N/A'}); market cap {r.market_cap.display()} "
             f"({r.market_cap.period_label or r.market_cap.status}).",
             f"Metrics available: {r.metrics_available} (needs {r.min_metrics_for_pass} for a Pass)."]
    if r.stale:
        lines.append(f"⚠️ {r.stale_label}")
    lines += [_metric_line(m) for m in r.metrics]
    if r.piotroski:
        lines.append(f"- Piotroski F-score: {r.piotroski.display}")
    if r.altman:
        lines.append(f"- Altman Z'': {r.altman.display}")
    if r.beneish:
        lines.append(f"- Beneish M-score: {r.beneish.display}" + (f" — {r.beneish.caveat}" if r.beneish.flag else ""))
    if r.earnings_yield:
        lines.append(f"- EV/EBIT earnings yield: {r.earnings_yield.display}"
                     + (" — ⚑ net cash exceeds market cap" if r.net_cash_flag else ""))
    if r.trap_risk:
        lines.append(f"- ⚑ Trap risk: {'; '.join(r.trap_risk_reasons)}"
                     + ("" if config.TRAP_RISK_FAILS_SCREEN else " (shown, not a Fail)"))
    if r.asset_floor:
        af = r.asset_floor
        lines.append(f"- {af.summary}; TBV {af.tbv.display()}, P/TBV {af.p_tbv.display()}, NCAV {af.ncav.display()}, "
                     f"NNWC {af.nnwc.display()} (information only)")
        if af.net_net:
            lines.append(f"  - ⚑ {NET_NET_LABEL}" + (f"; {af.burn_line}" if af.burn_line else ""))
    if r.quality:
        lines.append(f"- Quality: {r.quality.working}")
    for d in r.divergences:
        lines.append(f"- Stage-1/stage-2 divergence on {d.metric}: {d.stage1:.4g} vs {d.stage2:.4g} "
                     f"({d.rel_diff:.0%}); one source may be out of date")
    lines += [f"- Note: {n}" for n in r.notes]
    return "\n".join(lines)
