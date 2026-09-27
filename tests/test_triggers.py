"""Sell triggers: only THESIS_TRIGGER_FIELDS, validated on save, evaluated structurally;
N/A and n/m values are "can't evaluate", never a silent pass or fire."""

from datetime import date

import pytest

from portfolio import store
from portfolio.models import FIRED, NEAR, OK_STATE, UNKNOWN, Metric, Thesis, Transaction, Trigger
from portfolio.triggers import TriggerError, evaluate, traffic_light, validate_trigger

BUY = Transaction(txn_date=date(2026, 1, 5), side="buy", shares=10, price=50)


def _m(v, status="ok"):
    return Metric(value=v, status=status, display=str(v) if status == "ok" else status)


def test_trigger_on_allowed_field_saves_and_fires():
    hid = store.add_holding("TEST", "TFSA", "USD", BUY, Thesis(triggers=[Trigger(field="piotroski", op="<", literal=5)]))
    t = store.get_holding(hid).thesis.triggers[0]
    assert t.text == "piotroski < 5"
    assert evaluate(t, {"piotroski": _m(4.0)}).state == FIRED
    assert evaluate(t, {"piotroski": _m(7.0)}).state == OK_STATE


def test_trigger_on_disallowed_field_is_rejected_at_save_and_nothing_is_saved():
    with pytest.raises(TriggerError, match="not an allowed trigger field"):
        store.add_holding("TEST", "TFSA", "USD", BUY,
                          Thesis(triggers=[Trigger(field="piotroski", op="<", literal=5),
                                           Trigger(field="foo", op=">", literal=1)]))
    assert store.list_holdings() == []
    hid = store.add_holding("TEST", "TFSA", "USD", BUY, Thesis())
    with pytest.raises(TriggerError):
        store.add_trigger(store.get_holding(hid).thesis.thesis_id, Trigger(field="__import__", op="==", literal="os"))
    assert store.get_holding(hid).thesis.triggers == []


@pytest.mark.parametrize("trigger, message", [
    (Trigger(field="dividend_at_risk", op="<", literal=True), "only == or != apply"),
    (Trigger(field="net_debt_ebitda", op=">", literal="3.5"), "needs a number"),
    (Trigger(field="net_debt_ebitda", op=">", literal=True), "needs a number"),
    (Trigger(field="leadership.flag", op="==", literal="severe"), "must be one of"),
    (Trigger(field="price", op=">=", ref="piotroski"), "not a thesis level"),
    (Trigger(field="stale", op="==", ref="target_price"), "can't be compared"),
    (Trigger(field="price", op="=>", literal=1.0), "not allowed"),
    (Trigger(field="price", op=">="), "exactly one value"),
])
def test_invalid_triggers_are_rejected(trigger, message):
    with pytest.raises(TriggerError, match=message):
        validate_trigger(trigger)


def test_price_against_a_thesis_level():
    t = validate_trigger(Trigger(field="price", op=">=", ref="target_price"))
    assert t.text == "price >= target_price"
    assert evaluate(t, {"price": _m(66.0), "target_price": _m(65.0)}).state == FIRED
    st = evaluate(t, {"price": _m(66.0), "target_price": Metric(status="N/A - not set in the thesis")})
    assert st.state == UNKNOWN and "not set" in st.reason


def test_enum_and_bool_triggers():
    t = validate_trigger(Trigger(field="leadership.flag", op="==", literal="high"))
    assert t.text == 'leadership.flag == "high"'
    assert evaluate(t, {"leadership.flag": _m("high")}).state == FIRED
    b = validate_trigger(Trigger(field="dividend_at_risk", op="==", literal=True))
    assert b.text == "dividend_at_risk == true"
    assert evaluate(b, {"dividend_at_risk": _m(False)}).state == OK_STATE


def test_nm_value_cannot_be_evaluated_and_never_fires():
    t = validate_trigger(Trigger(field="net_debt_ebitda", op=">", literal=3.5))
    st = evaluate(t, {"net_debt_ebitda": _m(None, "n/m - negative EBITDA")})
    assert st.state == UNKNOWN and st.reason == "n/m - negative EBITDA"
    assert traffic_light([st]) == "amber"


def test_near_threshold_is_amber_and_fired_is_red():
    t = validate_trigger(Trigger(field="net_debt_ebitda", op=">", literal=3.5))
    near = evaluate(t, {"net_debt_ebitda": _m(3.3)})
    assert near.state == NEAR and traffic_light([near]) == "amber"
    ok = evaluate(t, {"net_debt_ebitda": _m(2.0)})
    assert traffic_light([ok]) == "green"
    fired = evaluate(t, {"net_debt_ebitda": _m(3.6)})
    assert traffic_light([ok, near, fired]) == "red"
    assert traffic_light([]) == "none"
