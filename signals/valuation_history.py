"""Historical valuation ratios from SEC EDGAR XBRL facts, and the valuation-based recovery clock
(SPEC "Valuation-based recovery": how long the ratio has taken to return to its own
VALUATION_RECOVERY_YEARS median).

- Market cap on each trading day = actual close × shares outstanding as filed by then, restated
  for later splits (yfinance closes are split-adjusted; XBRL share counts are as reported).
- P/E = market cap / TTM net income, P/B = market cap / equity, each using only figures filed by
  that day (data/xbrl.py known_on). A day with net income or equity ≤ 0 has no meaningful ratio
  (Rule 2b) and is left out, never set to zero.
- The ratio is P/E when it is meaningful on at least VALUATION_MIN_COVERAGE of the window's days,
  else P/B on the same test; financials and REITs use P/B (Rule 5). Neither → "Insufficient data".
- Money is converted to the trading currency with the latest FX rate when they differ (Rule 5); a
  constant rate leaves the ratio's position against its own median unchanged.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from pydantic import BaseModel, Field

import config
from data.values import Datum
from data.xbrl import CompanyFacts, instant_points, known_on, ttm_points

INSUFFICIENT = "Insufficient data"
SOURCE = "SEC EDGAR XBRL (10-K/10-Q as first filed, point in time) with actual closes"


class ValuationRecovery(BaseModel):
    status: str = "ok"  # ok | not_cheap | Unavailable/Insufficient reason
    ratio: str = ""  # "P/E" | "P/B"
    current: float | None = None
    median: float | None = None
    window_years: int = config.VALUATION_RECOVERY_YEARS
    spells: list[float] = Field(default_factory=list)  # completed cheap-side spells, months
    median_months: float | None = None
    p25: float | None = None
    p75: float | None = None
    coverage: float | None = None  # share of the window's days with a meaningful ratio
    notes: list[str] = Field(default_factory=list)
    source: str = SOURCE

    @property
    def display(self) -> str:
        if self.status not in ("ok", "not_cheap"):
            return self.status
        head = (f"{self.ratio} {self.current:.1f} vs its {self.window_years}-year median {self.median:.1f}")
        if self.status == "not_cheap":
            return (f"{head}: not {config.VALUATION_CHEAP_MARGIN:.0%} or more below its own median, so there is "
                    "nothing to time")
        if not self.spells:
            return f"{head}: below median, but no completed cheap spell in the window to time it by"
        few = (f"; only {len(self.spells)} completed spell{'s' if len(self.spells) != 1 else ''} "
               f"(fewer than MIN_EPISODES {config.MIN_EPISODES}), low confidence"
               if len(self.spells) < config.MIN_EPISODES else "")
        n = len(self.spells)
        return (f"{head}: past spells {config.VALUATION_CHEAP_MARGIN:.0%}+ below the median took a median "
                f"{_mo(self.median_months)} to get back to it (interquartile {self.p25:.0f}–{self.p75:.0f} months, "
                f"{n} spell{'s' if n != 1 else ''}){few}")


def valuation_recovery_months(ratio: pd.Series, cheap_below: bool = True) -> tuple[float | None, list[float]]:
    """Completed cheap spells against the ratio's own VALUATION_RECOVERY_YEARS median, in months.
    A spell starts when the ratio is VALUATION_CHEAP_MARGIN beyond the median on the cheap side
    (below it for P/E and P/B, above it for FCF yield) and ends on the first day back at the median,
    like a drawdown episode, so day-to-day wobbles around the median don't count as spells.
    Returns (median, spell lengths)."""
    s = ratio.dropna().sort_index()
    if s.empty:
        return None, []
    s = s[s.index >= s.index[-1] - pd.DateOffset(years=config.VALUATION_RECOVERY_YEARS)]
    med = float(s.median())
    m = config.VALUATION_CHEAP_MARGIN
    spells, start = [], None
    for d, v in s.items():
        if start is None:
            if (v <= med * (1 - m)) if cheap_below else (v >= med * (1 + m)):
                start = d
        elif (v >= med) if cheap_below else (v <= med):
            spells.append(_months(start.date(), d.date()))
            start = None
    return med, spells


def is_cheap(value: float, median: float, cheap_below: bool = True) -> bool:
    """Beyond VALUATION_CHEAP_MARGIN of its own median on the cheap side."""
    m = config.VALUATION_CHEAP_MARGIN
    return value <= median * (1 - m) if cheap_below else value >= median * (1 + m)


def _mo(m: float) -> str:
    return f"{m:.0f} month{'s' if round(m) != 1 else ''}"


def _months(start: date, end: date) -> float:
    return (end - start).days / config.DAYS_PER_MONTH


def split_factor_after(splits: pd.Series | None, d: date) -> float:
    f = 1.0
    for ts, ratio in (splits.items() if splits is not None else []):
        if pd.Timestamp(ts).date() > d and ratio and ratio > 0:
            f *= float(ratio)
    return f


def ratio_series(facts: CompanyFacts, closes: pd.Series, splits: pd.Series | None, fx: float = 1.0
                 ) -> dict[str, pd.Series]:
    """{"P/E": series, "P/B": series} on the price days, NaN where not meaningful or not yet known."""
    days = pd.DatetimeIndex(closes.index)
    sh = facts.concept("shares_outstanding")
    if sh.status != "ok":
        return {}
    shares = known_on([(end, known, v * split_factor_after(splits, end)) for end, known, v in instant_points(sh)],
                      days)
    mcap = closes.astype(float) * shares
    out: dict[str, pd.Series] = {}
    ni = facts.concept("net_income")
    if ni.status == "ok":
        earn = known_on(ttm_points(ni), days) * fx
        out["P/E"] = (mcap / earn).where(earn > 0)
    eq = facts.concept("equity")
    if eq.status == "ok":
        book = known_on(instant_points(eq), days) * fx
        out["P/B"] = (mcap / book).where(book > 0)
    return out


def _quartiles(xs: list[float]) -> tuple[float, float, float]:
    s = pd.Series(xs)
    return float(s.quantile(0.25)), float(s.median()), float(s.quantile(0.75))


def valuation_recovery(facts: CompanyFacts | None, facts_status: str, closes: pd.Series | None,
                       splits: pd.Series | None, sector_adjusted: bool, fx: Datum | None = None) -> ValuationRecovery:
    if facts is None:
        return ValuationRecovery(status=f"Unavailable — {facts_status}")
    if closes is None or closes.dropna().empty:
        return ValuationRecovery(status="Unavailable — no price history")
    notes = []
    rate = 1.0
    if fx is not None:
        if not fx.ok:
            return ValuationRecovery(status=f"Unavailable — currency conversion {fx.status}")
        rate = fx.value
        notes.append(f"reported figures converted at {rate:.4f} ({fx.period_label})")
    closes = closes.dropna().sort_index()
    series = ratio_series(facts, closes, splits, rate)
    if not series:
        return ValuationRecovery(status=f"{INSUFFICIENT} - shares outstanding not reported in XBRL")
    start = closes.index[-1] - pd.DateOffset(years=config.VALUATION_RECOVERY_YEARS)
    order = ["P/B"] if sector_adjusted else ["P/E", "P/B"]
    tried = []
    for name in order:
        s = series.get(name)
        if s is None:
            tried.append(f"{name}: inputs not reported")
            continue
        window = s[s.index >= start]
        coverage = float(window.notna().mean()) if len(window) else 0.0
        if coverage + config.RATIO_COMPARE_TOLERANCE < config.VALUATION_MIN_COVERAGE:
            tried.append(f"{name}: meaningful on {coverage:.0%} of the window's days "
                         f"(needs {config.VALUATION_MIN_COVERAGE:.0%}; n/m when earnings or equity ≤ 0)")
            continue
        med, spells = valuation_recovery_months(window)
        current = window.dropna().iloc[-1]
        res = ValuationRecovery(ratio=name, current=float(current), median=med, spells=spells, coverage=coverage,
                                notes=[*notes, *tried])
        if sector_adjusted:
            res.notes.append("financials and REITs: P/B (earnings-based ratios not used, Rule 5)")
        if pd.isna(s.iloc[-1]):
            res.notes.append(f"latest day has no meaningful {name}; the last meaningful value is used")
        if not is_cheap(current, med):
            res.status = "not_cheap"
        elif spells:
            res.p25, res.median_months, res.p75 = _quartiles(spells)
        return res
    return ValuationRecovery(status=f"{INSUFFICIENT} - " + "; ".join(tried), notes=notes)
