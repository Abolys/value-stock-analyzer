"""Devil's Advocate lens (LLM; SPEC "Score mapping — Devil's Advocate").

Runs after Quant, Macro and Moat. The payload is compact JSON built only from
their structured fields and the signals: every value as a short display
string, or its N/A / n/m reason, or its coverage label, never a full report
and never markdown. It adds one line per lens with the strongest bull point and
the biggest risk already found, each n/m with its reason (a negative
denominator is usually a red flag in itself), and the stale / mixed-period
markers. The score is how well the bull case survives the attack (DA_RUBRIC).
"""

from __future__ import annotations

from typing import Any

import config
from analysis.fmt import d_pct, d_x, human, is_mixed, pct
from analysis.inputs import AnalysisInputs
from analysis.models import DevilsAdvocateResult, EvidenceItem, MacroResult, MoatResult, QuantResult, insufficient
from data.values import Datum, nm
from llm import prompts
from llm.client import LLMClient, evidence_problems
from llm.schemas import DevilsAdvocateResponse
from screening.metrics import cash_runway, ttm_fcf
from signals.mapping import MappingStep
from signals.valuation import liquid_cash

LENS = "devils_advocate"


def _money(v: float | None, cur: str | None) -> str:
    return "N/A" if v is None else f"{cur + ' ' if cur else ''}{v:,.2f}"


def _lens_line(r: Any) -> dict[str, Any]:
    if r is None:
        return {"score": "N/A - lens did not run", "bull": "N/A", "risk": "N/A"}
    return {"score": round(r.score, 1) if r.ok else r.status, "bull": r.bull_point, "risk": r.key_risk}


def _dt(d: Datum, fmt: str = "num") -> str:
    if not d.ok:
        return d.status
    if fmt == "pct":
        return f"{d.value:.1%}"
    if fmt == "x":
        return f"{d.value:.2f}x"
    return human(d.value)


def _piotroski_accrual(x: AnalysisInputs) -> str:
    p = x.screen.piotroski
    if p is None:
        return "N/A - Data Incomplete"
    c = next((c for c in p.checks if c.name == "operating cash flow > net income"), None)
    if c is None:
        return p.status if p.status != "ok" else "N/A - Data Incomplete"
    return f"{c.outcome}" + (f" ({c.detail})" if c.detail else "")


def _leadership(x: AnalysisInputs) -> dict[str, Any]:
    lead = x.leadership
    if lead is None:
        return {"flag": "N/A - no leadership data source", "departures": None, "coverage": "none",
                "status_rule": "unknown"}
    out: dict[str, Any] = {"flag": lead.flag, "departures": lead.departures,
                           "coverage": lead.coverage_label or "none", "partial_coverage": lead.partial_coverage}
    if lead.events:
        out["events"] = [f"{e.date} {e.role} {e.person or '(name not parsed)'} ({', '.join(e.sources) or e.layer})"
                         for e in lead.events]
    if lead.unconfirmed_candidates:
        out["unconfirmed_6k_hits"] = len(lead.unconfirmed_candidates)
    # The rule the attack must respect: partial or no coverage is "unknown", never "clean".
    out["status_rule"] = "unknown" if (lead.partial_coverage or lead.flag.startswith("N/A")) else "full coverage"
    return out


def _insiders(x: AnalysisInputs) -> dict[str, Any] | str:
    s = x.insiders
    if not s.available:
        return s.coverage
    return {"buyers": s.buyers, "sellers": s.sellers_discretionary, "sellers_10b5_1": s.sellers_10b5_1,
            "cluster_buy": s.cluster_buy, "net_value": round(s.net_value), "coverage": s.coverage}


def _dividend(x: AnalysisInputs) -> dict[str, Any] | str:
    d = x.dividends
    if not d.payer:
        return d.status
    return {"trailing_yield": _dt(d.trailing_yield, "pct"), "fcf_payout": _dt(d.fcf_payout, "pct"),
            "earnings_payout": _dt(d.earnings_payout, "pct"), "at_risk": d.at_risk,
            "at_risk_reason": d.at_risk_reason or "none", "cuts": len(d.cuts),
            "cut_years": [c.year for c in d.cuts][-5:],
            "uninterrupted_years": d.uninterrupted_years}


def _asset_floor(x: AnalysisInputs) -> dict[str, Any] | str:
    af = x.screen.asset_floor
    if af is None:
        return "N/A - Data Incomplete"
    mcap = x.market_cap

    def to_mcap(v: Datum) -> str:
        if not v.ok:
            return v.status
        if not mcap.ok:
            return mcap.status
        return pct(v.value / mcap.value - 1, digits=0)

    cov = f"{af.coverage.value:.0%} ({af.coverage_band})" if af.coverage.ok else f"{af.coverage.status} ({af.coverage_band})"
    out: dict[str, Any] = {"tbv": _dt(af.tbv), "p_tbv": _dt(af.p_tbv, "x"), "coverage": cov,
                           "ncav_to_mcap": to_mcap(af.ncav), "nnwc_to_mcap": to_mcap(af.nnwc), "net_net": af.net_net}
    if af.burn_line:
        out["net_net_burn"] = af.burn_line
    return out


def build_da_payload(x: AnalysisInputs, quant: QuantResult | None, macro: MacroResult | None,
                     moat: MoatResult | None) -> dict[str, Any]:
    s = x.screen
    cur = x.info.get("currency")
    q = quant
    nm_reasons: dict[str, str] = {}
    mixed: list[str] = []

    def track(name: str, d: Datum) -> None:
        if d.is_nm:
            nm_reasons[name] = d.status
        if is_mixed(d):
            mixed.append(name)

    # Valuation (Quant)
    if q is not None and q.dcf is not None and q.dcf.ok:
        upside = pct(q.dcf.upside, digits=0)
        fair = _money(q.dcf.fair_value, cur)
    else:
        upside = q.status if q is not None and not q.ok else ("N/A - " + (q.method if q else "Quant did not run"))
        fair = upside
    method = {"dcf": "two-stage DCF on the average FCF of the last fiscal years",
              "runway": "cash runway (DCF skipped: FCF-negative or unstable FCF base)",
              "excess_return": "excess-return shortcut: fair P/B = ROE / cost of capital",
              "reit_ffo_dcf": "two-stage DCF on TTM FFO"}.get(q.method if q else "", "N/A")
    if x.route.sector_adjusted:
        runway = Datum.missing(nm("not meaningful for financials and REITs"))
    else:
        runway = q.runway_months if q is not None and q.runway_months.ok else cash_runway(liquid_cash(x.f), ttm_fcf(x.f))
    returns = q.returns if q is not None else Datum.missing()
    track("roic_vs_cost_of_capital", returns)
    track("cash_runway_months", runway)
    lev = macro.net_debt_ebitda if macro is not None else Datum.missing()
    cov = macro.interest_coverage if macro is not None else Datum.missing()
    track("net_debt_ebitda", lev)
    track("interest_coverage", cov)
    ey = s.earnings_yield.value if s.earnings_yield else Datum.missing()
    track("ev_ebit_yield", ey)
    for name, d in s.inputs.items():
        if is_mixed(d):
            mixed.append(name)
    for m in s.metrics:
        if is_mixed(m.value):
            mixed.append(m.name)
    af = s.asset_floor
    if af is not None:
        for name, d in (("asset_floor.p_tbv", af.p_tbv), ("asset_floor.coverage", af.coverage),
                        ("asset_floor.ncav", af.ncav), ("asset_floor.nnwc", af.nnwc)):
            track(name, d)
    for name, d in (("piotroski", s.piotroski), ("altman_z", s.altman), ("beneish_m", s.beneish)):
        st = d.status if d is not None else ""
        if st.startswith("n/m"):
            nm_reasons[name] = st

    peak = q.peak if q is not None else None
    payload: dict[str, Any] = {
        "ticker": x.ticker, "company": x.company, "sector": x.route.sector or "N/A",
        "industry": x.route.industry or "N/A", "treatment": x.route.label,
        "price": f"{_money(x.price.value, cur)} (actual latest, {x.price.period_end or 'N/A'})",
        "valuation_method": method,
        "dcf_implied_upside": upside,
        "fair_value": fair,
        "fair_value_range": q.grid.range_display if q is not None and q.grid is not None else "N/A",
        "implied_growth": q.reverse_dcf.display if q is not None and q.reverse_dcf is not None else "N/A",
        "cash_runway_months": round(runway.value) if runway.ok else runway.status,
        "roic_vs_cost_of_capital": (f"{returns.value:.1%} vs {config.COST_OF_CAPITAL:.0%}" if returns.ok
                                    else returns.status) + (f" ({q.returns_label})" if q and q.returns_label != "ROIC" else ""),
        "graham_number": (f"{_money(q.graham.value, cur)} ({pct(q.graham_upside, digits=0)} vs price)"
                          if q is not None and q.graham.ok else (q.graham.status if q else "N/A")),
        "quant_confidence": (q.confidence + (f" ({'; '.join(q.confidence_reasons)})" if q.confidence_reasons else "")
                             ) if q is not None else "N/A",
        "peak_earnings": (peak.flagged if peak is not None and peak.checked else False),
        "net_debt_ebitda": d_x(lev),
        "interest_coverage": d_x(cov),
        "leverage_trend": macro.leverage_trend if macro is not None else "N/A",
        "debt_maturity_proxy": macro.key_figures.get("Debt maturity (proxy)", "N/A") if macro is not None else "N/A",
        "cyclicality": x.cyclicality.detail,
        "moat_threat": (moat.sector_threat if moat is not None and moat.ok else
                        (moat.status if moat is not None else "N/A - Moat lens did not run")),
        "piotroski": s.piotroski.display if s.piotroski else "N/A",
        "piotroski_accrual_check": _piotroski_accrual(x),
        "altman_zone": (s.altman.zone if s.altman and s.altman.zone else (s.altman.status if s.altman else "N/A")),
        "altman_z": s.altman.display if s.altman else "N/A",
        "beneish_flag": (s.beneish.flag if s.beneish and s.beneish.m.ok else (s.beneish.status if s.beneish else "N/A")),
        "beneish_m": s.beneish.display if s.beneish else "N/A",
        "ev_ebit_yield": d_pct(ey) + (" (net cash exceeds market cap)" if s.net_cash_flag else ""),
        "share_count_trend": (f"{s.metric('shares').display} over {s.share_trend_span}"
                              if s.metric("shares") is not None and s.metric("shares").value.ok
                              else (s.metric("shares").value.status if s.metric("shares") else "N/A")),
        "dilution_flag": s.dilution_flag,
        "insiders_6mo": _insiders(x),
        "dividend": _dividend(x),
        "asset_floor": _asset_floor(x),
        "insider_ownership": d_pct(x.context.insider_ownership),
        "short_interest": (f"{x.context.short_interest.value:.1%} of float" if x.context.short_interest.ok
                           else x.context.short_interest.status),
        "estimate_revisions_90d": x.context.revisions_90d,
        "leadership": _leadership(x),
        "fundamentals_as_of": s.fundamentals_as_of.isoformat() if s.fundamentals_as_of else "N/A",
        "stale": s.stale,
        "mixed_periods": sorted(set(mixed)),
        "not_meaningful": nm_reasons,
        "lenses": {"quant": _lens_line(quant), "macro": _lens_line(macro), "moat": _lens_line(moat)},
    }
    if s.stale:
        payload["stale_detail"] = s.stale_label
    return payload


def da_problems(r: DevilsAdvocateResponse, payload: dict[str, Any]) -> list[str]:
    return evidence_problems(r.evidence, payload)


def enforce_leadership_coverage(res: DevilsAdvocateResult, payload: dict[str, Any]) -> None:
    """Partial or missing coverage is "unknown", never clean (SPEC "Leadership-turnover flag").
    Enforced deterministically on the result, whatever the model answered — no extra LLM call."""
    lead = payload["leadership"]
    if lead.get("status_rule") != "unknown" or res.leadership_status != "none found":
        return
    res.leadership_status = "unknown"
    res.leadership_turnover = (f"Unknown: coverage is partial or missing ({lead.get('coverage') or 'none'}), so "
                               f"\"no departures found\" can't be read as clean. Model's note: {res.leadership_turnover}")
    res.notes.append("leadership status corrected from 'none found' to 'unknown' (partial or missing coverage)")


def devils_advocate_lens(x: AnalysisInputs, quant: QuantResult | None, macro: MacroResult | None,
                         moat: MoatResult | None, llm: LLMClient) -> DevilsAdvocateResult:
    payload = build_da_payload(x, quant, macro, moat)
    version = prompts.PROMPT_VERSIONS[LENS]
    res = DevilsAdvocateResult(payload=payload, prompt_version=version, model=llm.model,
                               fundamentals_as_of=x.screen.fundamentals_as_of, stale=x.screen.stale,
                               stale_label=x.screen.stale_label)
    out = llm.run(ticker=x.ticker, lens=LENS, prompt_version=version, system=prompts.DA_SYSTEM,
                  user=prompts.da_user(payload), schema=DevilsAdvocateResponse,
                  cache_key=prompts.payload_json(payload), validate=lambda r: da_problems(r, payload))
    res.cache_hit, res.cost, res.list_price_cost = out.cache_hit, out.cost, out.list_price_cost
    res.input_tokens, res.output_tokens = out.input_tokens, out.output_tokens
    res.assumptions = {"rubric": config.DA_RUBRIC, "prompt_version": version, "model": llm.model,
                       "backend": llm.backend,
                       "min_evidence_facts": config.LLM_MIN_EVIDENCE_FACTS,
                       "convention": "score = how well the bull case survives the attack"}
    if not out.ok:
        res.status = insufficient(out.status)
        res.completeness = "no validated LLM response"
        res.bull_point, res.key_risk = "none (no Devil's Advocate score)", res.status
        res.mapping_line = f"Devil's Advocate rubric: {res.status}"
        res.rationale = f"**Devil's Advocate — {res.status}**"
        return res
    r = DevilsAdvocateResponse.model_validate(out.data)
    res.score = round(r.score, 2)
    for field in ("weakest_valuation_assumption", "weakest_moat_point", "accounting_red_flags", "implied_growth_view",
                  "implied_growth_reasoning", "insider_activity", "dividend_risk", "asset_floor",
                  "leadership_turnover", "leadership_status", "impairment_type", "impairment_reasoning",
                  "data_freshness", "bull_case_requirements"):
        setattr(res, field, getattr(r, field))
    enforce_leadership_coverage(res, payload)
    res.evidence = [EvidenceItem(**e.model_dump()) for e in r.evidence]
    res.bull_point = "; ".join(r.bull_case_requirements[:1]) or "none"
    res.key_risk = r.weakest_valuation_assumption
    res.mapping_steps = [MappingStep(name="Devil's Advocate rubric", input_display=f"impairment: {r.impairment_type}",
                                     output=res.score, kind="base", note=f"{len(res.evidence)} payload facts cited")]
    res.mapping_line = (f"Bull case survives the attack (impairment: {r.impairment_type}) → {res.score:.1f} "
                        f"(evidence: {', '.join(e.field for e in res.evidence)}); score {res.score:.1f}")
    gaps = [k for k, v in payload.items() if isinstance(v, str) and v.startswith(("N/A", "n/m", "Insufficient"))]
    res.completeness = "all payload fields available" if not gaps else f"gaps: {', '.join(gaps)}"
    res.key_figures = {"Impairment": r.impairment_type, "Implied growth view": r.implied_growth_view,
                       "Leadership": res.leadership_status}
    lines = [f"**Devil's Advocate — {res.display}** (impairment looks {r.impairment_type})",
             f"Mapping: {res.mapping_line}", r.rationale,
             f"- Assumption most likely wrong: {r.weakest_valuation_assumption}",
             f"- Weakest moat point: {r.weakest_moat_point}",
             f"- Accounting red flags: {r.accounting_red_flags}",
             f"- Implied growth: {r.implied_growth_view} — {r.implied_growth_reasoning}",
             f"- Insiders: {r.insider_activity}", f"- Dividend: {r.dividend_risk}",
             f"- Asset floor: {r.asset_floor}",
             f"- Leadership turnover ({res.leadership_status}): {res.leadership_turnover}",
             f"- Structural or cyclical: {r.impairment_type} — {r.impairment_reasoning}",
             f"- Data freshness: {r.data_freshness}",
             "- For the bull case to hold: " + "; ".join(r.bull_case_requirements),
             *[f"- Evidence: {e.field} = {e.value} — {e.why}" for e in res.evidence],
             *[f"- Note: {n}" for n in res.notes]]
    if res.stale:
        lines.insert(1, f"⚠️ {res.stale_label}")
    res.rationale = "\n".join(lines)
    return res
