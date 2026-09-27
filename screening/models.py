"""Typed screener outputs (CLAUDE.md Rule 3). Downstream code reads these fields,
never the markdown rationale."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from data.values import Datum
from signals.asset_floor import AssetFloor
from signals.trap_scores import AltmanResult, BeneishResult, PiotroskiResult
from signals.valuation import EarningsYield

PASS, FAIL, NA, NM, NOT_APPLICABLE = "pass", "fail", "N/A", "n/m", "not applicable"
STATUS_PASS, STATUS_FAIL, STATUS_INCOMPLETE, STATUS_FAILED_TO_LOAD = "Pass", "Fail", "Incomplete", "failed to load"
FAIL_DISPLAY = "Fail (manual only)"

# Metric slot keys (the four screen metrics, or their sector-adjusted equivalents, plus the optional fifth).
SLOT_MOS, SLOT_FCF, SLOT_LEVERAGE, SLOT_SHARES, SLOT_EARNINGS_YIELD = "mos", "fcf", "leverage", "shares", "earnings_yield"


class MetricResult(BaseModel):
    slot: str
    name: str
    value: Datum = Field(default_factory=Datum.missing)
    fmt: str = "pct"  # "pct" | "ratio" | "months"
    threshold: str = ""
    outcome: str = NA  # pass | fail | N/A | n/m | not applicable
    na_reason: str = ""  # why the comparison is N/A when the value itself exists (e.g. no risk-free source)
    inputs: dict[str, Datum] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def available(self) -> bool:
        """An n/m counts as available (and failing); N/A and not applicable don't."""
        return self.outcome in (PASS, FAIL, NM)

    @property
    def failing(self) -> bool:
        return self.outcome in (FAIL, NM)

    @property
    def display(self) -> str:
        if not self.value.ok:
            return self.value.status
        v = self.value.value
        if self.fmt == "pct":
            return f"{v:+.1%}"
        if self.fmt == "months":
            return f"{v:.0f} months"
        return f"{v:.2f}x"


class Divergence(BaseModel):
    metric: str
    stage1: float
    stage2: float
    rel_diff: float


class Stage1Result(BaseModel):
    metrics: list[MetricResult] = Field(default_factory=list)
    survives: bool = True
    cut_reasons: list[str] = Field(default_factory=list)
    market_cap: Datum = Field(default_factory=Datum.missing)
    notes: list[str] = Field(default_factory=list)
    field_statuses: dict[str, str] = Field(default_factory=dict)

    def metric(self, slot: str) -> MetricResult | None:
        return next((m for m in self.metrics if m.slot == slot), None)


class SubScore(BaseModel):
    name: str
    score: float | None = None
    input_display: str = ""
    status: str = "ok"


class QualityScore(BaseModel):
    score: float | None = None
    available: int = 0
    of: int = 4
    subscores: list[SubScore] = Field(default_factory=list)
    sector_adjusted: bool = False

    @property
    def display(self) -> str:
        suffix = ", sector-adjusted" if self.sector_adjusted else ""
        if self.score is None:
            return f"Insufficient data (0 of {self.of}{suffix})"
        return f"{self.score:.1f} ({self.available} of {self.of}{suffix})"

    @property
    def working(self) -> str:
        parts = [f"{s.name} {s.input_display} → {s.score:.1f}" if s.score is not None
                 else f"{s.name} {s.status}" for s in self.subscores]
        return "; ".join(parts) + (f"; quality {self.display}" if parts else "")


class ScreenResult(BaseModel):
    ticker: str
    name: str = ""
    sources: str = ""
    status: str = STATUS_INCOMPLETE
    status_reasons: list[str] = Field(default_factory=list)
    decided_at_stage: int = 2
    load_error: str = ""
    sector: str | None = None
    industry: str | None = None
    treatment: str = "Standard"
    currency: str | None = None
    price: Datum = Field(default_factory=Datum.missing)
    market_cap: Datum = Field(default_factory=Datum.missing)
    risk_free: Datum = Field(default_factory=Datum.missing)
    inputs: dict[str, Datum] = Field(default_factory=dict)
    metrics: list[MetricResult] = Field(default_factory=list)
    metrics_available: int = 0
    min_metrics_for_pass: int = 3
    stage1: Stage1Result | None = None
    fcf_negative: str = ""  # "FCF-negative" | "not FCF-negative" | "Insufficient data - too little history"
    piotroski: PiotroskiResult | None = None
    altman: AltmanResult | None = None
    beneish: BeneishResult | None = None
    earnings_yield: EarningsYield | None = None
    asset_floor: AssetFloor | None = None
    trap_risk: bool = False
    trap_risk_reasons: list[str] = Field(default_factory=list)
    net_cash_flag: bool = False
    dilution_flag: bool = False
    share_trend_span: str = ""
    quality: QualityScore | None = None
    fundamentals_as_of: date | None = None
    stale: bool = False
    stale_label: str = ""
    divergences: list[Divergence] = Field(default_factory=list)
    field_statuses: dict[str, str] = Field(default_factory=dict)
    assumptions: dict[str, float | str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    rationale: str = ""

    @property
    def display_status(self) -> str:
        return FAIL_DISPLAY if self.status == STATUS_FAIL else self.status

    def metric(self, slot: str) -> MetricResult | None:
        return next((m for m in self.metrics if m.slot == slot), None)
