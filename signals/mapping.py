"""Breakpoint mappings (SPEC "Score mapping").

`*_BREAKPOINTS` constants are lists of (value, score) pairs. Linear mappings
interpolate between points and are flat beyond the ends; step mappings (cash
runway) take the score of the highest breakpoint at or below the value.
Both clamp to SCORE_MIN..SCORE_MAX.
"""

from __future__ import annotations

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
