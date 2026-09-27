"""Typed turnaround results (CLAUDE.md Rule 3). Phase 5 shades `episodes` on the
price chart and reads every other field; nothing downstream parses `rationale`."""

from __future__ import annotations

import datetime as dt
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

import config
from data.corporate_actions import Break

MARKET_DRIVEN = "market-driven"
COMPANY_SPECIFIC = "company-specific"
UNCLASSIFIED = "unclassified"

STATUS_OK = "ok"
STATUS_NOT_IN_DRAWDOWN = "not_in_drawdown"
STATUS_WITHHELD = "withheld"

BASIS_SAME_TYPE = "same-type"
BASIS_ALL_TYPES = "all types (fallback)"
BASIS_PEERS = "peer-based"
PEER_LABEL = "Peer-based, lower confidence"
STRUCTURAL_LABEL = "Low confidence — may not be mean-reverting"
VALUATION_UNAVAILABLE = "Unavailable — needs longer fundamental history (FMP)"


class Segment(BaseModel):
    """A stretch of price history between corporate-action breaks. Rolling highs,
    drawdowns and recoveries are computed inside one segment only."""

    index: int
    start: date
    end: date
    closes: int


class Episode(BaseModel):
    ticker: str
    segment: int
    peak_date: date
    peak_price: float
    threshold_date: date  # first close DRAWDOWN_THRESHOLD below the rolling high
    trough_date: date
    trough_price: float
    recovery_date: date | None = None  # first close back within RECOVERY_BAND of peak_price
    drop: float  # 1 − trough / peak
    recovery_months: float | None = None  # from RECOVERY_CLOCK_START to recovery_date
    recovered: bool = False
    unrecovered_reason: str = ""
    episode_type: str = UNCLASSIFIED
    benchmark: str = ""
    benchmark_drop: float | None = None  # 1 − benchmark(trough) / benchmark(peak); negative = benchmark rose
    classification_note: str = ""


class CurrentDrawdown(BaseModel):
    drawdown: float  # 1 − latest close / rolling 52-week high
    as_of: date
    high_date: date
    high_price: float
    latest_price: float
    qualifying: bool
    trough_date: date | None = None
    trough_price: float | None = None
    episode_type: str = UNCLASSIFIED
    benchmark_drop: float | None = None
    classification_note: str = ""
    note: str = ""  # e.g. still inside an older unrecovered episode

    @property
    def label(self) -> str:
        return f"−{self.drawdown:.0%} from 52-week high"


class TechnicalSignal(BaseModel):
    name: str
    active: bool = False
    detail: str = ""
    as_of: date | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class Catalyst(BaseModel):
    kind: str  # earnings | leadership | lens | debt_maturity
    date: dt.date | None = None
    text: str
    source: str = ""


class Peer(BaseModel):
    ticker: str
    name: str = ""
    industry: str | None = None
    market_cap: float | None = None
    sources: str = ""  # universe lists it appears in


class PeerSelection(BaseModel):
    status: str = STATUS_OK  # ok, or "Unavailable - <reason>"
    ticker: str
    industry: str | None = None
    market_cap: float | None = None
    peers: list[Peer] = Field(default_factory=list)
    candidates_considered: int = 0  # universe tickers with a screen result
    not_screened: int = 0  # universe tickers without one (industry unknown)
    screen_run_id: int | None = None
    screen_run_date: date | None = None
    universe_lists: list[str] = Field(default_factory=list)
    source_note: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK and bool(self.peers)


class PeerHistory(BaseModel):
    ticker: str
    episodes: list[Episode] = Field(default_factory=list)
    error: str = ""


class RecoveryStats(BaseModel):
    basis: str
    episode_type: str  # the type the stats are drawn from, or "all types"
    episodes_used: int
    median_months: float
    p25_months: float
    p75_months: float
    note: str = ""


class TurnaroundResult(BaseModel):
    ticker: str
    status: str = STATUS_OK  # ok | not_in_drawdown | withheld | "Insufficient data - <reason>"
    headline: str = ""
    median_months: float | None = None
    iqr_months: tuple[float, float] | None = None
    episodes_used: int = 0
    basis: str = ""
    basis_note: str = ""
    current: CurrentDrawdown | None = None
    episodes: list[Episode] = Field(default_factory=list)
    recovered_count: int = 0
    unrecovered_count: int = 0
    segments: list[Segment] = Field(default_factory=list)
    breaks: list[Break] = Field(default_factory=list)
    confidence: str | None = None
    confidence_reasons: list[str] = Field(default_factory=list)
    confidence_rule: str = config.TURNAROUND_CONFIDENCE_RULE
    structural_flag: bool = False
    structural_note: str = ""
    survivorship_caveat: str = config.TURNAROUND_SURVIVORSHIP_CAVEAT
    valuation_recovery: str = VALUATION_UNAVAILABLE
    catalysts: list[Catalyst] = Field(default_factory=list)
    debt_maturity_note: str = ""
    signals: list[TechnicalSignal] = Field(default_factory=list)
    peer: PeerSelection | None = None
    peer_histories: list[PeerHistory] = Field(default_factory=list)
    asset_floor_line: str = ""
    benchmark: str = ""
    listing_country: str = ""
    price_kind: str = "adjusted closes (dividend- and split-adjusted)"
    price_as_of: date | None = None
    assumptions: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)
    rationale: str = ""

    @property
    def has_range(self) -> bool:
        return self.iqr_months is not None

    @property
    def active_signals(self) -> list[TechnicalSignal]:
        return [s for s in self.signals if s.active]
