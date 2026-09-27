"""Alerts: every alert type in the spec fires on a crafted fixture, each fires once per event
(no re-alert on the next run, re-armed once the condition clears), holding alerts are
journaled, and email is skipped cleanly when SMTP isn't configured."""

from datetime import date, datetime

import pytest

from analysis.macro import macro_lens
from analysis.pipeline import view_run
from analysis.quant import quant_lens
from data.leadership import LeadershipEvent, LeadershipResult
from portfolio import alerts as pa
from portfolio import notify, store
from portfolio.models import Thesis, Transaction, Trigger
from screening.engine import ScreenContext
from signals.insider_activity import InsiderSummary
from tests.analysis_helpers import make_inputs

T0 = datetime(2026, 2, 15, 9, 0)


def crafted(ticker="TEST", px=10.0, **changes):
    """A monitor run for the synthetic healthy company (price 10, Piotroski 9, fundamentals to 2025-12-31)."""
    x = make_inputs(px=px)
    run = view_run(x)
    run.quant, run.macro = quant_lens(x), macro_lens(x)
    run.ticker = ticker
    for k, v in changes.items():
        setattr(run, k, v)
    return run


class Monitor:
    """Stands in for monitor_run: returns the crafted run currently set for each ticker."""

    def __init__(self, **runs):
        self.runs = dict(runs)

    def __call__(self, ctx, ticker, llm=None, edgar=None):
        r = self.runs[ticker]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def ctx(fx_provider, monkeypatch):
    import config

    monkeypatch.setattr(pa, "watchlist_tickers", lambda: [])
    for k in ("SMTP_HOST", "ALERT_EMAIL_TO", "SMTP_USER", "SMTP_PASSWORD", "SMTP_PORT", "ALERT_EMAIL_FROM"):
        monkeypatch.delenv(k, raising=False)
    return ScreenContext(provider=fx_provider, db_path=config.RUNS_DB_PATH, today=date(2026, 2, 15))


def hold(ticker="TEST", triggers=(), bb=None, tp=None, snapshot=None):
    return store.add_holding(ticker, "TFSA", "USD", Transaction(txn_date=date(2026, 1, 5), side="buy", shares=10,
                                                                 price=9.0),
                             Thesis(buy_below_price=bb, target_price=tp, triggers=list(triggers)), snapshot=snapshot)


def check(ctx, mon, now=T0, **kw):
    return pa.check_alerts(ctx, "screen", monitor=mon, now=now, **kw)


def kinds(rep):
    return sorted(a.kind for a in rep.fired)


def test_buy_below_and_target_fire_for_holdings_and_watchlist_levels(ctx, monkeypatch):
    hid = hold(bb=12.0, tp=9.5)
    store.set_watch_level("WATCH", 11.0, 20.0)
    rep = check(ctx, Monitor(TEST=crafted(), WATCH=crafted("WATCH")))
    assert kinds(rep) == ["buy_below", "buy_below", "target"]
    msgs = " ".join(a.message for a in rep.fired)
    assert "at or below buy-below 12.00 (TFSA)" in msgs and "(watchlist)" in msgs
    kinds_j = [e.kind for e in store.journal(hid)]
    assert kinds_j.count("alert") == 2  # both holding alerts journaled; the watchlist one has no holding


def test_trigger_fires_once_and_does_not_realert_until_it_clears(ctx):
    hid = hold(triggers=[Trigger(field="piotroski", op="<", literal=5)])
    run = crafted()
    run.screen.piotroski.score = 4
    mon = Monitor(TEST=run)
    rep = check(ctx, mon)
    assert "trigger" in kinds(rep)
    assert store.get_holding(hid).thesis.triggers[0].fired_at is not None
    assert any(e.kind == "trigger" and "piotroski < 5" in e.text for e in store.journal(hid))
    # next run, same state: nothing new
    rep2 = check(ctx, mon, now=datetime(2026, 2, 16))
    assert "trigger" not in kinds(rep2)
    assert len([a for a in store.list_alerts() if a.kind == "trigger"]) == 1
    # the condition clears, then returns: it fires again (a new event)
    ok = crafted()
    check(ctx, Monitor(TEST=ok), now=datetime(2026, 2, 17))
    rep4 = check(ctx, mon, now=datetime(2026, 2, 18))
    assert "trigger" in kinds(rep4)
    assert len([a for a in store.list_alerts() if a.kind == "trigger"]) == 2


def test_trigger_that_cannot_be_evaluated_keeps_its_state(ctx):
    hold(triggers=[Trigger(field="net_debt_ebitda", op=">", literal=0.1)])
    rep = check(ctx, Monitor(TEST=crafted()))  # 0.2 > 0.1 fires
    assert "trigger" in kinds(rep)
    run = crafted()
    run.macro.net_debt_ebitda = run.macro.net_debt_ebitda.model_copy(update={"value": None,
                                                                             "status": "n/m - negative EBITDA"})
    check(ctx, Monitor(TEST=run), now=datetime(2026, 2, 16))  # n/m: can't evaluate, state unchanged
    rep3 = check(ctx, Monitor(TEST=crafted()), now=datetime(2026, 2, 17))
    assert "trigger" not in kinds(rep3)  # never cleared, so no second alert


def test_new_earnings_reported(ctx):
    hid = hold()
    first = check(ctx, Monitor(TEST=crafted()))
    assert "earnings" not in kinds(first)  # the first check records the baseline
    later = crafted(fundamentals_as_of=date(2026, 3, 31))
    rep = check(ctx, Monitor(TEST=later), now=datetime(2026, 5, 1))
    assert kinds(rep) == ["earnings"] and "2026-03-31" in rep.fired[0].message
    assert check(ctx, Monitor(TEST=later), now=datetime(2026, 5, 2)).fired == []
    assert any("New earnings reported" in e.text for e in store.journal(hid))


def _lead(*events):
    return LeadershipResult(ticker="TEST", flag="flagged" if events else "none", departures=len(events),
                            events=list(events), layers_used=["8-K, full history"])


def test_new_leadership_departure(ctx):
    hold()
    old = LeadershipEvent(ticker="TEST", date=date(2025, 3, 1), role="CFO", person="A. Old", layer="8-K")
    check(ctx, Monitor(TEST=crafted(leadership=_lead(old))))
    new = LeadershipEvent(ticker="TEST", date=date(2026, 2, 20), role="CEO", person="B. New", layer="8-K")
    rep = check(ctx, Monitor(TEST=crafted(leadership=_lead(old, new))), now=datetime(2026, 2, 21))
    assert kinds(rep) == ["leadership"] and "CEO B. New" in rep.fired[0].message
    assert check(ctx, Monitor(TEST=crafted(leadership=_lead(old, new))), now=datetime(2026, 2, 22)).fired == []


def test_insider_cluster_buy(ctx):
    store.set_watch_level("TEST", None, None)  # a watchlist name gets the non-price alerts too
    ins = InsiderSummary(coverage="Form 4, full history", buyers=3, cluster_buy=True,
                         cluster_window=(date(2026, 1, 5), date(2026, 2, 1)))
    rep = check(ctx, Monitor(TEST=crafted(insiders=ins)))
    assert kinds(rep) == ["insider_cluster"] and "3 insiders" in rep.fired[0].message
    assert check(ctx, Monitor(TEST=crafted(insiders=ins)), now=datetime(2026, 2, 16)).fired == []


def test_piotroski_drop_against_the_purchase_snapshot(ctx):
    from portfolio.models import Metric

    hold(snapshot={"piotroski": Metric(value=9.0, display="9")})
    run = crafted()
    run.screen.piotroski.score = 8  # a drop of 1: below ALERT_PIOTROSKI_DROP
    assert check(ctx, Monitor(TEST=run)).fired == []
    run2 = crafted()
    run2.screen.piotroski.score = 7  # 9 → 7
    rep = check(ctx, Monitor(TEST=run2), now=datetime(2026, 2, 16))
    assert kinds(rep) == ["piotroski_drop"] and "9 → 7" in rep.fired[0].message
    assert check(ctx, Monitor(TEST=run2), now=datetime(2026, 2, 17)).fired == []  # baseline reset to 7


def test_stale_fundamentals_on_a_holding_only(ctx):
    hold()
    store.set_watch_level("WATCH", None, None)
    stale = dict(stale=True, stale_label="fundamentals may be stale: as of 2025-06-30")
    rep = check(ctx, Monitor(TEST=crafted(**stale), WATCH=crafted("WATCH", **stale)))
    assert [(a.ticker, a.kind) for a in rep.fired] == [("TEST", "stale")]


def test_one_failing_ticker_is_recorded_and_the_others_still_run(ctx):
    hold(tp=9.5)
    store.set_watch_level("BAD", 1.0, None)
    rep = check(ctx, Monitor(TEST=crafted(), BAD=RuntimeError("boom")))
    assert kinds(rep) == ["target"] and "boom" in rep.errors["BAD"]
    row = store.latest_check()
    assert row.status == "completed" and row.fired == 1 and "BAD" in row.errors


def test_email_skipped_cleanly_when_smtp_not_configured(ctx):
    hold(tp=9.5)
    rep = check(ctx, Monitor(TEST=crafted()))
    assert rep.email_status == notify.SKIPPED_NOT_CONFIGURED
    assert store.list_alerts()[0].email_status == notify.SKIPPED_NOT_CONFIGURED


def test_email_sent_when_configured(ctx, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.host, self.port = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, user, pw):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("msg", msg["To"], msg["Subject"], msg.get_content()))

    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "me@example.com")
    monkeypatch.setenv("ALERT_EMAIL_TO", "me@example.com")
    hold(tp=9.5)
    rep = pa.check_alerts(ctx, "screen", monitor=Monitor(TEST=crafted()), now=T0,
                          send_email=lambda alerts: notify.send_alert_email(alerts, smtp_factory=FakeSMTP))
    assert rep.email_status == notify.SENT
    msgs = [s for s in sent if s[0] == "msg"]
    assert len(msgs) == 1 and "TEST" in msgs[0][2] and "at or above target" in msgs[0][3]


def test_last_metrics_feed_the_thesis_check(ctx):
    hid = hold(triggers=[Trigger(field="piotroski", op="<", literal=5)])
    check(ctx, Monitor(TEST=crafted()))
    m, label = pa.now_metrics("TEST")
    assert m["piotroski"].value == 9 and label.startswith("alert check")
    assert m["moat_score"].status == "N/A - no full analysis yet"
    hj = pa.journal_for("TEST")
    assert hj[0].holding.holding_id == hid and hj[0].check.triggers[0].state == "ok"
