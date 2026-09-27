"""The single Rule 2b ratio helper: N/A vs n/m, periods, market cap."""

import logging
from datetime import date

from data.ratios import market_cap, safe_ratio, sum_datums
from data.values import NA_INCOMPLETE, Datum, na_field_not_found


def d(v, end=None):
    return Datum(value=v, period_end=end)


def test_ratio_value_and_latest_period():
    r = safe_ratio(d(50, date(2026, 3, 31)), d(200, date(2026, 6, 30)), name="x", nonpositive_reason="y ≤ 0")
    assert r.ok and r.value == 0.25 and r.period_end == date(2026, 6, 30)
    assert not any("mixed periods" in n for n in r.notes)


def test_missing_input_is_na_not_zero_and_keeps_field_not_found():
    r = safe_ratio(Datum.missing(), d(10), name="x", nonpositive_reason="y ≤ 0")
    assert r.status == NA_INCOMPLETE and r.value is None
    r = safe_ratio(d(1), Datum.missing(na_field_not_found("ebitda")), name="x", nonpositive_reason="y ≤ 0")
    assert r.status == "N/A - field not found: ebitda"


def test_nonpositive_denominator_is_nm_with_reason():
    for den in (0.0, -5.0):
        r = safe_ratio(d(10), d(den), name="ND/EBITDA", nonpositive_reason="negative EBITDA")
        assert r.status == "n/m - negative EBITDA" and r.is_nm and not r.is_na and r.value is None


def test_mixed_periods_marked():
    r = safe_ratio(d(1, date(2025, 6, 30)), d(2, date(2026, 6, 30)), name="x", nonpositive_reason="z")
    assert r.ok and any(n.startswith("mixed periods") for n in r.notes)


def test_sum_never_treats_missing_as_zero():
    assert sum_datums({"a": d(1), "b": Datum.missing()}, "a+b").status == NA_INCOMPLETE
    assert sum_datums({"a": d(5), "b": d(2)}, "a−b", signs={"b": -1}).value == 3


def test_market_cap_is_shares_times_price_and_zero_is_a_logged_data_error(caplog):
    mc = market_cap(d(100, date(2026, 6, 30)), d(12.5, date(2026, 9, 25)))
    assert mc.value == 1250 and "actual latest price (2026-09-25)" in mc.period_label
    assert mc.period_end is None  # the price is live: ratios are dated by their fundamentals
    with caplog.at_level(logging.WARNING):
        zero = market_cap(d(0), d(10), "ZERO")
        missing = market_cap(Datum.missing(), d(10), "MISS")
    assert zero.is_na and "data error" in zero.status
    assert missing.is_na
    assert "ZERO" in caplog.text and "MISS" in caplog.text
