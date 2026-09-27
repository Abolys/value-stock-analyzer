from datetime import date

from tests.conftest import HANDMADE

from data.fixture_provider import fixture_edgar
from data.form4 import parse_form4
from data.insiders import COVERAGE_FORM4, COVERAGE_MANUAL, COVERAGE_NONE, collect_insider_data

F4 = HANDMADE / "form4"


def parse(name):
    return parse_form4((F4 / name).read_text())


def test_keeps_purchases_and_ignores_grants_and_exercises():
    buys = [t for n in ("buy_alice.xml", "buy_bob.xml", "buy_carol.xml") for t in parse(n).transactions]
    assert [t.type for t in buys] == ["buy"] * 3
    assert {t.insider for t in buys} == {"Alice Able", "Bob Baker", "Carol Chen"}
    assert buys[0].value == 10000 * 12.5 and buys[0].role == "Chief Executive Officer"
    other = parse("grant_and_exercise.xml")
    assert other.transactions == []
    assert other.ignored_codes == {"A": 1, "M": 2, "F": 1}


def test_sales_kept_and_10b5_1_marked():
    checkbox = parse("sale_10b5_checkbox.xml").transactions
    footnote = parse("sale_10b5_footnote.xml").transactions
    discretionary = parse("sale_discretionary.xml").transactions
    assert [t.type for t in checkbox + footnote + discretionary] == ["sell"] * 3
    assert checkbox[0].is_10b5_1 and footnote[0].is_10b5_1
    assert not discretionary[0].is_10b5_1


def test_real_form4_fixture_parses(tmp_path):
    edgar = fixture_edgar()
    data = collect_insider_data("JPM", edgar, since=date(2026, 3, 1), manual_path=tmp_path / "none.csv")
    assert data.coverage == [COVERAGE_FORM4]
    assert all(t.type in ("buy", "sell") for t in data.transactions)
    assert "A" in data.ignored_codes  # grants present in the real filings, ignored


def test_non_sec_ticker_gets_manual_or_na_coverage(tmp_path):
    edgar = fixture_edgar()
    csv = tmp_path / "insider_events.csv"
    csv.write_text("ticker,date,insider,role,type,shares,price,source\n")
    none = collect_insider_data("ABX.TO", edgar, since=date(2026, 1, 1), manual_path=csv)
    assert none.coverage_label == COVERAGE_NONE and none.transactions == []
    csv.write_text("ticker,date,insider,role,type,shares,price,source\n"
                   "ABX.TO,2026-05-01,Mark Hill,CEO,buy,1000,30.5,SEDI (read by hand)\n")
    manual = collect_insider_data("ABX.TO", edgar, since=date(2026, 1, 1), manual_path=csv)
    assert manual.coverage == [COVERAGE_MANUAL]
    assert manual.transactions[0].value == 30500 and manual.transactions[0].source == "SEDI (read by hand)"
