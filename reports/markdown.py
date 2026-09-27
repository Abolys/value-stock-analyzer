"""Markdown export. Markdown has no pages, so the footer closes every top-level
section and the document itself. Charts are embedded as base64 PNG data URIs."""

from __future__ import annotations

import base64

from reports.build import Report, Table


def _cell(v: str) -> str:
    return str(v).replace("|", "\\|").replace("\n", " ")


def _table(t: Table) -> str:
    lines = ["| " + " | ".join(_cell(h) for h in t.headers) + " |", "|" + "---|" * len(t.headers)]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in t.rows]
    return "\n".join(lines)


def to_markdown(rep: Report) -> str:
    out = [f"# {rep.title}"]
    foot = f"\n---\n_{rep.footer}_\n"
    for sec in rep.sections:
        out.append(f"## {sec.title}")
        out += sec.paragraphs
        out += [_table(t) for t in sec.tables]
        for f in sec.figures:
            if f.png is not None:
                out.append(f"![{f.title}](data:image/png;base64,{base64.b64encode(f.png).decode()})")
            else:
                out.append(f"_{f.title} chart not rendered: {f.reason}_")
            if f.caption:
                out.append(f"_{f.caption}_")
        out.append(foot)
    return "\n\n".join(out)
