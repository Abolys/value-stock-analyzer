"""Run the golden tickers through everything built so far, offline from
tests/fixtures, and check each hits the branch it is listed for
(SPEC "Golden test tickers"). Phase 2 adds the screener: each ticker is
screened as a manual ticker (full stage 2) and through the two-stage screen.
Phase 3 adds the four-lens analysis, with the LLM mocked (no API calls, no
cost): every golden ticker runs the whole pipeline without an exception.
Phase 4 adds the turnaround estimate: every ticker gets one with the
survivorship caveat, and HTZ's never reaches across the 2021 break.
Later phases extend CHECKS.

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
from screening.engine import ScreenContext, analyse_manual, screen_ticker  # noqa: E402
from screening.models import FAIL_DISPLAY, STATUS_FAILED_TO_LOAD, SLOT_FCF, SLOT_LEVERAGE  # noqa: E402
from signals.valuation import NM_FINANCIALS  # noqa: E402
from analysis.pipeline import run_analysis  # noqa: E402
from llm.cache import LLMCache  # noqa: E402
from llm.client import LLMClient  # noqa: E402
from tests.llm_fakes import FakeAPI  # noqa: E402


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
        "screen_manual": analyse_manual(ScreenContext(provider=provider, db_path=db_path, today=today), ticker),
        "screen": screen_ticker(ScreenContext(provider=provider, db_path=db_path, today=today), ticker, "golden"),
        **_analysis(provider, ticker, db_path, today),
    }


def _analysis(provider: DataProvider, ticker: str, db_path: Path, today) -> dict:
    """Phase 3: the four lenses and the aggregate, LLM mocked, EDGAR from fixtures."""
    api = FakeAPI()
    llm = LLMClient(api=api, cache=LLMCache(Path(db_path).parent / "golden_llm_cache.db"), db_path=db_path)
    run = run_analysis(ScreenContext(provider=provider, db_path=db_path, today=today), ticker, llm=llm,
                       edgar=fixture_edgar())
    return {"analysis": run, "llm_requests": api.requests}


def _analysed(r) -> str:
    a = r["analysis"]
    assert not a.load_error, f"analysis failed to load: {a.load_error}"
    assert not a.errors, f"lens exceptions: {a.errors}"
    for name in ("quant", "macro", "moat", "devils_advocate"):
        assert a.lens(name) is not None, f"{name} missing"
    assert a.aggregate is not None and a.aggregate.lenses_used >= 3, f"aggregate {a.aggregate}"
    assert "leadership" in a.devils_advocate.payload and "asset_floor" in a.devils_advocate.payload
    t = a.turnaround
    assert t is not None and t.current is not None, f"turnaround missing: {t}"
    assert t.survivorship_caveat == config.TURNAROUND_SURVIVORSHIP_CAVEAT and t.survivorship_caveat in t.rationale
    assert t.asset_floor_line.startswith("Asset floor"), t.asset_floor_line
    assert t.recovered_count + t.unrecovered_count == len(t.episodes)
    return (f"lenses Q {a.quant.display} ({a.quant.method}) / M {a.macro.display} / moat {a.moat.display} / "
            f"DA {a.devils_advocate.display} → {a.aggregate.display}; turnaround: {t.headline} "
            f"({len(t.episodes)} episodes, {t.unrecovered_count} unrecovered)")


def _screened(r) -> None:
    for key in ("screen_manual", "screen"):
        assert r[key].status != STATUS_FAILED_TO_LOAD, f"{key}: failed to load ({r[key].load_error})"


def _meli(r):
    assert r["actual_price"].ok and r["fcf_ttm"].ok, "MELI core data should load"
    _screened(r)
    assert r["screen"].display_status == FAIL_DISPLAY, f"MELI screen status {r['screen'].display_status}"
    assert r["screen_manual"].display_status == FAIL_DISPLAY and r["screen_manual"].decided_at_stage == 2, \
        "MELI entered manually should still get the full stage-2 analysis"
    a = r["analysis"]
    assert a.aggregate.lenses_used == 4, "MELI entered manually should get the full four-lens analysis"
    return (f"{FAIL_DISPLAY} ({r['screen'].status_reasons[0]}); full stage-2 analysis runs when entered manually; "
            f"{_analysed(r)}")


def _htz(r):
    assert any(b.date.isoformat() == "2021-07-01" for b in r["breaks"]), "HTZ break not found"
    t = r["share_trend"]
    assert t.span_start and t.span_start.isoformat() >= "2021-07-01", "share trend crosses the break"
    assert LAYER_8K in r["leadership"].layers_used, "leadership not evaluated from EDGAR 8-Ks"
    _screened(r)
    span = r["screen_manual"].share_trend_span
    assert span and span >= "2021-07-01", f"screen share trend crosses the break: {span}"
    tr = r["analysis"].turnaround
    assert all(s.start.isoformat() >= "2021-07-01" or s.end.isoformat() < "2021-07-01" for s in tr.segments), \
        f"a turnaround segment spans the break: {tr.segments}"
    assert all((e.peak_date.isoformat() >= "2021-07-01") == ((e.recovery_date or e.trough_date).isoformat()
                                                             >= "2021-07-01") for e in tr.episodes), \
        "a drawdown episode crosses the HTZ break"
    assert any(b.date.isoformat() == "2021-07-01" for b in tr.breaks), "turnaround did not load the HTZ break"
    lead = r["analysis"].devils_advocate.payload["leadership"]
    assert LAYER_8K in lead["coverage"], f"DA payload leadership coverage {lead}"
    return (f"break 2021-07-01; share trend {t.span_label}; leadership: {r['leadership'].summary}; "
            f"screen {r['screen_manual'].display_status}; {_analysed(r)}")


def _lcid(r):
    fcfs = [d.value for d in r["annual_fcf"][: config.FCF_NEGATIVE_MAX_YEARS]]
    assert len(fcfs) >= config.FCF_NEGATIVE_MIN_YEARS and sum(v < 0 for v in fcfs) > len(fcfs) / 2, \
        "LCID should be FCF-negative in most fiscal years"
    t = r["share_trend"]
    assert t.trend_per_year is not None and t.trend_per_year > config.DILUTION_FLAG_PER_YEAR, "dilution expected"
    assert any("reverse split" in n for n in t.notes), "LCID 1-for-10 reverse split not adjusted"
    _screened(r)
    s = r["screen_manual"]
    fcf_metric = s.metric(SLOT_FCF)
    assert s.fcf_negative == "FCF-negative" and fcf_metric.name.startswith("Cash runway"), \
        f"LCID should use cash runway, got {fcf_metric.name}"
    assert s.dilution_flag, "LCID dilution flag expected in the screen result"
    q = r["analysis"].quant
    assert q.method == "runway" and q.dcf is None, f"LCID Quant should skip the DCF, got {q.method}"
    assert q.score is None or q.score <= config.RUNWAY_SCORE_CAP
    return (f"FCF negative in {sum(v < 0 for v in fcfs)}/{len(fcfs)} FYs; shares {t.trend_per_year:+.1%}/yr "
            f"(reverse split adjusted); screen uses {fcf_metric.name}: {fcf_metric.display}; dilution flagged; "
            f"Quant skips the DCF (runway {q.runway_months.display()} months); {_analysed(r)}")


def _lulu(r):
    assert r["fy_labels"][0].startswith("FY ending Jan"), f"LULU FY label wrong: {r['fy_labels'][:1]}"
    assert r["route"].sector == "Consumer Cyclical", "LULU sector should be Consumer Cyclical (retail threat framing in Phase 3)"
    _screened(r)
    moat = r["analysis"].moat
    hint = moat.payload["sector_threat_hint"]
    assert "private-label" in hint and "AI disruption" not in hint, f"LULU threat hint {hint!r}"
    moat_req = next(q for q in r["llm_requests"] if q["output_config"]["format"]["schema"]["title"] == "MoatResponse")
    assert "Sector threat instruction" in moat_req["messages"][0]["content"] and hint in moat_req["messages"][0]["content"]
    return (f"{r['fy_labels'][0]}; sector {r['route'].sector} / {r['route'].industry}; "
            f"screen {r['screen_manual'].display_status}; moat threat hint: {hint}; {_analysed(r)}")


def _jpm(r):
    assert r["route"].sector_adjusted and r["route"].subsector == "bank", "JPM should route to banks"
    _screened(r)
    s = r["screen_manual"]
    assert s.metric(SLOT_FCF).name.startswith("ROE") and s.metric(SLOT_LEVERAGE).name.startswith("Price / tangible"), \
        "JPM should be screened on ROE spread and P/TBV"
    for name, status in (("Piotroski", s.piotroski.status), ("Altman", s.altman.status),
                         ("Beneish", s.beneish.status), ("EV/EBIT", s.earnings_yield.value.status)):
        assert status == f"n/m - {NM_FINANCIALS}", f"JPM {name} should be n/m, got {status}"
    assert s.asset_floor.ncav.is_nm and s.asset_floor.p_tbv.ok, "JPM: NCAV n/m, P/TBV kept"
    a = r["analysis"]
    assert a.quant.method == "excess_return", f"JPM Quant method {a.quant.method}"
    assert a.macro.reduced_data and a.macro.net_debt_ebitda.is_nm and a.macro.altman_z.is_nm, "JPM Macro reduced data"
    return (f"{r['route'].label} via industry {r['route'].industry!r}; ROE spread and P/TBV screened; "
            f"trap scores and EV/EBIT n/m; P/TBV {s.asset_floor.p_tbv.display()}; Quant excess-return, "
            f"Macro reduced data; {_analysed(r)}")


CHECKS = {"MELI": _meli, "HTZ": _htz, "LCID": _lcid, "LULU": _lulu, "JPM": _jpm}


def main() -> int:
    config.LLM_BACKEND = "none"  # offline: never reach the API or the Claude Code CLI (lens calls use FakeAPI)
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
