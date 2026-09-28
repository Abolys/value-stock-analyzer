"""Typed lens results (CLAUDE.md Rule 3). Downstream code reads these fields,
never the markdown rationale. Every lens carries its score (or an
"Insufficient data - <reason>" status), the mapping steps and mapping line,
key figures, assumptions, a data-completeness note and its confidence."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

import config
from analysis.turnaround_models import TurnaroundResult, Week52
from data.leadership import LeadershipResult
from data.values import Datum
from data.xbrl import DebtMaturities
from screening.models import ScreenResult
from signals.context import ContextFields
from signals.cyclicality import Cyclicality
from signals.dividends import DividendSafety
from signals.insider_activity import InsiderSummary
from signals.dcf import DcfBase, DcfResult, GrowthInput, PeakEarnings, ReverseDcf, SensitivityGrid
from signals.mapping import MappingStep

OK = "ok"
INSUFFICIENT = "Insufficient data"
LENSES = ("quant", "macro", "moat", "devils_advocate")
LENS_LABELS = {"quant": "Quantitative Fundamental", "macro": "Macro & Balance Sheet Risk",
               "moat": "Business Moat", "devils_advocate": "Devil's Advocate"}


def insufficient(reason: str) -> str:
    return reason if reason.startswith(INSUFFICIENT) else f"{INSUFFICIENT} - {reason}"


class LensResult(BaseModel):
    lens: str
    score: float | None = None
    status: str = OK
    rationale: str = ""
    mapping_steps: list[MappingStep] = Field(default_factory=list)
    mapping_line: str = ""
    key_figures: dict[str, str] = Field(default_factory=dict)
    assumptions: dict[str, Any] = Field(default_factory=dict)
    completeness: str = ""
    confidence: str = config.QUANT_CONFIDENCE_NORMAL
    confidence_reasons: list[str] = Field(default_factory=list)
    bull_point: str = ""
    key_risk: str = ""
    notes: list[str] = Field(default_factory=list)
    fundamentals_as_of: date | None = None
    stale: bool = False
    stale_label: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK and self.score is not None

    @property
    def label(self) -> str:
        return LENS_LABELS.get(self.lens, self.lens)

    @property
    def display(self) -> str:
        return f"{self.score:.1f}" if self.ok else self.status


class QuantResult(LensResult):
    lens: str = "quant"
    method: str = ""  # dcf | runway | excess_return | reit_ffo_dcf
    dcf: DcfResult | None = None  # the DCF the score uses (normalised when peak earnings are flagged)
    dcf_raw: DcfResult | None = None  # the unnormalised DCF, shown beside it when normalised
    dcf_before_sbc: DcfResult | None = None  # the same DCF on raw FCF (stock comp treated as free), for reference
    base: DcfBase | None = None
    growth: GrowthInput | None = None
    reverse_dcf: ReverseDcf | None = None
    grid: SensitivityGrid | None = None
    peak: PeakEarnings | None = None
    fcf_negative: str = ""
    runway_months: Datum = Field(default_factory=Datum.missing)
    returns: Datum = Field(default_factory=Datum.missing)  # ROIC, or ROA/ROE substitute
    returns_label: str = "ROIC"
    graham: Datum = Field(default_factory=Datum.missing)
    graham_upside: float | None = None
    piotroski: str = ""


class MacroResult(LensResult):
    lens: str = "macro"
    subscores_used: int = 0
    subscores_total: int = 5
    reduced_data: bool = False
    net_debt_ebitda: Datum = Field(default_factory=Datum.missing)
    interest_coverage: Datum = Field(default_factory=Datum.missing)
    altman_z: Datum = Field(default_factory=Datum.missing)
    altman_zone: str | None = None
    leverage_trend: str = ""  # falling | flat | rising | N/A reason
    leverage_trend_values: dict[str, float] = Field(default_factory=dict)
    cyclicality: Cyclicality | None = None
    current_debt: Datum = Field(default_factory=Datum.missing)
    long_term_debt: Datum = Field(default_factory=Datum.missing)
    maturity_note: str = ""
    debt_maturities: DebtMaturities | None = None  # the 10-K schedule (SEC XBRL), when filed


class EvidenceItem(BaseModel):
    field: str
    value: str
    why: str


class LLMLensResult(LensResult):
    payload: dict[str, Any] = Field(default_factory=dict)
    prompt_version: str = ""
    model: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)
    cache_hit: bool = False
    cost: float = 0.0  # billed API cost ($0 on the Claude Code subscription backend)
    list_price_cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0


class MoatResult(LLMLensResult):
    lens: str = "moat"
    sector_threat: str = ""
    sector_threat_hint: str = ""
    threat_reasoning: str = ""
    advantages: list[str] = Field(default_factory=list)
    pricing_power: str = ""


class DevilsAdvocateResult(LLMLensResult):
    lens: str = "devils_advocate"
    weakest_valuation_assumption: str = ""
    weakest_moat_point: str = ""
    accounting_red_flags: str = ""
    implied_growth_view: str = ""
    implied_growth_reasoning: str = ""
    insider_activity: str = ""
    dividend_risk: str = ""
    asset_floor: str = ""
    leadership_turnover: str = ""
    leadership_status: str = ""
    impairment_type: str = "unclear"  # structural | cyclical | sentiment | unclear (Phase 4 reads this)
    impairment_reasoning: str = ""
    data_freshness: str = ""
    bull_case_requirements: list[str] = Field(default_factory=list)


class AggregateResult(BaseModel):
    score: float | None = None
    verdict: str = INSUFFICIENT
    lenses_used: int = 0
    lenses_total: int = len(LENSES)
    weights_used: dict[str, float] = Field(default_factory=dict)
    missing: dict[str, str] = Field(default_factory=dict)  # lens → reason
    controversy: bool = False
    controversy_gap: float | None = None  # mean of the other lenses − Devil's Advocate
    rationale: str = ""
    not_applicable: str = ""  # why no verdict applies at all (e.g. a fund), else ""

    @property
    def display(self) -> str:
        if self.not_applicable:
            return self.not_applicable
        if self.score is None:
            return f"{INSUFFICIENT} (0 of {self.lenses_total} lenses)"
        return f"{self.score:.1f} ({self.lenses_used} of {self.lenses_total} lenses)"


class SeriesPoint(BaseModel):
    period_end: date
    value: float


class FundamentalSeries(BaseModel):
    """Fundamentals over time for the small multiples: each metric at its own period ends
    (quarterly when the provider has quarters, else fiscal years, recorded in `freqs`),
    in the trading currency. Missing metrics carry their reason instead of points."""

    points: dict[str, list[SeriesPoint]] = Field(default_factory=dict)  # metric → oldest first
    freqs: dict[str, str] = Field(default_factory=dict)  # metric → "quarterly" | "annual"
    missing: dict[str, str] = Field(default_factory=dict)  # metric → N/A reason
    currency: str | None = None
    provider: str = ""


class AnalysisRun(BaseModel):
    ticker: str
    analysis_id: int | None = None
    today: date | None = None
    quant: QuantResult | None = None
    macro: MacroResult | None = None
    moat: MoatResult | None = None
    devils_advocate: DevilsAdvocateResult | None = None
    aggregate: AggregateResult | None = None
    turnaround: TurnaroundResult | None = None
    total_cost: float = 0.0
    fundamentals_as_of: date | None = None
    stale: bool = False
    stale_label: str = ""
    errors: list[str] = Field(default_factory=list)
    load_error: str = ""
    # The per-ticker view model (Phase 5): everything the Stock page and the export draw,
    # serialisable so a stored run can be re-rendered.
    company: str = ""
    sector: str | None = None
    industry: str | None = None
    treatment: str = ""
    sector_adjusted: bool = False
    fund: bool = False  # an ETF or mutual fund (FUND_QUOTE_TYPES): price-based view only, no lenses
    currency: str | None = None
    price_as_of: date | None = None
    fundamentals_label: str = ""  # e.g. "TTM to 2026-06-30 (4 quarters)" or "annual, not TTM (...)"
    providers: list[str] = Field(default_factory=list)
    input_hash: str = ""
    screen: ScreenResult | None = None
    dividends: DividendSafety | None = None
    insiders: InsiderSummary | None = None
    leadership: LeadershipResult | None = None
    context: ContextFields | None = None
    series: FundamentalSeries | None = None
    week52: Week52 | None = None
    notes: list[str] = Field(default_factory=list)

    def lens(self, name: str) -> LensResult | None:
        return getattr(self, name)
