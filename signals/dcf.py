"""Two-stage DCF, reverse DCF, sensitivity grid and peak-earnings check
(SPEC "Valuation extras", "Score mapping — Quant", CLAUDE.md Rule 2b).

One function, `dcf_value`, computes fair value per share. The base case, the
reverse DCF and every cell of the sensitivity grid call it, so they can never
disagree on structure or assumptions.

- Base FCF = average SBC-adjusted FCF (FCF − stock-based compensation, like the screen's
  FCF yield) of the last DCF_BASE_YEARS fiscal years (Rule 2b); a year whose SBC isn't
  reported uses raw FCF, labelled. The raw-FCF base is kept for a "before SBC" fair value. An
  average ≤ 0, or a sign change inside the window → "Insufficient data -
  unstable FCF base"; fewer than FCF_NEGATIVE_MIN_YEARS years → "too little
  history".
- Stage-1 growth = revenue CAGR over the available fiscal years (not FCF
  growth), clamped to STAGE1_GROWTH_FLOOR..STAGE1_GROWTH_CAP.
- DCF_STAGE1_YEARS of growth, then TERMINAL_GROWTH (Gordon growth), all
  discounted at COST_OF_CAPITAL — the same rate the ROIC comparison uses.
- Fair value per share = (PV stage 1 + PV terminal [+ cash − debt when
  DCF_ADD_NET_CASH]) / shares.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

import config
from data.fundamentals import Fundamentals
from data.ratios import first_unusable, safe_ratio, sum_datums
from data.values import Datum, NA_INCOMPLETE, nm
from screening.metrics import annual_fcf
from signals.valuation import liquid_cash

INSUFFICIENT = "Insufficient data"
UNSTABLE_BASE = f"{INSUFFICIENT} - unstable FCF base"
TOO_LITTLE_HISTORY = f"{INSUFFICIENT} - too little history"


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
class DcfBase(BaseModel):
    value: float | None = None
    status: str = "ok"
    years: list[Datum] = Field(default_factory=list)
    detail: str = ""
    normalised: bool = False
    raw_value: float | None = None  # the same average on raw FCF (before stock-based compensation)
    sbc_note: str = ""  # which years lacked an SBC row, if any

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.value is not None


def sbc_adjusted_fcf(f: Fundamentals) -> tuple[list[Datum], list[str]]:
    """Annual FCF − stock-based compensation per fiscal year, newest first (stock comp is a real cost
    paid in shares). Years without an SBC row keep raw FCF; their labels are returned."""
    out, missing = [], []
    for d in annual_fcf(f):
        sbc = f.fy("stock_based_compensation", d.period_end)
        if sbc.ok:
            out.append(d.model_copy(update={"value": d.value - sbc.value,
                                            "notes": [*d.notes, f"SBC {sbc.value:,.4g} deducted"]}))
        else:
            out.append(d.model_copy(update={"notes": [*d.notes, "SBC not reported; raw FCF"]}))
            missing.append(d.period_label)
    return out, missing


def dcf_base(f: Fundamentals) -> DcfBase:
    """Average SBC-adjusted FCF over the last DCF_BASE_YEARS fiscal years, with the Rule 2b stability checks."""
    raw_years = annual_fcf(f)[: config.DCF_BASE_YEARS]
    adj, missing = sbc_adjusted_fcf(f)
    years = adj[: config.DCF_BASE_YEARS]
    missing = [m for m in missing if m in {d.period_label for d in years}]
    sbc_note = (f"SBC not reported for {', '.join(missing)} (raw FCF used there)" if missing
                else "SBC deducted in every year")
    labels = ", ".join(f"{d.period_label} {d.value:,.4g}" for d in years)
    if len(years) < config.FCF_NEGATIVE_MIN_YEARS:
        return DcfBase(status=TOO_LITTLE_HISTORY, years=years,
                       detail=f"{len(years)} fiscal year(s) of FCF; {config.FCF_NEGATIVE_MIN_YEARS} needed")
    avg = sum(d.value for d in years) / len(years)
    signs = {d.value > 0 for d in years if d.value != 0}
    short = "" if len(years) == config.DCF_BASE_YEARS else f"; only {len(years)} of {config.DCF_BASE_YEARS} years available"
    detail = f"average SBC-adjusted FCF of {len(years)} fiscal years ({labels}) = {avg:,.4g}{short}; {sbc_note}"
    raw_avg = sum(d.value for d in raw_years) / len(raw_years) if raw_years else None
    if len(signs) > 1:
        return DcfBase(status=UNSTABLE_BASE, years=years, raw_value=raw_avg, sbc_note=sbc_note,
                       detail=f"SBC-adjusted FCF changed sign within the window ({labels})")
    if avg <= 0:
        return DcfBase(status=UNSTABLE_BASE, years=years, raw_value=raw_avg, sbc_note=sbc_note,
                       detail=f"average SBC-adjusted FCF ≤ 0 ({detail})")
    return DcfBase(value=avg, years=years, detail=detail, raw_value=raw_avg, sbc_note=sbc_note)


class GrowthInput(BaseModel):
    cagr: Datum = Field(default_factory=Datum.missing)  # raw revenue CAGR
    used: float | None = None  # clamped stage-1 growth
    clamped: bool = False
    detail: str = ""


def revenue_cagr(f: Fundamentals) -> GrowthInput:
    """Revenue CAGR over the available fiscal years, clamped for use as DCF stage-1 growth."""
    revs = [d for d in f.annual_values("total_revenue") if d.ok]
    if len(revs) < 2:
        return GrowthInput(cagr=Datum.missing(f"{NA_INCOMPLETE} (fewer than 2 fiscal years of revenue)"))
    latest, oldest = revs[0], revs[-1]
    n = len(revs) - 1
    if oldest.value <= 0 or latest.value <= 0:
        return GrowthInput(cagr=Datum.missing(nm("revenue ≤ 0 at an end of the window")))
    cagr = (latest.value / oldest.value) ** (1 / n) - 1
    used = min(config.STAGE1_GROWTH_CAP, max(config.STAGE1_GROWTH_FLOOR, cagr))
    detail = f"revenue CAGR {cagr:+.1%}/yr over {n} years ({oldest.period_label} → {latest.period_label})"
    if used != cagr:
        detail += f", clamped to {used:+.1%} (bounds {config.STAGE1_GROWTH_FLOOR:+.0%} to {config.STAGE1_GROWTH_CAP:+.0%})"
    return GrowthInput(cagr=Datum(value=cagr, period_end=latest.period_end, period_label=detail),
                       used=used, clamped=used != cagr, detail=detail)


def net_cash(f: Fundamentals) -> Datum:
    """Cash + short-term investments − total debt (latest quarter)."""
    return sum_datums({"cash + short-term investments": liquid_cash(f), "total debt": f.bal("total_debt")},
                      "net cash", signs={"total debt": -1})


# --------------------------------------------------------------------------
# The one DCF function
# --------------------------------------------------------------------------
class DcfValue(BaseModel):
    per_share: float
    pv_stage1: float
    pv_terminal: float
    net_cash: float


def dcf_value(base: float, growth: float, rate: float, shares: float, net_cash_value: float,
              terminal: float | None = None, years: int | None = None) -> DcfValue | None:
    """Fair value per share. None when the rate does not exceed terminal growth or shares ≤ 0."""
    terminal = config.TERMINAL_GROWTH if terminal is None else terminal
    years = config.DCF_STAGE1_YEARS if years is None else years
    if rate <= terminal or shares <= 0:
        return None
    pv1, fcf = 0.0, base
    for t in range(1, years + 1):
        fcf *= 1 + growth
        pv1 += fcf / (1 + rate) ** t
    terminal_value = fcf * (1 + terminal) / (rate - terminal)
    pv_t = terminal_value / (1 + rate) ** years
    bridge = net_cash_value if config.DCF_ADD_NET_CASH else 0.0
    return DcfValue(per_share=(pv1 + pv_t + bridge) / shares, pv_stage1=pv1, pv_terminal=pv_t, net_cash=bridge)


class DcfInputs(BaseModel):
    """Everything the DCF needs, already validated (base > 0, shares > 0, net cash known)."""

    base: float
    growth: float
    shares: float
    net_cash: float
    price: float
    rate: float  # always COST_OF_CAPITAL in the base case (the ROIC comparison uses the same constant)

    def value(self, growth: float | None = None, rate: float | None = None) -> DcfValue | None:
        return dcf_value(self.base, self.growth if growth is None else growth, self.rate if rate is None else rate,
                         self.shares, self.net_cash)

    @property
    def assumptions(self) -> dict[str, float | str | bool]:
        return {"base_fcf": self.base, "stage1_growth": self.growth, "discount_rate (COST_OF_CAPITAL)": self.rate,
                "terminal_growth": config.TERMINAL_GROWTH, "stage1_years": config.DCF_STAGE1_YEARS,
                "shares": self.shares, "net_cash": self.net_cash, "DCF_ADD_NET_CASH": config.DCF_ADD_NET_CASH,
                "price (actual latest)": self.price}


class DcfResult(BaseModel):
    status: str = "ok"
    fair_value: float | None = None
    upside: float | None = None
    pv_stage1: float | None = None
    pv_terminal: float | None = None
    inputs: DcfInputs | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.fair_value is not None


def build_inputs(base: DcfBase, growth: GrowthInput, shares: Datum, cash: Datum, price: Datum,
                 rate: float | None = None) -> tuple[DcfInputs | None, str]:
    rate = config.COST_OF_CAPITAL if rate is None else rate
    if not base.ok:
        return None, base.status
    if growth.used is None:
        return None, f"{INSUFFICIENT} - stage-1 growth {growth.cagr.status}"
    bad = first_unusable(shares, price, cash)
    if bad is not None:
        return None, f"{INSUFFICIENT} - {bad.status}"
    if shares.value <= 0 or price.value <= 0:
        return None, f"{INSUFFICIENT} - shares or price ≤ 0 (data error)"
    return DcfInputs(base=base.value, growth=growth.used, shares=shares.value, net_cash=cash.value,
                     price=price.value, rate=rate), ""


def run_dcf(inputs: DcfInputs | None, status: str = "") -> DcfResult:
    if inputs is None:
        return DcfResult(status=status or INSUFFICIENT)
    v = inputs.value()
    if v is None:
        return DcfResult(status=f"{INSUFFICIENT} - discount rate ≤ terminal growth", inputs=inputs)
    return DcfResult(fair_value=v.per_share, upside=v.per_share / inputs.price - 1, pv_stage1=v.pv_stage1,
                     pv_terminal=v.pv_terminal, inputs=inputs)


# --------------------------------------------------------------------------
# Reverse DCF
# --------------------------------------------------------------------------
class ReverseDcf(BaseModel):
    status: str = "ok"  # ok | beyond range | n/m - ... | Insufficient data - ...
    implied_growth: float | None = None
    side: str = ""  # "below" | "above" when beyond range
    history: Datum = Field(default_factory=Datum.missing)

    @property
    def display(self) -> str:
        hist = f"history shows {self.history.value:+.1%}/yr" if self.history.ok else f"history {self.history.status}"
        if self.status == "ok":
            return f"Price implies {self.implied_growth:+.1%}/yr growth; {hist}"
        if self.status == "beyond range":
            lo, hi = config.REVERSE_DCF_SEARCH_RANGE
            bound = lo if self.side == "below" else hi
            return f"Price implies growth beyond range ({self.side} {bound:+.0%}/yr); {hist}"
        return self.status


def reverse_dcf(inputs: DcfInputs | None, history: Datum, status: str = "") -> ReverseDcf:
    """Solve (bisection) for the stage-1 growth that makes fair value equal the actual latest price."""
    if inputs is None:
        reason = status or INSUFFICIENT
        if reason.startswith(UNSTABLE_BASE) or "FCF-negative" in reason:
            reason = nm("FCF-negative or unstable FCF base")
        return ReverseDcf(status=reason, history=history)
    lo, hi = config.REVERSE_DCF_SEARCH_RANGE

    def gap(g: float) -> float:
        return inputs.value(growth=g).per_share - inputs.price

    if gap(lo) > 0:
        return ReverseDcf(status="beyond range", side="below", history=history)
    if gap(hi) < 0:
        return ReverseDcf(status="beyond range", side="above", history=history)
    for _ in range(config.REVERSE_DCF_MAX_ITERATIONS):
        mid = (lo + hi) / 2
        if gap(mid) < 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < config.REVERSE_DCF_TOLERANCE:
            break
    return ReverseDcf(implied_growth=(lo + hi) / 2, history=history)


# --------------------------------------------------------------------------
# Sensitivity grid
# --------------------------------------------------------------------------
class SensitivityGrid(BaseModel):
    rates: list[float] = Field(default_factory=list)
    growths: list[float] = Field(default_factory=list)
    values: list[list[float | None]] = Field(default_factory=list)  # values[rate_index][growth_index]
    range_low: float | None = None
    range_high: float | None = None
    status: str = "ok"

    @property
    def center(self) -> float | None:
        if not self.values:
            return None
        return self.values[len(self.rates) // 2][len(self.growths) // 2]

    @property
    def range_display(self) -> str:
        if self.range_low is None:
            return self.status
        return f"{self.range_low:,.2f}–{self.range_high:,.2f}"


def sensitivity_grid(inputs: DcfInputs | None, status: str = "") -> SensitivityGrid:
    """Fair value over COST_OF_CAPITAL ± steps × stage-1 growth ± steps; the range is the central 3 × 3."""
    if inputs is None:
        return SensitivityGrid(status=status or INSUFFICIENT)
    rates = [inputs.rate + d for d in config.SENSITIVITY_RATE_STEPS]
    growths = [inputs.growth + d for d in config.SENSITIVITY_GROWTH_STEPS]
    values = [[(v.per_share if (v := inputs.value(growth=g, rate=r)) else None) for g in growths] for r in rates]
    ri, gi = len(rates) // 2, len(growths) // 2
    central = [values[i][j] for i in range(ri - 1, ri + 2) for j in range(gi - 1, gi + 2)
               if 0 <= i < len(rates) and 0 <= j < len(growths) and values[i][j] is not None]
    return SensitivityGrid(rates=rates, growths=growths, values=values,
                           range_low=min(central) if central else None, range_high=max(central) if central else None)


# --------------------------------------------------------------------------
# Peak-earnings check (cyclicals)
# --------------------------------------------------------------------------
class PeakEarnings(BaseModel):
    checked: bool = False
    flagged: bool = False
    ttm_margin: Datum = Field(default_factory=Datum.missing)
    average_margin: float | None = None
    average_fcf_margin: float | None = None
    normalised_base: float | None = None
    detail: str = ""


def _margins(f: Fundamentals, num_field: str) -> list[float]:
    out = []
    for rev in f.annual_values("total_revenue"):
        if not rev.ok or rev.value <= 0:
            continue
        if num_field == "free_cash_flow":  # SBC-adjusted, like the DCF base it normalises
            num = next((d for d in sbc_adjusted_fcf(f)[0] if d.period_end == rev.period_end), None)
        else:
            num = f.fy(num_field, rev.period_end)
        if num is not None and num.ok:
            out.append(num.value / rev.value)
    return out


def peak_earnings(f: Fundamentals, cyclicality_score: float) -> PeakEarnings:
    """For cyclicals (cyclicality score 3): TTM operating margin ≥ PEAK_MARGIN_RATIO × the average
    operating margin of the available fiscal years → flagged; the DCF base is then normalised to
    TTM revenue × the average FCF margin over those years."""
    if cyclicality_score != config.CYCLICALITY_SCORES["cyclical"]:
        return PeakEarnings(detail="not a cyclical (cyclicality score ≠ 3); peak-earnings check not applied")
    rev = f.ttm("total_revenue")
    ttm_margin = safe_ratio(f.ttm("operating_income"), rev, name="TTM operating margin", nonpositive_reason="revenue ≤ 0")
    avg_ops = _margins(f, "operating_income")
    if not ttm_margin.ok or not avg_ops:
        return PeakEarnings(checked=True, ttm_margin=ttm_margin,
                            detail=f"peak-earnings check N/A (TTM margin {ttm_margin.status}; "
                                   f"{len(avg_ops)} fiscal-year margins)")
    avg = sum(avg_ops) / len(avg_ops)
    flagged = avg > 0 and ttm_margin.value >= config.PEAK_MARGIN_RATIO * avg
    res = PeakEarnings(checked=True, flagged=flagged, ttm_margin=ttm_margin, average_margin=avg,
                       detail=f"TTM operating margin {ttm_margin.value:.1%} vs {len(avg_ops)}-year average {avg:.1%} "
                              f"(flag at ≥ {config.PEAK_MARGIN_RATIO:g}×)")
    if flagged:
        fcf_margins = _margins(f, "free_cash_flow")
        if fcf_margins and rev.ok:
            res.average_fcf_margin = sum(fcf_margins) / len(fcf_margins)
            res.normalised_base = rev.value * res.average_fcf_margin
            res.detail += (f"; possibly peak earnings — DCF base normalised to TTM revenue {rev.value:,.4g} × "
                           f"average SBC-adjusted FCF margin {res.average_fcf_margin:.1%} = {res.normalised_base:,.4g}")
        else:
            res.detail += "; possibly peak earnings — FCF margins unavailable, base not normalised"
    return res
