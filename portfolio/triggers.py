"""Sell triggers: structured rules `field op value` on config.THESIS_TRIGGER_FIELDS only.

Rules are validated on save and evaluated by dispatching the operator through the
`operator` module; no text is ever evaluated. A current value that is N/A or n/m
makes the trigger "can't evaluate" with the reason: it neither fires nor passes
silently (Rules 2 and 2b).
"""

from __future__ import annotations

import operator as op_module

import config
from portfolio.models import (
    FIRED, LIGHT_AMBER, LIGHT_GREEN, LIGHT_NONE, LIGHT_RED, NEAR, OK_STATE, UNKNOWN, Metric, Trigger, TriggerStatus,
)

OPS = {"<": op_module.lt, "<=": op_module.le, ">": op_module.gt, ">=": op_module.ge, "==": op_module.eq,
       "!=": op_module.ne}


class TriggerError(ValueError):
    """A trigger that fails validation; the message says why."""


def field_spec(field: str) -> dict:
    spec = config.THESIS_TRIGGER_FIELDS.get(field)
    if spec is None:
        raise TriggerError(f"'{field}' is not an allowed trigger field (THESIS_TRIGGER_FIELDS)")
    return spec


def allowed_operators(field: str) -> list[str]:
    spec = field_spec(field)
    ops = config.TRIGGER_OPERATORS
    return [o for o in ops if o in config.TRIGGER_EQUALITY_OPERATORS] if spec["type"] in ("bool", "enum") else list(ops)


def validate_trigger(t: Trigger) -> Trigger:
    """Check a trigger before it is saved. Returns a normalised copy or raises TriggerError."""
    spec = field_spec(t.field)
    if t.op not in config.TRIGGER_OPERATORS or t.op not in OPS:
        raise TriggerError(f"operator '{t.op}' is not allowed (use one of {', '.join(config.TRIGGER_OPERATORS)})")
    if t.op not in allowed_operators(t.field):
        raise TriggerError(f"'{t.field}' is a {spec['type']} field: only {' or '.join(allowed_operators(t.field))} apply")
    if (t.ref is None) == (t.literal is None):
        raise TriggerError("a trigger needs exactly one value: a number/choice or a thesis level")
    kind = spec["type"]
    if t.ref is not None:
        if t.ref not in config.THESIS_LEVEL_FIELDS:
            raise TriggerError(f"'{t.ref}' is not a thesis level ({', '.join(config.THESIS_LEVEL_FIELDS)})")
        if kind != "number":
            raise TriggerError(f"'{t.field}' is a {kind} field and can't be compared with a price level")
        return t.model_copy()
    v = t.literal
    if kind == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise TriggerError(f"'{t.field}' needs a number, got {v!r}")
        return t.model_copy(update={"literal": float(v)})
    if kind == "bool":
        if not isinstance(v, bool):
            raise TriggerError(f"'{t.field}' needs true or false, got {v!r}")
        return t.model_copy()
    choices = spec.get("choices", [])
    if not isinstance(v, str) or v not in choices:
        raise TriggerError(f"'{t.field}' must be one of {', '.join(choices)}, got {v!r}")
    return t.model_copy()


def _threshold(t: Trigger, metrics: dict[str, Metric]) -> tuple[object | None, str, str]:
    """(value compared against, its display, reason when unavailable)."""
    if t.ref is None:
        disp = t.text.split(f" {t.op} ", 1)[1]
        return t.literal, disp, ""
    m = metrics.get(t.ref)
    if m is None or not m.ok:
        return None, t.ref, f"{t.ref}: {m.status if m else 'N/A - not set in the thesis'}"
    return m.value, f"{m.value:,.2f} ({t.ref})", ""


def evaluate(t: Trigger, metrics: dict[str, Metric]) -> TriggerStatus:
    cur = metrics.get(t.field)
    thr, thr_disp, thr_reason = _threshold(t, metrics)
    if cur is None or not cur.ok:
        reason = cur.status if cur is not None else "N/A - field not available"
        return TriggerStatus(trigger=t, state=UNKNOWN, current=reason, threshold=thr_disp, reason=reason)
    if thr is None:
        return TriggerStatus(trigger=t, state=UNKNOWN, current=cur.display, threshold=thr_disp, reason=thr_reason)
    fired = bool(OPS[t.op](cur.value, thr))
    if fired:
        return TriggerStatus(trigger=t, state=FIRED, current=cur.display, threshold=thr_disp)
    state = OK_STATE
    if (config.THESIS_TRIGGER_FIELDS[t.field]["type"] == "number" and t.op in ("<", "<=", ">", ">=")
            and abs(cur.value - thr) <= config.TRIGGER_NEAR_BAND * abs(thr)):
        state = NEAR
    return TriggerStatus(trigger=t, state=state, current=cur.display, threshold=thr_disp)


def traffic_light(statuses: list[TriggerStatus]) -> str:
    if not statuses:
        return LIGHT_NONE
    states = {s.state for s in statuses}
    if FIRED in states:
        return LIGHT_RED
    if NEAR in states or UNKNOWN in states:
        return LIGHT_AMBER
    return LIGHT_GREEN


def light_label(statuses: list[TriggerStatus]) -> str:
    fired = sum(s.state == FIRED for s in statuses)
    near = sum(s.state == NEAR for s in statuses)
    unknown = sum(s.state == UNKNOWN for s in statuses)
    if not statuses:
        return "no triggers"
    if fired:
        return f"{fired} fired"
    parts = ([f"{near} near"] if near else []) + ([f"{unknown} can't evaluate"] if unknown else [])
    return ", ".join(parts) or "ok"
