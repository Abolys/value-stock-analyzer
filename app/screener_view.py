"""Screener page view model (pure; no Streamlit).

- build_table: one row per ScreenResult with numeric sort values, display text,
  a cell state (pass / fail / na / nm / info) and a note for every cell. N/A and
  n/m cells sort as blanks and show their full reason in the cell (Rule 2b: n/m
  is shown with its reason in table cells, never as N/A). st.dataframe has no
  per-cell tooltips, so the notes (thresholds, Altman zone, Beneish) are listed
  in a "Cell notes" table under the results.
- filter_results: the status and source filters; the scatter follows the same set.
- screen_changes: the weekly "Changes since last screen" diff of two runs.
- scatter_points: margin of safety vs quality with the top-N labels and exclusions.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from pandas.io.formats.style import Styler
from pydantic import BaseModel, Field

import config
from app import theme
from app.charts import ScatterPoint
from data.universe import list_labels, load_list
from screening.models import (
    FAIL, FAIL_DISPLAY, NA, NM, NOT_APPLICABLE, PASS, SLOT_FCF, SLOT_LEVERAGE, SLOT_MOS, SLOT_SHARES,
    STATUS_FAIL, STATUS_FAILED_TO_LOAD, STATUS_INCOMPLETE, STATUS_PASS, MetricResult, ScreenResult,
)
from storage.screen_store import ScreenRun

STATUS_FILTERS = ["Pass", "Fail", "Incomplete", "All"]
DEFAULT_STATUS = "Pass"
_STATUS_OF_FILTER = {"Pass": STATUS_PASS, "Fail": STATUS_FAIL, "Incomplete": STATUS_INCOMPLETE}

COLUMNS = ["Ticker", "Source", "Margin of safety", "FCF yield vs 10Y", "Net debt/EBITDA", "Share trend",
           "EV/EBIT yield", "Piotroski", "Trap risk", "P/TBV", "Asset coverage", "Quality", "Status", "Data as of"]
COLUMN_HELP = {
    "Margin of safety": "Graham Number vs actual latest price",
    "FCF yield vs 10Y": "SBC-adjusted FCF yield vs the 10-year government yield of the trading currency; "
                        "cash runway in months for FCF-negative names; ROE spread / FFO yield when sector-adjusted",
    "Net debt/EBITDA": "Leverage; P/TBV (banks) or P/B (insurers, other financials) when sector-adjusted",
    "Share trend": "Split-adjusted share-count change per year, latest segment",
    "EV/EBIT yield": "TTM EBIT / enterprise value (information unless USE_EARNINGS_YIELD_IN_SCREEN)",
    "Piotroski": "F-score out of 9",
    "Trap risk": "Piotroski weak or Altman distress; hover for Altman zone and Beneish",
    "P/TBV": "Market cap / tangible book (information only)",
    "Asset coverage": "Tangible book / market cap, with band; net-net highlighted",
    "Quality": "Business quality 0–10 (n of 4 inputs); nothing divided by price",
    "Data as of": "Fundamentals' latest period end",
}


MISSING_SORT = float("-inf")
COLUMN_WIDTHS = {"Ticker": 150, "Source": 130, "Margin of safety": 150, "FCF yield vs 10Y": 190,
                 "Net debt/EBITDA": 190, "Share trend": 100, "EV/EBIT yield": 130, "Piotroski": 110,
                 "Trap risk": 140, "P/TBV": 150, "Asset coverage": 150, "Quality": 140, "Status": 130,
                 "Data as of": 140}  # pixels


class Cell(BaseModel):
    text: str
    state: str = "info"  # pass | fail | na | nm | info
    tip: str = ""
    sort: float | str | None = None


def _outcome_state(outcome: str) -> str:
    return {PASS: "pass", FAIL: "fail", NM: "nm", NA: "na", NOT_APPLICABLE: "na"}.get(outcome, "info")


def _short(status: str) -> str:
    """The cell text for a missing or not-meaningful value: the full status with its reason."""
    return status


def metric_cell(m: MetricResult | None, sector_adjusted: bool = False) -> Cell:
    if m is None:
        return Cell(text="N/A", state="na", tip="N/A - metric not computed")
    tip_parts = [m.name]
    if m.threshold:
        tip_parts.append(f"threshold {m.threshold}")
    if not m.value.ok:
        return Cell(text=_short(m.value.status), state="nm" if m.value.is_nm else "na",
                    tip="; ".join([m.name, m.value.status]))
    text = m.display
    if sector_adjusted and m.slot in (SLOT_FCF, SLOT_LEVERAGE):
        text = f"{m.name.split(' (')[0].split(' vs')[0]}: {text}"
    state = _outcome_state(m.outcome)
    if m.na_reason:  # the value exists but its comparison doesn't: the gap sits next to the number
        tip_parts.append(m.na_reason)
        text = f"{text} ({m.na_reason})"
        state = "na"
    return Cell(text=text, state=state, tip="; ".join(tip_parts), sort=m.value.value)


def _ev_cell(r: ScreenResult) -> Cell:
    ey = r.earnings_yield
    if ey is None:
        return Cell(text="N/A", state="na", tip="N/A - not computed")
    if not ey.value.ok:
        if ey.net_cash_flag:
            return Cell(text="net cash > mcap", state="nm",
                        tip=f"{ey.value.status} — a flag worth a look, not an error")
        return Cell(text=_short(ey.value.status), state="nm" if ey.value.is_nm else "na", tip=ey.value.status)
    v = ey.value.value
    return Cell(text=f"{v:.1%}", state="pass" if v >= config.MIN_EARNINGS_YIELD else "fail", sort=v,
                tip=f"threshold ≥ {config.MIN_EARNINGS_YIELD:.0%} (MIN_EARNINGS_YIELD)"
                    + ("" if config.USE_EARNINGS_YIELD_IN_SCREEN else "; information only, not a screen metric"))


def _piotroski_cell(r: ScreenResult) -> Cell:
    p = r.piotroski
    if p is None:
        return Cell(text="N/A", state="na", tip="N/A - not computed")
    if p.status != "ok" or p.score is None:
        return Cell(text=p.status,
                    state="nm" if p.status.startswith("n/m") else "na",
                    tip=f"{p.status} ({p.available} of 9 checks available)")
    state = "pass" if p.score >= config.PIOTROSKI_STRONG else "fail" if p.score <= config.PIOTROSKI_WEAK else "info"
    return Cell(text=f"{p.score} / 9", state=state, sort=float(p.score),
                tip=f"{p.available} of 9 checks; strong ≥ {config.PIOTROSKI_STRONG}, weak ≤ {config.PIOTROSKI_WEAK}")


def _trap_cell(r: ScreenResult) -> Cell:
    alt = f"Altman Z'' {r.altman.display}" if r.altman else "Altman N/A"
    ben = f"Beneish {r.beneish.display}" if r.beneish else "Beneish N/A"
    tip = f"{alt}; {ben} (Beneish is probabilistic; false positives happen)"
    if r.trap_risk:
        zone = f" (Altman {r.altman.zone})" if r.altman and r.altman.zone == "distress" else ""
        return Cell(text=f"trap risk{zone}", state="fail", tip="; ".join(r.trap_risk_reasons) + f" — {tip}", sort=1.0)
    return Cell(text="—", state="info", tip=tip, sort=0.0)


def _ptbv_cell(r: ScreenResult) -> Cell:
    af = r.asset_floor
    if af is None:
        return Cell(text="N/A", state="na", tip="N/A - not computed")
    if not af.p_tbv.ok:
        return Cell(text=_short(af.p_tbv.status), state="nm" if af.p_tbv.is_nm else "na", tip=af.p_tbv.status)
    return Cell(text=f"{af.p_tbv.value:.2f}x", sort=af.p_tbv.value, tip="information only (asset floor)")


def _coverage_cell(r: ScreenResult) -> Cell:
    af = r.asset_floor
    if af is None:
        return Cell(text="N/A", state="na", tip="N/A - not computed")
    if af.net_net:
        return Cell(text=f"NET-NET · {af.coverage.value:.0%}" if af.coverage.ok else "NET-NET", state="pass",
                    sort=af.coverage.value if af.coverage.ok else None,
                    tip="trades below net current assets" + (f"; {af.burn_line}" if af.burn_line else ""))
    if not af.coverage.ok:
        return Cell(text=af.coverage_band if af.coverage_band == "none" else _short(af.coverage.status),
                    state="nm" if af.coverage.is_nm else "na", tip=af.coverage.status)
    return Cell(text=f"{af.coverage.value:.0%} {af.coverage_band}", sort=af.coverage.value,
                tip="tangible book / market cap (information only)")


def _status_cell(r: ScreenResult) -> Cell:
    state = {STATUS_PASS: "pass", STATUS_FAIL: "fail", STATUS_INCOMPLETE: "na"}.get(r.status, "na")
    tip = "; ".join(r.status_reasons) or r.load_error
    return Cell(text=r.display_status, state=state, tip=tip, sort=r.display_status)


def row_cells(r: ScreenResult) -> dict[str, Cell]:
    sa = r.treatment != "Standard"
    q = r.quality
    return {
        "Ticker": Cell(text=r.ticker + (" · sector-adjusted" if sa else ""), sort=r.ticker,
                       tip=f"{r.name} — {r.treatment}" + (f" ({r.industry})" if r.industry else "")),
        "Source": Cell(text=r.sources, sort=r.sources, tip=r.sources),
        "Margin of safety": metric_cell(r.metric(SLOT_MOS), sa),
        "FCF yield vs 10Y": metric_cell(r.metric(SLOT_FCF), sa),
        "Net debt/EBITDA": metric_cell(r.metric(SLOT_LEVERAGE), sa),
        "Share trend": metric_cell(r.metric(SLOT_SHARES), sa),
        "EV/EBIT yield": _ev_cell(r),
        "Piotroski": _piotroski_cell(r),
        "Trap risk": _trap_cell(r),
        "P/TBV": _ptbv_cell(r),
        "Asset coverage": _coverage_cell(r),
        "Quality": Cell(text=q.display if q else "N/A", sort=q.score if q else None,
                        state="info" if q and q.score is not None else "na", tip=q.working if q else ""),
        "Status": _status_cell(r),
        "Data as of": Cell(text=(r.fundamentals_as_of.isoformat() if r.fundamentals_as_of else "N/A")
                                + (" ⚠ stale" if r.stale else ""),
                           state="nm" if r.stale else ("info" if r.fundamentals_as_of else "na"),
                           sort=r.fundamentals_as_of.isoformat() if r.fundamentals_as_of else None,
                           tip=r.stale_label if r.stale else "fundamentals' latest period end"),
    }


class ScreenerTable(BaseModel):
    """Parallel frames (same index and columns): sortable values, display text, states and tips."""

    model_config = {"arbitrary_types_allowed": True}

    values: pd.DataFrame
    text: pd.DataFrame
    states: pd.DataFrame
    tips: pd.DataFrame
    tickers: list[str] = Field(default_factory=list)

    def styler(self) -> Styler:
        sty = self.values.style
        for col in self.values.columns:
            groups: dict[str, list] = {}
            for idx, txt in self.text[col].items():
                groups.setdefault(txt, []).append(idx)
            for txt, idxs in groups.items():
                sty = sty.format(lambda _v, t=txt: t, subset=pd.IndexSlice[idxs, [col]])
        states = self.states

        def colour(_df: pd.DataFrame) -> pd.DataFrame:
            return states.map(lambda s: (f"color: {theme.CELL_TEXT[s]}; background-color: {theme.CELL_BG[s]}"
                                         if theme.CELL_TEXT.get(s) else ""))

        return sty.apply(colour, axis=None)

    def notes(self) -> pd.DataFrame:
        """Per-cell notes (st.dataframe has no cell tooltips): ticker, column, value, note."""
        rows = []
        for i, t in enumerate(self.tickers):
            for col in COLUMNS[2:]:
                tip = self.tips.at[i, col]
                if tip:
                    rows.append({"ticker": t, "column": col, "value": self.text.at[i, col], "note": tip})
        return pd.DataFrame(rows, columns=["ticker", "column", "value", "note"])


def build_table(results: list[ScreenResult]) -> ScreenerTable:
    cells = [row_cells(r) for r in results]
    idx = pd.RangeIndex(len(cells))

    def frame(attr: str) -> pd.DataFrame:
        return pd.DataFrame([{c: getattr(row[c], attr) for c in COLUMNS} for row in cells], index=idx, columns=COLUMNS)

    values = frame("sort")
    for col in COLUMNS:
        if all(isinstance(v, (int, float)) or v is None for v in values[col]):
            # Streamlit shows Styler display text only for non-null cells, so N/A and n/m sort as
            # −∞ (always at one end) and still show their reason.
            values[col] = pd.to_numeric(values[col], errors="coerce").fillna(MISSING_SORT)
        else:
            values[col] = values[col].fillna("")
    return ScreenerTable(values=values, text=frame("text"), states=frame("state"), tips=frame("tip"),
                         tickers=[r.ticker for r in results])


def all_sources(results: list[ScreenResult]) -> list[str]:
    return sorted({s.strip() for r in results for s in r.sources.split(",") if s.strip()})


def filter_results(results: list[ScreenResult], status: str = DEFAULT_STATUS,
                   sources: list[str] | None = None) -> list[ScreenResult]:
    """The table's filters (Pass only by default); failed-to-load tickers are listed separately, never here."""
    out = [r for r in results if r.status != STATUS_FAILED_TO_LOAD]
    if status != "All":
        out = [r for r in out if r.status == _STATUS_OF_FILTER[status]]
    if sources:
        wanted = set(sources)
        out = [r for r in out if wanted & {s.strip() for s in r.sources.split(",")}]
    return out


# --------------------------------------------------------------------------
# Scatter
# --------------------------------------------------------------------------
def scatter_points(results: list[ScreenResult], top_n: int = config.SCATTER_LABEL_TOP_N
                   ) -> tuple[list[ScatterPoint], list[str]]:
    """Points with a computable margin of safety and quality; the rest listed with the reason.
    Labels go to the top N by (quality rank + margin-of-safety rank), best first."""
    pts, excluded = [], []
    for r in results:
        if r.status == STATUS_FAILED_TO_LOAD:
            excluded.append(f"{r.ticker}: failed to load")
            continue
        m = r.metric(SLOT_MOS)
        if m is None or not m.value.ok:
            excluded.append(f"{r.ticker}: margin of safety {m.value.status if m else 'N/A - Data Incomplete'}")
            continue
        if r.quality is None and r.decided_at_stage == 1:
            excluded.append(f"{r.ticker}: cut at stage 1 (statements not fetched, so no quality score)")
            continue
        if r.quality is None or r.quality.score is None:
            excluded.append(f"{r.ticker}: quality {r.quality.display if r.quality else 'N/A - Data Incomplete'}")
            continue
        pts.append(ScatterPoint(ticker=r.ticker, name=r.name, mos=m.value.value, quality=r.quality.score, status=r.status,
                                quality_display=r.quality.display))
    if pts:
        df = pd.DataFrame([{"q": p.quality, "m": p.mos} for p in pts])
        rank = df["q"].rank(ascending=False, method="min") + df["m"].rank(ascending=False, method="min")
        for i in rank.sort_values(kind="stable").index[:top_n]:
            pts[i].label = True
    return pts, excluded


# --------------------------------------------------------------------------
# Changes since last screen
# --------------------------------------------------------------------------
class ScreenChanges(BaseModel):
    new_pass: list[str] = Field(default_factory=list)
    dropped_pass: list[str] = Field(default_factory=list)
    newly_incomplete: list[str] = Field(default_factory=list)
    newly_stale: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def screen_changes(prev: list[ScreenResult], curr: list[ScreenResult], prev_lists: list[str] | None = None,
                   curr_lists: list[str] | None = None) -> ScreenChanges:
    before = {r.ticker: r for r in prev}
    now = {r.ticker: r for r in curr}
    out = ScreenChanges()
    for t, r in sorted(now.items()):
        b = before.get(t)
        if r.status == STATUS_PASS and (b is None or b.status != STATUS_PASS):
            out.new_pass.append(t)
        if r.status == STATUS_INCOMPLETE and (b is None or b.status != STATUS_INCOMPLETE):
            out.newly_incomplete.append(t)
        if r.stale and (b is None or not b.stale):
            out.newly_stale.append(t)
    for t, b in sorted(before.items()):
        if b.status == STATUS_PASS and (t not in now or now[t].status != STATUS_PASS):
            out.dropped_pass.append(t)
    new_names = set(now) - set(before)
    if new_names:
        out.notes.append(f"{len(new_names)} ticker(s) not in the previous screen")
    if prev_lists is not None and curr_lists is not None and sorted(prev_lists) != sorted(curr_lists):
        labels = list_labels()
        out.notes.append("the two screens covered different lists (previous: "
                         + ", ".join(labels.get(k, k) for k in prev_lists) + "; latest: "
                         + ", ".join(labels.get(k, k) for k in curr_lists) + ")")
    dropped_missing = [t for t in out.dropped_pass if t not in now]
    if dropped_missing:
        out.notes.append(f"dropped because no longer screened: {', '.join(dropped_missing)}")
    return out


def short_list(tickers: list[str], limit: int = config.CHANGES_LIST_MAX) -> tuple[list[str], int]:
    return tickers[:limit], max(0, len(tickers) - limit)


# --------------------------------------------------------------------------
# Run progress and list picker
# --------------------------------------------------------------------------
def eta(run: ScreenRun, now: datetime) -> timedelta | None:
    """Estimated time left from the average time per finished ticker so far."""
    if run.attempted < config.SCREEN_ETA_MIN_DONE or not run.total:
        return None
    per = (now - run.started_at) / run.attempted
    return per * max(0, run.total - run.attempted)


def fmt_duration(td: timedelta) -> str:
    mins = int(td.total_seconds() // 60)
    return f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{max(mins, 1)} min"


def refresh_status() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(config.UNIVERSE_REFRESH_STATUS_PATH.read_text())
    except (OSError, ValueError):
        return {}


def list_picker_rows() -> list[dict[str, Any]]:
    status = refresh_status()
    rows = []
    for key, label in list_labels().items():
        df = load_list(key)
        st = status.get(key, {})
        rows.append({"key": key, "label": label, "count": len(df),
                     "as_of": ", ".join(sorted(set(df["as_of"]))) if len(df) else "empty",
                     "stale": st.get("status") == "stale", "stale_note": st.get("message", "")})
    return rows


def fail_display(status: str) -> str:
    return FAIL_DISPLAY if status == STATUS_FAIL else status
