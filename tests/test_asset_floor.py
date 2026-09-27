"""Asset floor (information only) and EV/EBIT earnings yield."""

import pytest

from data.values import Datum
from screening import stage2
from signals.asset_floor import AssetFloor, NEG_TBV, asset_floor
from signals.valuation import NET_CASH_REASON, ev_ebit_yield
from tests.screen_helpers import make_fundamentals, run_eval


def mc(v):
    return Datum(value=v)


def test_tbv_ptbv_and_coverage_band():
    # TBV = 1200 − 100 goodwill − 50 intangibles = 1050; P/TBV = 1000/1050; coverage 105% → fully covered
    af = asset_floor(make_fundamentals(), mc(1000), Datum(value=160))
    assert af.tbv.value == 1050
    assert af.p_tbv.value == pytest.approx(1000 / 1050)
    assert af.coverage.value == pytest.approx(1.05) and af.coverage_band == "fully covered"
    af = asset_floor(make_fundamentals(), mc(3000), Datum(value=160))
    assert af.coverage_band == "thin"  # 35%


def test_preferred_subtracted_where_reported():
    af = asset_floor(make_fundamentals({"preferred_stock": (50, 50)}), mc(1000), Datum(value=1))
    assert af.tbv.value == 1000


def test_net_net_flag_with_burn_duration():
    # NCAV = 2000 − 800 = 1200 ≥ market cap 500; quarterly burn = 200/4 = 50 → (1200 − 500)/50 = 14 quarters
    f = make_fundamentals({"current_assets": (2000, 1500)})
    af = asset_floor(f, mc(500), Datum(value=-200))
    assert af.ncav.value == 1200 and af.net_net
    assert af.burn_line == "discount gone in ~14 quarters at current burn"
    # Not burning cash: flag without a burn line
    af = asset_floor(f, mc(500), Datum(value=100))
    assert af.net_net and af.burn_line == ""
    # NCAV below market cap: no flag
    assert not asset_floor(f, mc(5000), Datum(value=-200)).net_net


def test_negative_tbv_gives_nm_and_coverage_none():
    f = make_fundamentals({"goodwill": None, "other_intangible_assets": None,
                           "goodwill_and_intangibles": (1500, 1500)})
    af = asset_floor(f, mc(1000), Datum(value=10))
    assert af.tbv.value == -300
    assert af.p_tbv.status == f"n/m - {NEG_TBV}" and af.coverage.status == f"n/m - {NEG_TBV}"
    assert af.coverage_band == "none"


def test_nnwc_weights_hand_computed():
    # cash 200 + STI 50 + 0.75 × 150 receivables + 0.5 × 100 inventory − 800 liabilities
    # = 250 + 112.5 + 50 − 800 = −387.5 (negative is the answer, never n/m)
    af = asset_floor(make_fundamentals(), mc(1000), Datum(value=1))
    assert af.nnwc.value == pytest.approx(-387.5)
    assert af.ncav.value == -200 and af.ncav.ok


def test_missing_inventory_left_out_with_note():
    af = asset_floor(make_fundamentals({"inventory": None}), mc(1000), Datum(value=1))
    assert af.nnwc.value == pytest.approx(-437.5)
    assert any("inventory not reported" in n for n in af.notes)


def test_financials_ncav_nnwc_nm_but_ptbv_kept():
    af = asset_floor(make_fundamentals(), mc(1000), Datum(value=1), sector_adjusted=True)
    assert af.ncav.is_nm and af.nnwc.is_nm
    assert af.p_tbv.ok


def test_jpm_fixture_keeps_ptbv_with_ncav_nm(fx_provider, tmp_path):
    from screening.engine import ScreenContext, analyse_manual

    res = analyse_manual(ScreenContext(provider=fx_provider, db_path=tmp_path / "r.db"), "JPM")
    assert res.asset_floor.ncav.is_nm and res.asset_floor.nnwc.is_nm
    assert res.asset_floor.p_tbv.ok and res.asset_floor.p_tbv.value > 0
    assert res.earnings_yield.value.status == "n/m - not meaningful for financials and REITs"


def test_asset_floor_never_changes_status_or_quality(monkeypatch):
    base = run_eval()
    # Very different asset floor inputs (goodwill, inventory, receivables feed only the floor here)...
    other = run_eval(make_fundamentals({"goodwill": (5000, 5000), "inventory": (0, 0), "receivables": (1, 1)}))
    assert other.asset_floor.coverage_band == "none"
    assert (other.status, other.quality.score) == (base.status, base.quality.score)
    # ...and a forced net-net floor with full coverage changes nothing either.
    forced = AssetFloor(tbv=Datum(value=1e9), p_tbv=Datum(value=0.01), ncav=Datum(value=1e9), nnwc=Datum(value=1e9),
                        coverage=Datum(value=99.0), coverage_band="fully covered", net_net=True)
    monkeypatch.setattr(stage2, "asset_floor", lambda *a, **k: forced)
    again = run_eval()
    assert again.asset_floor.net_net
    assert (again.status, again.status_reasons, again.quality.score) == (base.status, base.status_reasons,
                                                                          base.quality.score)


def test_ev_ebit_hand_computed():
    # EV = 1000 + 300 debt − (200 cash + 50 STI) = 1050 (no preferred / minority reported); EBIT 200
    ey = ev_ebit_yield(make_fundamentals(), mc(1000))
    assert ey.ev.value == 1050 and ey.value.value == pytest.approx(200 / 1050)
    assert not ey.net_cash_flag
    assert any("preferred equity not reported" in n for n in ey.notes)


def test_ev_nonpositive_raises_net_cash_flag():
    f = make_fundamentals({"cash_and_equivalents": (2000, 150)})
    ey = ev_ebit_yield(f, mc(1000))  # EV = 1000 + 300 − 2050 = −750
    assert ey.net_cash_flag and ey.value.status == f"n/m - {NET_CASH_REASON}"
    res = run_eval(f)
    assert res.net_cash_flag and "net cash exceeds market cap" in res.rationale


def test_ebit_nonpositive_is_nm():
    ey = ev_ebit_yield(make_fundamentals({"ebit": (-10, 5)}), mc(1000))
    assert ey.value.status == "n/m - EBIT ≤ 0"
