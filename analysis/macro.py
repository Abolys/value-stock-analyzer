"""Macro & Balance Sheet Risk lens (deterministic; SPEC "Score mapping — Macro").

The score is the average of the sub-scores that have data (the ones used are reported):
- Net debt / EBITDA via NET_DEBT_EBITDA_BREAKPOINTS when EBITDA > 0. Rule 2b:
  EBITDA ≤ 0 with net debt → 1; EBITDA ≤ 0 with net cash → scored from cash
  runway (RUNWAY_BREAKPOINTS), so a negative ratio can never map to 10.
- Interest coverage (EBIT / interest expense) via INTEREST_COVERAGE_BREAKPOINTS.
  EBIT ≤ 0 → 1 whatever the interest; no interest expense → 10 only when EBIT > 0.
- Altman Z'' via ALTMAN_BREAKPOINTS (n/m for financials and REITs).
- Leverage trend over the available fiscal years (LEVERAGE_TREND_SCORES; ±LEVERAGE_TREND_FLAT_BAND is flat;
  financials: liabilities / equity with ±LEVERAGE_TREND_FLAT_BAND_FINANCIALS).
- Cyclicality from SECTOR_CYCLICALITY with INDUSTRY_CYCLICALITY_OVERRIDES.

Debt maturity: free sources give no maturity schedule, so the current vs
long-term debt split is shown as a proxy and labelled as such.

Financials and REITs: net debt/EBITDA and interest coverage are N/A; the lens
uses the leverage trend (liabilities / equity, its own flat band) and
cyclicality only, and is marked "reduced data".
"""

from __future__ import annotations

from datetime import date

import config
from analysis.fmt import d_num, d_x
from analysis.inputs import AnalysisInputs
from analysis.models import MacroResult, insufficient
from data.fundamentals import Fundamentals
from data.ratios import safe_ratio, sum_datums
from data.values import Datum, nm
from screening.metrics import cash_runway, net_debt, net_debt_to_ebitda, ttm_fcf
from signals.mapping import MappingStep, mapped, mapping_line
from signals.valuation import liquid_cash, ttm_ebit

SUBSCORES = ["net debt / EBITDA", "interest coverage", "Altman Z''", "leverage trend", "cyclicality"]
MATURITY_NOTE = ("Maturity proxy: current vs long-term debt split (free sources give no reliable maturity "
                 "schedule; no specific maturity dates are shown)")
SA_NA = "N/A - sector-adjusted (not meaningful for financials and REITs)"


def _na(name: str, status: str) -> MappingStep:
    return MappingStep(name=name, input_display=status, kind="info", note="excluded from the average")


# --------------------------------------------------------------------------
def leverage_subscore(x: AnalysisInputs) -> tuple[MappingStep, Datum]:
    f = x.f
    cash, debt, ebitda = liquid_cash(f), f.bal("total_debt"), f.ttm("ebitda")
    nd = net_debt(debt, cash)
    ratio = net_debt_to_ebitda(nd, ebitda)
    name = "Net debt / EBITDA"
    if ratio.ok:
        return mapped(name, ratio.value, config.NET_DEBT_EBITDA_BREAKPOINTS, f"{ratio.value:.2f}x", kind="subscore"), ratio
    if ratio.is_nm and nd.ok and nd.value > 0:
        return MappingStep(name=name, input_display=f"{ratio.status} (net debt {nd.value:,.4g})",
                           output=config.SCORE_MIN, kind="subscore", note="Rule 2b: negative EBITDA with net debt"), ratio
    if ratio.is_nm:
        runway = cash_runway(cash, ttm_fcf(f))
        if runway.ok:
            step = mapped("Leverage from cash runway", runway.value, config.RUNWAY_BREAKPOINTS,
                          f"{runway.value:.0f} months ({ratio.status})", kind="subscore", step=True)
            step.note = "Rule 2b: negative EBITDA with net cash is scored from cash runway, never from the ratio"
            return step, ratio
        if runway.is_nm:
            top = max(s for _, s in config.RUNWAY_BREAKPOINTS)
            return MappingStep(name="Leverage from cash runway", input_display=f"{runway.status} ({ratio.status})",
                               output=top, kind="subscore",
                               note="not burning cash → top of RUNWAY_BREAKPOINTS"), ratio
        return _na(name, f"{ratio.status}; cash runway {runway.status}"), ratio
    return _na(name, ratio.status), ratio


def coverage_subscore(x: AnalysisInputs) -> tuple[MappingStep, Datum]:
    f = x.f
    ebit, interest = ttm_ebit(f), f.ttm("interest_expense")
    name = "Interest coverage"
    if not ebit.ok:
        return _na(name, f"EBIT {ebit.status}"), Datum.missing(ebit.status)
    if ebit.value <= 0:
        return MappingStep(name=name, input_display=f"EBIT {ebit.value:,.4g} ≤ 0", output=config.EBIT_NONPOSITIVE_COVERAGE_SCORE,
                           kind="subscore", note="Rule 2b: EBIT ≤ 0 scores 1 regardless of interest expense"), \
            Datum.missing(nm("EBIT ≤ 0"))
    no_interest = interest.ok and interest.value == 0
    if not interest.ok:
        debt = f.bal("total_debt")
        no_interest = debt.ok and debt.value == 0
        if not no_interest:
            return _na(name, f"interest expense {interest.status}"), Datum.missing(interest.status)
    if no_interest:
        return MappingStep(name=name, input_display="no interest expense (EBIT > 0)",
                           output=config.NO_INTEREST_EXPENSE_SCORE, kind="subscore"), Datum.missing(nm("no interest expense"))
    cov = safe_ratio(ebit, interest.model_copy(update={"value": abs(interest.value)}), name="interest coverage",
                     nonpositive_reason="no interest expense", num_name="EBIT", den_name="interest expense")
    return mapped(name, cov.value, config.INTEREST_COVERAGE_BREAKPOINTS, f"{cov.value:.1f}x", kind="subscore"), cov


def altman_subscore(x: AnalysisInputs) -> MappingStep:
    a = x.screen.altman
    if a is None or not a.z.ok:
        return _na("Altman Z''", a.status if a else "N/A - Data Incomplete")
    return mapped("Altman Z''", a.z.value, config.ALTMAN_BREAKPOINTS, f"{a.z.value:.2f} ({a.zone})", kind="subscore")


def _fy_cash(f: Fundamentals, d: date) -> Datum:
    combined = f.fy("cash_and_short_term_investments", d)
    if combined.ok:
        return combined
    cash, sti = f.fy("cash_and_equivalents", d), f.fy("short_term_investments", d)
    return sum_datums({"cash": cash, "sti": sti}, "cash + sti") if sti.ok else cash


def leverage_by_year(f: Fundamentals, sector_adjusted: bool) -> dict[str, float]:
    """Per fiscal year: net debt / EBITDA, or liabilities / equity for financials (oldest first)."""
    out: dict[str, float] = {}
    for d in sorted(f.fiscal_year_ends()):
        if sector_adjusted:
            r = safe_ratio(f.fy("total_liabilities", d), f.fy("stockholders_equity", d), name="L/E",
                           nonpositive_reason="negative equity")
        else:
            nd = sum_datums({"debt": f.fy("total_debt", d), "cash": _fy_cash(f, d)}, "net debt", signs={"cash": -1})
            r = net_debt_to_ebitda(nd, f.fy("ebitda", d))
        if r.ok:
            out[f.fy("total_assets", d).period_label or d.isoformat()] = r.value
    return out


def trend_subscore(x: AnalysisInputs) -> tuple[MappingStep, str, dict[str, float]]:
    sa = x.route.sector_adjusted
    values = leverage_by_year(x.f, sa)
    what = "liabilities / equity (financials)" if sa else "net debt / EBITDA"
    name = f"Leverage trend ({what})"
    if len(values) < 2:
        return _na(name, f"N/A - {len(values)} fiscal year(s) with a usable ratio; 2 needed"), "N/A", values
    first, last = list(values.values())[0], list(values.values())[-1]
    change = last - first
    band = config.LEVERAGE_TREND_FLAT_BAND_FINANCIALS if sa else config.LEVERAGE_TREND_FLAT_BAND
    direction = "falling" if change < -band else "rising" if change > band else "flat"
    shown = " → ".join(f"{v:.2f}x" for v in values.values())
    return MappingStep(name=name, input_display=f"{direction} ({shown}; ±{band}x is flat)",
                       output=config.LEVERAGE_TREND_SCORES[direction], kind="subscore"), direction, values


def macro_lens(x: AnalysisInputs) -> MacroResult:
    sa = x.route.sector_adjusted
    res = MacroResult(reduced_data=sa)
    steps: list[MappingStep] = []
    if sa:
        steps += [_na("Net debt / EBITDA", SA_NA), _na("Interest coverage", SA_NA)]
        res.net_debt_ebitda = Datum.missing(nm("not meaningful for financials and REITs"))
        res.interest_coverage = Datum.missing(nm("not meaningful for financials and REITs"))
    else:
        lev, res.net_debt_ebitda = leverage_subscore(x)
        cov, res.interest_coverage = coverage_subscore(x)
        steps += [lev, cov]
    steps.append(altman_subscore(x))
    if x.screen.altman is not None:
        res.altman_z, res.altman_zone = x.screen.altman.z, x.screen.altman.zone
    trend, res.leverage_trend, res.leverage_trend_values = trend_subscore(x)
    steps.append(trend)
    res.cyclicality = x.cyclicality
    steps.append(MappingStep(name="Cyclicality", input_display=x.cyclicality.detail, output=x.cyclicality.score,
                             kind="subscore"))
    res.mapping_steps = steps

    used = [s for s in steps if s.kind == "subscore" and s.output is not None]
    res.subscores_used, res.subscores_total = len(used), len(SUBSCORES)
    missing = [f"{s.name}: {s.input_display}" for s in steps if s.kind == "info"]
    res.completeness = f"{len(used)} of {len(SUBSCORES)} sub-scores" + (" (reduced data: sector-adjusted)" if sa else "") \
        + (f"; excluded: {'; '.join(missing)}" if missing else "")
    if used:
        res.score = round(sum(s.output for s in used) / len(used), 2)
    else:
        res.status = insufficient("no sub-score has data")
    res.mapping_line = mapping_line(steps, res.score).replace("; score", f"; average of {len(used)} → score")

    f = x.f
    res.current_debt, res.long_term_debt = f.bal("current_debt"), f.bal("long_term_debt")
    cur, lt = res.current_debt, res.long_term_debt
    split = (f"current {d_num(cur)} vs long-term {d_num(lt)}"
             + (f" ({cur.value / (cur.value + lt.value):.0%} due within a year)"
                if cur.ok and lt.ok and cur.value + lt.value > 0 else ""))
    res.maturity_note = f"{MATURITY_NOTE}: {split}"
    res.key_figures = {"Net debt / EBITDA": d_x(res.net_debt_ebitda),
                       "Interest coverage": d_x(res.interest_coverage),
                       "Altman Z''": x.screen.altman.display if x.screen.altman else "N/A",
                       "Leverage trend": res.leverage_trend, "Cyclicality": x.cyclicality.detail,
                       "Debt maturity (proxy)": split}
    res.assumptions = {"NET_DEBT_EBITDA_BREAKPOINTS": config.NET_DEBT_EBITDA_BREAKPOINTS,
                       "INTEREST_COVERAGE_BREAKPOINTS": config.INTEREST_COVERAGE_BREAKPOINTS,
                       "ALTMAN_BREAKPOINTS": config.ALTMAN_BREAKPOINTS,
                       "LEVERAGE_TREND_FLAT_BAND": (config.LEVERAGE_TREND_FLAT_BAND_FINANCIALS if sa
                                                    else config.LEVERAGE_TREND_FLAT_BAND),
                       "cyclicality source": x.cyclicality.source}
    res.fundamentals_as_of = x.screen.fundamentals_as_of
    res.stale, res.stale_label = x.screen.stale, x.screen.stale_label
    if x.cyclicality.source == "default":
        res.notes.append(f"cyclicality: {x.cyclicality.detail}")
    if used:
        best, worst = max(used, key=lambda s: s.output), min(used, key=lambda s: s.output)
        res.bull_point, res.key_risk = best.line, worst.line
    else:
        res.bull_point, res.key_risk = "none (no macro score)", res.status
    res.rationale = rationale(res)
    return res


def rationale(r: MacroResult) -> str:
    lines = [f"**Macro & Balance Sheet Risk — {r.display}** ({r.completeness})", f"Mapping: {r.mapping_line}"]
    if r.stale:
        lines.append(f"⚠️ {r.stale_label}")
    lines += [f"- {k}: {v}" for k, v in r.key_figures.items()]
    lines.append(f"- {r.maturity_note}")
    lines += [f"- Note: {n}" for n in r.notes]
    return "\n".join(lines)
