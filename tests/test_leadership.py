from datetime import date

from tests.conftest import HANDMADE

from data import leadership as ld
from data.fixture_provider import fixture_edgar
from storage import db

TODAY = date(2026, 9, 1)
START = ld.lookback_start(TODAY)


def doc(name, form="8-K", filed=date(2026, 3, 4), acc="0000000001-26-000001"):
    return ld.FilingDoc(form=form, filing_date=filed, accession=acc, text=(HANDMADE / name).read_text())


# ---------------------------------------------------------------- 8-K Item 5.02
def test_8k_item_502_cfo_departure():
    events = ld.parse_8k_item_502(doc("8k_502_cfo_departure.htm"), "SYN")
    assert [(e.role, e.date) for e in events] == [("CFO", date(2026, 3, 4))]
    assert events[0].person == "Jane Q Smith"
    assert "resign" in events[0].detail


def test_8k_caption_and_other_items_do_not_count():
    # The Item caption says "Departure of ... Officers" and Item 9.01 mentions a CEO retiring;
    # neither is a CEO event. A board resignation and a divisional CEO don't count either.
    assert [e.role for e in ld.parse_8k_item_502(doc("8k_502_cfo_departure.htm"), "SYN")] == ["CFO"]
    assert ld.parse_8k_item_502(doc("8k_502_director_only.htm"), "SYN", company_name="Synthetic Corp") == []


def test_8k_real_fixture_lulu_ceo_departure(db_path):
    r = ld.leadership_flag("LULU", today=date(2026, 9, 26), edgar=fixture_edgar(), db_path=db_path,
                           manual_path=db_path.parent / "none.csv")
    assert r.layers_used == [ld.LAYER_8K] and not r.partial_coverage
    assert [(e.role, e.date, e.person) for e in r.events] == [("CEO", date(2025, 12, 11), "McDonald")]
    assert r.flag == "flagged"


def test_8k_divisional_ceo_on_real_jpm_fixture_not_counted(db_path):
    r = ld.leadership_flag("JPM", today=date(2026, 9, 26), edgar=fixture_edgar(), db_path=db_path,
                           manual_path=db_path.parent / "none.csv")
    assert r.flag == "none" and not r.partial_coverage  # "CEO of CCB" retiring is divisional


# ---------------------------------------------------------------- 6-K
def test_6k_keyword_hit_confirmed_by_llm():
    seen = []

    def confirm(d, ticker):
        seen.append(d.accession)
        return ld.LLMConfirmation(status="confirmed", departure=True, role="CEO", person="Peter Grant",
                                  effective_date=date(2026, 6, 30))

    layer = ld.layer_6k("NMM.TO", [doc("6k_ceo_change.htm", "6-K", date(2026, 5, 12), "a1"),
                                   doc("6k_no_keywords.htm", "6-K", date(2026, 5, 13), "a2")], START, TODAY, confirm)
    assert seen == ["a1"]  # only the keyword hit goes to the LLM
    assert [(e.role, e.person, e.date) for e in layer.events] == [("CEO", "Peter Grant", date(2026, 6, 30))]


def test_6k_keyword_hit_rejected_by_llm():
    def reject(d, ticker):
        return ld.LLMConfirmation(status="rejected", departure=False)

    layer = ld.layer_6k("NMM.TO", [doc("6k_keyword_no_departure.htm", "6-K", date(2026, 8, 1), "b1")],
                        START, TODAY, reject)
    assert ld.keyword_hits((HANDMADE / "6k_keyword_no_departure.htm").read_text())
    assert layer.events == [] and layer.candidates == []


def test_6k_keyword_miss_never_reaches_llm():
    def boom(d, ticker):
        raise AssertionError("LLM must not be called without a keyword hit")

    layer = ld.layer_6k("NMM.TO", [doc("6k_no_keywords.htm", "6-K", date(2026, 5, 13), "c1")], START, TODAY, boom)
    assert layer.events == []


def test_llm_stub_leaves_hits_unconfirmed():
    layer = ld.layer_6k("NMM.TO", [doc("6k_ceo_change.htm", "6-K", date(2026, 5, 12), "d1")], START, TODAY)
    assert layer.events == [] and layer.candidates[0]["status"] == "unconfirmed"


# ---------------------------------------------------------------- officer snapshots
def _info(ceo, cfo):
    return {"companyOfficers": [{"name": ceo, "title": "President & Chief Executive Officer"},
                                {"name": cfo, "title": "Executive VP & CFO"},
                                {"name": "Pat Other", "title": "Chief Operating Officer"}]}


def test_officer_snapshot_change_in_cfo(db_path):
    db.save_officer_snapshot("NMM.TO", _info("Ann CEO", "Carl CFO"), date(2026, 3, 1), db_path)
    db.save_officer_snapshot("NMM.TO", _info("Ann CEO", "Dora CFO"), date(2026, 6, 1), db_path)
    snaps = db.get_officer_snapshots("NMM.TO", db_path)
    assert snaps[0].ceo == ["Ann CEO"] and snaps[0].cfo == ["Carl CFO"]
    events = ld.officer_change_events(snaps)
    assert [(e.role, e.person, e.date) for e in events] == [("CFO", "Carl CFO", date(2026, 6, 1))]


def test_save_snapshot_accepts_info_result(db_path, fx_provider):
    info = fx_provider.get_info("LULU")
    snap = db.save_officer_snapshot("LULU", info, date(2026, 9, 1), db_path)
    assert snap.ceo and snap.cfo  # "Interim Co-CEO & CFO" counts for both roles


# ---------------------------------------------------------------- manual + merge
def test_manual_event(tmp_path):
    csv = tmp_path / "leadership_events.csv"
    csv.write_text("ticker,date,role,person,note,source\n"
                   "NMM.TO,2026-02-10,CEO,Peter Grant,Announced on SEDAR+,read by hand\n")
    layer = ld.layer_manual("NMM.TO", TODAY, csv)
    r = ld.merge_leadership("NMM.TO", [layer], TODAY)
    assert r.flag == "flagged" and r.events[0].person == "Peter Grant"
    assert r.partial_coverage and "partial coverage" in r.coverage_label  # manual alone is never full coverage


def test_partial_coverage_never_reported_clean(db_path):
    db.save_officer_snapshot("NMM.TO", _info("Ann CEO", "Carl CFO"), date(2026, 7, 1), db_path)
    db.save_officer_snapshot("NMM.TO", _info("Ann CEO", "Carl CFO"), date(2026, 8, 1), db_path)
    r = ld.leadership_flag("NMM.TO", today=TODAY, edgar=None, db_path=db_path, manual_path=db_path.parent / "none.csv")
    assert r.departures == 0 and r.partial_coverage
    assert r.coverage_label == "officer tracking since 2026-07-01 · partial coverage"
    assert r.summary.startswith("No departures found · ") and "partial coverage" in r.summary


def test_no_source_is_na(db_path):
    r = ld.leadership_flag("NMM.TO", today=TODAY, edgar=None, db_path=db_path, manual_path=db_path.parent / "no.csv")
    assert r.flag == ld.NO_SOURCE


def test_merge_dedupes_by_person_and_month_and_counts_high():
    e1 = ld.LeadershipEvent(ticker="X", date=date(2026, 3, 4), role="CFO", person="Jane Smith", layer=ld.LAYER_8K)
    e2 = ld.LeadershipEvent(ticker="X", date=date(2026, 3, 20), role="CFO", person="Ms. Jane Smith", layer="manual")
    e3 = ld.LeadershipEvent(ticker="X", date=date(2026, 5, 1), role="CEO", person="Bo Chief", layer=ld.LAYER_8K)
    full = ld.LayerResult(name=ld.LAYER_8K, coverage_start=START, coverage_end=TODAY, events=[e1, e3])
    manual = ld.LayerResult(name="manual", coverage_start=None, coverage_end=TODAY, events=[e2], systematic=False)
    r = ld.merge_leadership("X", [full, manual], TODAY)
    assert r.departures == 2 and r.flag == "high" and not r.partial_coverage
    jane = next(e for e in r.events if e.role == "CFO")
    assert jane.sources == [ld.LAYER_8K, "manual"]


def test_events_outside_lookback_ignored():
    old = ld.LeadershipEvent(ticker="X", date=START.replace(year=START.year - 1), role="CEO", layer=ld.LAYER_8K)
    layer = ld.LayerResult(name=ld.LAYER_8K, coverage_start=START, coverage_end=TODAY, events=[old])
    assert ld.merge_leadership("X", [layer], TODAY).flag == "none"


def test_snapshot_script_resumes_by_skipping_done_tickers(db_path, fx_provider):
    from scripts.snapshot_officers import run

    first = run(fx_provider, ["LULU", "MSFT"], today=TODAY, db_path=db_path)
    assert all(not v.startswith(("skipped", "failed")) for v in first.values())
    again = run(fx_provider, ["LULU", "MSFT", "JPM"], today=TODAY, db_path=db_path)
    assert again["LULU"].startswith("skipped") and again["MSFT"].startswith("skipped")
    assert not again["JPM"].startswith("skipped")
