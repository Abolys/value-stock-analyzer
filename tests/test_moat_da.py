"""Moat prompt content and the Devil's Advocate payload."""

import json

import pytest

import config
from analysis.devils_advocate import build_da_payload, devils_advocate_lens
from analysis.macro import macro_lens
from analysis.moat import moat_lens, moat_request, threat_hint
from analysis.quant import quant_lens
from data.leadership import LeadershipResult
from llm import prompts
from llm.cache import LLMCache
from llm.client import LLMClient
from tests.analysis_helpers import fixture_inputs, make_inputs
from tests.llm_fakes import DA_OK, FakeAPI
from tests.screen_helpers import make_fundamentals

# Every field of the example payload in the phase prompt, plus the other signals the SPEC routes to the DA.
EXAMPLE_KEYS = ["dcf_implied_upside", "cash_runway_months", "roic_vs_cost_of_capital", "net_debt_ebitda",
                "moat_threat", "leadership", "fundamentals_as_of", "stale", "mixed_periods", "piotroski",
                "altman_zone", "beneish_flag", "implied_growth", "fair_value_range", "peak_earnings",
                "ev_ebit_yield", "insiders_6mo", "dividend", "asset_floor", "insider_ownership", "short_interest",
                "estimate_revisions_90d"]
SIGNAL_KEYS = EXAMPLE_KEYS + ["piotroski_accrual_check", "beneish_m", "altman_z", "graham_number",
                              "interest_coverage", "leverage_trend", "debt_maturity_proxy", "share_count_trend",
                              "dilution_flag", "not_meaningful", "lenses"]
ASSET_FLOOR_KEYS = {"tbv", "p_tbv", "coverage", "ncav_to_mcap", "nnwc_to_mcap", "net_net"}


def lenses(x):
    return quant_lens(x), macro_lens(x), None


def test_lulu_moat_prompt_has_the_retail_threat_instruction(fx_provider, db_path):
    x = fixture_inputs(fx_provider, "LULU", db_path)
    payload, system, user = moat_request(x)
    assert payload["industry"] == "Apparel Retail"
    assert "Sector threat instruction" in user
    assert "private-label" in payload["sector_threat_hint"] and "brand" in payload["sector_threat_hint"]
    assert "AI disruption" not in payload["sector_threat_hint"]
    assert "single most relevant threat" in system and "Do not default to AI disruption" in system
    assert "gross_margin_by_fy" in payload and "roic_by_fy" in payload


def test_threat_hints_by_industry_then_sector():
    assert "AI disruption" in threat_hint("Technology", "Software - Infrastructure")
    assert "regulation" in threat_hint("Financial Services", "Banks - Diversified")
    assert threat_hint("Technology", "Semiconductors") == config.SECTOR_THREAT_HINTS["Technology"]


def test_moat_result_returns_the_identified_threat(tmp_path, fx_provider, db_path):
    x = fixture_inputs(fx_provider, "LULU", db_path)
    r = moat_lens(x, LLMClient(api=FakeAPI(), cache=LLMCache(tmp_path / "c.db"), db_path=tmp_path / "r.db"))
    assert r.ok and r.sector_threat == "private-label erosion" and len(r.evidence) >= 2
    assert "threat" in r.mapping_line and r.mapping_line.endswith("score 6.0")


def test_da_payload_is_compact_json_without_markdown():
    x = make_inputs()
    payload = build_da_payload(x, *lenses(x))
    text = prompts.payload_json(payload)
    assert "\n" not in text and "**" not in text and "#" not in text and "- " not in text[:1]
    assert json.loads(text) == json.loads(json.dumps(payload, default=str))
    assert len(text) < 5000
    user = prompts.da_user(payload)
    assert user.count("<payload>") == 1 and text in user
    # One line per lens: bull point and risk, not a report.
    for lens in ("quant", "macro", "moat"):
        assert set(payload["lenses"][lens]) == {"score", "bull", "risk"}


def test_da_payload_includes_every_signal_with_na_or_coverage():
    x = make_inputs()  # no EDGAR, no dividends, no leadership, no estimates
    payload = build_da_payload(x, *lenses(x))
    for k in SIGNAL_KEYS:
        assert k in payload, k
    assert payload["insiders_6mo"].startswith("N/A - no insider data source")
    assert payload["dividend"] == "N/A - no dividend"
    assert payload["leadership"]["flag"].startswith("N/A") and payload["leadership"]["status_rule"] == "unknown"
    assert payload["estimate_revisions_90d"].startswith("N/A")
    assert payload["moat_threat"].startswith("N/A")
    assert payload["lenses"]["moat"]["score"].startswith("N/A")
    assert ASSET_FLOOR_KEYS <= set(payload["asset_floor"])


def test_da_payload_asset_floor_missing_is_na():
    x = make_inputs()
    x.screen.asset_floor = None
    assert build_da_payload(x, *lenses(x))["asset_floor"].startswith("N/A")


def test_da_payload_with_edgar_coverage(fx_provider, db_path):
    from data.fixture_provider import fixture_edgar

    x = fixture_inputs(fx_provider, "LULU", db_path, edgar=fixture_edgar())
    payload = build_da_payload(x, *lenses(x))
    assert payload["insiders_6mo"]["coverage"] == "Form 4, full history"
    assert "8-K, full history" in payload["leadership"]["coverage"]
    assert set(payload["asset_floor"]) >= ASSET_FLOOR_KEYS


def test_nm_values_reach_the_devils_advocate_with_reasons():
    x = make_inputs(make_fundamentals({"ebitda": (-50, 30), "invested_capital": (-10, -10)}))
    payload = build_da_payload(x, *lenses(x))
    assert payload["net_debt_ebitda"] == "n/m - negative EBITDA"
    assert payload["not_meaningful"]["net_debt_ebitda"] == "n/m - negative EBITDA"
    assert "cash_runway_months" in payload["not_meaningful"]  # "n/m - not burning cash"


def test_jpm_payload_carries_financials_nm_reasons(fx_provider, db_path):
    x = fixture_inputs(fx_provider, "JPM", db_path)
    payload = build_da_payload(x, *lenses(x))
    for k in ("piotroski", "altman_z", "beneish_m", "net_debt_ebitda", "ev_ebit_yield", "asset_floor.ncav"):
        assert payload["not_meaningful"][k].startswith("n/m - not meaningful for financials"), k
    assert payload["cash_runway_months"].startswith("n/m")
    assert payload["asset_floor"]["p_tbv"].endswith("x")


def test_stale_payload_is_marked():
    from tests.screen_helpers import T
    from datetime import timedelta

    x = make_inputs(today=T + timedelta(days=config.STALE_FUNDAMENTALS_DAYS + 30))
    payload = build_da_payload(x, *lenses(x))
    assert payload["stale"] is True and "stale" in payload["stale_detail"]


def test_partial_leadership_coverage_cannot_be_reported_clean(tmp_path):
    lead = LeadershipResult(ticker="TEST", flag="none", departures=0, partial_coverage=True,
                            coverage_label="officer tracking since 2026-08-01 (partial coverage)")
    x = make_inputs(leadership=lead)
    api = FakeAPI({"DevilsAdvocateResponse": {**DA_OK, "leadership_status": "none found",
                                              "leadership_turnover": "No departures."}})
    r = devils_advocate_lens(x, *lenses(x), LLMClient(api=api, cache=LLMCache(tmp_path / "c.db"),
                                                       db_path=tmp_path / "r.db"))
    assert len(api.requests) == 1  # corrected deterministically, no retry call
    assert r.ok and r.leadership_status == "unknown" and r.key_figures["Leadership"] == "unknown"
    assert r.leadership_turnover.startswith("Unknown: coverage is partial or missing (officer tracking since")
    assert "corrected from 'none found' to 'unknown'" in r.rationale


def test_full_coverage_none_found_is_kept(tmp_path):
    lead = LeadershipResult(ticker="TEST", flag="none", departures=0, coverage_label="8-K, full history")
    x = make_inputs(leadership=lead)
    api = FakeAPI({"DevilsAdvocateResponse": {**DA_OK, "leadership_status": "none found"}})
    r = devils_advocate_lens(x, *lenses(x), LLMClient(api=api, cache=LLMCache(tmp_path / "c.db"),
                                                       db_path=tmp_path / "r.db"))
    assert r.leadership_status == "none found" and not r.notes


def test_da_result_fields(tmp_path):
    x = make_inputs()
    r = devils_advocate_lens(x, *lenses(x), LLMClient(api=FakeAPI(), cache=LLMCache(tmp_path / "c.db"),
                                                       db_path=tmp_path / "r.db"))
    assert r.ok and r.impairment_type == "cyclical" and r.score == 5.0
    assert r.bull_case_requirements and r.weakest_valuation_assumption and r.leadership_turnover
    assert "Leadership turnover" in r.rationale
