"""The Portfolio page and the app shell's alert pieces (offline fixtures): the sidebar's unread
count, the app-start check launched only when due, and the Portfolio page with a held ticker
(holdings table, then vs now, triggers, journal, inbox)."""

from datetime import date, datetime, timedelta

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import config
from data.cache import CachedProvider, DiskCache
from data.fixture_provider import captured_on, fixture_edgar, fixture_provider
from data.health import HealthReport
from portfolio import store
from portfolio.models import Alert, Thesis, Transaction, Trigger


@pytest.fixture(autouse=True)
def _fresh_resource_cache():
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.fixture
def provider(tmp_path):
    return CachedProvider(fixture_provider(), DiskCache(tmp_path / "cache.db"))


@pytest.fixture
def offline_app(monkeypatch, provider):
    from app import services

    monkeypatch.setattr(services, "build_provider", lambda: provider)
    monkeypatch.setattr(services, "health", lambda p: HealthReport(ok=True, failures=[], checked_at=datetime(2026, 9, 1)))
    monkeypatch.setattr(services, "build_edgar", lambda p: fixture_edgar())
    monkeypatch.setattr(services, "valet_fetch", lambda: None)


def _hold_lulu(price=150.0) -> int:
    th = Thesis(intrinsic_value=400.0, buy_below_price=320.0, target_price=420.0, basis="test",
                reasons=[{"text": "Brand intact"}, {"text": "Net cash"}],
                triggers=[Trigger(field="piotroski", op="<", literal=5), Trigger(field="price", op=">=",
                                                                                   ref="target_price")])
    return store.add_holding("LULU", "TFSA", "USD", Transaction(txn_date=date(2026, 3, 2), side="buy", shares=10,
                                                                 price=price), th)


def test_sidebar_shows_unread_alert_count_and_app_start_launches_the_check(offline_app, _no_background_checks):
    hid = _hold_lulu()
    store.insert_alert(Alert(ticker="LULU", holding_id=hid, kind="target", event_key="x", message="test"))
    at = AppTest.from_file("../app/main.py", default_timeout=120)
    at.run()
    assert not at.exception
    links = [e.label for e in at.sidebar.get("page_link")]
    assert any("Alerts: 1 unread" in label for label in links), links
    assert _no_background_checks == [("app_start", None)]  # a holding exists and no check has run yet
    at.run()  # a rerun in the same session never launches a second check
    assert len(_no_background_checks) == 1


def test_app_start_check_is_rate_limited(_no_background_checks):
    from app import alert_jobs

    _hold_lulu()
    cid = store.start_check("app_start")
    store.finish_check(cid, store.COMPLETED)
    assert not alert_jobs.due()  # one just ran
    later = datetime.now() + timedelta(minutes=config.ALERT_CHECK_MIN_INTERVAL_MINUTES + 1)
    assert alert_jobs.due(now=later)


def test_nothing_to_check_launches_nothing(monkeypatch, _no_background_checks):
    from app import alert_jobs
    from portfolio import alerts as pa

    monkeypatch.setattr(pa, "watchlist_tickers", lambda: [])
    assert alert_jobs.maybe_start() is False and _no_background_checks == []


def _portfolio_page(provider):
    from app.views import portfolio

    portfolio.render(provider)


def test_portfolio_page_renders_holding_triggers_journal_and_inbox(provider, monkeypatch):
    from portfolio import alerts as pa
    from screening.engine import ScreenContext

    monkeypatch.setattr(pa, "watchlist_tickers", lambda: [])
    hid = _hold_lulu()
    ctx = ScreenContext(provider=provider, db_path=config.RUNS_DB_PATH, today=captured_on("LULU"))
    rep = pa.check_alerts(ctx, "manual", edgar=fixture_edgar(), send_email=lambda a: "skipped - test")
    assert not rep.errors
    store.add_journal(hid, "Holding up well")
    at = AppTest.from_function(_portfolio_page, args=(provider,), default_timeout=120)
    at.run()
    assert not at.exception
    assert [t.value for t in at.title] == ["Portfolio"]
    tables = [df.value for df in at.dataframe]
    holdings = next(t for t in tables if "Triggers" in t.columns)
    assert holdings.iloc[0]["Ticker"] == "LULU" and holdings.iloc[0]["Account"] == "TFSA"
    assert "vs SPY" in holdings.iloc[0]["vs index"] or holdings.iloc[0]["vs index"].startswith("N/A")
    assert any("At purchase" in t.columns for t in tables)  # then vs now
    trig = next(t for t in tables if "Rule" in t.columns)
    assert list(trig["Rule"]) == ["piotroski < 5", "price >= target_price"]
    md = " ".join(m.value for m in at.markdown)
    assert "Holding up well" in md and "then vs now" in md
    assert [c.label for c in at.checkbox if c.label in ("Brand intact", "Net cash")] == ["Brand intact", "Net cash"]
    assert "Alerts" in [h.value for h in at.subheader]
    captions = " ".join(c.value for c in at.caption)
    assert "Last alert check" in captions and "dividends excluded on both sides" in captions


def test_portfolio_page_without_holdings_explains_how_to_add(provider, monkeypatch):
    from portfolio import alerts as pa

    monkeypatch.setattr(pa, "watchlist_tickers", lambda: [])
    at = AppTest.from_function(_portfolio_page, args=(provider,), default_timeout=60)
    at.run()
    assert not at.exception
    assert any("Add to portfolio" in i.value for i in at.info)


def test_export_includes_the_thesis_journal():
    from portfolio.alerts import journal_for
    from reports.build import journal_section

    hid = _hold_lulu()
    store.add_journal(hid, "Thesis intact after Q2")
    sec = journal_section(journal_for("LULU"))
    assert sec.title == "Thesis journal"
    text = " ".join(sec.paragraphs) + " ".join(" ".join(" ".join(r) for r in t.rows) for t in sec.tables)
    assert "LULU · TFSA" in text and "Thesis intact after Q2" in text and "Brand intact" in text
    assert "piotroski < 5" in text
    assert journal_section([]).paragraphs == ["Not held: no thesis or journal."]


def _stock_page(provider):
    import streamlit as st

    from app.views import stock

    st.session_state.setdefault("ticker", "LULU")
    stock.render(provider)


def test_add_to_portfolio_from_the_stock_page_prefills_and_saves_the_snapshot(offline_app, provider):
    at = AppTest.from_function(_stock_page, args=(provider,), default_timeout=180)
    at.run()
    assert not at.exception
    at.button(key="add-pf-LULU").click().run()
    assert not at.exception
    iv = at.number_input(key="th-iv").value
    bb = at.number_input(key="th-bb").value
    assert iv > 0 and bb == pytest.approx(iv * (1 - config.MIN_MARGIN_OF_SAFETY))  # pre-filled from the analysis
    assert at.number_input(key="th-tp").value == pytest.approx(iv)
    at.number_input(key="th-shares").set_value(10.0)
    at.text_area(key="th-reasons").set_value("Brand intact\nNet cash")
    # build one rule with the trigger builder: piotroski < 5
    at.selectbox(key="th-rules-LULU-field").set_value("piotroski")
    at.selectbox(key="th-rules-LULU-op-number").set_value("<")
    at.number_input(key="th-rules-LULU-num").set_value(5.0)
    at.button(key="th-rules-LULU-add").click().run()
    assert not at.exception
    at.button(key="th-save").click().run()
    assert not at.exception
    [h] = store.list_holdings()
    assert h.ticker == "LULU" and h.transactions[0].shares == 10 and h.snapshot_analysis_id is not None
    assert h.snapshot["quant_score"].ok and h.snapshot["price"].ok
    assert [t.text for t in h.thesis.triggers] == ["piotroski < 5"]
    assert [r.text for r in h.thesis.reasons] == ["Brand intact", "Net cash"]
    assert h.thesis.buy_below_price == pytest.approx(bb)
