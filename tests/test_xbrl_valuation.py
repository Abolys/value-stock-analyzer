"""SEC EDGAR XBRL facts: point-in-time parsing, TTM from quarters (Q4 derived), the historical
valuation ratio and its recovery clock, and the 10-K debt maturity schedule."""

from datetime import date

import pandas as pd
import pytest

import config
from data.values import Datum
from data.xbrl import (
    NOT_REPORTED, debt_maturities, discrete_quarters, known_on, parse_company_facts, ttm_points,
)
from signals.valuation_history import valuation_recovery, valuation_recovery_months


def fact(start, end, val, filed, form="10-Q"):
    return {"start": start, "end": end, "val": val, "filed": filed, "form": form}


def facts_json(net_income=(), equity=(), shares=(), maturities=None):
    usgaap = {"NetIncomeLoss": {"units": {"USD": list(net_income)}},
              "StockholdersEquity": {"units": {"USD": list(equity)}}}
    for tag, rows in (maturities or {}).items():
        usgaap[tag] = {"units": {"USD": rows}}
    return {"cik": 1, "entityName": "Test Co", "facts": {
        "us-gaap": usgaap, "dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": list(shares)}}}}}


def test_first_filed_value_and_periodic_forms_only():
    data = facts_json(net_income=[
        fact("2025-01-01", "2025-03-31", 100, "2025-05-01"),
        fact("2025-01-01", "2025-03-31", 120, "2026-05-01"),  # restated a year later: not known in 2025
        fact("2025-01-01", "2025-03-31", 999, "2025-04-20", form="8-K"),  # not a periodic report
    ])
    c = parse_company_facts(data).concept("net_income")
    assert [(f.value, f.filed) for f in c.facts] == [(100, date(2025, 5, 1))]
    assert c.tag == "us-gaap:NetIncomeLoss"
    assert parse_company_facts(data).concept("debt_due_12m").status == NOT_REPORTED


def test_ttm_derives_q4_from_the_annual_report_and_is_known_from_its_filing_date():
    ni = [fact("2024-01-01", "2024-03-31", 10, "2024-05-01"), fact("2024-04-01", "2024-06-30", 20, "2024-08-01"),
          fact("2024-07-01", "2024-09-30", 30, "2024-11-01"),
          fact("2024-01-01", "2024-12-31", 100, "2025-02-20", form="10-K"),  # Q4 = 100 − 60 = 40
          fact("2025-01-01", "2025-03-31", 15, "2025-05-01")]
    c = parse_company_facts(facts_json(net_income=ni)).concept("net_income")
    q = discrete_quarters(c)
    assert [(x.end, x.value) for x in q][-2:] == [(date(2024, 12, 31), 40), (date(2025, 3, 31), 15)]
    pts = ttm_points(c)
    assert pts == [(date(2024, 12, 31), date(2025, 2, 20), 100.0), (date(2025, 3, 31), date(2025, 5, 1), 105.0)]
    days = pd.DatetimeIndex(["2025-02-19", "2025-02-20", "2025-05-02"])
    assert list(known_on(pts, days).fillna(-1)) == [-1, 100.0, 105.0]  # nothing before it was filed


def _world(ni_per_q=25.0, years=6, split_on=None):
    """Quarterly net income, equity 1,000 and 100 shares filed each quarter; daily closes."""
    ends = pd.date_range("2019-03-31", periods=years * 4, freq="QE")
    ni, eq, sh = [], [], []
    for e in ends:
        start = (e - pd.offsets.QuarterEnd(1) + pd.Timedelta(days=1)).date().isoformat()
        filed = (e + pd.Timedelta(days=35)).date().isoformat()
        ni.append(fact(start, e.date().isoformat(), ni_per_q, filed))
        eq.append({"end": e.date().isoformat(), "val": 1000.0, "filed": filed, "form": "10-Q"})
        after_split = split_on is not None and e >= pd.Timestamp(split_on)
        sh.append({"end": e.date().isoformat(), "val": 200.0 if after_split else 100.0, "filed": filed, "form": "10-Q"})
    return parse_company_facts(facts_json(net_income=ni, equity=eq, shares=sh))


def test_valuation_recovery_on_pe_with_a_timed_cheap_spell():
    facts = _world()
    days = pd.bdate_range("2020-06-01", "2024-12-31")
    closes = pd.Series(20.0, index=days)  # P/E = 20 × 100 / 100 = 20 most of the time
    closes[(days >= "2022-01-03") & (days < "2022-04-01")] = 10.0  # 50% below the median for ~3 months
    closes.iloc[-60:] = 12.0  # cheap again now
    v = valuation_recovery(facts, "ok", closes, None, sector_adjusted=False)
    assert v.ratio == "P/E" and v.median == pytest.approx(20.0) and v.current == pytest.approx(12.0)
    assert v.status == "ok" and len(v.spells) == 1 and v.median_months == pytest.approx(88 / config.DAYS_PER_MONTH, abs=0.1)
    assert "took a median 3 months to get back to it" in v.display and "low confidence" in v.display


def test_losses_switch_to_pb_and_financials_always_use_pb():
    facts = _world(ni_per_q=-5.0)  # losses: P/E is n/m every day
    closes = pd.Series(20.0, index=pd.bdate_range("2020-06-01", "2024-12-31"))
    v = valuation_recovery(facts, "ok", closes, None, sector_adjusted=False)
    assert v.ratio == "P/B" and any("P/E: meaningful on 0%" in n for n in v.notes)
    assert valuation_recovery(_world(), "ok", closes, None, sector_adjusted=True).ratio == "P/B"
    assert v.status == "not_cheap" and "nothing to time" in v.display


def test_share_counts_are_restated_for_later_splits():
    facts = _world(split_on="2023-01-03")  # filings report 100 shares before the split, 200 after
    days = pd.bdate_range("2020-06-01", "2024-12-31")
    closes = pd.Series(10.0, index=days)  # split-adjusted closes: the source restates the older ones
    splits = pd.Series({pd.Timestamp("2023-01-03"): 2.0})
    v = valuation_recovery(facts, "ok", closes, splits, sector_adjusted=False)
    # shares filed before the split (100) are restated to 200: P/E stays 10 × 200 / 100 = 20 throughout
    assert v.median == pytest.approx(20.0) and v.current == pytest.approx(20.0) and v.status == "not_cheap"


def test_without_facts_or_prices_the_reason_is_given():
    assert valuation_recovery(None, "N/A - not an SEC filer", None, None, False).status == \
        "Unavailable — N/A - not an SEC filer"
    assert valuation_recovery(_world(), "ok", pd.Series(dtype=float), None, False).status.startswith("Unavailable")
    bad_fx = valuation_recovery(_world(), "ok", pd.Series([1.0], index=[pd.Timestamp("2024-01-02")]), None, False,
                                fx=Datum.missing("N/A - FX rate USDCAD=X unavailable"))
    assert "currency conversion" in bad_fx.status


def test_spell_helper_ignores_wobbles_around_the_median():
    idx = pd.date_range(end="2026-01-31", periods=4 * 365, freq="D")
    pe = pd.Series(10.0, index=idx)
    pe.iloc[::2] = 9.8  # daily wobble 2% under the median: not a cheap spell
    pe.iloc[400:491] = 5.0
    med, spells = valuation_recovery_months(pe)
    assert len(spells) == 1


def test_debt_maturity_schedule_and_its_age():
    tags = {"LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths": 100,
            "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo": 200,
            "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive": 700}
    rows = {t: [{"end": "2025-12-31", "val": v, "filed": "2026-02-20", "form": "10-K"},
                {"end": "2024-12-31", "val": v / 2, "filed": "2025-02-20", "form": "10-K"}] for t, v in tags.items()}
    facts = parse_company_facts(facts_json(maturities=rows))
    dm = debt_maturities(facts, date(2026, 6, 1))
    assert dm.as_of == date(2025, 12, 31) and dm.buckets == {"next 12 months": 100, "year 2": 200, "after year 5": 700}
    assert dm.within_two_years == 300 and "30% due within 2 years" in dm.line()
    assert debt_maturities(facts, date(2026, 1, 15)).as_of == date(2024, 12, 31)  # the 2025 10-K isn't filed yet
    old = debt_maturities(facts, date(2028, 1, 1))
    assert old.status.startswith("N/A - the latest XBRL maturity schedule is at 2025-12-31")
    assert debt_maturities(parse_company_facts(facts_json()), date(2026, 1, 1)).status.startswith(NOT_REPORTED)


def test_golden_facts_load_through_the_edgar_fixtures(fx_provider, db_path):
    from data.fixture_provider import fixture_edgar
    from tests.analysis_helpers import fixture_inputs

    x = fixture_inputs(fx_provider, "HTZ", db_path, edgar=fixture_edgar())
    assert x.xbrl_status == "ok" and x.xbrl.concept("net_income").status == "ok"
    dm = debt_maturities(x.xbrl, x.today)
    assert dm.status == "ok" and "next 12 months" in dm.buckets
    no_edgar = fixture_inputs(fx_provider, "HTZ", db_path)
    assert no_edgar.xbrl is None and no_edgar.xbrl_status == "N/A - SEC EDGAR not loaded"
