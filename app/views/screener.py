"""Screener page (docs/ui-mockup.html → Screener): run header, list picker and
"Run new screen", changes since last screen, the results table (Pass only by
default) and the margin of safety vs quality scatter. Clicking a table row, a
scatter point or a name in the changes panel opens that ticker's Stock page."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

import config
from app import auth, charts, screen_jobs, theme, ui
from app import screener_view as sv
from app.stock_view import Tag
from data.universe import list_labels
from screening.models import STATUS_FAILED_TO_LOAD
from storage import screen_store as store

STATUS_TAG = {store.COMPLETED: "success", store.RUNNING: "accent", store.STOPPED: "warning", store.BLOCKED: "danger"}


def run_header(shown: store.ScreenRun, latest: store.ScreenRun | None, results) -> None:
    labels = list_labels()
    failed = [r for r in results if r.status == STATUS_FAILED_TO_LOAD]
    with st.container(border=True):
        tags = [Tag(text=shown.status.capitalize(), kind=STATUS_TAG.get(shown.status, "accent"))]
        line = (f"{ui.tags_html(tags)} <b>{shown.started_at:%b %d, %Y %H:%M}</b> · lists: "
                + ", ".join(labels.get(k, k) for k in shown.lists)
                + f" · {shown.attempted:,} screened · {shown.passed_stage1:,} passed stage 1 · "
                  f"{shown.passed_stage2:,} Pass · {shown.failed_to_load:,} failed to load")
        st.markdown(line, unsafe_allow_html=True)
        st.caption(f"{shown.refetched_reported:,} refetched after earnings · {shown.served_from_cache:,} from cache. "
                   "Failed to load is never counted as a Fail.")
        if latest is not None and latest.run_id != shown.run_id and shown.status == store.COMPLETED:
            detail = "; ".join(latest.health_failures) or latest.note or ""
            st.warning(f"The newest run ({latest.run_id}, {latest.started_at:%b %d %H:%M}) is **{latest.status}**"
                       + (f": {detail}" if detail else "") + f". Showing the last completed screen ({shown.run_id}).")
        if shown.flagged_fields:
            st.warning(f"Fields N/A for more than {config.FIELD_NA_SPIKE:.0%} of tickers — likely renamed upstream: "
                       + ", ".join(shown.flagged_fields))
        if failed:
            with st.expander(f"Failed to load ({len(failed)})"):
                st.dataframe(pd.DataFrame([{"ticker": r.ticker, "reason": r.load_error,
                                            "likely cause": sv.load_failure_hint(r.load_error)} for r in failed]),
                             hide_index=True, width="stretch",
                             column_config={"ticker": st.column_config.Column(width=90),
                                             "likely cause": st.column_config.Column(width=240)})


@st.fragment(run_every=config.SCREEN_PROGRESS_POLL_SECONDS)
def run_progress() -> None:
    """Live status of a background run, re-read from the database on every poll. Phases:
    preparing (the row exists but the universe is not loaded yet), fetching latest prices,
    then the per-ticker bar with an estimated time left. Warns when a run makes no progress
    or has stopped without finishing."""
    db = config.RUNS_DB_PATH
    run = screen_jobs.active_run(db)
    if run is None:
        paused = screen_jobs.interrupted_run(db)
        if paused is not None:
            where = (f"after {paused.attempted:,} of {paused.total:,} tickers "
                     if paused.total else "before it could screen any ticker ")
            st.warning(f"Screen run {paused.run_id} stopped ('{paused.status}') {where}— resume it below "
                       "to continue where it left off.")
        return
    labels = ", ".join(list_labels().get(k, k) for k in run.lists) or "all lists"
    if not run.total:
        st.info(f"Screen run {run.run_id} ({labels}) is preparing: health check and universe load…")
    elif not run.attempted:
        st.info(f"Screen run {run.run_id} ({labels}): fetching latest prices for {run.total:,} tickers…")
    else:
        left = sv.eta(run, datetime.now())
        st.progress(run.attempted / run.total if run.total else 0.0,
                    text=f"Screen run {run.run_id}: {run.attempted:,} of {run.total:,} tickers "
                         f"({run.passed_stage1:,} passed stage 1, {run.failed_to_load:,} failed to load)"
                         + (f" · about {sv.fmt_duration(left)} left" if left is not None else " · estimating time left…"))
    waited_min = (datetime.now() - run.started_at).total_seconds() / 60
    if not run.attempted and waited_min > config.SCREEN_STALL_WARN_MINUTES:
        st.warning(f"No tickers screened after {waited_min:.0f} minutes — the background run may have stalled "
                   "(or the app's container slept while idle). It is still alive and will keep updating here.")


def list_picker(has_completed: bool) -> None:
    db = config.RUNS_DB_PATH
    if not auth.is_owner():
        run_progress()
        st.caption("Screens are run by the app's owner; you're viewing the latest results.")
        return
    busy = screen_jobs.active_run(db) is not None
    with st.expander("Run a new screen", expanded=not has_completed or busy):
        rows = sv.list_picker_rows()
        cols = st.columns(min(4, len(rows)) or 1)
        chosen = []
        for i, row in enumerate(rows):
            label = (f"{row['label']} ({row['count']:,}) · as of {row['as_of']}" + (" · ⚠ stale" if row["stale"] else "")
                     + (f" · ⚠ {row['age_days']} days old" if row["aged"] else ""))
            with cols[i % len(cols)]:
                if st.checkbox(label, value=row["key"] == "cowz", key=f"list-{row['key']}",
                               help=row["stale_note"] or None):
                    chosen.append(row["key"])
        st.caption("Refresh the lists with `python scripts/refresh_universe.py`; a failed download keeps the previous "
                   "list and marks it stale.")
        if st.button("Run new screen", type="primary", disabled=busy or not chosen):
            try:
                log = screen_jobs.launch(chosen, db_path=db)
                st.success(f"Screen started in the background (log: {log}).")
            except RuntimeError as exc:
                st.warning(str(exc))
        run_progress()
        paused = screen_jobs.interrupted_run(db)
        if paused and not busy:
            st.warning(f"Run {paused.run_id} is '{paused.status}' after {paused.attempted:,} of {paused.total:,} "
                       f"tickers. {paused.note or ''}")
            if st.button("Resume run"):
                screen_jobs.launch(resume=True, db_path=db)
                st.rerun()


def changes_panel(changes: sv.ScreenChanges | None) -> None:
    st.subheader("Changes since last screen")
    if changes is None:
        st.caption("Needs two completed screens.")
        return
    cards = [("New Pass", changes.new_pass, "new"), ("Dropped from Pass", changes.dropped_pass, "drop"),
             ("Newly Incomplete", changes.newly_incomplete, "inc"), ("Newly stale", changes.newly_stale, "stale")]
    cols = st.columns(4)
    for col, (title, tickers, key) in zip(cols, cards):
        with col.container(border=True):
            st.caption(f"{title} ({len(tickers)})")
            shown, more = sv.short_list(tickers)
            for t in shown:
                if st.button(t, key=f"chg-{key}-{t}", type="tertiary"):
                    ui.open_ticker(t)
            if more:
                st.caption(f"+{more} more")
            if not tickers:
                st.caption("—")
    for n in changes.notes:
        st.caption(n)


def results_table(results) -> list:
    c1, c2 = st.columns([2, 3])
    status = c1.radio("Status", sv.STATUS_FILTERS, index=sv.STATUS_FILTERS.index(sv.DEFAULT_STATUS),
                      horizontal=True, key="screen-status")
    sources = c2.multiselect("Source", sv.all_sources(results), key="screen-sources",
                             placeholder="All sources")
    shown = sv.filter_results(results, status, sources)
    st.subheader("Results")
    st.caption(f"Showing {status if status != 'All' else 'all statuses'} · "
               f"{', '.join(sources) if sources else 'all sources'} · {len(shown)} tickers")
    if not shown:
        st.info("No tickers match these filters.")
        return shown
    table = sv.build_table(shown)
    event = st.dataframe(table.styler(), hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key="screen-table",
                         column_config={c: st.column_config.Column(help=sv.COLUMN_HELP.get(c),
                                                                   width=sv.COLUMN_WIDTHS.get(c))
                                        for c in sv.COLUMNS})
    st.caption("Green / red = against the config threshold · grey N/A = data missing · amber n/m = not meaningful, "
               "each with its reason · click a row to open the ticker")
    with st.expander("Cell notes (thresholds, reasons, Altman zone and Beneish)"):
        st.dataframe(table.notes(), hide_index=True, width="stretch",
                     column_config={"ticker": st.column_config.Column(width=90),
                                    "column": st.column_config.Column(width=180),
                                    "value": st.column_config.Column(width=180)})
    rows = event.selection.rows if event and event.selection else []
    if rows:
        ui.open_ticker(table.tickers[rows[0]])
    return shown


def scatter(shown) -> None:
    st.subheader("Margin of safety vs quality")
    pts, excluded = sv.scatter_points(shown)
    out = charts.screener_scatter(pts, excluded)
    event = ui.chart(out, key="screen-scatter", on_select="rerun", selection_mode="points")
    if excluded:
        with st.expander(f"Not on the scatter ({len(excluded)})"):
            st.dataframe(pd.DataFrame([dict(zip(("ticker", "reason"), e.split(": ", 1))) for e in excluded]),
                         hide_index=True, width="stretch",
                         column_config={"ticker": st.column_config.Column(width=90)})
    sel = event.selection.points if event and event.selection else []
    if sel:
        idx = sel[0].get("point_index", sel[0].get("point_number"))
        if idx is not None and idx < len(pts):
            ui.open_ticker(pts[idx].ticker)


def render(provider) -> None:
    st.title("Screener")
    db = config.RUNS_DB_PATH
    completed = store.list_runs(db, limit=1000)
    completed = [r for r in completed if r.status == store.COMPLETED]
    list_picker(bool(completed))
    run, done = store.results_run(db)
    if run is None:
        latest = store.latest_run(db)
        if latest and latest.status == store.BLOCKED:
            st.error(f"Run {latest.run_id} was blocked: health check failed — " + "; ".join(latest.health_failures))
        st.info("No screen results yet.")
        return
    results = store.load_results(run.run_id, db)
    partial = run.status != store.COMPLETED
    if partial:
        st.warning(f"No screen has completed yet. Showing the **partial** run {run.run_id} ({run.status}): "
                   f"{done:,} of {run.total:,} tickers screened so far. Tickers not reached yet are missing; "
                   "refresh the page to see more.")
    run_header(run, store.latest_run(db), results)
    st.space(16)
    changes = None
    if not partial and len(completed) > 1:
        prev = completed[1]
        changes = sv.screen_changes(store.load_results(prev.run_id, db), results, prev.lists, run.lists)
    changes_panel(changes)
    st.space(16)
    shown = results_table(results)
    st.space(16)
    scatter(shown)
    div = store.load_divergences(run.run_id, db)
    if div:
        with st.expander(f"{sv.DIVERGENCE_TITLE} ({len(div)})"):
            st.caption(sv.DIVERGENCE_NOTE)
            rf = {r.ticker: (r.risk_free.value if r.risk_free.ok else None) for r in results}
            st.dataframe(theme.style_states(sv.divergence_frame(div), sv.divergence_states(div, rf)),
                         hide_index=True, width="stretch")
    if run.field_na:
        with st.expander("Per-field N/A counts"):
            st.dataframe(pd.DataFrame([{"field": k, "N/A": v["na"], "of": v["of"]} for k, v in run.field_na.items()]),
                         hide_index=True,
                         column_config={"field": st.column_config.Column(width=220),
                                        "N/A": st.column_config.Column(width=80),
                                        "of": st.column_config.Column(width=80)})
