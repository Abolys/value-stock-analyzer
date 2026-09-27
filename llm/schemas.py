"""Pydantic response schemas for the LLM calls (CLAUDE.md Rule 4). The JSON
schema sent as output_config.format is derived from these models, and every
response is validated against them before use."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

import config


class Evidence(BaseModel):
    """A specific fact from the payload that justifies the score (SPEC evidence rule)."""

    field: str = Field(description="The payload field name the fact comes from, exactly as it appears in the "
                                   "payload (use dotted paths for nested fields, e.g. 'dividend.fcf_payout').")
    value: str = Field(description="The value of that field as it appears in the payload.")
    why: str = Field(description="One sentence: why this fact supports the score.")


class MoatResponse(BaseModel):
    sector_threat: str = Field(description="The single most relevant threat to this business's competitive "
                                           "position, named in a few words.")
    threat_reasoning: str = Field(description="Why this threat, and not another, matters most for this business.")
    advantages: list[str] = Field(description="The competitive advantages the business has, if any.")
    pricing_power: str = Field(description="Assessment of pricing power against the named threat.")
    score: float = Field(ge=config.SCORE_MIN, le=config.SCORE_MAX,
                         description="1-10 against the rubric; values between anchors are allowed.")
    evidence: list[Evidence] = Field(description="At least 2 specific facts from the payload that justify the score.")
    strongest_bull_point: str = Field(description="One line: the strongest point for the moat.")
    biggest_risk: str = Field(description="One line: the biggest risk to the moat.")
    rationale: str = Field(description="A short paragraph explaining the score.")


class DevilsAdvocateResponse(BaseModel):
    weakest_valuation_assumption: str = Field(
        description="The single DCF or cash-runway assumption most likely to be wrong, and why.")
    weakest_moat_point: str = Field(description="The weakest part of the moat argument.")
    accounting_red_flags: str = Field(
        description="Accrual quality and cash-conversion concerns, citing the Beneish flag and Piotroski's accrual "
                    "check (operating cash flow vs net income) from the payload.")
    implied_growth_view: Literal["too pessimistic", "fair", "too optimistic", "unknown"] = Field(
        description="Whether the growth the price implies (reverse DCF) is too pessimistic, fair, or too "
                    "optimistic given the business; unknown when the payload has no implied growth.")
    implied_growth_reasoning: str
    insider_activity: str = Field(description="What insiders are doing (buying into the drop, or selling), "
                                              "respecting the stated coverage.")
    dividend_risk: str = Field(description="Whether a dividend is at risk, or 'no dividend'.")
    asset_floor: str = Field(description="How much of the price the asset floor covers if the earnings case "
                                         "fails, and whether those assets are likely worth their book value.")
    leadership_turnover: str = Field(description="Leadership turnover from the leadership flag, stating its "
                                                 "coverage. Partial or missing coverage means unknown, never clean.")
    leadership_status: Literal["turnover found", "none found", "unknown"]
    impairment_type: Literal["structural", "cyclical", "sentiment", "unclear"] = Field(
        description="Whether the weakness looks like permanent impairment (structural) or cyclical / "
                    "sentiment-driven.")
    impairment_reasoning: str
    data_freshness: str = Field(description="When the payload is stale or has mixed periods, whether the upside "
                                            "or the risks could be an artifact of out-of-date numbers; otherwise "
                                            "say the data is current.")
    bull_case_requirements: list[str] = Field(description="What would have to be true for the bull case to hold.")
    score: float = Field(ge=config.SCORE_MIN, le=config.SCORE_MAX,
                         description="How well the bull case survives the attack, 1-10 against the rubric.")
    evidence: list[Evidence] = Field(description="At least 2 specific facts from the payload that justify the score.")
    rationale: str = Field(description="A short paragraph explaining the score.")


class DepartureResponse(BaseModel):
    departure: bool = Field(description="True only if the filing announces that the company's own CEO or CFO is "
                                        "leaving (resignation, retirement, termination, stepping down, or being "
                                        "replaced). Appointments with no departure are false.")
    role: Literal["CEO", "CFO"] | None = Field(description="The departing officer's role, or null.")
    person: str | None = Field(description="The departing officer's name, or null.")
    effective_date: str | None = Field(description="Effective date of the departure as YYYY-MM-DD, or null if "
                                                   "not stated.")
    explanation: str = Field(description="One sentence quoting or paraphrasing the relevant statement.")
