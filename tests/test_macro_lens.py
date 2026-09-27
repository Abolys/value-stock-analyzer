"""Macro & Balance Sheet lens: sub-score breakpoints, Rule 2b cases, averaging, financials."""

import pytest

import config
from analysis.macro import altman_subscore, coverage_subscore, leverage_subscore, macro_lens
from data.values import Datum
from signals.trap_scores import AltmanResult
from tests.analysis_helpers import fixture_inputs, make_inputs
from tests.screen_helpers import make_fundamentals, make_info


@pytest.mark.parametrize("z,expected", [(-2.0, 1), (0.0, 1), (1.1, 3), (1.85, 5), (2.6, 7), (3.3, 8.5), (4.0, 10),
                                        (9.0, 10)])
def test_altman_subscore_follows_breakpoints(z, expected):
    x = make_inputs()
    x.screen.altman = AltmanResult(z=Datum(value=z), zone="grey")
    assert altman_subscore(x).output == pytest.approx(expected)


def test_altman_is_nm_for_jpm(fx_provider, db_path):
    m = macro_lens(fixture_inputs(fx_provider, "JPM", db_path))
    assert m.altman_z.status.startswith("n/m")
    step = next(s for s in m.mapping_steps if s.name == "Altman Z''")
    assert step.output is None and step.kind == "info"


def test_jpm_reduced_data_path(fx_provider, db_path):
    m = macro_lens(fixture_inputs(fx_provider, "JPM", db_path))
    assert m.reduced_data and m.ok
    used = {s.name.split(" (")[0] for s in m.mapping_steps if s.kind == "subscore"}
    assert used == {"Leverage trend", "Cyclicality"}
    assert m.net_debt_ebitda.is_nm and m.interest_coverage.is_nm
    assert "reduced data" in m.completeness
    assert "liabilities / equity (financials)" in m.mapping_line
    assert m.assumptions["LEVERAGE_TREND_FLAT_BAND"] == config.LEVERAGE_TREND_FLAT_BAND_FINANCIALS


def test_base_company_averages_available_subscores():
    # Net debt 50 / EBITDA 250 = 0.2x → 9.6; interest expense not reported with debt > 0 → excluded;
    # net debt/EBITDA by year 0.82x → 0.20x → falling → 8; Industrials → 5; Altman from the statements.
    m = macro_lens(make_inputs())
    lev = next(s for s in m.mapping_steps if s.name == "Net debt / EBITDA")
    assert lev.output == pytest.approx(9.6)
    assert m.leverage_trend == "falling"
    used = [s for s in m.mapping_steps if s.kind == "subscore"]
    assert m.subscores_used == len(used) == 4
    assert m.score == pytest.approx(round(sum(s.output for s in used) / 4, 2))
    assert "4 of 5 sub-scores" in m.completeness and "Interest coverage" in m.completeness


def test_ebit_nonpositive_coverage_is_1_even_without_interest():
    x = make_inputs(make_fundamentals({"ebit": (-50, 20), "operating_income": (-50, 20), "total_debt": (0, 0),
                                       "long_term_debt": (0, 0)}))
    step, _ = coverage_subscore(x)
    assert step.output == config.EBIT_NONPOSITIVE_COVERAGE_SCORE == 1


def test_no_interest_expense_scores_10_only_with_positive_ebit():
    x = make_inputs(make_fundamentals({"total_debt": (0, 0), "long_term_debt": (0, 0)}))
    step, _ = coverage_subscore(x)
    assert step.output == 10 and "no interest expense" in step.input_display


def test_interest_coverage_breakpoints():
    x = make_inputs(make_fundamentals({"interest_expense": (40, 40)}))  # EBIT 200 / 40 = 5x → 7
    step, cov = coverage_subscore(x)
    assert cov.value == pytest.approx(5.0) and step.output == pytest.approx(7)


def test_negative_ebitda_with_net_debt_scores_1():
    x = make_inputs(make_fundamentals({"ebitda": (-50, 30)}))  # net debt 50
    step, ratio = leverage_subscore(x)
    assert ratio.status == "n/m - negative EBITDA" and step.output == 1


def test_negative_ebitda_with_net_cash_scores_from_runway_never_10():
    # Net cash 250 − 100 = 150; TTM FCF −120 → runway 250 / 10 = 25 months → 4 (24–36 band).
    x = make_inputs(make_fundamentals({"ebitda": (-50, 30), "total_debt": (100, 100), "free_cash_flow": (-120, 10)}))
    step, ratio = leverage_subscore(x)
    assert ratio.status == "n/m - negative EBITDA, net cash"
    assert step.name == "Leverage from cash runway" and step.output == 4
    m = macro_lens(x)
    assert m.score < config.SCORE_MAX
    assert all(s.output != 10 for s in m.mapping_steps if "Leverage from" in s.name or s.name == "Net debt / EBITDA")


def test_negative_ebitda_net_cash_not_burning_gets_top_of_runway_table():
    x = make_inputs(make_fundamentals({"ebitda": (-50, 30), "total_debt": (100, 100)}))  # FCF +160
    step, _ = leverage_subscore(x)
    assert step.output == max(s for _, s in config.RUNWAY_BREAKPOINTS) < 10


def test_airline_cyclicality_override_used():
    m = macro_lens(make_inputs(info=make_info(sector="Industrials", industry="Airlines")))
    cyc = next(s for s in m.mapping_steps if s.name == "Cyclicality")
    assert cyc.output == 3


def test_maturity_proxy_is_labelled():
    m = macro_lens(make_inputs())
    assert "current vs long-term debt split" in m.maturity_note and "no specific maturity dates" in m.maturity_note
    assert m.maturity_note in m.rationale


def test_quant_and_macro_ignore_the_asset_floor():
    from analysis.quant import quant_lens

    x = make_inputs()
    y = make_inputs()
    y.screen.asset_floor = None
    assert quant_lens(x).score == quant_lens(y).score
    assert macro_lens(x).score == macro_lens(y).score
    assert quant_lens(x).mapping_line == quant_lens(y).mapping_line


@pytest.mark.parametrize("liabilities,direction", [((1100, 1200), "flat"), ((1100, 2500), "falling"),
                                                   ((2500, 1100), "rising")])
def test_financials_leverage_trend_uses_its_own_band(liabilities, direction):
    # Equity 100 → liabilities / equity 11.0x now vs 12.0x (−1.0x: flat), 25x (falling), or rising.
    info = make_info(sector="Financial Services", industry="Banks - Regional")
    f = make_fundamentals({"total_liabilities": liabilities, "stockholders_equity": (100, 100)})
    m = macro_lens(make_inputs(f, info=info))
    assert m.leverage_trend == direction
