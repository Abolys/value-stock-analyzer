"""Quantitative Fundamental lens (deterministic; SPEC "Score mapping — Quant").

Standard companies:
- Two-stage DCF from the 3-year average FCF (signals/dcf.py), growth = clamped
  revenue CAGR, discounted at COST_OF_CAPITAL. Implied upside → base score via
  QUANT_UPSIDE_BREAKPOINTS.
- ROIC vs the same COST_OF_CAPITAL: +1 when the spread ≥ ROIC_SPREAD_BONUS_THRESHOLD,
  −1 when ROIC is below it. ROIC n/m → return on total assets, labelled.
- Piotroski adjustment after the ROIC one: +0.5 at F ≥ PIOTROSKI_STRONG, −1 at
  F ≤ PIOTROSKI_WEAK, nothing when Piotroski is "Insufficient data".
- Peak-earnings cyclicals: the score uses the normalised DCF, both values are
  shown, and confidence is "low".
- Graham Number cross-check: disagreement on direction with the DCF → confidence
  "low"; never changes the score.
- Reverse DCF and the sensitivity range sit beside the score; they never change it.
- Structurally FCF-negative, or an unstable FCF base (Rule 2b): cash runway via
  RUNWAY_BREAKPOINTS, no ROIC adjustment, capped at RUNWAY_SCORE_CAP.
- Too little FCF history → "Insufficient data - too little history".

Financials (Rule 5, confirmed method): banks, insurers and other financials use
an excess-return shortcut, fair P/B = ROE / COST_OF_CAPITAL (zero growth),
fair value = fair P/B × book value per share; the ROE spread replaces the ROIC
adjustment. REITs run the same DCF on a TTM FFO base.
"""

from __future__ import annotations

import config
from analysis.fmt import d_pct, money, pct
from analysis.inputs import AnalysisInputs
from analysis.models import OK, QuantResult, insufficient
from data.values import Datum, nm
from screening.metrics import FCF_NEGATIVE, TOO_LITTLE_HISTORY, annual_fcf, cash_runway, fcf_negative_test, ttm_fcf
from signals import dcf
from signals.mapping import MappingStep, _clamp, mapped, mapping_line
from signals.returns import ffo, roe, roic
from signals.valuation import graham_number, liquid_cash

METHOD_DCF, METHOD_RUNWAY = "dcf", "runway"
METHOD_EXCESS_RETURN, METHOD_REIT = "excess_return", "reit_ffo_dcf"


# --------------------------------------------------------------------------
# Shared steps
# --------------------------------------------------------------------------
def returns_adjustment(value: Datum, label: str) -> MappingStep:
    """ROIC (or its substitute) vs COST_OF_CAPITAL → ±ROIC_SPREAD_BONUS."""
    coc = config.COST_OF_CAPITAL
    if not value.ok:
        return MappingStep(name=f"{label} vs cost of capital", input_display=value.status, kind="info",
                           note="no adjustment")
    spread = value.value - coc
    adj = 0.0
    if spread >= config.ROIC_SPREAD_BONUS_THRESHOLD:
        adj = config.ROIC_SPREAD_BONUS
    elif value.value < coc:
        adj = config.ROIC_SPREAD_PENALTY
    return MappingStep(name=f"{label} spread", input_display=f"{spread * 100:+.1f} pts ({value.value:.1%} vs {coc:.0%})",
                       output=adj, kind="adjust")


def piotroski_adjustment(x: AnalysisInputs) -> MappingStep:
    p = x.screen.piotroski
    if p is None or p.score is None:
        return MappingStep(name="Piotroski", input_display=p.display if p else "N/A", kind="info",
                           note="no adjustment")
    adj = 0.0
    if p.score >= config.PIOTROSKI_STRONG:
        adj = config.PIOTROSKI_ADJUSTMENT["strong"]
    elif p.score <= config.PIOTROSKI_WEAK:
        adj = config.PIOTROSKI_ADJUSTMENT["weak"]
    return MappingStep(name="Piotroski", input_display=f"{p.score} / 9", output=adj, kind="adjust")


def combine_steps(steps: list[MappingStep]) -> float | None:
    base = next((s.output for s in steps if s.kind == "base" and s.output is not None), None)
    if base is None:
        return None
    score = base + sum(s.output for s in steps if s.kind == "adjust" and s.output is not None)
    for s in steps:
        if s.kind == "cap" and s.output is not None:
            score = min(score, s.output)
    return round(_clamp(score), 2)


def _graham_cross_check(res: QuantResult, x: AnalysisInputs, upside: float | None) -> None:
    g = graham_number(x.input("EPS (TTM, diluted)"), x.input("book value per share"))
    res.graham = g
    price = x.price
    if not (g.ok and price.ok):
        res.key_figures["Graham Number"] = g.status if not g.ok else "N/A"
        return
    res.graham_upside = g.value / price.value - 1
    res.key_figures["Graham Number"] = f"{money(g.value)} ({pct(res.graham_upside)} vs price)"
    if upside is not None and (res.graham_upside > 0) != (upside > 0):
        res.confidence = config.QUANT_CONFIDENCE_LOW
        res.confidence_reasons.append(
            f"Graham Number ({pct(res.graham_upside)}) and the valuation ({pct(upside)}) disagree on direction")


def _finish(res: QuantResult, x: AnalysisInputs) -> QuantResult:
    res.score = combine_steps(res.mapping_steps) if res.status == OK else None
    if res.score is None and res.status == OK:
        res.status = insufficient("no base score")
    res.mapping_line = mapping_line(res.mapping_steps, res.score)
    res.fundamentals_as_of = x.screen.fundamentals_as_of
    res.stale, res.stale_label = x.screen.stale, x.screen.stale_label
    res.piotroski = x.screen.piotroski.display if x.screen.piotroski else "N/A"
    res.assumptions.setdefault("COST_OF_CAPITAL", config.COST_OF_CAPITAL)
    _bull_and_risk(res)
    res.rationale = rationale(res)
    return res


def _bull_and_risk(res: QuantResult) -> None:
    if not res.ok:
        res.bull_point, res.key_risk = "none (no quantitative score)", res.status
        return
    base = next(s for s in res.mapping_steps if s.kind == "base")
    adj = [s for s in res.mapping_steps if s.kind == "adjust" and s.output]
    positives = [s for s in adj if s.output > 0]
    negatives = [s for s in adj if s.output < 0]
    res.bull_point = base.line if (base.output or 0) >= config.QUANT_UPSIDE_BREAKPOINTS[1][1] else \
        (positives[0].line if positives else f"{base.line} (no clear bull point)")
    if negatives:
        res.key_risk = negatives[0].line
    elif res.confidence_reasons:
        res.key_risk = res.confidence_reasons[0]
    elif (base.output or 0) < config.QUANT_UPSIDE_BREAKPOINTS[1][1]:
        res.key_risk = base.line
    else:
        res.key_risk = f"valuation rests on {res.growth.detail if res.growth else 'the stage-1 growth assumption'}"


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
def _runway_path(res: QuantResult, x: AnalysisInputs, why: str) -> QuantResult:
    res.method = METHOD_RUNWAY
    runway = cash_runway(liquid_cash(x.f), ttm_fcf(x.f))
    res.runway_months = runway
    res.key_figures["Cash runway"] = f"{runway.value:.0f} months" if runway.ok else runway.status
    res.notes.append(f"{why}: cash runway replaces the DCF; no ROIC adjustment")
    if not runway.ok:
        res.status = insufficient(f"{why}; cash runway {runway.status}")
        return _finish(res, x)
    res.mapping_steps = [
        mapped("Cash runway", runway.value, config.RUNWAY_BREAKPOINTS, f"{runway.value:.0f} months", step=True),
        piotroski_adjustment(x),
        MappingStep(name="Runway cap", input_display="", output=config.RUNWAY_SCORE_CAP, kind="cap",
                    note="a cash-burning company can't score bullish on numbers alone"),
    ]
    res.completeness = "cash runway available (DCF not applicable)"
    _graham_cross_check(res, x, None)
    return _finish(res, x)


def _dcf_path(res: QuantResult, x: AnalysisInputs, base: dcf.DcfBase, method: str, returns: Datum,
              returns_label: str) -> QuantResult:
    f = x.f
    res.method, res.base = method, base
    res.growth = growth = dcf.revenue_cagr(f)
    shares, price = x.input("shares outstanding"), x.price
    cash = dcf.net_cash(f)
    inputs, status = dcf.build_inputs(base, growth, shares, cash, price, rate=config.COST_OF_CAPITAL)
    raw = dcf.run_dcf(inputs, status)
    scored, scored_inputs = raw, inputs
    res.peak = peak = dcf.peak_earnings(f, x.cyclicality.score) if method == METHOD_DCF else None
    if peak and peak.flagged and peak.normalised_base and inputs is not None:
        if peak.normalised_base > 0:
            scored_inputs = inputs.model_copy(update={"base": peak.normalised_base})
            scored = dcf.run_dcf(scored_inputs)
            res.dcf_raw = raw
            res.confidence = config.QUANT_CONFIDENCE_LOW
            res.confidence_reasons.append("possibly peak earnings: DCF base normalised to TTM revenue × average "
                                          "FCF margin")
        else:
            res.confidence = config.QUANT_CONFIDENCE_LOW
            res.confidence_reasons.append("possibly peak earnings; normalised base ≤ 0, raw DCF used")
    res.dcf = scored
    res.reverse_dcf = dcf.reverse_dcf(scored_inputs, growth.cagr, status)
    res.grid = dcf.sensitivity_grid(scored_inputs, status)
    res.returns, res.returns_label = returns, returns_label
    if scored_inputs is not None:
        res.assumptions.update(scored_inputs.assumptions)
        res.assumptions["growth_source"] = growth.detail
        res.assumptions["base_source"] = base.detail
    if not scored.ok:
        res.status = insufficient(scored.status)
        return _finish(res, x)
    res.key_figures.update({
        "Fair value (DCF)": money(scored.fair_value, x.info.get("currency")),
        "Implied upside": pct(scored.upside),
        "Price (actual latest)": money(price.value, x.info.get("currency")),
        "Fair-value range (central 3×3)": res.grid.range_display,
        "Reverse DCF": res.reverse_dcf.display,
        "Stage-1 growth": growth.detail,
        f"{returns_label}": d_pct(returns),
    })
    if res.dcf_raw is not None and res.dcf_raw.ok:
        res.key_figures["Fair value (raw, unnormalised)"] = money(res.dcf_raw.fair_value, x.info.get("currency"))
    label = "DCF upside" + (" (normalised)" if res.dcf_raw is not None else "")
    res.mapping_steps = [
        mapped(label, scored.upside, config.QUANT_UPSIDE_BREAKPOINTS, pct(scored.upside, digits=0)),
        returns_adjustment(returns, returns_label),
        piotroski_adjustment(x),
    ]
    _graham_cross_check(res, x, scored.upside)
    have = sum(d.ok for d in (returns,)) + (x.screen.piotroski is not None and x.screen.piotroski.score is not None)
    res.completeness = f"DCF complete; {have} of 2 adjustments available"
    return _finish(res, x)


def _excess_return_path(res: QuantResult, x: AnalysisInputs) -> QuantResult:
    res.method = METHOD_EXCESS_RETURN
    coc = config.COST_OF_CAPITAL
    roe_v = roe(x.f)
    bvps, price = x.input("book value per share"), x.price
    res.returns, res.returns_label = roe_v, "ROE"
    res.reverse_dcf = dcf.ReverseDcf(status=nm("not meaningful for financials (excess-return method)"))
    res.grid = dcf.SensitivityGrid(status=nm("not meaningful for financials (excess-return method)"))
    res.assumptions.update({"method": "excess-return shortcut: fair P/B = ROE / COST_OF_CAPITAL (zero growth)",
                            "COST_OF_CAPITAL": coc})
    res.notes.append(f"{x.route.label}: FCF-based DCF is not meaningful; fair value = ROE / cost of capital × "
                     "book value per share (zero-growth excess-return shortcut)")
    if not roe_v.ok:
        res.status = insufficient(f"ROE {roe_v.status}")
        return _finish(res, x)
    if not (bvps.ok and price.ok) or bvps.value <= 0:
        res.status = insufficient(f"book value per share {bvps.status if not bvps.ok else 'n/m - ≤ 0'}")
        return _finish(res, x)
    fair_pb = roe_v.value / coc
    fair = fair_pb * bvps.value
    upside = fair / price.value - 1
    res.dcf = dcf.DcfResult(fair_value=fair, upside=upside)
    res.key_figures.update({"Fair value (excess return)": money(fair, x.info.get("currency")),
                            "Fair P/B": f"{fair_pb:.2f}x", "Implied upside": pct(upside),
                            "Price (actual latest)": money(price.value, x.info.get("currency")),
                            "ROE": d_pct(roe_v), "Book value per share": money(bvps.value)})
    res.mapping_steps = [mapped("Excess-return upside", upside, config.QUANT_UPSIDE_BREAKPOINTS, pct(upside, digits=0)),
                         returns_adjustment(roe_v, "ROE"),
                         piotroski_adjustment(x)]
    _graham_cross_check(res, x, upside)
    res.completeness = "ROE and book value available (sector-adjusted method)"
    return _finish(res, x)


def quant_lens(x: AnalysisInputs) -> QuantResult:
    res = QuantResult()
    res.assumptions["COST_OF_CAPITAL"] = config.COST_OF_CAPITAL
    if x.route.sector_adjusted:
        if x.route.subsector == "reit":
            fo = ffo(x.f)
            base = dcf.DcfBase(value=fo.value, detail=f"TTM FFO (approximate) {fo.display()}") if fo.ok and fo.value > 0 \
                else dcf.DcfBase(status=insufficient(f"FFO {fo.status if not fo.ok else '≤ 0'}"))
            return _dcf_path(res, x, base, METHOD_REIT, roe(x.f), "ROE")
        return _excess_return_path(res, x)

    test = fcf_negative_test(annual_fcf(x.f))
    res.fcf_negative = test.status
    if test.status == TOO_LITTLE_HISTORY:
        res.status = insufficient(f"too little history ({test.detail})")
        res.method = METHOD_DCF
        return _finish(res, x)
    if test.status == FCF_NEGATIVE:
        return _runway_path(res, x, f"structurally FCF-negative ({test.detail})")
    base = dcf.dcf_base(x.f)
    if base.status == dcf.UNSTABLE_BASE:
        res.base = base
        res.notes.append(f"{base.status}: {base.detail}")
        return _runway_path(res, x, f"{base.status} ({base.detail})")
    if not base.ok:
        res.base = base
        res.status = insufficient(f"{base.status} ({base.detail})")
        return _finish(res, x)
    r = roic(x.f)
    label = "ROA (substitute: ROIC n/m)" if r.is_substitute else "ROIC"
    res.notes += [f"{label}: {n}" for n in r.notes]
    return _dcf_path(res, x, base, METHOD_DCF, r.value, label)


# --------------------------------------------------------------------------
def rationale(r: QuantResult) -> str:
    lines = [f"**Quantitative Fundamental — {r.display}**" + (f" · confidence {r.confidence}" if r.ok else ""),
             f"Mapping: {r.mapping_line}"]
    if r.stale:
        lines.append(f"⚠️ {r.stale_label}")
    for k, v in r.key_figures.items():
        lines.append(f"- {k}: {v}")
    if r.base is not None and r.base.detail:
        lines.append(f"- DCF base: {r.base.detail}")
    if r.peak is not None and r.peak.checked:
        lines.append(f"- Peak-earnings check: {r.peak.detail}")
    for c in r.confidence_reasons:
        lines.append(f"- Low confidence: {c}")
    lines.append(f"- Piotroski: {r.piotroski}")
    lines += [f"- Note: {n}" for n in r.notes]
    lines.append("- The reverse DCF and fair-value range are shown beside the score; they don't change it.")
    return "\n".join(lines)
