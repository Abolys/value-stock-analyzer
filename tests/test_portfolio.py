"""Portfolio position maths: average-cost realised and unrealised gain, split restatement,
return vs the benchmark over a known period (index-equivalent), and the "then vs now" diff."""

from datetime import date

import pandas as pd
import pytest

from data.values import Datum
from portfolio.models import Metric, Transaction
from portfolio.positions import benchmark_return, position_summary, with_benchmark
from portfolio.thesis import BETTER, NA_CHANGE, SAME, WORSE, then_vs_now

D1, D2, D3, NOW = date(2025, 1, 10), date(2025, 4, 11), date(2025, 7, 11), date(2026, 1, 9)


def txns():
    return [Transaction(txn_date=D1, side="buy", shares=10, price=100),
            Transaction(txn_date=D2, side="buy", shares=10, price=120),
            Transaction(txn_date=D3, side="sell", shares=10, price=130)]


def price(v=150.0):
    return Datum(value=v, period_end=NOW)


def bench() -> pd.Series:
    # Weekend-free series with the closes on the transaction dates and today; one gap day (D1 − 1).
    return pd.Series({pd.Timestamp(date(2025, 1, 9)): 199.0, pd.Timestamp(D1): 200.0, pd.Timestamp(D2): 220.0,
                      pd.Timestamp(D3): 250.0, pd.Timestamp(NOW): 260.0})


def test_holding_with_sells_computes_realised_and_unrealised_gain():
    p = position_summary(txns(), price())
    # Average cost after the two buys: (1000 + 1200) / 20 = 110.
    assert p.realised == pytest.approx(10 * (130 - 110))  # 200
    assert p.shares == pytest.approx(10)
    assert p.avg_cost == pytest.approx(110)
    assert p.cost_basis == pytest.approx(1100)
    assert p.market_value == pytest.approx(1500)
    assert p.unrealised == pytest.approx(400)
    assert p.total_gain == pytest.approx(600)
    assert p.total_return == pytest.approx(600 / 2200)


def test_fees_reduce_realised_gain_and_raise_cost():
    t = [Transaction(txn_date=D1, side="buy", shares=10, price=100, fees=10),
         Transaction(txn_date=D3, side="sell", shares=5, price=120, fees=5)]
    p = position_summary(t, price(120))
    assert p.avg_cost == pytest.approx(101)
    assert p.realised == pytest.approx(5 * (120 - 101) - 5)
    assert p.unrealised == pytest.approx(5 * (120 - 101))


def test_return_vs_benchmark_over_a_known_period():
    r, reason = benchmark_return(txns(), bench(), NOW)
    assert reason == ""
    units = 1000 / 200 + 1200 / 220  # the same cash flows buy index units on the same days
    # the sell sells half the units at 250; the other half is worth 260 today
    expected = (units / 2 * 250 + units / 2 * 260 - 2200) / 2200
    assert r == pytest.approx(expected)
    p = with_benchmark(position_summary(txns(), price()), txns(), "SPY", bench())
    assert p.benchmark_return == pytest.approx(expected)
    assert p.vs_benchmark == pytest.approx(600 / 2200 - expected)
    assert any("dividends excluded on both sides" in n for n in p.notes)


def test_benchmark_uses_last_close_before_a_non_trading_day():
    t = [Transaction(txn_date=date(2025, 1, 11), side="buy", shares=1, price=100)]  # a Saturday
    r, _ = benchmark_return(t, bench(), NOW)
    assert r == pytest.approx(260 / 200 - 1)


def test_benchmark_history_too_short_is_na_not_zero():
    t = [Transaction(txn_date=date(2020, 1, 2), side="buy", shares=1, price=100)]
    r, reason = benchmark_return(t, bench(), NOW)
    assert r is None and reason.startswith("N/A - benchmark history starts after")
    p = with_benchmark(position_summary(t, price()), t, "SPY", bench())
    assert p.vs_benchmark is None and any("N/A" in n for n in p.notes)


def test_split_after_buy_restates_shares_and_price():
    t = [Transaction(txn_date=D1, side="buy", shares=10, price=100)]
    splits = pd.Series({pd.Timestamp(date(2025, 6, 2)): 2.0})
    p = position_summary(t, price(60), splits)
    assert p.shares == pytest.approx(20) and p.avg_cost == pytest.approx(50)
    assert p.unrealised == pytest.approx(200)
    assert any("restated for splits" in n for n in p.notes)


def test_missing_price_gives_na_never_zero():
    p = position_summary(txns(), Datum.missing())
    assert p.status.startswith("N/A") and p.market_value is None and p.unrealised is None
    assert p.realised == pytest.approx(200)  # realised gain needs no price


def test_sell_exceeding_shares_is_flagged():
    t = [Transaction(txn_date=D1, side="buy", shares=5, price=100),
         Transaction(txn_date=D2, side="sell", shares=6, price=100)]
    assert "exceeds" in position_summary(t, price()).status


def test_fx_conversion_to_holding_currency():
    t = [Transaction(txn_date=D1, side="buy", shares=10, price=130)]
    p = position_summary(t, price(100), fx=Datum(value=1.35, period_label="USDCAD=X"))
    assert p.market_value == pytest.approx(1350)
    p = position_summary(t, price(100), fx=Datum.missing())
    assert "currency conversion" in p.status and p.market_value is None


def _m(v):
    return Metric(value=v, display=str(v))


def test_then_vs_now_reports_changed_scores_by_direction():
    then = {"quant_score": _m(7.4), "macro_score": _m(6.8), "moat_score": _m(6.5), "devils_advocate_score": _m(5.0),
            "piotroski": _m(6.0), "net_debt_ebitda": _m(2.1), "price": _m(49.0)}
    now = {"quant_score": _m(7.0), "macro_score": _m(7.1), "moat_score": _m(6.5), "devils_advocate_score": _m(3.1),
           "piotroski": _m(4.0), "net_debt_ebitda": _m(2.9), "price": _m(42.0),
           "aggregate_score": Metric(status="N/A - no full analysis yet", display="N/A - no full analysis yet")}
    rows = {r.field: r for r in then_vs_now(then, now)}
    assert rows["quant_score"].change == WORSE and rows["quant_score"].delta == pytest.approx(-0.4)
    assert rows["macro_score"].change == BETTER
    assert rows["moat_score"].change == SAME
    assert rows["devils_advocate_score"].change == WORSE
    assert rows["piotroski"].change == WORSE
    assert rows["net_debt_ebitda"].change == WORSE  # lower is better: 2.1 → 2.9 is worse
    assert rows["price"].change == "changed"  # neutral direction
    assert rows["aggregate_score"].change == NA_CHANGE and rows["aggregate_score"].now.startswith("N/A")


def test_default_levels_explain_a_runway_company_has_no_fair_value():
    from analysis.macro import macro_lens
    from analysis.pipeline import view_run
    from analysis.quant import quant_lens
    from portfolio.thesis import default_levels
    from tests.analysis_helpers import make_inputs
    from tests.screen_helpers import make_fundamentals

    burning = make_fundamentals({"free_cash_flow": (-100, -80), "operating_cash_flow": (-60, -40),
                                 "diluted_eps": (-1.0, -0.8), "net_income": (-100, -80)})
    x = make_inputs(burning)
    run = view_run(x)
    run.quant, run.macro = quant_lens(x), macro_lens(x)
    lv = default_levels(run)
    assert lv["intrinsic_value"] is None and "cash-runway method" in lv["basis"] and "(ok)" not in lv["basis"]
