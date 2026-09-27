"""Alerts for holdings and the watchlist (SPEC "Portfolio and thesis tracking").

`check_alerts` runs at the end of every screen run (scripts/run_screen.py) and on app
start (scripts/check_alerts.py, launched in the background by app/alert_jobs.py). For
each ticker it loads fresh inputs and recomputes the deterministic lenses (Quant, Macro;
no Moat or Devil's Advocate, so a check costs no LLM money beyond the cached 6-K
departure confirmation), then evaluates every alert type:

- price at or below a buy-below price, or at or above a target (thesis or watchlist level);
- new earnings reported (fundamentals_as_of moved forward since the last check);
- a new leadership departure; an insider cluster buy;
- Piotroski dropping by ALERT_PIOTROSKI_DROP or more below its baseline;
- any sell trigger firing;
- fundamentals going stale on a holding.

Each alert fires once per event:
- conditions (price levels, triggers, cluster buy, stale) keep an "active" flag in
  monitor_state and fire only when it turns on, so they re-arm once the condition clears;
  a value that is N/A or n/m leaves the flag unchanged;
- events (earnings, departures, Piotroski drops) carry a unique event key, and the alerts
  table refuses a second (ticker, kind, event key). The first check of a ticker records
  their baselines without alerting.

Holding alerts also go into the holding's journal. New alerts are emailed when SMTP is set.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Callable

import config
from analysis.models import AnalysisRun
from portfolio import metrics as pm
from portfolio import notify
from portfolio import store
from portfolio.models import FIRED, UNKNOWN, Alert, CheckReport, Holding, HoldingJournal, Metric
from portfolio.thesis import with_levels
from portfolio.triggers import evaluate
from storage import history

log = logging.getLogger(__name__)

Monitor = Callable[..., AnalysisRun]


def monitor_run(ctx, ticker: str, llm=None, edgar=None) -> AnalysisRun:
    """Fresh inputs plus the deterministic lenses; the LLM lenses are left to full analyses."""
    from analysis.inputs import AnalysisLoadError, load_inputs
    from analysis.macro import macro_lens
    from analysis.pipeline import view_run
    from analysis.quant import quant_lens
    from llm.departure import confirm_with

    confirm = confirm_with(llm) if llm is not None and llm.configured else None
    try:
        x = load_inputs(ctx, ticker, edgar=edgar, confirm=confirm)
    except AnalysisLoadError as exc:
        return AnalysisRun(ticker=ticker, today=ctx.today, load_error=str(exc))
    run = view_run(x, ctx.provider)
    for name, fn in (("quant", quant_lens), ("macro", macro_lens)):
        try:
            setattr(run, name, fn(x))
        except Exception as exc:  # recorded, never fatal
            run.errors.append(f"{name}: {type(exc).__name__}: {exc}")
    return run


def current_metrics(run: AnalysisRun, db_path=None) -> tuple[dict[str, Metric], str]:
    """The check's metrics, with Moat / Devil's Advocate / aggregate from the latest full analysis."""
    check = pm.extract_metrics(run, source=f"check {run.today}")
    full = history.latest_full_run(run.ticker, path=db_path)
    label = f"analysis {full.analysis_id} ({full.today})" if full else ""
    return pm.merge_lens_scores(check, pm.extract_metrics(full) if full else None, label), label


def watchlist_tickers() -> list[str]:
    from data.universe import load_list

    try:
        df = load_list("watchlist")
    except Exception as exc:  # a malformed file must not stop the holdings' alerts
        log.warning("watchlist unreadable: %s", exc)
        return []
    return sorted({t.strip().upper() for t in df["ticker"].tolist() if isinstance(t, str) and t.strip()})


class _Ctx:
    """Per-ticker evaluation state: where alerts go and what fired."""

    def __init__(self, ticker: str, holdings: list[Holding], source: str, today: date, path, now: datetime):
        self.ticker, self.holdings, self.source, self.today, self.path, self.now = (
            ticker, holdings, source, today, path, now)
        self.fired: list[Alert] = []

    def fire(self, kind: str, event_key: str, message: str, holding: Holding | None = None) -> Alert | None:
        targets = [holding] if holding is not None else self.holdings
        a = Alert(ticker=self.ticker, holding_id=targets[0].holding_id if targets else None, kind=kind,
                  event_key=event_key, message=message, created_at=self.now, source=self.source)
        aid = store.insert_alert(a, self.path)
        if aid is None:
            return None  # already alerted for this event
        a.alert_id = aid
        for h in targets:
            store.add_journal(h.holding_id, f"Alert: {config.ALERT_KINDS.get(kind, kind)} — {message}",
                              kind="trigger" if kind == "trigger" else "alert", alert_id=aid, when=self.now,
                              path=self.path)
        self.fired.append(a)
        return a

    def condition(self, key: str, active: bool | None, kind: str, message: str,
                  holding: Holding | None = None) -> Alert | None:
        """Fire when a condition turns on; None (can't evaluate) leaves the flag unchanged."""
        if active is None:
            return None
        state_key = f"active:{key}"
        was = bool(store.get_state(self.ticker, state_key, False, self.path))
        store.set_state(self.ticker, state_key, bool(active), self.path)
        if active and not was:
            return self.fire(kind, f"{key}@{self.now.isoformat(timespec='seconds')}", message, holding)
        return None


def _price_levels(c: _Ctx, price: Metric, levels: list[tuple[str, float | None, float | None, Holding | None]]) -> None:
    for key, bb, tp, holding in levels:
        where = f" ({holding.account})" if holding else " (watchlist)"
        if bb is not None:
            c.condition(f"buy_below:{key}", price.value <= bb if price.ok else None, "buy_below",
                        f"price {price.display} at or below buy-below {bb:,.2f}{where}", holding)
        if tp is not None:
            c.condition(f"target:{key}", price.value >= tp if price.ok else None, "target",
                        f"price {price.display} at or above target {tp:,.2f}{where}", holding)


def _triggers(c: _Ctx, metrics: dict[str, Metric]) -> None:
    for h in c.holdings:
        if h.thesis is None:
            continue
        m = with_levels(metrics, h.thesis)
        for t in h.thesis.triggers:
            st = evaluate(t, m)
            active = None if st.state == UNKNOWN else st.state == FIRED
            a = c.condition(f"trigger:{t.trigger_id}", active, "trigger",
                            f"sell trigger fired: {t.text} (now {st.current}; threshold {st.threshold})", h)
            if a is not None:
                store.mark_trigger_fired(t.trigger_id, c.now, c.path)


def _earnings(c: _Ctx, run: AnalysisRun, first: bool) -> None:
    cur = run.fundamentals_as_of
    prev = store.get_state(c.ticker, "fundamentals_as_of", None, c.path)
    if cur is None:
        return
    store.set_state(c.ticker, "fundamentals_as_of", cur.isoformat(), c.path)
    if not first and prev and cur > date.fromisoformat(prev):
        c.fire("earnings", f"earnings:{cur}", f"new earnings reported: fundamentals now to {cur} (were to {prev}); "
                                              "refresh the analysis")


def _leadership(c: _Ctx, run: AnalysisRun, first: bool) -> None:
    lead = run.leadership
    if lead is None:
        return
    keys = {f"{e.date}:{e.role}:{e.person}": e for e in lead.events}
    seen = set(store.get_state(c.ticker, "leadership_seen", [], c.path) or [])
    store.set_state(c.ticker, "leadership_seen", sorted(seen | set(keys)), c.path)
    if first:
        return
    for k in sorted(set(keys) - seen):
        e = keys[k]
        c.fire("leadership", f"leadership:{k}", f"leadership departure: {e.role} {e.person} ({e.date}; {e.layer})")


def _cluster(c: _Ctx, run: AnalysisRun) -> None:
    ins = run.insiders
    if ins is None or not ins.available:
        return
    win = f" {ins.cluster_window[0]} to {ins.cluster_window[1]}" if ins.cluster_window else ""
    c.condition("insider_cluster", ins.cluster_buy, "insider_cluster",
                f"insider cluster buy ({ins.buyers} insiders buying{win}; {ins.coverage})")


def _piotroski(c: _Ctx, cur: Metric) -> None:
    if not cur.ok:
        return
    base = store.get_state(c.ticker, "piotroski_baseline", None, c.path)
    if base is None:
        snaps = [h.snapshot["piotroski"].value for h in c.holdings
                 if "piotroski" in h.snapshot and h.snapshot["piotroski"].ok]
        base = max(snaps) if snaps else cur.value
    if base - cur.value >= config.ALERT_PIOTROSKI_DROP:
        c.fire("piotroski_drop", f"piotroski:{base:g}->{cur.value:g}@{c.today}",
               f"Piotroski dropped {base:g} → {cur.value:g} (alert at a drop of {config.ALERT_PIOTROSKI_DROP}+)")
        base = cur.value
    base = max(base, cur.value)
    store.set_state(c.ticker, "piotroski_baseline", base, c.path)


def evaluate_ticker(run: AnalysisRun, metrics: dict[str, Metric], holdings: list[Holding], watch=None,
                    source: str = "manual", path=None, now: datetime | None = None) -> list[Alert]:
    """Every alert type for one ticker; returns the alerts that fired now."""
    now = now or datetime.now()
    c = _Ctx(run.ticker.upper(), holdings, source, run.today or now.date(), path, now)
    first = store.get_state(c.ticker, "first_checked", None, path) is None
    levels = [(f"h{h.holding_id}", h.thesis.buy_below_price, h.thesis.target_price, h)
              for h in holdings if h.thesis is not None]
    if watch is not None:
        levels.append(("watch", watch.buy_below_price, watch.target_price, None))
    _price_levels(c, metrics["price"], levels)
    _triggers(c, metrics)
    _earnings(c, run, first)
    _leadership(c, run, first)
    _cluster(c, run)
    _piotroski(c, metrics["piotroski"])
    if holdings:
        c.condition("stale", bool(run.stale), "stale",
                    f"fundamentals may be stale: {run.stale_label or 'as of ' + str(run.fundamentals_as_of)}")
    if first:
        store.set_state(c.ticker, "first_checked", c.now.isoformat(timespec="seconds"), path)
    store.set_state(c.ticker, "last_metrics", {"at": c.now.isoformat(timespec="seconds"), "metrics": pm.dump(metrics)},
                    path)
    return c.fired


def check_alerts(ctx, source: str, llm=None, edgar=None, monitor: Monitor = monitor_run,
                 tickers: list[str] | None = None, now: datetime | None = None,
                 send_email: Callable[[list[Alert]], str] = notify.send_alert_email,
                 check_id: int | None = None) -> CheckReport:
    """Evaluate alerts for every holding and watchlist ticker (or just `tickers`)."""
    now = now or datetime.now()
    path = ctx.db_path
    holdings = store.list_holdings(path=path)
    watch = store.watch_levels(path)
    universe = sorted({h.ticker for h in holdings} | set(watchlist_tickers()) | set(watch))
    todo = [t.upper() for t in tickers] if tickers else universe
    rep = CheckReport(source=source, started_at=now, tickers=todo)
    rep.check_id = check_id if check_id is not None else store.start_check(source, path=path)
    try:
        for t in todo:
            try:
                run = monitor(ctx, t, llm=llm, edgar=edgar)
                if run.load_error:
                    rep.errors[t] = f"failed to load: {run.load_error}"
                    continue
                if run.errors:
                    rep.errors[t] = "; ".join(run.errors)
                metrics, _ = current_metrics(run, path)
                rep.metrics[t] = pm.dump(metrics)
                rep.fired += evaluate_ticker(run, metrics, [h for h in holdings if h.ticker == t], watch.get(t),
                                             source, path, now)
            except Exception as exc:  # one ticker never stops the others; recorded on the check
                log.exception("alert check failed for %s", t)
                rep.errors[t] = f"{type(exc).__name__}: {exc}"
        rep.email_status = send_email(rep.fired)
        if rep.fired:
            store.set_email_status([a.alert_id for a in rep.fired if a.alert_id], rep.email_status, path)
        store.finish_check(rep.check_id, store.COMPLETED, len(todo), len(rep.fired), rep.errors, rep.email_status, path)
    except Exception as exc:
        store.finish_check(rep.check_id, f"failed: {type(exc).__name__}: {exc}", len(todo), len(rep.fired),
                           rep.errors, rep.email_status, path)
        raise
    return rep


def last_metrics(ticker: str, path=None) -> tuple[dict[str, Metric], str]:
    """The metrics stored by the latest alert check of a ticker, and when it ran."""
    blob = store.get_state(ticker, "last_metrics", None, path)
    if not blob:
        return {}, ""
    return pm.load(blob.get("metrics")), blob.get("at", "")


def now_metrics(ticker: str, path=None) -> tuple[dict[str, Metric], str]:
    """Today's metrics for a thesis check: the latest alert check's (with Moat / Devil's Advocate /
    aggregate from the latest full analysis), else the latest full analysis alone."""
    m, at = last_metrics(ticker, path)
    if m:
        return m, f"alert check {at.replace('T', ' ')}"
    full = history.latest_full_run(ticker, path=path)
    if full is None:
        return {}, "no alert check or analysis yet"
    return pm.extract_metrics(full, source=f"analysis {full.analysis_id}"), f"analysis {full.analysis_id} ({full.today})"


def journal_for(ticker: str, path=None) -> list[HoldingJournal]:
    """Every holding of a ticker (open or closed) with its thesis check and journal, for the export."""
    from portfolio.thesis import thesis_check

    out = []
    for h in store.list_holdings(include_closed=True, ticker=ticker, path=path):
        m, label = now_metrics(h.ticker, path)
        out.append(HoldingJournal(holding=h, check=thesis_check(h, m, label), entries=store.journal(h.holding_id, path)))
    return out
