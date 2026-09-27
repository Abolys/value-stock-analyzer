"""Aggregate verdict (SPEC "Scoring conventions").

Weighted mean (LENS_WEIGHTS) of the lenses that produced a score. Missing
lenses are excluded and the weights renormalised, never averaged in as zero;
the result says how many lenses it rests on ("6.8 (3 of 4 lenses)"). The label
comes from VERDICT_BANDS. "High controversy" fires when the Devil's Advocate is
at least CONTROVERSY_GAP below the mean of the other lenses that scored.
"""

from __future__ import annotations

import config
from analysis.models import LENSES, AggregateResult, LensResult


def verdict_label(score: float) -> str:
    for lower, label in config.VERDICT_BANDS:
        if score >= lower:
            return label
    return config.VERDICT_BANDS[-1][1]


def aggregate(results: dict[str, LensResult | None]) -> AggregateResult:
    res = AggregateResult(lenses_total=len(LENSES))
    scored = {k: r for k, r in results.items() if k in LENSES and r is not None and r.ok}
    res.missing = {k: (results[k].status if results.get(k) is not None else "lens did not run")
                   for k in LENSES if k not in scored}
    if not scored:
        res.rationale = f"Aggregate: Insufficient data (0 of {len(LENSES)} lenses scored)"
        return res
    total_w = sum(config.LENS_WEIGHTS[k] for k in scored)
    res.weights_used = {k: config.LENS_WEIGHTS[k] / total_w for k in scored}
    res.score = round(sum(res.weights_used[k] * r.score for k, r in scored.items()), 2)
    res.lenses_used = len(scored)
    res.verdict = verdict_label(res.score)
    others = [r.score for k, r in scored.items() if k != "devils_advocate"]
    da = scored.get("devils_advocate")
    if da is not None and others:
        mean_others = sum(others) / len(others)
        res.controversy_gap = round(mean_others - da.score, 2)
        res.controversy = res.controversy_gap >= config.CONTROVERSY_GAP
    parts = [f"{k} {r.score:.1f} × {res.weights_used[k]:.2f}" for k, r in scored.items()]
    lines = [f"**Aggregate — {res.display}: {res.verdict}**",
             "Weighted mean of available lenses (weights renormalised): " + " + ".join(parts)]
    for k, why in res.missing.items():
        lines.append(f"- Excluded {k}: {why}")
    if res.controversy_gap is not None:
        lines.append(f"- Devil's Advocate is {res.controversy_gap:+.1f} below the other lenses' mean"
                     + (f" — ⚑ high controversy (≥ {config.CONTROVERSY_GAP:g})" if res.controversy else ""))
    res.rationale = "\n".join(lines)
    return res
