"""Screen status rule (SPEC "Screener execution").

- Pass: every available metric passes and at least the minimum are available.
  An n/m counts as available and failing.
- Fail: any available metric fails (shown as "Fail (manual only)").
- Incomplete: nothing failed, but too few metrics were available.
The trap-risk flag turns a result into a Fail only when TRAP_RISK_FAILS_SCREEN is on.
"""

from __future__ import annotations

import config
from screening.models import SLOT_EARNINGS_YIELD, STATUS_FAIL, STATUS_INCOMPLETE, STATUS_PASS, MetricResult


def min_metrics_for_pass(metrics: list[MetricResult]) -> int:
    if any(m.slot == SLOT_EARNINGS_YIELD for m in metrics):
        return config.MIN_METRICS_FOR_PASS_WITH_EARNINGS_YIELD
    return config.MIN_METRICS_FOR_PASS


def screen_status(metrics: list[MetricResult], trap_risk: bool = False) -> tuple[str, list[str]]:
    available = [m for m in metrics if m.available]
    failing = [m for m in available if m.failing]
    reasons = [f"{m.name}: {m.display} ({m.outcome}, needs {m.threshold})" for m in failing]
    if trap_risk and config.TRAP_RISK_FAILS_SCREEN:
        reasons.append("trap-risk flag (TRAP_RISK_FAILS_SCREEN is on)")
    if reasons:
        return STATUS_FAIL, reasons
    need = min_metrics_for_pass(metrics)
    if len(available) >= need:
        return STATUS_PASS, [f"all {len(available)} available metrics pass"]
    return STATUS_INCOMPLETE, [f"only {len(available)} of {len(metrics)} metrics available (needs {need})"]
