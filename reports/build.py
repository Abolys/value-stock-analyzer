"""The per-ticker report: an ordered list of sections built from one AnalysisRun
and its charts, rendered to Markdown (reports/markdown.py) and .docx
(reports/docx_export.py). Every assumption and data gap is included, and the
footer (SPEC "Charts" → Exports) goes on every page."""

from __future__ import annotations

import re
from typing import Any, Callable

from pydantic import BaseModel, Field

import config
from analysis.models import LENSES, DevilsAdvocateResult, LLMLensResult, MoatResult, QuantResult
from app import stock_view as sv
from app.charts import ChartOut
from analysis.models import AnalysisRun
from reports.images import Renderer, render_png

DISCLAIMER = "Personal research output, not investment advice."


class Table(BaseModel):
    headers: list[str]
    rows: list[list[str]]


class Figure(BaseModel):
    title: str
    png: bytes | None = None
    reason: str = ""  # why it was not rendered
    caption: str = ""


class Section(BaseModel):
    title: str
    paragraphs: list[str] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)
    figures: list[Figure] = Field(default_factory=list)


class Report(BaseModel):
    ticker: str
    title: str
    sections: list[Section] = Field(default_factory=list)
    footer: str = ""


def footer_text(run: AnalysisRun) -> str:
    price = run.price_as_of.isoformat() if run.price_as_of else "N/A"
    fund = run.fundamentals_as_of.isoformat() if run.fundamentals_as_of else "N/A"
    providers = ", ".join(run.providers) or "N/A"
    return (f"Price as of {price} · Fundamentals as of {fund} · Data: {providers} · "
            f"Value Stock Analyzer v{config.APP_VERSION} · {DISCLAIMER}")


def plain(md: str) -> str:
    """Markdown emphasis stripped for .docx paragraphs."""
    # Markers only at word boundaries, so identifiers such as COST_OF_CAPITAL keep their underscores.
    return re.sub(r"(?<!\w)(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1(?!\w)", r"\2", md or "")


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.4g}"
    return str(v)


def _figure(c: ChartOut | None, render: Renderer) -> Figure | None:
    if c is None:
        return None
    png, reason = render(c.fig)
    return Figure(title=c.title, png=png, reason=reason, caption=c.caption)


def _lens_section(lens, label: str) -> Section:
    sec = Section(title=label)
    if lens is None:
        sec.paragraphs.append("Not run.")
        return sec
    sec.paragraphs.append(f"Score: {lens.display}" + (f" · confidence {lens.confidence}" if lens.ok else ""))
    if lens.mapping_line:
        sec.paragraphs.append(f"How this score was built: {lens.mapping_line}")
    for c in lens.confidence_reasons:
        sec.paragraphs.append(f"Low confidence: {c}")
    if lens.completeness:
        sec.paragraphs.append(f"Data completeness: {lens.completeness}")
    if isinstance(lens, MoatResult) and lens.sector_threat:
        sec.paragraphs.append(f"Sector threat: {lens.sector_threat}")
    if isinstance(lens, DevilsAdvocateResult):
        for label2, value in (("Weakest valuation assumption", lens.weakest_valuation_assumption),
                              ("Weakest moat point", lens.weakest_moat_point),
                              ("Accounting red flags", lens.accounting_red_flags),
                              ("Implied growth", lens.implied_growth_view), ("Insiders", lens.insider_activity),
                              ("Dividend", lens.dividend_risk), ("Asset floor", lens.asset_floor),
                              ("Leadership", lens.leadership_turnover),
                              ("Impairment", f"{lens.impairment_type} — {lens.impairment_reasoning}"),
                              ("Data freshness", lens.data_freshness)):
            if value:
                sec.paragraphs.append(f"{label2}: {value}")
        if lens.bull_case_requirements:
            sec.paragraphs.append("For the bull case to hold: " + "; ".join(lens.bull_case_requirements))
    if isinstance(lens, LLMLensResult) and lens.evidence:
        sec.tables.append(Table(headers=["Evidence field", "Value", "Why"],
                                rows=[[e.field, e.value, e.why] for e in lens.evidence]))
    if lens.key_figures:
        sec.tables.append(Table(headers=["Key figure", "Value"], rows=[[k, v] for k, v in lens.key_figures.items()]))
    if lens.assumptions:
        sec.tables.append(Table(headers=["Assumption", "Value"],
                                rows=[[k, _fmt(v)] for k, v in lens.assumptions.items()]))
    if lens.rationale:
        sec.paragraphs.append(lens.rationale)
    return sec


def build_report(run: AnalysisRun, chart_map: dict[str, ChartOut | None],
                 render: Renderer = render_png) -> Report:
    rep = Report(ticker=run.ticker, title=f"{run.ticker} — {run.company}", footer=footer_text(run))
    fig = lambda key: _figure(chart_map.get(key), render)  # noqa: E731
    s = run.screen

    head = Section(title="Summary")
    head.paragraphs += [sv.verdict_badge(run.aggregate), sv.asof_line(run),
                        f"Sector {run.sector or 'N/A'} · industry {run.industry or 'N/A'} · {run.treatment} · "
                        f"trading currency {run.currency or 'N/A'}"]
    head.paragraphs += [f"Tag: {t.text}" + (f" ({t.tip})" if t.tip else "") for t in sv.header_tags(run)]
    if run.week52:
        head.paragraphs.append(f"52-week range {run.week52.low:,.2f}–{run.week52.high:,.2f} (adjusted closes); "
                               f"latest {run.week52.latest:,.2f}, {run.week52.label}")
    if s is not None:
        head.paragraphs.append(f"Screen status (information only; manual tickers bypass the screen): "
                               f"{s.display_status}; quality {s.quality.display if s.quality else 'N/A'}")
    head.figures += [f for f in (fig("week52"),) if f]
    rep.sections.append(head)

    lenses = Section(title="Lens scores")
    lenses.tables.append(Table(headers=["Lens", "Score", "How it was built"],
                               rows=[[sv.LENS_SHORT[n], run.lens(n).display if run.lens(n) else "not run",
                                      run.lens(n).mapping_line if run.lens(n) else ""] for n in LENSES]))
    if run.aggregate:
        lenses.paragraphs.append(f"Aggregate {run.aggregate.display} — {run.aggregate.verdict}")
        lenses.paragraphs.append(run.aggregate.rationale)
    lenses.figures += [f for f in (fig("dot_strip"),) if f]
    rep.sections.append(lenses)

    for n in LENSES:
        rep.sections.append(_lens_section(run.lens(n), sv.LENS_SHORT[n] + " lens"))

    fund = Section(title="Fundamentals over time")
    fund.figures += [f for f in (fig("small_multiples"),) if f]
    rep.sections.append(fund)

    val = Section(title="Valuation")
    val.paragraphs += sv.valuation_lines(run.quant if isinstance(run.quant, QuantResult) else None, run.currency)
    val.figures += [f for f in (fig("heatmap"),) if f]
    rep.sections.append(val)

    peers = Section(title="Versus peers")
    f_peer = fig("peers")
    if f_peer:
        peers.figures.append(f_peer)
    else:
        peers.paragraphs.append("Peer strip unavailable: no peers from the universe lists with a screen result.")
    rep.sections.append(peers)

    af = Section(title="Asset floor")
    c = chart_map.get("asset_floor")
    if c is not None:
        af.paragraphs += c.notes + c.flags
    af.figures += [f for f in (fig("asset_floor"),) if f]
    rep.sections.append(af)

    trap = Section(title="Value-trap scores, EV/EBIT and insiders")
    c = chart_map.get("trap")
    if c is not None:
        trap.paragraphs += c.notes
    trap.paragraphs += [sv.ev_line(run), sv.insider_line(run)]
    trap.figures += [f for f in (fig("trap"),) if f]
    rep.sections.append(trap)

    div = Section(title="Dividend and context")
    div.paragraphs.append(sv.dividend_line(run))
    div.tables.append(Table(headers=["Context (never scored)", "Value"], rows=[list(x) for x in sv.context_items(run)]))
    div.figures += [f for f in (fig("dividend_bars"), fig("dividend_payout")) if f]
    rep.sections.append(div)

    t = run.turnaround
    tur = Section(title="Turnaround outlook")
    if t is None:
        tur.paragraphs.append("Not run.")
    else:
        tur.paragraphs.append(t.headline or t.status)
        if t.confidence:
            tur.paragraphs.append(f"Confidence: {t.confidence} — {'; '.join(t.confidence_reasons)}")
        tur.paragraphs.append(f"Confidence rule: {t.confidence_rule}")
        tur.paragraphs.append(t.survivorship_caveat)
        tur.paragraphs.append("Near-term signals: " + ("; ".join(f"{x.name} — {x.detail}" for x in t.active_signals)
                                                       or "none active"))
        tur.paragraphs.append("Catalysts: " + "; ".join(x.text + (f" ({x.source})" if x.source else "")
                                                         for x in t.catalysts))
        tur.paragraphs.append(t.asset_floor_line + " (context only)")
        tur.paragraphs.append(f"Valuation-based recovery: {t.valuation_recovery}")
        if t.assumptions:
            tur.tables.append(Table(headers=["Assumption", "Value"],
                                    rows=[[k, _fmt(v)] for k, v in t.assumptions.items()]))
    tur.figures += [f for f in (fig("turnaround_range"), fig("drawdown")) if f]
    rep.sections.append(tur)

    assumptions = Section(title="Assumptions")
    assumptions.tables.append(Table(headers=["Constant", "Value"], rows=[
        [k, _fmt(getattr(config, k))] for k in (
            "COST_OF_CAPITAL", "TERMINAL_GROWTH", "DCF_STAGE1_YEARS", "DCF_BASE_YEARS", "STAGE1_GROWTH_CAP",
            "STAGE1_GROWTH_FLOOR", "MIN_MARGIN_OF_SAFETY", "MIN_FCF_SPREAD_OVER_10Y", "MAX_NET_DEBT_EBITDA",
            "MAX_SHARE_GROWTH_PER_YEAR", "MIN_CASH_RUNWAY_MONTHS", "LENS_WEIGHTS", "CONTROVERSY_GAP",
            "DRAWDOWN_THRESHOLD", "RECOVERY_BAND", "MIN_EPISODES")]))
    if s is not None and s.assumptions:
        assumptions.tables.append(Table(headers=["Screen assumption", "Value"],
                                        rows=[[k, _fmt(v)] for k, v in s.assumptions.items()]))
    assumptions.paragraphs.append(f"Input hash {run.input_hash or 'N/A'}; analysis id {run.analysis_id}; "
                                  f"LLM cost ${run.total_cost:.4f}")
    rep.sections.append(assumptions)

    gaps = Section(title="Data gaps")
    rows = [[a, b] for a, b in sv.data_gaps(run, chart_map)]
    for sec in rep.sections:
        rows += [[f"{f.title} chart", f"not rendered: {f.reason}"] for f in sec.figures if f.png is None]
    if rows:
        gaps.tables.append(Table(headers=["Item", "Reason"], rows=rows))
    else:
        gaps.paragraphs.append("No data gaps recorded.")
    rep.sections.append(gaps)
    return rep


def export_both(run: AnalysisRun, chart_map: dict[str, ChartOut | None],
                render: Renderer = render_png) -> tuple[str, bytes]:
    from reports.docx_export import to_docx
    from reports.markdown import to_markdown

    rep = build_report(run, chart_map, render)
    return to_markdown(rep), to_docx(rep)


ExportFn = Callable[[], bytes]
