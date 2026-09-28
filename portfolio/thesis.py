"""Thesis at purchase and the thesis check.

- `purchase_snapshot`: adding a holding reuses today's finished full analysis of the
  ticker, or runs one, and freezes it (its analysis id plus the flattened metrics).
- `default_levels`: the thesis form's pre-fill from an analysis: intrinsic value = the
  DCF base-case fair value (or, when there is none, the Graham Number, labelled), buy-below
  MIN_MARGIN_OF_SAFETY below it, target = the intrinsic value.
- `then_vs_now` / `thesis_check`: each trigger evaluated on today's metrics, and the purchase
  snapshot compared field by field, with each change coloured by the field's direction.
"""

from __future__ import annotations

from datetime import date

import config
from analysis.models import AnalysisRun
from portfolio import metrics as pm
from portfolio.models import Holding, Metric, ThenNowRow, Thesis, ThesisCheck
from portfolio.triggers import evaluate, traffic_light
from storage import history

BETTER, WORSE, CHANGED, SAME, NA_CHANGE = "better", "worse", "changed", "same", "n/a"


def purchase_snapshot(ticker: str, ctx, llm=None, edgar=None, today: date | None = None) -> AnalysisRun:
    """Today's finished full analysis of the ticker, or a new one (LLM responses are cached)."""
    from analysis.pipeline import run_analysis

    run = history.latest_full_run(ticker, on_date=today or date.today(), path=ctx.db_path)
    if run is not None:
        return run
    return run_analysis(ctx, ticker, llm=llm, edgar=edgar)


def default_levels(run: AnalysisRun) -> dict:
    """intrinsic_value, buy_below_price, target_price and the basis text."""
    q = run.quant
    iv, basis = None, ""
    if q is not None and q.dcf is not None and q.dcf.ok:
        iv, basis = q.dcf.fair_value, f"DCF base case (analysis {run.analysis_id}, {run.today})"
    elif q is not None and q.graham.ok:
        iv, basis = q.graham.value, (f"Graham Number (DCF {q.dcf.status if q.dcf else 'not run'}; "
                                     f"analysis {run.analysis_id}, {run.today})")
    else:
        if q is None:
            why = "Quant lens not run"
        elif run.fund:
            why = "a fund has no company fair value"
        elif q.dcf is not None and not q.dcf.ok:
            why = q.dcf.status
        elif q.method == "runway":
            why = "cash-runway method (FCF-negative or unstable FCF base): no DCF or Graham fair value"
        elif not q.ok:
            why = q.status
        else:
            why = f"no fair value from the {q.method or 'Quant'} method"
        basis = f"No fair value from the analysis ({why}); enter your own"
    bb = iv * (1 - config.MIN_MARGIN_OF_SAFETY) if iv is not None else None
    return {"intrinsic_value": iv, "buy_below_price": bb, "target_price": iv, "basis": basis}


def _delta(then: Metric, now: Metric) -> float | None:
    if isinstance(then.value, bool) or isinstance(now.value, bool):
        return None
    if isinstance(then.value, (int, float)) and isinstance(now.value, (int, float)):
        return float(now.value) - float(then.value)
    return None


def then_vs_now(then: dict[str, Metric], now: dict[str, Metric],
                fields: list[str] | None = None) -> list[ThenNowRow]:
    rows = []
    for f in fields or config.THEN_VS_NOW_FIELDS:
        spec = config.THESIS_TRIGGER_FIELDS[f]
        a, b = then.get(f), now.get(f)
        a_disp = a.display if a is not None else "N/A - not in the purchase snapshot"
        b_disp = b.display if b is not None else "N/A - not available"
        if a is None or b is None or not a.ok or not b.ok:
            change = NA_CHANGE if (a is None or b is None or a.ok != b.ok) else SAME
            rows.append(ThenNowRow(field=f, label=spec["label"], then=a_disp, now=b_disp, change=change))
            continue
        d = _delta(a, b)
        if a.value == b.value:
            change = SAME
        elif d is not None and spec["direction"]:
            change = BETTER if (d > 0) == (spec["direction"] > 0) else WORSE
        else:
            change = CHANGED
        rows.append(ThenNowRow(field=f, label=spec["label"], then=a_disp, now=b_disp, change=change, delta=d))
    return rows


def with_levels(metrics: dict[str, Metric], thesis: Thesis | None) -> dict[str, Metric]:
    out = dict(metrics)
    if thesis is not None:
        for k, v in thesis.levels().items():
            out[k] = Metric(value=float(v), display=f"{v:,.2f}") if v is not None else Metric(
                status="N/A - not set in the thesis", display="N/A - not set in the thesis")
    return out


def thesis_check(h: Holding, now: dict[str, Metric], now_source: str = "") -> ThesisCheck:
    now = with_levels(now, h.thesis)
    statuses = [evaluate(t, now) for t in (h.thesis.triggers if h.thesis else [])]
    notes = []
    if not h.snapshot:
        notes.append("No purchase snapshot stored: 'then' is N/A")
    return ThesisCheck(holding_id=h.holding_id or 0, ticker=h.ticker, triggers=statuses, light=traffic_light(statuses),
                       then_now=then_vs_now(h.snapshot, now), now_source=now_source, notes=notes)


def snapshot_of(run: AnalysisRun, thesis: Thesis | None = None) -> dict[str, Metric]:
    return pm.extract_metrics(run, thesis, source=f"analysis {run.analysis_id} ({run.today})")
