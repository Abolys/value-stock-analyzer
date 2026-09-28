"""Screener view model: table colouring against thresholds, N/A vs n/m cells, the
Pass-only default filter, the scatter following the filter with top-N labels and
an exclusion caption, the changes panel on two saved runs, and the ETA."""

from datetime import datetime, timedelta

import pytest

import config
from app import charts
from app import screener_view as sv
from data.values import Datum
from screening.models import STATUS_FAILED_TO_LOAD, FAIL, NM, SLOT_LEVERAGE, SLOT_MOS, STATUS_FAIL, STATUS_INCOMPLETE, STATUS_PASS
from storage import screen_store as store
from tests.screen_helpers import run_eval


def result(ticker: str, status: str = STATUS_PASS, mos: float | None = 0.3, quality: float | None = 7.0,
           sources: str = "COWZ", stale: bool = False):
    r = run_eval()
    r.ticker, r.name, r.status, r.sources, r.stale = ticker, f"{ticker} Inc", status, sources, stale
    m = r.metric(SLOT_MOS)
    m.value = Datum(value=mos) if mos is not None else Datum.missing("n/m - negative EPS")
    r.quality.score = quality
    return r


def test_cells_coloured_against_thresholds_and_na_nm_distinct():
    passing = run_eval()
    assert passing.status == STATUS_PASS
    failing = run_eval(px=30.0)  # Graham Number well below price
    failing.ticker = "FAILS"
    nm = run_eval()
    nm.ticker = "NM"
    lev = nm.metric(SLOT_LEVERAGE)
    lev.value, lev.outcome = Datum.missing("n/m - negative EBITDA"), NM
    na = run_eval()
    na.ticker = "NA"
    na.metric(SLOT_MOS).value = Datum.missing("N/A - Data Incomplete")
    table = sv.build_table([passing, failing, nm, na])
    st = table.states
    assert st.at[0, "Margin of safety"] == "pass"
    assert failing.metric(SLOT_MOS).outcome == FAIL and st.at[1, "Margin of safety"] == "fail"
    assert st.at[2, "Net debt/EBITDA"] == "nm" and table.text.at[2, "Net debt/EBITDA"] == "n/m - negative EBITDA"
    assert st.at[3, "Margin of safety"] == "na" and table.text.at[3, "Margin of safety"] == "N/A - Data Incomplete"
    assert table.values.at[2, "Net debt/EBITDA"] == sv.MISSING_SORT  # sorts to one end, not as zero
    assert "threshold" in table.tips.at[0, "Margin of safety"]
    css = table.styler().to_html()
    assert "n/m - negative EBITDA" in css and "N/A - Data Incomplete" in css
    notes = table.notes()
    assert {"ticker", "column", "value", "note"} <= set(notes.columns) and len(notes)


def test_table_columns_are_the_specified_set():
    assert sv.COLUMNS == ["Ticker", "Source", "Margin of safety", "FCF yield vs 10Y", "Net debt/EBITDA",
                          "Share trend", "EV/EBIT yield", "Piotroski", "Trap risk", "P/TBV", "Asset coverage",
                          "Quality", "Status", "Data as of"]


def test_sector_adjusted_rows_labelled_with_their_own_metrics():
    from tests.screen_helpers import make_info

    bank = run_eval(info=make_info(sector="Financial Services", industry="Banks - Regional"))
    cells = sv.row_cells(bank)
    assert "sector-adjusted" in cells["Ticker"].text
    assert cells["FCF yield vs 10Y"].text.startswith("ROE")


def test_table_defaults_to_pass_only():
    rs = [result("A"), result("B", STATUS_FAIL), result("C", STATUS_INCOMPLETE)]
    assert sv.DEFAULT_STATUS == "Pass"
    assert [r.ticker for r in sv.filter_results(rs)] == ["A"]
    assert [r.ticker for r in sv.filter_results(rs, "All")] == ["A", "B", "C"]
    assert [r.ticker for r in sv.filter_results(rs, "All", ["Dataroma"])] == []


def test_scatter_follows_filter_and_labels_only_top_n():
    rs = [result(f"T{i:02d}", mos=0.01 * i, quality=1 + 0.5 * i) for i in range(14)]
    rs += [result("FAILER", STATUS_FAIL, mos=0.9, quality=9.9)]
    shown = sv.filter_results(rs)  # Pass only
    pts, excluded = sv.scatter_points(shown, top_n=10)
    assert "FAILER" not in {p.ticker for p in pts}
    labelled = [p.ticker for p in pts if p.label]
    assert len(labelled) == 10 and set(labelled) == {f"T{i:02d}" for i in range(4, 14)}
    assert excluded == []


def test_scatter_exclusion_caption():
    rs = [result("OK"), result("NEG", mos=None)]
    pts, excluded = sv.scatter_points(rs)
    out = charts.screener_scatter(pts, excluded)
    assert [p.ticker for p in pts] == ["OK"]
    assert "1 margin of safety n/m - negative EPS: NEG" in out.caption
    assert any(s.type == "line" and s.x0 == 0 for s in out.fig.layout.shapes)  # line at zero margin of safety


def test_scatter_caption_groups_hundreds_of_stage1_cuts():
    import config

    rs = [result("OK")]
    for i in range(40):
        r = result(f"C{i:02d}", STATUS_FAIL)
        r.quality, r.decided_at_stage = None, 1
        rs.append(r)
    bad = result("GONE")
    bad.status = STATUS_FAILED_TO_LOAD
    rs.append(bad)
    pts, excluded = sv.scatter_points(rs)
    assert len(excluded) == 41  # every ticker still listed (the page shows them in full)
    assert "C00: cut at stage 1 (statements not fetched, so no quality score)" in excluded
    out = charts.screener_scatter(pts, excluded)
    assert out.excluded[0].startswith("40 cut at stage 1 (statements not fetched, so no quality score): C00, C01")
    assert out.excluded[0].endswith(f"+{40 - config.CHANGES_LIST_MAX} more")
    assert "1 failed to load: GONE" in out.caption
    assert "C39" not in out.caption


def test_changes_panel_on_two_saved_runs(db_path):
    prev = [result("STAY"), result("DROP"), result("NEWP", STATUS_FAIL), result("OLD", stale=False)]
    curr = [result("STAY"), result("DROP", STATUS_FAIL), result("NEWP"), result("OLD", STATUS_INCOMPLETE, stale=True)]
    ids = []
    for batch in (prev, curr):
        rid = store.create_run(["cowz"], path=db_path)
        for r in batch:
            store.write_result(rid, r, db_path)
        store.update_run(rid, db_path, status=store.COMPLETED)
        ids.append(rid)
    ch = sv.screen_changes(store.load_results(ids[0], db_path), store.load_results(ids[1], db_path),
                           ["cowz"], ["cowz"])
    assert ch.new_pass == ["NEWP"]
    assert ch.dropped_pass == ["DROP", "OLD"]  # OLD went from Pass to Incomplete
    assert ch.newly_stale == ["OLD"]
    assert ch.newly_incomplete == ["OLD"]
    assert ch.notes == []
    shown, more = sv.short_list([f"X{i}" for i in range(config.CHANGES_LIST_MAX + 3)])
    assert len(shown) == config.CHANGES_LIST_MAX and more == 3


def test_changes_note_different_lists():
    ch = sv.screen_changes([result("A")], [result("A")], ["cowz"], ["cowz", "sp400"])
    assert any("different lists" in n for n in ch.notes)


def test_eta_from_time_per_ticker():
    started = datetime(2026, 9, 1, 12, 0)
    run = store.ScreenRun(run_id=1, started_at=started, status=store.RUNNING, total=100, attempted=20)
    assert sv.eta(run, started + timedelta(minutes=10)) == timedelta(minutes=40)
    early = run.model_copy(update={"attempted": config.SCREEN_ETA_MIN_DONE - 1})
    assert sv.eta(early, started + timedelta(minutes=1)) is None


@pytest.mark.parametrize("status,expected", [("stale", True), ("updated", False)])
def test_list_picker_marks_stale_lists(monkeypatch, tmp_path, status, expected):
    from data.universe import RefreshOutcome, save_refresh_status

    path = tmp_path / "refresh_status.json"
    monkeypatch.setattr(config, "UNIVERSE_REFRESH_STATUS_PATH", path)
    save_refresh_status([RefreshOutcome(key="cowz", status=status, message="STALE: COWZ kept previous list")])
    row = next(r for r in sv.list_picker_rows() if r["key"] == "cowz")
    assert row["stale"] is expected and row["as_of"]


def test_scatter_marks_pass_line_status_shapes_and_label_headroom():
    import config

    rs = [result("P1", mos=0.3, quality=10.0), result("I1", STATUS_INCOMPLETE, mos=0.1), result("F1", STATUS_FAIL)]
    pts, excluded = sv.scatter_points(rs, top_n=3)
    out = charts.screener_scatter(pts, excluded)
    tr = out.fig.data[0]
    assert list(tr.marker.symbol) == ["circle", "circle-open", "x-thin-open"]
    assert any(s.type == "line" and s.x0 == config.MIN_MARGIN_OF_SAFETY for s in out.fig.layout.shapes)
    assert out.fig.layout.yaxis.range[1] > 10  # a label on a 10.0 point stays inside the plot
    assert "● Pass · ○ Incomplete · ✕ Fail" in out.caption
