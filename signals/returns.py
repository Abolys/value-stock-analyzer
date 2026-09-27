"""Return metrics shared by the screener, the quality score and (Phase 3) the lenses.

- ROIC = NOPAT / invested capital, NOPAT = TTM EBIT × (1 − tax rate). The tax
  rate is TTM tax provision / TTM pretax income clamped to TAX_RATE_BOUNDS;
  when that is n/m (pretax ≤ 0) or missing, STATUTORY_TAX_RATE_FALLBACK is used
  and labelled. Invested capital ≤ 0 → ROIC n/m and return on total assets is
  substituted, labelled as the substitute (Rule 2b).
- ROE = TTM net income / latest shareholders' equity; equity ≤ 0 → n/m.
- FFO ≈ net income + depreciation & amortisation − gains on property sales
  (labelled approximate), for REITs (Rule 5).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

import config
from data.fundamentals import Fundamentals
from data.ratios import combine, first_unusable, safe_ratio, sum_datums
from data.values import Datum
from signals.valuation import optional_part, ttm_ebit

ROA_SUBSTITUTE = "return on total assets (substitute: ROIC n/m - invested capital ≤ 0)"


class ReturnMetric(BaseModel):
    value: Datum = Field(default_factory=Datum.missing)
    label: str = "ROIC"
    notes: list[str] = Field(default_factory=list)

    @property
    def is_substitute(self) -> bool:
        return self.label != "ROIC"


def tax_rate(f: Fundamentals) -> tuple[float, str]:
    rate = safe_ratio(f.ttm("tax_provision"), f.ttm("pretax_income"), name="effective tax rate",
                      nonpositive_reason="pretax income ≤ 0")
    lo, hi = config.TAX_RATE_BOUNDS
    if rate.ok:
        clamped = min(hi, max(lo, rate.value))
        note = f"effective tax rate {rate.value:.1%}" + (f" clamped to {clamped:.1%}" if clamped != rate.value else "")
        return clamped, note
    fb = config.STATUTORY_TAX_RATE_FALLBACK
    return fb, f"effective tax rate {rate.status}; fallback {fb:.0%} used"


def roa(f: Fundamentals) -> Datum:
    return safe_ratio(f.ttm("net_income"), f.bal("total_assets"), name="ROA", nonpositive_reason="total assets ≤ 0")


def roic(f: Fundamentals) -> ReturnMetric:
    ebit = ttm_ebit(f)
    rate, tax_note = tax_rate(f)
    ic = f.bal("invested_capital")
    if ic.ok and ic.value <= 0:
        return ReturnMetric(value=roa(f), label=ROA_SUBSTITUTE,
                            notes=[f"invested capital {ic.value:,.4g} ≤ 0"])
    if not ebit.ok:
        return ReturnMetric(value=Datum.missing(ebit.status), notes=[tax_note])
    nopat = combine(ebit.value * (1 - rate), {"EBIT": ebit}, label=f"NOPAT = EBIT × (1 − {rate:.1%})")
    value = safe_ratio(nopat, ic, name="ROIC", nonpositive_reason="invested capital ≤ 0",
                       num_name="NOPAT", den_name="invested capital")
    return ReturnMetric(value=value, label="ROIC", notes=[tax_note])


def roe(f: Fundamentals) -> Datum:
    return safe_ratio(f.ttm("net_income"), f.bal("stockholders_equity"), name="ROE",
                      nonpositive_reason="negative equity", num_name="net income", den_name="equity")


def ffo(f: Fundamentals) -> Datum:
    """Approximate funds from operations (TTM)."""
    ni = f.ttm("net_income")
    da = f.ttm("depreciation_amortization")
    if not da.ok:
        da = f.ttm("depreciation_cf")
    gains, note = optional_part(f.ttm("gain_on_sale_of_ppe"), "gains on property sales")
    bad = first_unusable(ni, da)
    if bad is not None:
        return Datum.missing(bad.status)
    out = sum_datums({"net income": ni, "D&A": da, "gains on sale": gains}, "FFO (approximate)",
                     signs={"gains on sale": -1})
    out.notes.append("FFO approximate: net income + D&A − gains on property sales")
    if note:
        out.notes.append(note)
    return out
