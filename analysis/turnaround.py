"""Turnaround timing (SPEC "Turnaround estimate integrity").

Runs after the four lenses and reads their structured output. From the stock's
own dividend- and split-adjusted closes (Rule 5) it finds every drawdown of at
least DRAWDOWN_THRESHOLD below the rolling 52-week high, measures how long each
took to get back within RECOVERY_BAND of its prior high, classifies each (and
the current drop) as market-driven or company-specific against the listing
country's benchmark, and reports the median and interquartile range of past
recovery times for drops of the current type. Never a single date.

- The price history is split at corporate-action breaks; no rolling high,
  drawdown or recovery is computed across one.
- Unrecovered episodes (still open today, or cut off by a break) are counted
  and listed, never dropped.
- Too few episodes of the current type → all types, said so, one confidence
  level lower. Too few episodes at all → PEER_COUNT universe peers, labelled
  "Peer-based, lower confidence".
- A structural impairment flag from the Devil's Advocate withholds the range
  or forces Low confidence (TURNAROUND_STRUCTURAL_ACTION); nothing overrides it.
- Technical signals, the insider cluster buy, catalysts and the asset floor are
  reported beside the range and never change it.
"""

from __future__ import annotations

import logging
import math
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

import config
from analysis.models import AnalysisRun, insufficient
from analysis.turnaround_models import (
    BASIS_ALL_TYPES, BASIS_PEERS, BASIS_SAME_TYPE, COMPANY_SPECIFIC, MARKET_DRIVEN, PEER_LABEL, STATUS_NOT_IN_DRAWDOWN,
    STATUS_OK, STATUS_WITHHELD, STRUCTURAL_LABEL, UNCLASSIFIED, VALUATION_UNAVAILABLE, Catalyst, CurrentDrawdown,
    Episode, Peer, PeerHistory, PeerSelection, RecoveryStats, Segment, TechnicalSignal, TurnaroundResult,
)
from data import corporate_actions as ca
from data import prices
from data.corporate_actions import Break
from data.provider import DataProvider, ProviderError
from data.universe import list_labels, load_universe
from signals.asset_floor import AssetFloor
from signals.insider_activity import InsiderSummary, cluster_buy
from storage import screen_store

if TYPE_CHECKING:
    from analysis.inputs import AnalysisInputs

log = logging.getLogger(__name__)

NO_SCREEN_RUN = "Unavailable - no completed screen run (peers' industry and market cap unknown)"
VALUATION_FMP_TODO = "Unavailable — FMP is configured but its historical ratios are not implemented yet"


# --------------------------------------------------------------------------
# Benchmark by listing country
# --------------------------------------------------------------------------
def listing_country(ticker: str) -> str:
    t = ticker.upper()
    for suffix, country in config.LISTING_COUNTRY_SUFFIXES.items():
        if t.endswith(suffix.upper()):
            return country
    return config.LISTING_COUNTRY_DEFAULT


def benchmark_for(ticker: str) -> str | None:
    return config.BENCHMARKS.get(listing_country(ticker))


class Benchmarks:
    """Adjusted closes per benchmark symbol, loaded once per turnaround run."""

    def __init__(self, provider: DataProvider):
        self.provider = provider
        self._cache: dict[str, tuple[pd.Series | None, str]] = {}

    def get(self, symbol: str | None, country: str = "") -> tuple[pd.Series | None, str]:
        if not symbol:
            return None, f"no benchmark configured for listing country {country or 'unknown'}"
        if symbol not in self._cache:
            try:
                s = prices.adjusted_closes(self.provider, symbol).dropna()
                self._cache[symbol] = (s[s > 0], "") if len(s) else (None, f"no {symbol} history")
            except ProviderError as exc:
                self._cache[symbol] = (None, f"{symbol} history unavailable ({exc})")
        return self._cache[symbol]


# --------------------------------------------------------------------------
# Price history, segments and episodes (adjusted closes only)
# --------------------------------------------------------------------------
class PriceHistory(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: str
    closes: pd.Series
    ranges: pd.DataFrame | None = None  # adjusted daily "high" / "low", when the provider has them
    breaks: list[Break] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def load_history(provider: DataProvider, ticker: str) -> PriceHistory:
    """Adjusted closes plus corporate-action breaks. Raises ProviderError when there are no closes."""
    closes = prices.adjusted_closes(provider, ticker).dropna()
    closes = closes[closes > 0]
    if closes.empty:
        raise ProviderError(f"no positive adjusted closes for {ticker}")
    notes: list[str] = []
    try:
        # Break detection reads actual closes and raw share counts, as in the share-count trend.
        actual = prices.actual_closes(provider, ticker)
        splits = provider.get_splits(ticker)
        sh = provider.get_shares_history(ticker)
        breaks = ca.corporate_action_breaks(ticker, actual, sh.series, splits)
    except ProviderError as exc:
        breaks = ca.load_manual_breaks(ticker)
        notes.append(f"break heuristic unavailable ({exc}); manual corporate-action list only")
    try:
        ranges = provider.get_price_range(ticker)
    except ProviderError as exc:
        ranges = None
        notes.append(f"daily highs and lows unavailable ({exc}); Williams %R is close-based")
    return PriceHistory(ticker=ticker, closes=closes, ranges=ranges, breaks=breaks, notes=notes)


def segments(closes: pd.Series, break_dates: list[date]) -> list[pd.Series]:
    """Split at each break; the break day starts the new segment."""
    cuts = sorted(pd.Timestamp(d) for d in break_dates)
    out, rest = [], closes.sort_index()
    for cut in cuts:
        before, rest = rest[rest.index < cut], rest[rest.index >= cut]
        if len(before):
            out.append(before)
    if len(rest):
        out.append(rest)
    return out


def rolling_high(seg: pd.Series) -> pd.Series:
    """Rolling 52-week high within one segment (shorter at the segment's start)."""
    return seg.rolling(config.ROLLING_HIGH_DAYS, min_periods=1).max()


def _window_argmax(vals: np.ndarray, i: int, floor: int = 0) -> int:
    """Index of the rolling 52-week high at i (the window starts no earlier than `floor`).
    On ties the most recent close at the high wins: a drop starts from the last time price stood there."""
    lo = max(floor, i - config.ROLLING_HIGH_DAYS + 1)
    window = vals[lo:i + 1]
    return lo + len(window) - 1 - int(np.argmax(window[::-1]))


def _months(start: date, end: date) -> float:
    return (end - start).days / config.DAYS_PER_MONTH


CLOCKS = ("trough", "threshold", "peak")


def clock_label(clock: str) -> str:
    return {"trough": "from the trough", "threshold": f"from the first {config.DRAWDOWN_THRESHOLD:.0%} fall",
            "peak": "from the prior high"}[clock]


def find_episodes(seg: pd.Series, segment: int, ticker: str, unrecovered_reason: str) -> list[Episode]:
    """Drawdowns of at least DRAWDOWN_THRESHOLD below the rolling high inside one segment.
    An episode recovers at the first close back within RECOVERY_BAND of its own peak
    (not the decaying rolling high). The next one can't open before that, and its peak
    is the rolling high since the recovery, so one peak never starts two episodes."""
    vals = seg.to_numpy(dtype=float)
    idx = seg.index
    out: list[Episode] = []
    start: tuple[int, int] | None = None  # (peak index, threshold index)
    floor = 0  # the rolling-high window never reaches back before the last recovery

    def make(peak: int, thr: int, rec: int | None) -> Episode:
        end = rec if rec is not None else len(vals) - 1
        trough = peak + int(np.argmin(vals[peak:end + 1]))
        pk, th, tr = idx[peak].date(), idx[thr].date(), idx[trough].date()
        rd = idx[rec].date() if rec is not None else None
        months = {c: _months(d, rd) for c, d in zip(CLOCKS, (tr, th, pk))} if rd else {}
        return Episode(ticker=ticker, segment=segment, peak_date=pk, peak_price=float(vals[peak]), threshold_date=th,
                       trough_date=tr, trough_price=float(vals[trough]), recovery_date=rd,
                       drop=1 - vals[trough] / vals[peak], recovered=rec is not None,
                       recovery_months=months.get(config.RECOVERY_CLOCK_START), months_from=months,
                       unrecovered_reason="" if rec is not None else unrecovered_reason)

    for i, v in enumerate(vals):
        if start is None:
            peak = _window_argmax(vals, i, floor)
            if v <= (1 - config.DRAWDOWN_THRESHOLD) * vals[peak]:
                start = (peak, i)
        elif v >= (1 - config.RECOVERY_BAND) * vals[start[0]]:
            out.append(make(*start, i))
            start, floor = None, i
    if start is not None:
        out.append(make(*start, None))
    return out


def benchmark_drop(bench: pd.Series | None, peak: date, trough: date, reason: str = "") -> tuple[float | None, str]:
    """1 − benchmark(trough) / benchmark(peak), on the benchmark's adjusted closes."""
    if bench is None or bench.empty:
        return None, reason or "benchmark history unavailable"
    if bench.index[0] > pd.Timestamp(peak):
        return None, f"benchmark history starts {bench.index[0].date()}, after the peak ({peak})"
    b0, b1 = bench.asof(pd.Timestamp(peak)), bench.asof(pd.Timestamp(trough))
    if pd.isna(b0) or pd.isna(b1) or b0 <= 0:
        return None, "benchmark has no close at the peak or trough"
    return float(1 - b1 / b0), ""


def classify(stock_drop: float, bench_drop: float | None) -> str:
    """market-driven if the benchmark fell by at least MARKET_DRIVEN_RATIO × the stock's drop."""
    if bench_drop is None:
        return UNCLASSIFIED
    hurdle = config.MARKET_DRIVEN_RATIO * stock_drop - config.RATIO_COMPARE_TOLERANCE
    return MARKET_DRIVEN if bench_drop >= hurdle else COMPANY_SPECIFIC


def _classify_episode(e: Episode, bench: pd.Series | None, symbol: str, reason: str) -> None:
    bd, why = benchmark_drop(bench, e.peak_date, e.trough_date, reason)
    e.benchmark, e.benchmark_drop, e.episode_type = symbol, bd, classify(e.drop, bd)
    e.classification_note = why or (f"{symbol} {-bd:+.0%} vs stock {-e.drop:+.0%} over the same peak-to-trough "
                                     f"window (market-driven at ≥ {config.MARKET_DRIVEN_RATIO:.0%} of the stock's drop)")


def current_drawdown(seg: pd.Series) -> CurrentDrawdown:
    vals = seg.to_numpy(dtype=float)
    last = len(vals) - 1
    hi = _window_argmax(vals, last)
    tr = hi + int(np.argmin(vals[hi:]))
    dd = 1 - vals[last] / vals[hi]
    qualifying = dd >= config.DRAWDOWN_THRESHOLD - config.RATIO_COMPARE_TOLERANCE
    return CurrentDrawdown(drawdown=float(dd), as_of=seg.index[last].date(), high_date=seg.index[hi].date(),
                           high_price=float(vals[hi]), latest_price=float(vals[last]), qualifying=qualifying,
                           trough_date=seg.index[tr].date() if qualifying else None,
                           trough_price=float(vals[tr]) if qualifying else None)


class History(BaseModel):
    ticker: str
    segments: list[Segment]
    episodes: list[Episode]
    current: CurrentDrawdown
    last_segment: pd.Series
    last_ranges: pd.DataFrame | None = None
    model_config = ConfigDict(arbitrary_types_allowed=True)


def build_history(h: PriceHistory, bench: pd.Series | None, symbol: str, bench_reason: str = "") -> History:
    segs = segments(h.closes, [b.date for b in h.breaks])
    later_breaks = sorted(b.date for b in h.breaks)
    seg_models, episodes = [], []
    for i, seg in enumerate(segs):
        is_last = i == len(segs) - 1
        if is_last:
            reason = f"still below the prior high as of {seg.index[-1].date()}"
        else:
            nxt = next((d for d in later_breaks if pd.Timestamp(d) > seg.index[-1]), None)
            reason = f"segment ended at corporate-action break {nxt} before recovery"
        seg_models.append(Segment(index=i, start=seg.index[0].date(), end=seg.index[-1].date(), closes=len(seg)))
        episodes.extend(find_episodes(seg, i, h.ticker, reason))
    for e in episodes:
        _classify_episode(e, bench, symbol, bench_reason)
    cur = current_drawdown(segs[-1])
    if cur.qualifying:
        bd, why = benchmark_drop(bench, cur.high_date, cur.trough_date, bench_reason)
        cur.benchmark_drop, cur.episode_type = bd, classify(1 - cur.trough_price / cur.high_price, bd)
        cur.classification_note = why
    open_ep = next((e for e in episodes if not e.recovered and e.segment == len(segs) - 1), None)
    if open_ep and open_ep.peak_date < cur.high_date:
        cur.note = (f"still inside an unrecovered episode that began {open_ep.peak_date} "
                    f"(−{1 - cur.latest_price / open_ep.peak_price:.0%} from that peak); the 52-week high has since "
                    f"rolled down to {cur.high_price:,.2f} on {cur.high_date}")
    last = segs[-1]
    ranges = h.ranges.reindex(last.index) if h.ranges is not None and not h.ranges.empty else None
    return History(ticker=h.ticker, segments=seg_models, episodes=episodes, current=cur, last_segment=last,
                   last_ranges=ranges)


# --------------------------------------------------------------------------
# Recovery statistics and confidence
# --------------------------------------------------------------------------
def _quartiles(months: list[float]) -> tuple[float, float, float]:
    p25, med, p75 = (float(v) for v in np.percentile(months, [25, 50, 75]))
    return p25, med, p75


def _stats(eps: list[Episode], basis: str, typ: str, note: str) -> RecoveryStats:
    p25, med, p75 = _quartiles([e.recovery_months for e in eps])
    st = RecoveryStats(basis=basis, episode_type=typ, episodes_used=len(eps), median_months=med,
                       p25_months=p25, p75_months=p75, note=note)
    alt = config.RECOVERY_CLOCK_SECONDARY
    if alt and alt != config.RECOVERY_CLOCK_START and all(alt in e.months_from for e in eps):
        st.secondary_p25_months, st.secondary_median_months, st.secondary_p75_months = \
            _quartiles([e.months_from[alt] for e in eps])
    return st


def recovery_stats(episodes: list[Episode], current_type: str) -> tuple[RecoveryStats | None, bool, str]:
    """Same-type recovered episodes; fewer than MIN_EPISODES → all types (fallback=True).
    Returns (stats or None when fewer than MIN_EPISODES recovered at all, fallback, note)."""
    recovered = [e for e in episodes if e.recovered and e.recovery_months is not None]
    if current_type in (MARKET_DRIVEN, COMPANY_SPECIFIC):
        same = [e for e in recovered if e.episode_type == current_type]
        if len(same) >= config.MIN_EPISODES:
            return _stats(same, BASIS_SAME_TYPE, current_type, ""), False, ""
        note = (f"only {len(same)} past {current_type} drop{'s' if len(same) != 1 else ''} recovered "
                f"(fewer than MIN_EPISODES = {config.MIN_EPISODES}), so all {len(recovered)} recovered episodes of "
                f"every type are used; confidence lowered one level")
    else:
        note = (f"the current drop is unclassified, so all {len(recovered)} recovered episodes of every type are "
                f"used; confidence lowered one level")
    if len(recovered) < config.MIN_EPISODES:
        return None, True, note
    return _stats(recovered, BASIS_ALL_TYPES, "all types", note), True, note


def confidence_level(n: int, downgrades: int) -> str:
    levels = config.TURNAROUND_CONFIDENCE_LEVELS
    base = 2 if n >= config.TURNAROUND_HIGH_MIN_EPISODES else 1 if n >= config.MIN_EPISODES else 0
    return levels[max(0, min(base, len(levels) - 1) - downgrades)]


def structural_flag(run: AnalysisRun | None) -> tuple[bool, str]:
    da = run.devils_advocate if run else None
    if da is None or not da.ok:
        status = da.status if da is not None else "not run"
        return False, f"structural-vs-cyclical check unavailable (Devil's Advocate: {status})"
    if da.impairment_type == "structural":
        return True, f"Devil's Advocate judges the impairment structural: {da.impairment_reasoning or 'no reason given'}"
    return False, f"Devil's Advocate judges the impairment {da.impairment_type}"


def fmt_range(lo: float, hi: float) -> str:
    a, b = math.floor(lo), math.ceil(hi)
    return f"about {a} months" if a == b else f"{a}–{b} months"


# --------------------------------------------------------------------------
# Peers (Phase 5's peer strip reuses select_peers)
# --------------------------------------------------------------------------
def all_universe_keys() -> list[str]:
    return list(config.UNIVERSE_SOURCES) + list(config.MANUAL_UNIVERSE_LISTS)


def select_peers(ticker: str, industry: str | None, market_cap: float | None,
                 db_path: Path | str = config.RUNS_DB_PATH, universe_keys: list[str] | None = None,
                 universe_dir: Path = config.UNIVERSE_DIR, n: int = config.PEER_COUNT) -> PeerSelection:
    """Same yfinance industry, nearest `n` by market cap (log distance), from the combined
    universe lists. Industry and market cap come from the latest completed screen run,
    the only place the app has them for every universe ticker."""
    keys = universe_keys or all_universe_keys()
    labels = list_labels()
    sel = PeerSelection(ticker=ticker, industry=industry, market_cap=market_cap,
                        universe_lists=[labels.get(k, k) for k in keys])
    if not industry:
        sel.status = f"Unavailable - industry unknown for {ticker}"
        return sel
    if market_cap is None or market_cap <= 0:
        sel.status = f"Unavailable - market cap N/A for {ticker}"
        return sel
    uni = load_universe(keys, universe_dir)
    if uni.empty:
        sel.status = "Unavailable - the universe lists are empty"
        return sel
    run = screen_store.latest_completed_run(db_path)
    if run is None:
        sel.status = NO_SCREEN_RUN
        return sel
    results = {r.ticker: r for r in screen_store.load_results(run.run_id, db_path)}
    sel.screen_run_id, sel.screen_run_date = run.run_id, run.started_at.date()
    cands: list[Peer] = []
    for _, row in uni.iterrows():
        t = row["ticker"]
        if t.upper() == ticker.upper():
            continue
        r = results.get(t)
        if r is None:
            sel.not_screened += 1
            continue
        sel.candidates_considered += 1
        if r.industry == industry and r.market_cap.ok and r.market_cap.value > 0:
            cands.append(Peer(ticker=t, name=r.name or row.get("name", ""), industry=r.industry,
                              market_cap=r.market_cap.value, sources=row.get("source", "")))
    cands.sort(key=lambda p: abs(math.log(p.market_cap / market_cap)))
    sel.peers = cands[:n]
    sel.source_note = (f"Peers come from the universe lists ({', '.join(sel.universe_lists)}), the app's only source "
                       f"of companies: same industry ({industry}), nearest {n} by market cap. Industry and market cap "
                       f"from screen run {run.run_id} ({sel.screen_run_date}); {sel.not_screened} universe tickers "
                       f"not in that run were not considered.")
    if not sel.peers:
        sel.status = f"Unavailable - no universe ticker in industry {industry!r} with a market cap in screen run {run.run_id}"
    return sel


def peer_histories(provider: DataProvider, sel: PeerSelection, benches: Benchmarks) -> list[PeerHistory]:
    out = []
    for p in sel.peers:
        try:
            h = load_history(provider, p.ticker)
        except ProviderError as exc:
            out.append(PeerHistory(ticker=p.ticker, error=f"price history unavailable ({exc})"))
            continue
        country = listing_country(p.ticker)
        bench, why = benches.get(benchmark_for(p.ticker), country)
        out.append(PeerHistory(ticker=p.ticker,
                               episodes=build_history(h, bench, benchmark_for(p.ticker) or "", why).episodes))
    return out


# --------------------------------------------------------------------------
# Near-term signals (deterministic; reported beside the range, never override it)
# --------------------------------------------------------------------------
def macd_crossover(closes: pd.Series) -> TechnicalSignal:
    params = {"fast": config.MACD_FAST, "slow": config.MACD_SLOW, "signal": config.MACD_SIGNAL,
              "lookback_days": config.MACD_CROSSOVER_LOOKBACK_DAYS}
    sig = TechnicalSignal(name="Bullish MACD crossover", params=params)
    need = config.MACD_SLOW + config.MACD_SIGNAL
    if len(closes) < need:
        sig.detail = f"Insufficient data - {len(closes)} closes in the latest segment (needs {need})"
        return sig
    macd = (closes.ewm(span=config.MACD_FAST, adjust=False).mean()
            - closes.ewm(span=config.MACD_SLOW, adjust=False).mean())
    signal = macd.ewm(span=config.MACD_SIGNAL, adjust=False).mean()
    above = macd > signal
    cross = above & ~above.shift(1, fill_value=True)
    recent = cross.iloc[-config.MACD_CROSSOVER_LOOKBACK_DAYS:]
    sig.as_of = closes.index[-1].date()
    if recent.any():
        d = recent[recent].index[-1].date()
        sig.active = True
        sig.detail = f"MACD crossed above its signal line on {d} (MACD {macd.iloc[-1]:+.3f})"
    else:
        sig.detail = f"no crossover in the last {config.MACD_CROSSOVER_LOOKBACK_DAYS} trading days"
    return sig


def williams_r(closes: pd.Series, ranges: pd.DataFrame | None = None) -> pd.Series:
    """Williams %R over WILLIAMS_R_PERIOD days from daily highs and lows (adjusted like the
    closes). Without them, or on a day one is missing, the close stands in."""
    highs = lows = closes
    if ranges is not None:
        highs = ranges["high"].reindex(closes.index).fillna(closes).clip(lower=closes)
        lows = ranges["low"].reindex(closes.index).fillna(closes).clip(upper=closes)
    hh = highs.rolling(config.WILLIAMS_R_PERIOD).max()
    ll = lows.rolling(config.WILLIAMS_R_PERIOD).min()
    rng = (hh - ll).where(hh > ll)
    return -100 * (hh - closes) / rng


def williams_r_signal(closes: pd.Series, ranges: pd.DataFrame | None = None) -> TechnicalSignal:
    basis = "daily highs and lows" if ranges is not None else "close-based"
    params = {"period": config.WILLIAMS_R_PERIOD, "oversold": config.WILLIAMS_R_OVERSOLD,
              "lookback_days": config.WILLIAMS_R_LOOKBACK_DAYS, "basis": basis}
    name = "Williams %R rising out of oversold" + (" (close-based)" if ranges is None else "")
    sig = TechnicalSignal(name=name, params=params)
    need = config.WILLIAMS_R_PERIOD + config.WILLIAMS_R_LOOKBACK_DAYS
    if len(closes) < need:
        sig.detail = f"Insufficient data - {len(closes)} closes in the latest segment (needs {need})"
        return sig
    wr = williams_r(closes, ranges)
    now, before = wr.iloc[-1], wr.iloc[-config.WILLIAMS_R_LOOKBACK_DAYS - 1:-1]
    sig.as_of = closes.index[-1].date()
    if pd.isna(now):
        sig.detail = "Insufficient data - flat prices over the period"
        return sig
    if now > config.WILLIAMS_R_OVERSOLD and (before <= config.WILLIAMS_R_OVERSOLD).any():
        sig.active = True
        sig.detail = (f"%R {now:.0f}, up from ≤ {config.WILLIAMS_R_OVERSOLD:.0f} within the last "
                      f"{config.WILLIAMS_R_LOOKBACK_DAYS} trading days")
    else:
        sig.detail = f"%R {now:.0f}"
    return sig


def _pivot_lows(vals: np.ndarray, k: int) -> list[int]:
    return [i for i in range(k, len(vals) - k) if vals[i] == vals[i - k:i + k + 1].min()]


def double_bottom(closes: pd.Series) -> TechnicalSignal:
    params = {"window_days": config.DOUBLE_BOTTOM_WINDOW_DAYS, "pivot_days": config.DOUBLE_BOTTOM_PIVOT_DAYS,
              "tolerance": config.DOUBLE_BOTTOM_TOLERANCE,
              "min_separation_days": config.DOUBLE_BOTTOM_MIN_SEPARATION_DAYS,
              "min_bounce": config.DOUBLE_BOTTOM_MIN_BOUNCE}
    sig = TechnicalSignal(name="Double bottom forming", params=params)
    w = closes.iloc[-config.DOUBLE_BOTTOM_WINDOW_DAYS:]
    vals = w.to_numpy(dtype=float)
    if len(vals) < 2 * config.DOUBLE_BOTTOM_PIVOT_DAYS + config.DOUBLE_BOTTOM_MIN_SEPARATION_DAYS:
        sig.detail = f"Insufficient data - {len(vals)} closes in the window"
        return sig
    sig.as_of = w.index[-1].date()
    pivots = _pivot_lows(vals, config.DOUBLE_BOTTOM_PIVOT_DAYS)
    last = vals[-1]
    for j in range(len(pivots) - 1, 0, -1):
        b = pivots[j]
        for a in reversed(pivots[:j]):
            if b - a < config.DOUBLE_BOTTOM_MIN_SEPARATION_DAYS:
                continue
            lo_a, lo_b = vals[a], vals[b]
            if abs(lo_b / lo_a - 1) > config.DOUBLE_BOTTOM_TOLERANCE:
                continue
            neck = vals[a:b + 1].max()
            if neck < max(lo_a, lo_b) * (1 + config.DOUBLE_BOTTOM_MIN_BOUNCE):
                continue
            if lo_b < last < neck:
                sig.active = True
                sig.detail = (f"lows {lo_a:,.2f} ({w.index[a].date()}) and {lo_b:,.2f} ({w.index[b].date()}); "
                              f"price {last:,.2f} below the neckline {neck:,.2f}")
                return sig
    sig.detail = "no qualifying pair of lows"
    return sig


def technical_signals(closes: pd.Series, ranges: pd.DataFrame | None = None) -> list[TechnicalSignal]:
    return [macd_crossover(closes), williams_r_signal(closes, ranges), double_bottom(closes)]


def insider_signal(ins: InsiderSummary | None, current: CurrentDrawdown | None, today: date) -> TechnicalSignal:
    """An insider cluster buy inside the current drawdown (from the 52-week high to today)."""
    sig = TechnicalSignal(name="Insider cluster buy during the current drawdown",
                          params={"min_insiders": config.INSIDER_CLUSTER_MIN, "days": config.INSIDER_CLUSTER_DAYS})
    if current is None or not current.qualifying:
        sig.detail = "no qualifying drawdown"
        return sig
    if ins is None or not ins.available:
        sig.detail = f"insider data: {ins.coverage if ins else 'not loaded'}"
        return sig
    since = ins.history_since or ins.since
    loaded = ins.history_buys if ins.history_since else ins.buys
    buys = [t for t in loaded if current.high_date <= t.date <= today]
    window = cluster_buy(buys)
    partial = ""
    if since and since > current.high_date:
        partial = f"; insider data covers {since} onward only, the drawdown began {current.high_date}"
    sig.as_of = today
    if window:
        names = {t.insider for t in buys if window[0] <= t.date <= window[1]}
        sig.active = True
        sig.detail = f"{len(names)} insiders bought between {window[0]} and {window[1]} ({ins.coverage}){partial}"
    else:
        sig.detail = f"no cluster buy since the {current.high_date} high ({ins.coverage}){partial}"
    return sig


# --------------------------------------------------------------------------
# Catalysts, valuation recovery, asset floor
# --------------------------------------------------------------------------
def catalysts(x: "AnalysisInputs", run: AnalysisRun | None) -> tuple[list[Catalyst], str]:
    out: list[Catalyst] = []
    e = x.f.earnings
    if e is not None and e.next is not None and e.next >= x.today:
        out.append(Catalyst(kind="earnings", date=e.next, text=f"Next earnings {e.next}",
                            source=f"yfinance calendar ({e.provider or 'yfinance'})"))
    else:
        out.append(Catalyst(kind="earnings", text="Next earnings date: N/A - not in the yfinance calendar"))
    lead = x.leadership
    if lead is None:
        out.append(Catalyst(kind="leadership", text="Leadership: N/A - not evaluated"))
    else:
        out.append(Catalyst(kind="leadership", text=f"Leadership flag {lead.flag}: {lead.summary}",
                            source="; ".join(lead.layers_used) or lead.coverage_label))
        for ev in sorted(lead.events, key=lambda v: v.date, reverse=True):
            who = f" ({ev.person})" if ev.person else ""
            out.append(Catalyst(kind="leadership", date=ev.date, text=f"{ev.role} departure{who}",
                                source=", ".join(ev.sources) or ev.layer))
    da = run.devils_advocate if run else None
    if da is not None and da.ok:
        for req in da.bull_case_requirements:
            out.append(Catalyst(kind="lens", text=req, source="Devil's Advocate: what the bull case needs"))
    note = ("Debt maturity dates: not shown — no source supplied them (FMP not configured). The Macro lens's "
            "current vs long-term debt split is the proxy." if not config.fmp_enabled() else
            "Debt maturity dates: not shown — the FMP maturity schedule is not implemented yet. The Macro lens's "
            "current vs long-term debt split is the proxy.")
    return out, note


def valuation_recovery_status() -> str:
    return VALUATION_FMP_TODO if config.fmp_enabled() else VALUATION_UNAVAILABLE


def valuation_recovery_months(ratio: pd.Series, cheap_below: bool = True) -> tuple[float | None, list[float]]:
    """Completed spells on the cheap side of the ratio's own VALUATION_RECOVERY_YEARS median
    (below it for P/E and P/B, above it for FCF yield), in months from the spell's start to
    the first day back at the median. Returns (median, spell lengths). Ready for FMP history."""
    s = ratio.dropna().sort_index()
    if s.empty:
        return None, []
    s = s[s.index >= s.index[-1] - pd.DateOffset(years=config.VALUATION_RECOVERY_YEARS)]
    med = float(s.median())
    cheap = s < med if cheap_below else s > med
    spells, start = [], None
    for d, c in cheap.items():
        if c and start is None:
            start = d
        elif not c and start is not None:
            spells.append(_months(start.date(), d.date()))
            start = None
    return med, spells


def asset_floor_line(af: AssetFloor | None) -> str:
    return af.summary if af is not None else "Asset floor: N/A - Data Incomplete"


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------
def assumptions(symbol: str | None) -> dict:
    return {"price": "adjusted closes", "DRAWDOWN_THRESHOLD": config.DRAWDOWN_THRESHOLD,
            "RECOVERY_BAND": config.RECOVERY_BAND, "ROLLING_HIGH_DAYS": config.ROLLING_HIGH_DAYS,
            "MARKET_DRIVEN_RATIO": config.MARKET_DRIVEN_RATIO, "MIN_EPISODES": config.MIN_EPISODES,
            "PEER_COUNT": config.PEER_COUNT, "RECOVERY_CLOCK_START": config.RECOVERY_CLOCK_START,
            "TURNAROUND_STRUCTURAL_ACTION": config.TURNAROUND_STRUCTURAL_ACTION, "benchmark": symbol or "N/A"}


def _pool(histories: list[PeerHistory]) -> list[Episode]:
    return [e for h in histories for e in h.episodes]


def turnaround(x: "AnalysisInputs", run: AnalysisRun | None, provider: DataProvider,
               db_path: Path | str = config.RUNS_DB_PATH, universe_keys: list[str] | None = None,
               universe_dir: Path = config.UNIVERSE_DIR) -> TurnaroundResult:
    country = listing_country(x.ticker)
    symbol = benchmark_for(x.ticker)
    res = TurnaroundResult(ticker=x.ticker, benchmark=symbol or "", listing_country=country,
                           assumptions=assumptions(symbol), asset_floor_line=asset_floor_line(x.screen.asset_floor),
                           valuation_recovery=valuation_recovery_status())
    res.catalysts, res.debt_maturity_note = catalysts(x, run)
    res.structural_flag, res.structural_note = structural_flag(run)
    try:
        h = load_history(provider, x.ticker)
    except ProviderError as exc:
        res.status = insufficient(f"price history unavailable ({exc})")
        res.headline = res.status
        res.rationale = rationale(res)
        return res
    res.notes.extend(h.notes)
    benches = Benchmarks(provider)
    bench, bench_reason = benches.get(symbol, country)
    if bench_reason:
        res.notes.append(f"episodes unclassified: {bench_reason}")
    hist = build_history(h, bench, symbol or "", bench_reason)
    res.segments, res.episodes, res.current, res.breaks = hist.segments, hist.episodes, hist.current, h.breaks
    res.price_as_of = hist.current.as_of
    res.recovered_count = sum(e.recovered for e in hist.episodes)
    res.unrecovered_count = len(hist.episodes) - res.recovered_count
    res.signals = technical_signals(hist.last_segment, hist.last_ranges) + [insider_signal(x.insiders, hist.current, x.today)]

    cur = hist.current
    if not cur.qualifying:
        res.status = STATUS_NOT_IN_DRAWDOWN
        res.headline = f"Not in a qualifying drawdown (currently {cur.label})"
        res.rationale = rationale(res)
        return res

    stats, fallback, note = recovery_stats(hist.episodes, cur.episode_type)
    downgrades = int(fallback)
    if stats is None:
        own = res.recovered_count
        res.peer = select_peers(x.ticker, x.screen.industry, x.market_cap.value if x.market_cap.ok else None,
                                db_path, universe_keys, universe_dir)
        if res.peer.ok:
            res.peer_histories = peer_histories(provider, res.peer, benches)
            stats, fallback, pnote = recovery_stats(_pool(res.peer_histories), cur.episode_type)
            note = f"own history: {own} recovered episode{'s' if own != 1 else ''} (fewer than MIN_EPISODES)"
            if stats is not None:
                stats.basis = BASIS_PEERS
                note += f"; {PEER_LABEL}" + (f"; peers: {pnote}" if fallback else "")
                downgrades = 1 + int(fallback)
        if stats is None:
            pooled = sum(e.recovered for e in _pool(res.peer_histories))
            res.status = insufficient(
                f"fewer than MIN_EPISODES ({config.MIN_EPISODES}) recovered drawdown episodes "
                f"(own: {own}; peers: {pooled if res.peer.ok else res.peer.status})")
            res.headline = res.status
            res.basis_note = note
            res.rationale = rationale(res)
            return res

    res.basis, res.basis_note, res.episodes_used = stats.basis, note, stats.episodes_used
    res.median_months, res.iqr_months = stats.median_months, (stats.p25_months, stats.p75_months)
    if stats.secondary_median_months is not None:
        res.secondary_clock = config.RECOVERY_CLOCK_SECONDARY
        res.secondary_median_months = stats.secondary_median_months
        res.secondary_iqr_months = (stats.secondary_p25_months, stats.secondary_p75_months)
        res.secondary_line = (f"{clock_label(res.secondary_clock).capitalize()}: "
                              f"{fmt_range(*res.secondary_iqr_months)} (median {res.secondary_median_months:.0f})")
    res.confidence = confidence_level(stats.episodes_used, downgrades)
    res.confidence_reasons.append(f"{stats.episodes_used} recovered episodes behind the range")
    if fallback:
        res.confidence_reasons.append("fewer than MIN_EPISODES of the current type: all types used, one level lower")
    if stats.basis == BASIS_PEERS:
        res.confidence_reasons.append(f"{PEER_LABEL}: one level lower")
    kind = f"past {stats.episode_type} drops" if stats.episode_type != "all types" else "past drops of all types"
    where = f" at {len([p for p in res.peer_histories if p.episodes])} peers" if stats.basis == BASIS_PEERS else ""
    res.headline = (f"{fmt_range(stats.p25_months, stats.p75_months)} {clock_label(res.clock)} "
                    f"(median {stats.median_months:.0f}), "
                    f"based on {stats.episodes_used} {kind}{where}")
    if stats.basis == BASIS_PEERS:
        res.headline += f" — {PEER_LABEL}"

    if res.structural_flag:  # applied last: nothing overrides it
        if config.TURNAROUND_STRUCTURAL_ACTION == "withhold":
            res.status = STATUS_WITHHELD
            res.headline = ("Withheld — the Devil's Advocate flags a structural impairment; the drop may not be "
                            "mean-reverting, so past recoveries aren't applied")
            res.median_months = res.iqr_months = res.confidence = None
            res.secondary_median_months = res.secondary_iqr_months = None
            res.secondary_line = ""
            res.confidence_reasons.append("structural impairment flag: range withheld")
        else:
            res.confidence = config.TURNAROUND_CONFIDENCE_LEVELS[0]
            res.headline = f"{STRUCTURAL_LABEL}: {res.headline}"
            res.confidence_reasons.append("structural impairment flag: forced to Low")
    res.rationale = rationale(res)
    return res


def _pct(v: float | None) -> str:
    return "N/A" if v is None else f"{-v:+.0%}"


def rationale(r: TurnaroundResult) -> str:
    lines = [f"**Turnaround outlook — {r.headline or r.status}**"]
    if r.confidence:
        if r.secondary_line:
            lines.append(r.secondary_line + " (same episodes, a different start for the clock)")
        lines.append(f"Confidence: {r.confidence} ({'; '.join(r.confidence_reasons)}). Rule: {r.confidence_rule}")
    if r.basis_note:
        lines.append(f"Basis: {r.basis or 'none'} — {r.basis_note}")
    if r.current:
        c = r.current
        cur = f"Current drop: {c.label} (high {c.high_price:,.2f} on {c.high_date}, adjusted closes)"
        if c.qualifying:
            cur += f"; type {c.episode_type} ({r.benchmark} {_pct(c.benchmark_drop)} over the same window)"
            if c.classification_note:
                cur += f" — {c.classification_note}"
        lines.append(cur + (f". {c.note}" if c.note else ""))
    lines.append(f"Structural check: {r.structural_note}")
    lines.append(f"Episodes: {len(r.episodes)} in total, {r.recovered_count} recovered, {r.unrecovered_count} "
                 f"unrecovered (counted, not dropped). Recovery time runs from the {config.RECOVERY_CLOCK_START} to "
                 f"the first close within {config.RECOVERY_BAND:.0%} of the prior high.")
    if r.breaks:
        lines.append("Corporate-action breaks (no high, drawdown or recovery computed across them): "
                     + ", ".join(f"{b.date} ({b.type})" for b in r.breaks))
    if r.peer is not None:
        names = ", ".join(p.ticker for p in r.peer.peers) or "none"
        lines.append(f"Peers: {names} — {r.peer.source_note or r.peer.status}")
    lines.append(f"Valuation-based recovery: {r.valuation_recovery}")
    lines.append(r.asset_floor_line + " (context only; never changes the range or confidence)")
    active = [s for s in r.signals if s.active]
    lines.append("Near-term signals: " + ("; ".join(f"{s.name}: {s.detail}" for s in active) if active else "none active"))
    lines.append("Catalysts: " + "; ".join(c.text + (f" [{c.source}]" if c.source else "") for c in r.catalysts))
    if r.debt_maturity_note:
        lines.append(r.debt_maturity_note)
    lines.extend(f"Note: {n}" for n in r.notes)
    lines.append(f"_{r.survivorship_caveat}_")
    return "\n\n".join(lines)
