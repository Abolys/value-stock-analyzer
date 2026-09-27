"""Quantitative Fundamental lens: score mapping, Rule 2b branches, confidence, financials."""

import pytest

import config
from analysis.quant import combine_steps, piotroski_adjustment, quant_lens, returns_adjustment
from data.values import Datum
from signals.mapping import map_linear
from signals.trap_scores import PiotroskiResult
from tests.analysis_helpers import fixture_inputs, make_inputs
from tests.screen_helpers import P, P2, T, make_fundamentals, make_info


def test_base_company_scores_by_the_visible_mapping():
    q = quant_lens(make_inputs())
    assert q.ok and q.method == "dcf"
    base, roic_step, piot = q.mapping_steps
    assert base.name == "DCF upside" and base.output == pytest.approx(
        map_linear(config.QUANT_UPSIDE_BREAKPOINTS, q.dcf.upside), abs=0.01)
    assert q.score == pytest.approx(combine_steps(q.mapping_steps))
    assert q.mapping_line.startswith("DCF upside") and q.mapping_line.endswith(f"score {q.score:.1f}")
    assert "Mapping:" in q.rationale and q.mapping_line in q.rationale
    for key in ("base_fcf", "stage1_growth", "discount_rate (COST_OF_CAPITAL)", "terminal_growth", "net_cash"):
        assert key in q.assumptions


@pytest.mark.parametrize("roic,adj", [(0.15, 1.0), (0.14, 1.0), (0.10, 0.0), (0.089, -1.0)])
def test_roic_bonus_and_penalty(roic, adj):
    # COST_OF_CAPITAL 9%: spread ≥ 5 pts → +1; below 9% → −1; in between → 0.
    assert returns_adjustment(Datum(value=roic), "ROIC").output == adj


@pytest.mark.parametrize("score,adj", [(9, 0.5), (7, 0.5), (6, 0.0), (4, 0.0), (3, -1.0), (0, -1.0)])
def test_piotroski_adjustment_at_7_and_3(score, adj):
    x = make_inputs()
    x.screen.piotroski = PiotroskiResult(score=score, available=9)
    assert piotroski_adjustment(x).output == adj


def test_piotroski_insufficient_gives_no_adjustment():
    x = make_inputs()
    x.screen.piotroski = PiotroskiResult(score=None, available=5, status="Insufficient data")
    step = piotroski_adjustment(x)
    assert step.output is None and step.kind == "info"


def test_piotroski_applied_after_roic_in_the_mapping_line():
    q = quant_lens(make_inputs())
    assert [s.name for s in q.mapping_steps][:3] == ["DCF upside", "ROIC spread", "Piotroski"]


def test_single_cost_of_capital_in_dcf_and_roic(monkeypatch):
    monkeypatch.setattr(config, "COST_OF_CAPITAL", 0.12)
    q = quant_lens(make_inputs())
    assert q.dcf.inputs.rate == 0.12 and q.assumptions["discount_rate (COST_OF_CAPITAL)"] == 0.12
    assert "vs 12%" in q.mapping_steps[1].input_display


def test_fcf_negative_uses_runway_with_cap_and_no_roic_step():
    # FCF negative in both years; runway = 250 / (10/12) = 300 months → 5; Piotroski 9 → +0.5 → capped at 5.
    f = make_fundamentals({"free_cash_flow": (-10, -20)})
    q = quant_lens(make_inputs(f))
    assert q.method == "runway" and q.dcf is None
    assert q.runway_months.value == pytest.approx(300)
    assert not any("ROIC" in s.name for s in q.mapping_steps)
    assert q.score == config.RUNWAY_SCORE_CAP
    assert "capped at 5" in q.mapping_line


def test_runway_breakpoint_score():
    # Burn 125/yr on cash 250 → 24 months → 4 (24–36), Piotroski 9 → +0.5 → 4.5.
    q = quant_lens(make_inputs(make_fundamentals({"free_cash_flow": (-125, -20)})))
    assert q.mapping_steps[0].output == 4 and q.score == 4.5


def test_sign_flip_is_unstable_base_and_switches_to_runway():
    # 1 of 2 years negative: not structurally FCF-negative, but the base flips sign (Rule 2b).
    # Latest year −120 → burn 120/yr on 250 cash → 25 months → 4.
    q = quant_lens(make_inputs(make_fundamentals({"free_cash_flow": (-120, 125)})))
    assert q.method == "runway" and q.base.status == "Insufficient data - unstable FCF base"
    assert q.mapping_steps[0].output == 4


def test_unstable_base_without_burn_is_insufficient_data():
    q = quant_lens(make_inputs(make_fundamentals({"free_cash_flow": (160, -125)})))
    assert not q.ok and q.status.startswith("Insufficient data - unstable FCF base")
    assert "not burning cash" in q.status


def test_too_little_history_is_insufficient():
    q = quant_lens(make_inputs(make_fundamentals(years=(T,))))
    assert q.status.startswith("Insufficient data - too little history")


def test_roic_nm_switches_to_return_on_assets():
    q = quant_lens(make_inputs(make_fundamentals({"invested_capital": (-100, -100)})))
    assert "ROA" in q.returns_label and q.returns.value == pytest.approx(150 / 2000)
    assert "ROA" in q.mapping_steps[1].name


def test_graham_disagreement_lowers_confidence_but_not_score():
    fair = quant_lens(make_inputs()).dcf.fair_value
    graham = (22.5 * 1.5 * 12) ** 0.5  # 20.12
    assert fair > graham
    px = (fair + graham) / 2  # DCF says undervalued, Graham says overvalued
    q = quant_lens(make_inputs(px=px))
    assert q.confidence == config.QUANT_CONFIDENCE_LOW and "disagree" in q.confidence_reasons[0]
    assert q.score == pytest.approx(combine_steps(q.mapping_steps))
    assert all("Graham" not in s.name for s in q.mapping_steps)


def test_peak_margin_cyclical_normalised_and_low_confidence():
    # Operating margins 40%, 10%, 10% → flagged. Raw base = (300 + 40 + 40) / 3 = 126.7;
    # normalised = TTM revenue 1000 × average FCF margin (30% + 5% + 5%) / 3 = 133.3.
    f = make_fundamentals({"total_revenue": (1000, 800, 800), "operating_income": (400, 80, 80),
                           "free_cash_flow": (300, 40, 40), "cost_of_revenue": (500, 400, 400),
                           "gross_profit": (500, 400, 400)}, years=(T, P, P2))
    q = quant_lens(make_inputs(f, info=make_info(sector="Basic Materials", industry="Steel")))
    assert q.peak.flagged
    assert q.confidence == config.QUANT_CONFIDENCE_LOW
    assert q.dcf_raw is not None and q.dcf_raw.inputs.base == pytest.approx(380 / 3)
    assert q.dcf.inputs.base == pytest.approx(q.peak.normalised_base) == pytest.approx(400 / 3)
    assert q.dcf_raw.fair_value != pytest.approx(q.dcf.fair_value)
    assert "Fair value (raw, unnormalised)" in q.key_figures
    assert q.mapping_steps[0].name == "DCF upside (normalised)"


def test_reverse_dcf_and_range_do_not_change_the_score():
    q = quant_lens(make_inputs())
    assert q.reverse_dcf.status in ("ok", "beyond range") and q.grid.range_low is not None
    assert all(s.name not in ("Reverse DCF", "Sensitivity") for s in q.mapping_steps)


def test_lcid_skips_the_dcf(fx_provider, db_path):
    q = quant_lens(fixture_inputs(fx_provider, "LCID", db_path))
    assert q.method == "runway" and q.dcf is None and q.score <= config.RUNWAY_SCORE_CAP


def test_jpm_uses_sector_adjusted_excess_return(fx_provider, db_path):
    q = quant_lens(fixture_inputs(fx_provider, "JPM", db_path))
    assert q.method == "excess_return" and q.returns_label == "ROE"
    assert q.reverse_dcf.status.startswith("n/m") and q.grid.status.startswith("n/m")
    assert q.mapping_steps[0].name == "Excess-return upside"


def test_bank_with_negative_equity_is_insufficient():
    info = make_info(sector="Financial Services", industry="Banks - Regional")
    q = quant_lens(make_inputs(make_fundamentals({"stockholders_equity": (-100, -50)}), info=info))
    assert not q.ok and "negative equity" in q.status
