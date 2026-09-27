"""Run the golden tickers through everything built so far, offline from
tests/fixtures, and check each hits the branch it is listed for
(SPEC "Golden test tickers"). Later phases extend CHECKS.

    python scripts/golden_check.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from data import corporate_actions as ca  # noqa: E402
from data import currency, periods, prices, sector, shares  # noqa: E402
from data.fixture_provider import captured_on, fixture_edgar, fixture_provider  # noqa: E402
from data.leadership import LAYER_8K, leadership_flag  # noqa: E402
from data.provider import DataProvider  # noqa: E402


def run_all(provider: DataProvider, ticker: str, db_path: Path) -> dict:
    """Everything Phase 1 builds, for one ticker. Raises on any unexpected exception."""
    today = captured_on(ticker)
    info = provider.get_info(ticker)
    stmts = {(k, f): provider.get_statement(ticker, k, f)
             for k in ("income", "balance", "cashflow") for f in ("annual", "quarterly")}
    mismatch = currency.detect_mismatch(info)
    fx = currency.fx_rate(provider, *mismatch) if mismatch else None
    info_vals = currency.info_datums(info, fx)
    stmts = {k: currency.to_trading_currency(provider, info, s) for k, s in stmts.items()}
    fcf = periods.ttm(stmts[("cashflow", "quarterly")], stmts[("cashflow", "annual")], "free_cash_flow")
    debt = periods.latest_balance(stmts[("balance", "quarterly")], stmts[("balance", "annual")], "total_debt")
    px = prices.actual_closes(provider, ticker)
    splits = provider.get_splits(ticker)
    sh = provider.get_shares_history(ticker)
    breaks = ca.corporate_action_breaks(ticker, px, sh.series, splits)
    return {
        "info": info, "info_values": info_vals, "route": sector.route(info), "fcf_ttm": fcf, "debt": debt,
        "fy_labels": [periods.fiscal_year_label(d) for d in stmts[("income", "annual")].periods],
        "annual_fcf": periods.fiscal_years(stmts[("cashflow", "annual")], "free_cash_flow"),
        "stale": periods.staleness(periods.latest_period_end(stmts[("income", "quarterly")]),
                                   provider.get_earnings_dates(ticker), today),
        "actual_price": prices.actual_latest_price(provider, ticker),
        "adjusted": prices.adjusted_closes(provider, ticker),
        "breaks": breaks,
        "share_trend": shares.share_trend(sh.series, splits, ca.break_dates(breaks), sh.source),
        "leadership": leadership_flag(ticker, today=today, edgar=fixture_edgar(), db_path=db_path),
        "estimates": provider.get_analyst_estimates(ticker),
        "dividends": provider.get_dividends(ticker),
    }


def _meli(r):
    assert r["actual_price"].ok and r["fcf_ttm"].ok, "MELI core data should load"
    return "full Phase-1 pipeline runs (screen Fail branch arrives in Phase 2)"


def _htz(r):
    assert any(b.date.isoformat() == "2021-07-01" for b in r["breaks"]), "HTZ break not found"
    t = r["share_trend"]
    assert t.span_start and t.span_start.isoformat() >= "2021-07-01", "share trend crosses the break"
    assert LAYER_8K in r["leadership"].layers_used, "leadership not evaluated from EDGAR 8-Ks"
    return f"break 2021-07-01; share trend {t.span_label}; leadership: {r['leadership'].summary}"


def _lcid(r):
    fcfs = [d.value for d in r["annual_fcf"][: config.FCF_NEGATIVE_MAX_YEARS]]
    assert len(fcfs) >= config.FCF_NEGATIVE_MIN_YEARS and sum(v < 0 for v in fcfs) > len(fcfs) / 2, \
        "LCID should be FCF-negative in most fiscal years"
    t = r["share_trend"]
    assert t.trend_per_year is not None and t.trend_per_year > config.DILUTION_FLAG_PER_YEAR, "dilution expected"
    assert any("reverse split" in n for n in t.notes), "LCID 1-for-10 reverse split not adjusted"
    return f"FCF negative in {sum(v < 0 for v in fcfs)}/{len(fcfs)} FYs; shares {t.trend_per_year:+.1%}/yr (reverse split adjusted)"


def _lulu(r):
    assert r["fy_labels"][0].startswith("FY ending Jan"), f"LULU FY label wrong: {r['fy_labels'][:1]}"
    assert r["route"].sector == "Consumer Cyclical", "LULU sector should be Consumer Cyclical (retail threat framing in Phase 3)"
    return f"{r['fy_labels'][0]}; sector {r['route'].sector} / {r['route'].industry}"


def _jpm(r):
    assert r["route"].sector_adjusted and r["route"].subsector == "bank", "JPM should route to banks"
    return f"{r['route'].label} via industry {r['route'].industry!r}"


CHECKS = {"MELI": _meli, "HTZ": _htz, "LCID": _lcid, "LULU": _lulu, "JPM": _jpm}


def main() -> int:
    provider = fixture_provider()
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for ticker, check in CHECKS.items():
            try:
                print(f"PASS {ticker}: {check(run_all(provider, ticker, Path(tmp) / 'runs.db'))}")
            except Exception as exc:  # report every ticker, then fail
                failures += 1
                print(f"FAIL {ticker}: {type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
