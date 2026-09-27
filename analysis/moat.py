"""Business Moat lens (LLM; SPEC "Score mapping — Business Moat").

Input: the business summary (third-party text, in a <business_summary> data
block), sector, industry, and the gross-margin and return-on-capital trends by
fiscal year. The prompt first identifies the single most relevant threat for
the sector (hint from INDUSTRY_THREAT_HINTS, then SECTOR_THREAT_HINTS), then
scores competitive advantage and pricing power against it, on MOAT_RUBRIC. The
response must cite at least LLM_MIN_EVIDENCE_FACTS payload facts.
"""

from __future__ import annotations

from typing import Any

import config
from analysis.fmt import d_pct
from analysis.inputs import AnalysisInputs
from analysis.models import EvidenceItem, MoatResult, insufficient
from data.periods import fiscal_year_label
from data.ratios import safe_ratio
from llm import prompts
from llm.client import LLMClient, evidence_problems
from llm.schemas import MoatResponse
from signals.mapping import MappingStep
from signals.returns import roa, roe, roe_fy, roic, roic_fy
from signals.trap_scores import _gross_margin

LENS = "moat"


def threat_hint(sector: str | None, industry: str | None) -> str:
    for prefix, hint in config.INDUSTRY_THREAT_HINTS.items():
        if industry and industry.startswith(prefix):
            return hint
    if sector and sector in config.SECTOR_THREAT_HINTS:
        return config.SECTOR_THREAT_HINTS[sector]
    return "none configured for this sector; identify the most relevant threat from the business summary"


def build_moat_payload(x: AnalysisInputs) -> dict[str, Any]:
    f, sa = x.f, x.route.sector_adjusted
    years = sorted(f.fiscal_year_ends())
    gross = {fiscal_year_label(d): d_pct(_gross_margin(f, d)) for d in years}
    if sa:
        returns_label = "roe_by_fy"
        returns = {fiscal_year_label(d): d_pct(roe_fy(f, d)) for d in years}
        returns_ttm = d_pct(roe(f))
    else:
        returns_label = "roic_by_fy"
        returns = {fiscal_year_label(d): d_pct(roic_fy(f, d)) for d in years}
        r = roic(f)
        returns_ttm = d_pct(r.value) + (" (ROA substitute: ROIC n/m)" if r.is_substitute else "")
    op_margin = safe_ratio(f.ttm("operating_income"), f.ttm("total_revenue"), name="operating margin",
                           nonpositive_reason="revenue ≤ 0")
    gm_ttm = safe_ratio(f.ttm("gross_profit"), f.ttm("total_revenue"), name="gross margin",
                        nonpositive_reason="revenue ≤ 0")
    payload = {
        "ticker": x.ticker, "company": x.company, "sector": x.route.sector or "N/A",
        "industry": x.route.industry or "N/A",
        "sector_threat_hint": threat_hint(x.route.sector, x.route.industry),
        "gross_margin_by_fy": gross or "N/A - no fiscal years", "gross_margin_ttm": d_pct(gm_ttm),
        "operating_margin_ttm": d_pct(op_margin),
        returns_label: returns or "N/A - no fiscal years",
        ("roe_ttm" if sa else "roic_ttm"): returns_ttm,
        "cost_of_capital": f"{config.COST_OF_CAPITAL:.1%}",
        "return_on_assets_ttm": d_pct(roa(f)),
        "fundamentals_as_of": x.screen.fundamentals_as_of.isoformat() if x.screen.fundamentals_as_of else "N/A",
        "stale": x.screen.stale,
    }
    if not x.business_summary:
        payload["business_summary"] = "N/A - no business summary from the provider"
    return payload


def moat_request(x: AnalysisInputs) -> tuple[dict[str, Any], str, str]:
    """(payload, system prompt, user message)."""
    payload = build_moat_payload(x)
    return payload, prompts.MOAT_SYSTEM, prompts.moat_user(payload, x.business_summary)


def moat_lens(x: AnalysisInputs, llm: LLMClient) -> MoatResult:
    payload, system, user = moat_request(x)
    version = prompts.PROMPT_VERSIONS[LENS]
    res = MoatResult(payload=payload, prompt_version=version, model=llm.model,
                     sector_threat_hint=payload["sector_threat_hint"],
                     fundamentals_as_of=x.screen.fundamentals_as_of, stale=x.screen.stale,
                     stale_label=x.screen.stale_label)
    out = llm.run(ticker=x.ticker, lens=LENS, prompt_version=version, system=system, user=user,
                  schema=MoatResponse,
                  cache_key=prompts.payload_json(payload) + "\x00" + x.business_summary,
                  validate=lambda r: evidence_problems(r.evidence, payload))
    res.cache_hit, res.cost, res.list_price_cost = out.cache_hit, out.cost, out.list_price_cost
    res.input_tokens, res.output_tokens = out.input_tokens, out.output_tokens
    res.assumptions = {"rubric": config.MOAT_RUBRIC, "prompt_version": version, "model": llm.model,
                       "backend": llm.backend,
                       "min_evidence_facts": config.LLM_MIN_EVIDENCE_FACTS}
    if not out.ok:
        res.status = insufficient(out.status)
        res.completeness = "no validated LLM response"
        res.bull_point, res.key_risk = "none (no moat score)", res.status
        res.mapping_line = f"Moat rubric: {res.status}"
        res.rationale = f"**Business Moat — {res.status}**"
        return res
    r = MoatResponse.model_validate(out.data)
    res.score = round(r.score, 2)
    res.sector_threat, res.threat_reasoning = r.sector_threat, r.threat_reasoning
    res.advantages, res.pricing_power = r.advantages, r.pricing_power
    res.evidence = [EvidenceItem(**e.model_dump()) for e in r.evidence]
    res.bull_point, res.key_risk = r.strongest_bull_point, r.biggest_risk
    res.mapping_steps = [MappingStep(name="Moat rubric", input_display=f"threat: {r.sector_threat}",
                                     output=res.score, kind="base",
                                     note=f"{len(res.evidence)} payload facts cited")]
    res.mapping_line = (f"Moat rubric vs threat '{r.sector_threat}' → {res.score:.1f} "
                        f"(evidence: {', '.join(e.field for e in res.evidence)}); score {res.score:.1f}")
    missing = [k for k, v in payload.items() if isinstance(v, str) and v.startswith(("N/A", "n/m"))]
    res.completeness = "all payload fields available" if not missing else f"gaps: {', '.join(missing)}"
    res.key_figures = {"Sector threat": r.sector_threat, "Pricing power": r.pricing_power,
                       "Gross margin (TTM)": payload["gross_margin_ttm"]}
    lines = [f"**Business Moat — {res.display}** (threat: {r.sector_threat})", f"Mapping: {res.mapping_line}",
             r.rationale, f"- Why this threat: {r.threat_reasoning}",
             f"- Advantages: {', '.join(r.advantages) or 'none identified'}", f"- Pricing power: {r.pricing_power}",
             *[f"- Evidence: {e.field} = {e.value} — {e.why}" for e in res.evidence]]
    if res.stale:
        lines.insert(1, f"⚠️ {res.stale_label}")
    res.rationale = "\n".join(lines)
    return res
