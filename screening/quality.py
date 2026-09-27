"""Quality score (SPEC "Scoring conventions"): the screener scatter's y-axis.

It measures the business, not the price: nothing divided by price or market
cap. QualityInputs has no price or market-cap field, so none can be passed in.
Each input maps to 1-10 by breakpoints; the score is the mean of the
available sub-scores, reported as "n of 4" (financials and REITs: ROE spread
and share trend, "n of 2").
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

import config
from data.values import Datum
from screening.models import QualityScore, SubScore
from signals.mapping import map_linear, map_step


class QualityInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    returns_spread: Datum = Field(default_factory=Datum.missing)  # ROIC (or ROA substitute / ROE) − COST_OF_CAPITAL
    returns_label: str = "ROIC spread"
    fcf_margin: Datum = Field(default_factory=Datum.missing)  # SBC-adjusted FCF / revenue
    net_debt: Datum = Field(default_factory=Datum.missing)
    ebitda: Datum = Field(default_factory=Datum.missing)
    net_debt_ebitda: Datum = Field(default_factory=Datum.missing)
    runway_months: Datum = Field(default_factory=Datum.missing)
    share_trend: Datum = Field(default_factory=Datum.missing)
    sector_adjusted: bool = False


def _linear(name: str, d: Datum, breakpoints, fmt: str) -> SubScore:
    if not d.ok:
        return SubScore(name=name, status=d.status)
    return SubScore(name=name, score=map_linear(breakpoints, d.value), input_display=format(d.value, fmt))


def leverage_subscore(q: QualityInputs) -> SubScore:
    """NET_DEBT_EBITDA_BREAKPOINTS when EBITDA > 0; Rule 2b cases otherwise."""
    name = "leverage"
    if q.ebitda.ok and q.ebitda.value <= 0:
        if q.net_debt.ok and q.net_debt.value > 0:
            return SubScore(name=name, score=config.SCORE_MIN, input_display="n/m - negative EBITDA with net debt")
        if q.net_debt.ok:
            if q.runway_months.ok:
                return SubScore(name=name, score=map_step(config.RUNWAY_BREAKPOINTS, q.runway_months.value),
                                input_display=f"negative EBITDA, net cash; runway {q.runway_months.value:.0f} months")
            return SubScore(name=name, status=f"negative EBITDA, net cash; runway {q.runway_months.status}")
    return _linear(name, q.net_debt_ebitda, config.NET_DEBT_EBITDA_BREAKPOINTS, ".2f")


def quality_score(q: QualityInputs) -> QualityScore:
    subs = [_linear(q.returns_label, q.returns_spread, config.QUALITY_ROIC_SPREAD_BREAKPOINTS, "+.1%")]
    if not q.sector_adjusted:
        subs.append(_linear("FCF margin", q.fcf_margin, config.QUALITY_FCF_MARGIN_BREAKPOINTS, "+.1%"))
        subs.append(leverage_subscore(q))
    subs.append(_linear("share trend", q.share_trend, config.QUALITY_SHARE_TREND_BREAKPOINTS, "+.1%"))
    scored = [s.score for s in subs if s.score is not None]
    return QualityScore(score=sum(scored) / len(scored) if scored else None, available=len(scored),
                        of=len(subs), subscores=subs, sector_adjusted=q.sector_adjusted)
