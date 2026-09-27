"""Screen status rule, trap-risk and earnings-yield switches, and the quality score."""

import pytest

import config
from data.values import Datum
from screening.models import (
    FAIL, FAIL_DISPLAY, NA, NM, PASS, SLOT_EARNINGS_YIELD, STATUS_FAIL, STATUS_INCOMPLETE, STATUS_PASS, MetricResult,
)
from screening.quality import QualityInputs, quality_score
from screening.status import screen_status
from tests.screen_helpers import make_fundamentals, run_eval


def m(outcome, slot="x"):
    return MetricResult(slot=slot, name=slot, outcome=outcome,
                        value=Datum(value=1.0) if outcome in (PASS, FAIL) else Datum.missing(
                            "n/m - r" if outcome == NM else "N/A - Data Incomplete"))


@pytest.mark.parametrize("outcomes,expected", [
    ([PASS, PASS, PASS, NA], STATUS_PASS),        # all available pass, 3 available
    ([PASS, PASS, PASS, PASS], STATUS_PASS),
    ([PASS, FAIL, PASS, PASS], STATUS_FAIL),      # any fail
    ([PASS, PASS, NA, NA], STATUS_INCOMPLETE),    # nothing failed, only 2 available
    ([PASS, PASS, PASS, NM], STATUS_FAIL),        # n/m counts as available and failing
    ([NM, NA, NA, NA], STATUS_FAIL),
])
def test_screen_status_rule(outcomes, expected):
    status, reasons = screen_status([m(o, f"s{i}") for i, o in enumerate(outcomes)])
    assert status == expected and reasons


def test_fail_is_displayed_as_manual_only():
    res = run_eval(px=100)  # Graham 20.1 vs price 100 → fails margin of safety
    assert res.status == STATUS_FAIL and res.display_status == FAIL_DISPLAY


def test_trap_risk_fires_on_weak_piotroski_without_failing():
    # Everything deteriorates year on year → F-score 1 (only CFO > 0 passes... and CFO > NI)
    weak = make_fundamentals({
        "net_income": (100, 150), "total_assets": (2000, 1900), "long_term_debt": (400, 300),
        "current_assets": (500, 600), "diluted_shares": (110, 100), "gross_profit": (300, 400),
        "total_revenue": (900, 1000), "cost_of_revenue": (600, 600),
    })
    res = run_eval(weak)
    assert res.piotroski.score <= config.PIOTROSKI_WEAK
    assert res.trap_risk and any("Piotroski" in r for r in res.trap_risk_reasons)
    assert not any("trap" in r for r in res.status_reasons)
    assert "shown, not a Fail" in res.rationale


def distress():
    return make_fundamentals({"working_capital": (-900, 240), "retained_earnings": (-1500, 600),
                              "total_liabilities": (1900, 820), "stockholders_equity": (100, 1080)})


def test_trap_risk_fires_on_distress_zone_and_does_not_fail_by_default():
    res = run_eval(distress())
    assert res.altman.zone == "distress" and res.trap_risk
    base = run_eval()
    assert not base.trap_risk
    assert all("trap" not in r for r in res.status_reasons)  # any Fail comes from metrics, not the flag


def test_trap_risk_fails_screen_when_switched_on(monkeypatch):
    monkeypatch.setattr(config, "TRAP_RISK_FAILS_SCREEN", True)
    status, reasons = screen_status([m(PASS, f"s{i}") for i in range(4)], trap_risk=True)
    assert status == STATUS_FAIL and any("trap-risk" in r for r in reasons)
    res = run_eval(distress())
    assert res.status == STATUS_FAIL and any("trap-risk" in r for r in res.status_reasons)


def test_trap_risk_alone_passes_by_default():
    status, _ = screen_status([m(PASS, f"s{i}") for i in range(4)], trap_risk=True)
    assert status == STATUS_PASS


def test_earnings_yield_switch_adds_fifth_metric(monkeypatch):
    assert len(run_eval().metrics) == 4
    monkeypatch.setattr(config, "USE_EARNINGS_YIELD_IN_SCREEN", True)
    res = run_eval()
    assert len(res.metrics) == 5 and res.metric(SLOT_EARNINGS_YIELD) is not None
    assert res.min_metrics_for_pass == config.MIN_METRICS_FOR_PASS_WITH_EARNINGS_YIELD == 4
    assert res.metric(SLOT_EARNINGS_YIELD).outcome == PASS  # 200/1050 = 19% ≥ 8%
    # 3 of 5 available is no longer enough for a Pass.
    status, _ = screen_status([m(PASS, "a"), m(PASS, "b"), m(PASS, SLOT_EARNINGS_YIELD), m(NA, "c"), m(NA, "d")])
    assert status == STATUS_INCOMPLETE
    # Financials keep their four sector-adjusted slots (EV/EBIT is n/m for them).
    from tests.screen_helpers import make_info
    bank = run_eval(info=make_info("Financial Services", "Banks - Regional"))
    assert len(bank.metrics) == 4


# --------------------------------------------------------------------------
# Quality score
# --------------------------------------------------------------------------
PRICE_WORDS = ("price", "market", "cap", "yield", "margin_of_safety")


def test_quality_inputs_have_no_price_or_market_cap_field():
    fields = set(QualityInputs.model_fields)
    assert not any(w in f for f in fields for w in PRICE_WORDS), fields
    with pytest.raises(Exception):
        QualityInputs(market_cap=Datum(value=1))  # extra fields are forbidden


def test_quality_ignores_price_and_reports_n_of_4():
    cheap, dear = run_eval(px=5), run_eval(px=500)
    assert cheap.quality.score == dear.quality.score
    assert cheap.quality.display.endswith("(4 of 4)")


def test_quality_hand_computed():
    # ROIC = 200 × (1 − 40/190) / 1300 = 0.12146 → spread +3.146 pts → 5 + 2×(3.146/5) = 6.2583
    # FCF margin = (160 − 10)/1000 = 15% → 7 + 3×0.5 = 8.5
    # leverage = (300 − 250)/250 = 0.2x → 10 − 2×0.2 = 9.6
    # share trend −1%/yr → 6 + 4×(1/3) = 7.3333
    q = run_eval().quality
    scores = {s.name: s.score for s in q.subscores}
    assert scores["ROIC spread"] == pytest.approx(5 + 2 * ((200 * (1 - 40 / 190) / 1300 - 0.09) / 0.05))
    assert scores["FCF margin"] == pytest.approx(8.5)
    assert scores["leverage"] == pytest.approx(9.6)
    assert scores["share trend"] == pytest.approx(6 + 4 / 3)
    assert q.score == pytest.approx(sum(scores.values()) / 4)


def test_quality_missing_inputs_excluded_not_zero():
    q = quality_score(QualityInputs(share_trend=Datum(value=0.0)))
    assert q.available == 1 and q.of == 4 and q.score == 6.0
    assert q.display == "6.0 (1 of 4)"
