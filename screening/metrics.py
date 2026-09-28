"""The screen metrics, as pure functions of Datums (Rules 2, 2b, 3b).

Each returns a MetricResult with the value, the threshold used, the outcome
(pass / fail / N/A / n/m) and its inputs. `slack` is STAGE1_SLACK in stage 1
and 0 in stage 2: it loosens the hurdle a metric is compared against
(hurdle × (1 − slack) for "at least" tests, ceiling × (1 + slack) for
"at most" tests), so it also works for thresholds of zero.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel

import config
from data.fundamentals import Fundamentals
from data.ratios import combine, first_unusable, safe_ratio, sum_datums
from data.shares import ShareTrend, dilution_flag
from data.values import Datum, nm
from screening.models import (
    FAIL, NA, NM, NOT_APPLICABLE, PASS, SLOT_EARNINGS_YIELD, SLOT_FCF, SLOT_LEVERAGE, SLOT_MOS, SLOT_SHARES,
    MetricResult,
)
from signals.valuation import EarningsYield, graham_number

FCF_NEGATIVE = "FCF-negative"
NOT_FCF_NEGATIVE = "not FCF-negative"
TOO_LITTLE_HISTORY = "Insufficient data - too little history"
SBC_NOT_REPORTED = "SBC not reported; unadjusted FCF yield"
NOT_BURNING = "not burning cash"


def outcome_of(value: Datum, passes: Callable[[float], bool]) -> str:
    if value.ok:
        return PASS if passes(value.value) else FAIL
    return NM if value.is_nm else NA


def _slack_note(slack: float) -> list[str]:
    return [f"stage-1 hurdle loosened by STAGE1_SLACK {slack:.0%}"] if slack else []


# --------------------------------------------------------------------------
# 1. Margin of safety (Graham Number vs actual latest price)
# --------------------------------------------------------------------------
def margin_of_safety(eps: Datum, bvps: Datum, price: Datum, slack: float = 0.0) -> MetricResult:
    graham = graham_number(eps, bvps)
    if graham.ok:
        gap = sum_datums({"Graham Number": graham, "price": price}, "Graham − price", signs={"price": -1})
        mos = safe_ratio(gap, price, name="margin of safety", nonpositive_reason="price ≤ 0")
    else:
        mos = Datum.missing(graham.status)
    hurdle = (1 + config.MIN_MARGIN_OF_SAFETY) * (1 - slack) - 1
    return MetricResult(slot=SLOT_MOS, name="Margin of safety (Graham Number vs price)", value=mos,
                        threshold=f"≥ {hurdle:+.0%}", outcome=outcome_of(mos, lambda v: v >= hurdle),
                        inputs={"EPS": eps, "book value per share": bvps, "Graham Number": graham,
                                "actual latest price": price},
                        notes=_slack_note(slack))


# --------------------------------------------------------------------------
# 2. FCF yield vs the 10-year yield, or cash runway for FCF-negative names
# --------------------------------------------------------------------------
def ttm_fcf(f: Fundamentals) -> Datum:
    """Raw TTM free cash flow: the FCF row, else operating cash flow + capex (capex is negative)."""
    fcf = f.ttm("free_cash_flow")
    if fcf.ok:
        return fcf
    alt = sum_datums({"operating cash flow": f.ttm("operating_cash_flow"),
                      "capital expenditure": f.ttm("capital_expenditure")}, "OCF + capex")
    if alt.ok:
        alt.notes.append("FCF row not reported; operating cash flow + capital expenditure used")
        return alt
    return fcf


def annual_fcf(f: Fundamentals) -> list[Datum]:
    out = []
    cashflow = f.stmt("cashflow", "annual")
    for d in (cashflow.periods if cashflow is not None else []):
        v = f.fy("free_cash_flow", d)
        if not v.ok:
            v = sum_datums({"operating cash flow": f.fy("operating_cash_flow", d),
                            "capital expenditure": f.fy("capital_expenditure", d)}, "OCF + capex")
        if v.ok:
            out.append(v)
    return out


class FcfNegativeTest(BaseModel):
    status: str
    negative_years: int = 0
    years_used: int = 0
    detail: str = ""


def fcf_negative_test(annual: list[Datum]) -> FcfNegativeTest:
    """FCF_NEGATIVE_RULE: negative FCF in a majority of the available fiscal years
    (up to the last FCF_NEGATIVE_MAX_YEARS), with at least FCF_NEGATIVE_MIN_YEARS."""
    years = [d for d in annual if d.ok][: config.FCF_NEGATIVE_MAX_YEARS]
    if len(years) < config.FCF_NEGATIVE_MIN_YEARS:
        return FcfNegativeTest(status=TOO_LITTLE_HISTORY, years_used=len(years),
                               detail=f"{len(years)} fiscal year(s) of FCF; {config.FCF_NEGATIVE_MIN_YEARS} needed")
    neg = sum(d.value < 0 for d in years)
    status = FCF_NEGATIVE if neg > len(years) / 2 else NOT_FCF_NEGATIVE
    labels = ", ".join(f"{d.period_label} {d.value:,.3g}" for d in years)
    return FcfNegativeTest(status=status, negative_years=neg, years_used=len(years),
                           detail=f"negative in {neg} of {len(years)} fiscal years ({labels})")


def fcf_yield(fcf: Datum, sbc: Datum | None, mcap: Datum, rf: Datum, slack: float = 0.0) -> MetricResult:
    """SBC-adjusted FCF yield vs the trading currency's 10-year yield.

    `sbc=None` means the unadjusted stage-1 estimate. A missing SBC row uses raw
    FCF, labelled (SPEC "SBC not reported").
    """
    notes = _slack_note(slack)
    if sbc is None:
        num, label = fcf, "FCF yield (unadjusted, stage-1 estimate)"
    elif sbc.ok:
        num = sum_datums({"FCF": fcf, "SBC": sbc}, "FCF − SBC", signs={"SBC": -1})
        label = "SBC-adjusted FCF yield vs 10-year"
    else:
        num, label = fcf, "FCF yield vs 10-year (SBC not reported, unadjusted)"
        notes.append(f"{SBC_NOT_REPORTED} ({sbc.status})")
    yld = safe_ratio(num, mcap, name=label, nonpositive_reason="market cap ≤ 0",
                     num_name="FCF", den_name="market cap")
    inputs = {"FCF (TTM, raw)": fcf, "market cap": mcap, "10-year yield": rf}
    if sbc is not None:
        inputs["SBC (TTM)"] = sbc
    if not rf.ok:
        return MetricResult(slot=SLOT_FCF, name=label, value=yld, outcome=NA, inputs=inputs,
                            threshold="vs 10-year yield", na_reason=f"comparison {rf.status}",
                            notes=[*notes, f"comparison {rf.status}"])
    hurdle = (rf.value + config.MIN_FCF_SPREAD_OVER_10Y) * (1 - slack)
    return MetricResult(slot=SLOT_FCF, name=label, value=yld, inputs=inputs,
                        threshold=f"≥ {hurdle:.2%} (10-year {rf.value:.2%} + {config.MIN_FCF_SPREAD_OVER_10Y:.2%})",
                        outcome=outcome_of(yld, lambda v: v >= hurdle), notes=notes)


def cash_runway(cash: Datum, fcf_raw: Datum) -> Datum:
    """Months of cash (incl. short-term investments) at the current raw FCF burn.

    Burn = −TTM raw FCF (not SBC-adjusted: stock comp isn't cash out).
    Burn ≤ 0 → n/m "not burning cash".
    """
    bad = first_unusable(cash, fcf_raw)
    if bad is not None:
        return Datum.missing(bad.status)
    if fcf_raw.value >= 0:
        return Datum.missing(nm(NOT_BURNING), notes=[f"TTM FCF {fcf_raw.value:,.4g}"])
    monthly = combine(-fcf_raw.value / config.MONTHS_PER_YEAR, {"FCF": fcf_raw}, label="monthly burn")
    return safe_ratio(cash, monthly, name="cash runway (months)", nonpositive_reason=NOT_BURNING,
                      num_name="cash + short-term investments", den_name="monthly burn")


def fleet_runway_caveat(industry: str | None) -> str:
    """The FLEET_RUNWAY_CAVEAT for fleet-capex industries (rental and leasing), else ""."""
    if industry and any(industry.startswith(p) for p in config.FLEET_CAPEX_INDUSTRIES):
        return f"{industry}: {config.FLEET_RUNWAY_CAVEAT}"
    return ""


def runway_metric(runway: Datum, cash: Datum, fcf_raw: Datum, slack: float = 0.0,
                  label: str = "Cash runway (FCF-negative)") -> MetricResult:
    hurdle = config.MIN_CASH_RUNWAY_MONTHS * (1 - slack)
    return MetricResult(slot=SLOT_FCF, name=label, value=runway, fmt="months", threshold=f"≥ {hurdle:.0f} months",
                        outcome=outcome_of(runway, lambda v: v >= hurdle),
                        inputs={"cash + short-term investments": cash, "FCF (TTM, raw)": fcf_raw},
                        notes=_slack_note(slack))


# --------------------------------------------------------------------------
# 3. Net debt / EBITDA
# --------------------------------------------------------------------------
def net_debt(debt: Datum, cash: Datum) -> Datum:
    return sum_datums({"total debt": debt, "cash + short-term investments": cash}, "net debt",
                      signs={"cash + short-term investments": -1})


def net_debt_to_ebitda(nd: Datum, ebitda: Datum) -> Datum:
    """Rule 2b: EBITDA ≤ 0 → n/m, with the net-debt / net-cash case in the reason."""
    bad = first_unusable(nd, ebitda)
    if bad is not None:
        return Datum.missing(bad.status)
    if ebitda.value <= 0:
        reason = "negative EBITDA" if nd.value > 0 else "negative EBITDA, net cash"
        return Datum.missing(nm(reason), notes=[f"EBITDA {ebitda.value:,.4g}, net debt {nd.value:,.4g}"])
    return safe_ratio(nd, ebitda, name="net debt / EBITDA", nonpositive_reason="negative EBITDA",
                      num_name="net debt", den_name="EBITDA")


def leverage_metric(debt: Datum, cash: Datum, ebitda: Datum, slack: float = 0.0) -> MetricResult:
    nd = net_debt(debt, cash)
    ratio = net_debt_to_ebitda(nd, ebitda)
    ceiling = config.MAX_NET_DEBT_EBITDA * (1 + slack)
    out = MetricResult(slot=SLOT_LEVERAGE, name="Net debt / EBITDA", value=ratio, fmt="ratio",
                       threshold=f"≤ {ceiling:.2f}x", outcome=outcome_of(ratio, lambda v: v <= ceiling),
                       inputs={"total debt": debt, "cash + short-term investments": cash, "net debt": nd,
                               "EBITDA (TTM)": ebitda},
                       notes=_slack_note(slack))
    if ratio.is_nm:
        out.notes.append("n/m counts as failing the leverage screen")
    return out


# --------------------------------------------------------------------------
# 4. Share-count trend
# --------------------------------------------------------------------------
def share_trend_metric(trend: ShareTrend) -> MetricResult:
    if trend.trend_per_year is None:
        value = Datum.missing(trend.status)
    else:
        value = Datum(value=trend.trend_per_year, period_end=trend.span_end,
                      period_label=f"annualised over {trend.span_label}", notes=list(trend.notes))
    out = MetricResult(slot=SLOT_SHARES, name="Share-count trend (split-adjusted, latest segment)", value=value,
                       threshold=f"≤ {config.MAX_SHARE_GROWTH_PER_YEAR:+.1%}/yr",
                       outcome=outcome_of(value, lambda v: v <= config.MAX_SHARE_GROWTH_PER_YEAR),
                       notes=[*trend.flags, *trend.notes])
    if trend.span_label:
        out.notes.insert(0, f"span {trend.span_label} (source: {trend.source})")
    if dilution_flag(trend):
        out.notes.append(f"dilution flag: {trend.trend_per_year:+.1%}/yr > {config.DILUTION_FLAG_PER_YEAR:.0%}/yr")
    return out


# --------------------------------------------------------------------------
# 5. Sector-adjusted equivalents (Rule 5)
# --------------------------------------------------------------------------
def roe_spread_metric(roe: Datum) -> MetricResult:
    spread = roe.model_copy(update={"value": roe.value - config.COST_OF_CAPITAL}) if roe.ok else roe
    return MetricResult(slot=SLOT_FCF, name=f"ROE − cost of capital ({config.COST_OF_CAPITAL:.0%})", value=spread,
                        threshold=f"≥ {config.MIN_ROE_SPREAD:+.1%}",
                        outcome=outcome_of(spread, lambda v: v >= config.MIN_ROE_SPREAD), inputs={"ROE": roe})


def p_tbv_metric(p_tbv: Datum) -> MetricResult:
    return MetricResult(slot=SLOT_LEVERAGE, name="Price / tangible book (banks)", value=p_tbv, fmt="ratio",
                        threshold=f"≤ {config.MAX_P_TBV_BANK:.2f}x",
                        outcome=outcome_of(p_tbv, lambda v: v <= config.MAX_P_TBV_BANK))


def p_b_metric(mcap: Datum, equity: Datum) -> MetricResult:
    pb = safe_ratio(mcap, equity, name="P/B", nonpositive_reason="negative equity",
                    num_name="market cap", den_name="equity")
    return MetricResult(slot=SLOT_LEVERAGE, name="Price / book", value=pb, fmt="ratio",
                        threshold=f"≤ {config.MAX_P_B:.2f}x", outcome=outcome_of(pb, lambda v: v <= config.MAX_P_B),
                        inputs={"market cap": mcap, "equity": equity})


def ffo_yield_metric(ffo: Datum, mcap: Datum, rf: Datum) -> MetricResult:
    m = fcf_yield(ffo, None, mcap, rf)  # FFO has no SBC adjustment
    m.name = "FFO yield (1 / P-FFO, approximate) vs 10-year"
    m.inputs = {"FFO (TTM, approximate)": ffo, "market cap": mcap, "10-year yield": rf}
    return m


def not_applicable(slot: str, name: str, reason: str) -> MetricResult:
    return MetricResult(slot=slot, name=name, value=Datum.missing(f"N/A - {reason}"), outcome=NOT_APPLICABLE)


def earnings_yield_metric(ey: EarningsYield) -> MetricResult:
    return MetricResult(slot=SLOT_EARNINGS_YIELD, name="EV/EBIT earnings yield", value=ey.value,
                        threshold=f"≥ {config.MIN_EARNINGS_YIELD:.0%}",
                        outcome=outcome_of(ey.value, lambda v: v >= config.MIN_EARNINGS_YIELD),
                        inputs={"EV": ey.ev, "EBIT (TTM)": ey.ebit}, notes=list(ey.notes))

