"""Synthetic AnalysisInputs for lens tests, built on tests/screen_helpers.py so
every expected value can be hand-computed."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from analysis.inputs import AnalysisInputs
from data.fundamentals import Fundamentals
from data.provider import InfoResult
from data.sector import route
from signals.context import context_fields
from signals.cyclicality import cyclicality
from signals.dividends import dividend_safety
from signals.insider_activity import insider_summary
from tests.screen_helpers import TODAY, make_fundamentals, make_info, run_eval


def make_inputs(f: Fundamentals | None = None, info: InfoResult | None = None, px: float = 10.0,
                dividends: pd.Series | None = None, leadership=None, **kw) -> AnalysisInputs:
    info = info or make_info()
    f = f or make_fundamentals()
    screen = run_eval(f, px=px, info=info, **kw)
    r = route(info)
    return AnalysisInputs(ticker="TEST", today=TODAY, info=info, route=r, f=f, screen=screen,
                          cyclicality=cyclicality(info.get("sector"), info.get("industry")),
                          dividends=dividend_safety(dividends, f, screen.price, TODAY, r.sector_adjusted),
                          insiders=insider_summary(None, TODAY), leadership=leadership,
                          context=context_fields(info, None))


def fixture_inputs(provider, ticker: str, db_path: Path, edgar=None):
    from analysis.inputs import load_inputs
    from data.fixture_provider import captured_on
    from screening.engine import ScreenContext

    return load_inputs(ScreenContext(provider=provider, db_path=db_path, today=captured_on(ticker)), ticker, edgar=edgar)
