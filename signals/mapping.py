"""Breakpoint mappings (SPEC "Score mapping").

`*_BREAKPOINTS` constants are lists of (value, score) pairs. Linear mappings
interpolate between points and are flat beyond the ends; step mappings (cash
runway) take the score of the highest breakpoint at or below the value.
Both clamp to SCORE_MIN..SCORE_MAX.
"""

from __future__ import annotations

from pydantic import BaseModel

import config


def _clamp(score: float) -> float:
    return max(config.SCORE_MIN, min(config.SCORE_MAX, float(score)))


def map_linear(breakpoints: list[tuple[float, float]], x: float) -> float:
    pts = sorted(breakpoints)
    if x <= pts[0][0]:
        return _clamp(pts[0][1])
    if x >= pts[-1][0]:
        return _clamp(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return _clamp(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    raise AssertionError("unreachable")


def map_step(breakpoints: list[tuple[float, float]], x: float) -> float:
    pts = sorted(breakpoints)
    score = pts[0][1]
    for bx, by in pts:
        if x >= bx:
            score = by
    return _clamp(score)


class MappingStep(BaseModel):
    """One visible step of a score mapping (SPEC "Score mapping": every rationale shows its working).

    kind: "base" (a breakpoint mapping that sets the score), "adjust" (a ± adjustment),
    "cap" (a ceiling), "subscore" (one input to an average), "info" (shown, not scored).
    """

    name: str
    input_display: str
    output: float | None = None
    kind: str = "base"
    note: str = ""

    @property
    def line(self) -> str:
        if self.output is None:
            return f"{self.name} {self.input_display}" + (f" ({self.note})" if self.note else "")
        if self.kind == "adjust":
            return f"{self.name} {self.input_display} → {self.output:+g}"
        if self.kind == "cap":
            return f"{self.name} → capped at {self.output:g}"
        return f"{self.name} {self.input_display} → {self.output:.1f}"


def mapped(name: str, x: float, breakpoints: list[tuple[float, float]], input_display: str,
           kind: str = "base", step: bool = False) -> MappingStep:
    """Map `x` through breakpoints (linear by default, step for the runway table) as a visible step."""
    score = map_step(breakpoints, x) if step else map_linear(breakpoints, x)
    return MappingStep(name=name, input_display=input_display, output=round(score, 2), kind=kind)


def mapping_line(steps: list[MappingStep], score: float | None) -> str:
    parts = [s.line for s in steps]
    parts.append(f"score {score:.1f}" if score is not None else "score Insufficient data")
    return "; ".join(parts)
