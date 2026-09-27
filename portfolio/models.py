"""Typed portfolio records and results (CLAUDE.md Rule 3): holdings with dated buys and
sells, the thesis at purchase with its structured sell triggers, the journal, alerts,
and the results of a thesis check. Downstream code reads these fields, never text."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

BUY, SELL = "buy", "sell"

# Trigger states. "can't evaluate" carries the N/A or n/m reason of the current value.
FIRED, NEAR, OK_STATE, UNKNOWN = "fired", "near", "ok", "can't evaluate"
# Traffic light for a holding's triggers.
LIGHT_RED, LIGHT_AMBER, LIGHT_GREEN, LIGHT_NONE = "red", "amber", "green", "none"


class Transaction(BaseModel):
    txn_id: int | None = None
    holding_id: int | None = None
    txn_date: date
    side: Literal["buy", "sell"]
    shares: float
    price: float
    fees: float = 0.0
    note: str = ""


class Trigger(BaseModel):
    """A structured sell rule: `field op value`, where value is a literal or a thesis level."""

    trigger_id: int | None = None
    field: str
    op: str
    literal: float | bool | str | None = None
    ref: str | None = None  # a config.THESIS_LEVEL_FIELDS name, e.g. "target_price"
    fired_at: datetime | None = None

    @property
    def text(self) -> str:
        rhs = self.ref if self.ref else (str(self.literal).lower() if isinstance(self.literal, bool)
                                         else f'"{self.literal}"' if isinstance(self.literal, str) else
                                         f"{self.literal:g}")
        return f"{self.field} {self.op} {rhs}"


class Reason(BaseModel):
    reason_id: int | None = None
    text: str
    still_holds: bool | None = None  # None = not reviewed yet
    reviewed_at: datetime | None = None


class Thesis(BaseModel):
    thesis_id: int | None = None
    holding_id: int | None = None
    created_at: datetime | None = None
    intrinsic_value: float | None = None
    buy_below_price: float | None = None
    target_price: float | None = None
    basis: str = ""  # where the intrinsic value came from, e.g. "DCF base case, analysis 12"
    reasons: list[Reason] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=list)

    def levels(self) -> dict[str, float | None]:
        return {"intrinsic_value": self.intrinsic_value, "buy_below_price": self.buy_below_price,
                "target_price": self.target_price}


class Holding(BaseModel):
    holding_id: int | None = None
    ticker: str
    account: str
    currency: str
    created_at: datetime | None = None
    snapshot_analysis_id: int | None = None
    snapshot: dict[str, "Metric"] = Field(default_factory=dict)
    closed: bool = False
    transactions: list[Transaction] = Field(default_factory=list)
    thesis: Thesis | None = None

    @property
    def first_buy(self) -> date | None:
        buys = [t.txn_date for t in self.transactions if t.side == BUY]
        return min(buys) if buys else None


class JournalEntry(BaseModel):
    entry_id: int | None = None
    holding_id: int
    created_at: datetime
    kind: str = "note"  # note | trigger | alert | reason
    text: str
    alert_id: int | None = None


class Alert(BaseModel):
    alert_id: int | None = None
    ticker: str
    holding_id: int | None = None
    kind: str
    event_key: str
    message: str
    created_at: datetime | None = None
    source: str = "manual"
    read_at: datetime | None = None
    email_status: str | None = None


class WatchLevel(BaseModel):
    ticker: str
    buy_below_price: float | None = None
    target_price: float | None = None
    basis: str = ""
    updated_at: datetime | None = None


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
class Metric(BaseModel):
    """One flat metric for triggers and "then vs now": its value, or the N/A / n/m reason."""

    value: float | bool | str | None = None
    status: str = "ok"  # ok | "N/A - …" | "n/m - …"
    display: str = ""
    as_of: date | None = None
    source: str = ""  # e.g. "check 2026-09-27" or "analysis 12 (2026-09-20)"

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.value is not None


class TriggerStatus(BaseModel):
    trigger: Trigger
    state: str  # fired | near | ok | can't evaluate
    current: str = ""  # the current value's display (or its reason)
    threshold: str = ""  # the value compared against (a ref resolved to its number)
    reason: str = ""


class PositionSummary(BaseModel):
    """Average-cost position maths at the actual latest price (Rule 5), in the holding's currency."""

    shares: float = 0.0
    avg_cost: float | None = None
    invested: float = 0.0  # total cash put in (buys incl. fees)
    cost_basis: float = 0.0  # average cost × shares still held
    price: float | None = None
    price_as_of: date | None = None
    market_value: float | None = None
    realised: float = 0.0
    unrealised: float | None = None
    total_gain: float | None = None
    total_return: float | None = None  # (value + sell proceeds − invested) / invested
    benchmark: str = ""
    benchmark_return: float | None = None  # the same cash flows mirrored into the benchmark
    vs_benchmark: float | None = None  # total_return − benchmark_return (fraction; ×100 = points)
    status: str = "ok"  # ok, or the reason value / return are N/A
    notes: list[str] = Field(default_factory=list)


class ThenNowRow(BaseModel):
    field: str
    label: str
    then: str
    now: str
    change: str = "same"  # better | worse | changed | same | n/a
    delta: float | None = None


class ThesisCheck(BaseModel):
    holding_id: int
    ticker: str
    triggers: list[TriggerStatus] = Field(default_factory=list)
    light: str = LIGHT_NONE
    then_now: list[ThenNowRow] = Field(default_factory=list)
    now_source: str = ""  # which run the "now" column comes from
    notes: list[str] = Field(default_factory=list)


class CheckReport(BaseModel):
    check_id: int | None = None
    source: str
    started_at: datetime
    tickers: list[str] = Field(default_factory=list)
    fired: list[Alert] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    email_status: str = ""
    metrics: dict[str, dict[str, Any]] = Field(default_factory=dict)  # ticker → field → Metric dump


class HoldingJournal(BaseModel):
    """One holding's thesis, trigger status and journal, for the per-ticker export."""

    holding: Holding
    check: ThesisCheck | None = None
    entries: list[JournalEntry] = Field(default_factory=list)


Holding.model_rebuild()
