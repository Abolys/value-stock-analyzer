"""One flat metric map per ticker (config.THESIS_TRIGGER_FIELDS), read from an AnalysisRun's
typed fields. It feeds the sell triggers, the "then vs now" comparison and the stored
purchase snapshot, so all three agree on what a field means.

Missing inputs stay "N/A - …" and sign traps stay "n/m - …" (Rules 2 and 2b); nothing is
turned into zero. Lens scores come from whichever run is passed in: an alert check
recomputes Quant and Macro only, so Moat, the Devil's Advocate and the aggregate come
from the latest full analysis (see `merge_lens_scores`)."""

from __future__ import annotations

from datetime import date
from typing import Any

import config
from analysis.models import AnalysisRun, LensResult
from data.values import NA_INCOMPLETE, Datum
from portfolio.models import Metric, Thesis
from screening.models import SLOT_FCF, SLOT_MOS, SLOT_SHARES

LENS_FIELDS = {"quant_score": "quant", "macro_score": "macro", "moat_score": "moat",
               "devils_advocate_score": "devils_advocate"}


def _na(reason: str = NA_INCOMPLETE) -> Metric:
    return Metric(status=reason, display=reason)


def _num(v: float | None, fmt: str = "{:,.2f}", as_of: date | None = None) -> Metric:
    if v is None:
        return _na()
    return Metric(value=float(v), display=fmt.format(v), as_of=as_of)


def _datum(d: Datum | None, fmt: str = "{:,.2f}") -> Metric:
    if d is None:
        return _na()
    if not d.ok:
        return _na(d.status)
    return Metric(value=float(d.value), display=fmt.format(d.value), as_of=d.period_end)


def _flag(v: bool | None, as_of: date | None = None) -> Metric:
    if v is None:
        return _na()
    return Metric(value=bool(v), display="yes" if v else "no", as_of=as_of)


def _lens(lens: LensResult | None) -> Metric:
    if lens is None:
        return _na("N/A - lens not run")
    if not lens.ok:
        return _na(lens.status)
    return Metric(value=float(lens.score), display=f"{lens.score:.1f}", as_of=lens.fundamentals_as_of)


def extract_metrics(run: AnalysisRun, thesis: Thesis | None = None, source: str = "") -> dict[str, Metric]:
    s = run.screen
    out: dict[str, Metric] = {}
    out["price"] = _datum(s.price if s else None)
    for field, name in LENS_FIELDS.items():
        out[field] = _lens(run.lens(name))
    agg = run.aggregate
    out["aggregate_score"] = (Metric(value=agg.score, display=agg.display) if agg and agg.score is not None
                              else _na(agg.display if agg else "N/A - lens not run"))
    # Value-trap scores (screen result; financials get n/m with the reason).
    if s and s.piotroski is not None:
        p = s.piotroski
        out["piotroski"] = (Metric(value=float(p.score), display=p.display) if p.score is not None and p.status == "ok"
                            else _na(p.status if p.status != "ok" else NA_INCOMPLETE))
    else:
        out["piotroski"] = _na()
    if s and s.altman is not None:
        out["altman_z"] = _datum(s.altman.z)
        out["altman_zone"] = (Metric(value=s.altman.zone, display=s.altman.zone) if s.altman.zone
                              else _na(s.altman.z.status if not s.altman.z.ok else NA_INCOMPLETE))
    else:
        out["altman_z"] = out["altman_zone"] = _na()
    if s and s.beneish is not None:
        out["beneish_flag"] = _flag(s.beneish.flag) if s.beneish.m.ok else _na(s.beneish.m.status)
    else:
        out["beneish_flag"] = _na()
    # Balance sheet (Macro lens fields; n/m for negative EBITDA and for financials).
    macro = run.macro
    out["net_debt_ebitda"] = _datum(macro.net_debt_ebitda if macro else None)
    out["interest_coverage"] = _datum(macro.interest_coverage if macro else None)
    # Screen metrics. The FCF slot holds the FCF yield only for standard, FCF-positive names.
    fcf = s.metric(SLOT_FCF) if s else None
    if fcf is not None and fcf.fmt == "pct" and "FCF yield" in fcf.name:
        out["fcf_yield"] = _datum(fcf.value, "{:.2%}")
    else:
        out["fcf_yield"] = _na(f"N/A - not applicable ({fcf.name})" if fcf is not None else NA_INCOMPLETE)
    q = run.quant
    out["cash_runway_months"] = _datum(q.runway_months if q else None, "{:,.0f} mo")
    if q is not None and not q.runway_months.ok and q.runway_months.status == NA_INCOMPLETE and q.fcf_negative \
            and q.fcf_negative != "FCF-negative":
        out["cash_runway_months"] = _na(f"N/A - not applicable ({q.fcf_negative})")
    mos = s.metric(SLOT_MOS) if s else None
    out["margin_of_safety"] = _datum(mos.value if mos else None, "{:+.1%}")
    if q is not None and q.dcf is not None and q.dcf.ok:
        out["dcf_fair_value"] = _num(q.dcf.fair_value)
        out["dcf_upside"] = _num(q.dcf.upside, "{:+.1%}")
    else:
        if q is None:
            reason = "N/A - Quant lens not run"
        elif q.dcf is not None:
            reason = q.dcf.status
        elif not q.ok:
            reason = q.status
        else:
            reason = f"N/A - no DCF for the {q.method or 'Quant'} method"
        out["dcf_fair_value"] = out["dcf_upside"] = _na(reason)
    sh = s.metric(SLOT_SHARES) if s else None
    out["share_trend"] = _datum(sh.value if sh else None, "{:+.1%}/yr")
    w = run.week52
    out["drawdown"] = (Metric(value=w.drawdown, display=f"{-w.drawdown:+.0%}", as_of=w.as_of) if w
                       else _na("N/A - no 52-week range"))
    # Signals.
    lead = run.leadership
    out["leadership.flag"] = (Metric(value=lead.flag, display=lead.flag + (" (partial coverage)"
                                                                          if lead.partial_coverage else ""))
                              if lead and lead.flag in ("none", "flagged", "high")
                              else _na(lead.flag if lead else "N/A - leadership not checked"))
    ins = run.insiders
    out["insider_cluster_buy"] = (_flag(ins.cluster_buy) if ins is not None and ins.available
                                  else _na("N/A - no insider data source"))
    div = run.dividends
    out["dividend_at_risk"] = (_flag(div.at_risk) if div is not None and div.payer
                               else _na(div.status if div is not None else NA_INCOMPLETE))
    out["stale"] = _flag(run.stale, run.fundamentals_as_of)
    if thesis is not None:
        for k, v in thesis.levels().items():
            out[k] = _num(v) if v is not None else _na("N/A - not set in the thesis")
    else:
        for k in config.THESIS_LEVEL_FIELDS:
            out[k] = _na("N/A - no thesis")
    for m in out.values():
        m.source = m.source or source
    return out


def merge_lens_scores(check: dict[str, Metric], full: dict[str, Metric] | None, full_label: str) -> dict[str, Metric]:
    """A check recomputes Quant and Macro but not the LLM lenses: take Moat, the Devil's
    Advocate and the aggregate from the latest full analysis, labelled with its source."""
    out = dict(check)
    for k in ("moat_score", "devils_advocate_score", "aggregate_score"):
        if full is not None and k in full:
            out[k] = full[k].model_copy(update={"source": full_label})
        else:
            out[k] = _na("N/A - no full analysis yet")
    return out


def dump(metrics: dict[str, Metric]) -> dict[str, Any]:
    return {k: m.model_dump(mode="json") for k, m in metrics.items()}


def load(blob: dict[str, Any] | None) -> dict[str, Metric]:
    return {k: Metric.model_validate(v) for k, v in (blob or {}).items()}
