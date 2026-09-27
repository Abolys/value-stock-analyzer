"""Every threshold and default for the app (CLAUDE.md Rule 1).

Defaults come from the "Thresholds and defaults" table and the Score mapping /
Scoring conventions sections of docs/SPEC.md. No numeric threshold may appear
anywhere else in the code base; add new ones here AND to the SPEC table.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# --------------------------------------------------------------------------
# Environment (never hard-code keys or the model name)
# --------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "")
FMP_API_KEY = os.getenv("FMP_API_KEY", "")


def fmp_enabled() -> bool:
    """FMP is used only when a key is configured (read at call time so tests can toggle it)."""
    return bool(os.getenv("FMP_API_KEY", "").strip())


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
DATA_DIR = ROOT / "data"
CACHE_DB_PATH = DATA_DIR / "cache" / "cache.db"
RUNS_DB_PATH = ROOT / "storage" / "runs.db"
UNIVERSE_DIR = DATA_DIR / "universe"
UNIVERSE_RAW_DIR = UNIVERSE_DIR / "raw"
FIXTURES_DIR = ROOT / "tests" / "fixtures"
CORPORATE_ACTIONS_CSV = DATA_DIR / "corporate_actions.csv"
LEADERSHIP_EVENTS_CSV = DATA_DIR / "leadership_events.csv"
INSIDER_EVENTS_CSV = DATA_DIR / "insider_events.csv"
SEC_CIK_OVERRIDES_CSV = DATA_DIR / "sec_cik_overrides.csv"

# --------------------------------------------------------------------------
# Valuation / DCF
# --------------------------------------------------------------------------
COST_OF_CAPITAL = 0.09  # single discount rate: DCF AND the ROIC comparison
TERMINAL_GROWTH = 0.025
STAGE1_GROWTH_CAP = 0.15
STAGE1_GROWTH_FLOOR = -0.05
DCF_STAGE1_YEARS = 5
DCF_BASE_YEARS = 3
# FCF_NEGATIVE_RULE: negative FCF in a majority of the available fiscal years
# (up to the last FCF_NEGATIVE_MAX_YEARS), with at least FCF_NEGATIVE_MIN_YEARS.
FCF_NEGATIVE_MAX_YEARS = 4
FCF_NEGATIVE_MIN_YEARS = 2

# --------------------------------------------------------------------------
# Screen thresholds
# --------------------------------------------------------------------------
MIN_MARGIN_OF_SAFETY = 0.20
MIN_FCF_SPREAD_OVER_10Y = 0.0
MIN_METRICS_FOR_PASS = 3
MAX_NET_DEBT_EBITDA = 3.0
MAX_SHARE_GROWTH_PER_YEAR = 0.0
DILUTION_FLAG_PER_YEAR = 0.03
MIN_CASH_RUNWAY_MONTHS = 24
STAGE1_SLACK = 0.25
STAGE_DIVERGENCE = 0.30
SCATTER_LABEL_TOP_N = 10

# Risk-free 10-year yield per trading currency. "scale" converts the quoted
# number to a decimal: ^TNX is quoted in percent (5.18 == 5.18%, verified
# 2026-09 against yfinance 1.7.0); the BoC series is also in percent.
RISK_FREE_SOURCES = {
    "USD": {"kind": "yfinance", "symbol": "^TNX", "scale": 0.01,
            "label": "US 10-year Treasury (^TNX)"},
    "CAD": {"kind": "boc_valet", "series": "BD.CDN.10YR.DQ.YLD", "scale": 0.01,
            "label": "Government of Canada 10-year benchmark (BoC Valet BD.CDN.10YR.DQ.YLD)"},
}
# Sanity range for a quoted yield, in the source's own units (percent).
RISK_FREE_QUOTE_RANGE = (0.0, 20.0)
BOC_VALET_URL = "https://www.bankofcanada.ca/valet/observations/{series}/json?recent=5"

# --------------------------------------------------------------------------
# Aggregate / scoring conventions
# --------------------------------------------------------------------------
LENS_WEIGHTS = {"quant": 0.25, "macro": 0.25, "moat": 0.25, "devils_advocate": 0.25}
CONTROVERSY_GAP = 3.0
# Verdict label bands: (lower bound inclusive, label), checked from the top.
VERDICT_BANDS = [(7.5, "bullish"), (6.0, "lean bullish"), (4.0, "neutral"), (float("-inf"), "bearish")]
SCORE_MIN = 1.0
SCORE_MAX = 10.0

# Quality score (screener scatter y-axis); nothing divided by price.
QUALITY_ROIC_SPREAD_BREAKPOINTS = [(-0.05, 2), (0.0, 5), (0.05, 7), (0.15, 10)]
QUALITY_FCF_MARGIN_BREAKPOINTS = [(0.0, 1), (0.05, 5), (0.10, 7), (0.20, 10)]
QUALITY_SHARE_TREND_BREAKPOINTS = [(-0.03, 10), (0.0, 6), (0.02, 4), (0.05, 1)]

# --------------------------------------------------------------------------
# Score mapping — Quant lens
# --------------------------------------------------------------------------
QUANT_UPSIDE_BREAKPOINTS = [(-0.30, 2), (0.0, 5), (0.30, 7.5), (0.60, 10)]
ROIC_SPREAD_BONUS = 1.0
ROIC_SPREAD_PENALTY = -1.0
ROIC_SPREAD_BONUS_THRESHOLD = 0.05
PIOTROSKI_ADJUSTMENT = {"strong": 0.5, "weak": -1.0}
# Cash runway (months) → score for FCF-negative names:
# < 12 → 1, 12–24 → 3, 24–36 → 4, > 36 → 5 (step function, lower bound inclusive).
RUNWAY_BREAKPOINTS = [(0, 1), (12, 3), (24, 4), (36, 5)]
RUNWAY_SCORE_CAP = 5.0

# --------------------------------------------------------------------------
# Score mapping — Macro lens
# --------------------------------------------------------------------------
NET_DEBT_EBITDA_BREAKPOINTS = [(0.0, 10), (1.0, 8), (2.0, 6), (3.0, 4), (5.0, 1)]
INTEREST_COVERAGE_BREAKPOINTS = [(1.5, 1), (3.0, 4), (5.0, 7), (8.0, 10)]
NO_INTEREST_EXPENSE_SCORE = 10.0  # only when EBIT > 0 (Rule 2b)
EBIT_NONPOSITIVE_COVERAGE_SCORE = 1.0
ALTMAN_BREAKPOINTS = [(0.0, 1), (1.1, 3), (2.6, 7), (4.0, 10)]
LEVERAGE_TREND_SCORES = {"falling": 8, "flat": 5, "rising": 2}
LEVERAGE_TREND_FLAT_BAND = 0.3  # ±0.3x counts as flat

CYCLICALITY_SCORES = {"defensive": 8, "mixed": 5, "cyclical": 3}
CYCLICALITY_DEFAULT_SCORE = 5
# Keyed on yfinance sector names (verified against real info data 2026-09).
SECTOR_CYCLICALITY = {
    "Consumer Defensive": 8,
    "Utilities": 8,
    "Healthcare": 8,
    "Technology": 5,
    "Communication Services": 5,
    "Industrials": 5,
    "Consumer Cyclical": 5,
    "Financial Services": 5,
    "Real Estate": 5,
    "Energy": 3,
    "Basic Materials": 3,
}
INDUSTRY_CYCLICALITY_OVERRIDES = {
    "Airlines": 3,
    "Auto Manufacturers": 3,
    "Auto Parts": 3,
    "Travel Services": 3,
    "Lodging": 3,
    "Resorts & Casinos": 3,
    "Steel": 3,
    "Oil & Gas E&P": 3,
}

# --------------------------------------------------------------------------
# LLM lenses (rubrics are versioned with their prompts in Phase 3)
# --------------------------------------------------------------------------
MOAT_RUBRIC = {
    2: "No pricing power; commoditised product or service; the main sector threat is already eroding margins or share.",
    5: "A real advantage (brand, switching costs, scale, network, cost position) that is narrowing or only partly defends against the main threat.",
    8: "A durable advantage that clearly holds up against the main threat, visible in stable or rising margins and returns.",
}
DA_RUBRIC = {
    2: "The attack finds a likely permanent impairment or a balance-sheet risk the bull case can't answer.",
    5: "Real risks, but the bull case holds if one named assumption holds.",
    8: "The attack finds only minor risks, or risks the current price already reflects.",
}
LLM_MIN_EVIDENCE_FACTS = 2
CALIBRATION_MAX_SHIFT = 2.0
# Per-million-token prices; check against current Anthropic pricing in Phase 3.
LLM_PRICE_PER_MTOK_IN = 3.0
LLM_PRICE_PER_MTOK_OUT = 15.0

# --------------------------------------------------------------------------
# Turnaround
# --------------------------------------------------------------------------
DRAWDOWN_THRESHOLD = 0.25
RECOVERY_BAND = 0.10
MIN_EPISODES = 3
BENCHMARKS = {"US": "SPY", "CA": "^GSPTSE"}
MARKET_DRIVEN_RATIO = 0.5
PEER_COUNT = 5
ROLLING_HIGH_DAYS = 252  # trading days in a 52-week window

# --------------------------------------------------------------------------
# Leadership turnover
# --------------------------------------------------------------------------
LEADERSHIP_LOOKBACK_MONTHS = 24
LEADERSHIP_HIGH_COUNT = 2
# 6-K pre-filter: a filing goes to the LLM check only when its text or exhibit
# descriptions contain at least one role keyword AND one action keyword
# (case-insensitive, word-boundary match).
LEADERSHIP_KEYWORDS = {
    "role": ["Chief Executive", "Chief Financial", "CEO", "CFO"],
    "action": ["resign", "retire", "step down", "steps down", "stepping down",
               "succession", "appoint", "interim"],
}
# 8-K Item 5.02 sentence-level matching: a departure needs a role term AND a
# departure term in the same sentence (the Item caption itself is stripped).
LEADERSHIP_ROLE_TERMS = {
    "CEO": ["chief executive officer", "ceo"],
    "CFO": ["chief financial officer", "cfo"],
}
DEPARTURE_TERMS = [
    "resign", "retire", "step down", "steps down", "stepping down", "stepped down",
    "will depart", "departure of", "terminated", "termination of", "will leave",
    "separation from", "no longer serve", "cease to serve", "ceased to serve",
]
# Officer titles that identify the CEO/CFO in yfinance companyOfficers.
OFFICER_ROLE_TITLE_TERMS = {
    "CEO": ["chief executive officer", "ceo"],
    "CFO": ["chief financial officer", "cfo"],
}

# --------------------------------------------------------------------------
# Corporate actions and share counts
# --------------------------------------------------------------------------
CORP_ACTION_PRICE_GAP = 0.70
CORP_ACTION_SHARE_CHANGE = 0.50
CORP_ACTION_SHARE_WINDOW_DAYS = 120  # share change must fall this close to the price gap
SPLIT_MATCH_TOLERANCE = 0.15  # a share jump within ±15% of the split ratio "is" the split

# --------------------------------------------------------------------------
# Caching and fetching
# --------------------------------------------------------------------------
CACHE_TTL_PRICES_DAYS = 1
EARNINGS_REFETCH_GRACE_DAYS = 3
CACHE_TTL_FUNDAMENTALS_MAX_DAYS = 100
OFFICER_REFRESH_DAYS = 30
YF_MIN_SECONDS_BETWEEN_CALLS = 1.0
FETCH_MAX_RETRIES = 3
FETCH_BACKOFF_SECONDS = 30  # doubles on each retry
BATCH_PRICE_CHUNK_SIZE = 100
PRICE_HISTORY_PERIOD = "10y"
EDGAR_MAX_REQUESTS_PER_SECOND = 10
HEALTH_CHECK_TTL_MINUTES = 60

# --------------------------------------------------------------------------
# Periods and staleness (Rule 3b)
# --------------------------------------------------------------------------
STALE_FUNDAMENTALS_DAYS = 120
MIXED_PERIOD_DAYS = 100
TTM_QUARTERS = 4
# Consecutive quarters are this many days apart (min, max) when building TTM.
QUARTER_GAP_DAYS = (80, 100)

# --------------------------------------------------------------------------
# Data-source resilience
# --------------------------------------------------------------------------
CANARY_TICKERS = ["SPY", "MSFT", "CNR.TO"]
GOLDEN_TICKERS = ["MELI", "HTZ", "LCID", "LULU", "JPM"]
CIRCUIT_BREAKER_WINDOW = 50
CIRCUIT_BREAKER_FAIL_RATE = 0.5
FIELD_NA_SPIKE = 0.8

# --------------------------------------------------------------------------
# Sector handling (Rule 5)
# --------------------------------------------------------------------------
SECTOR_ADJUSTED_SECTORS = ["Financial Services", "Real Estate"]
# Matched by prefix on the yfinance industry string (verified 2026-09:
# JPM "Banks - Diversified", O "REIT - Retail").
SUBSECTOR_RULES = [
    ("Banks", "bank"),
    ("REIT", "reit"),
    ("Insurance", "insurer"),
]
SUBSECTOR_DEFAULT = "other_financial"

# --------------------------------------------------------------------------
# Value-trap, valuation and ownership signals
# --------------------------------------------------------------------------
PIOTROSKI_MIN_CHECKS = 7
PIOTROSKI_STRONG = 7
PIOTROSKI_WEAK = 3
ALTMAN_ZONES = {"distress_below": 1.10, "safe_above": 2.60}
BENEISH_THRESHOLD = -1.78
REVERSE_DCF_SEARCH_RANGE = (-0.30, 0.50)
SENSITIVITY_RATE_STEPS = [-0.02, -0.01, 0.0, 0.01, 0.02]
SENSITIVITY_GROWTH_STEPS = [-0.05, -0.025, 0.0, 0.025, 0.05]
USE_EARNINGS_YIELD_IN_SCREEN = False
MIN_EARNINGS_YIELD = 0.08
TRAP_RISK_FAILS_SCREEN = False
PEAK_MARGIN_RATIO = 1.5
INSIDER_LOOKBACK_MONTHS = 6
INSIDER_CLUSTER_MIN = 3
INSIDER_CLUSTER_DAYS = 90
DIVIDEND_FCF_PAYOUT_MAX = 1.0
DIVIDEND_CUT_THRESHOLD = 0.10

# --------------------------------------------------------------------------
# Portfolio (Phase 6)
# --------------------------------------------------------------------------
ALERT_PIOTROSKI_DROP = 2
THESIS_TRIGGER_FIELDS: list[str] = []  # filled in Phase 6

# --------------------------------------------------------------------------
# Screener universe (issuers change these URLs; keep them here, not in code)
# --------------------------------------------------------------------------
# Small-cap Cash Cows ticker confirmed as CALF on paceretfs.com (2026-09).
# paceretfs.com sits behind a Cloudflare bot check, so these downloads often
# fail; drop a manually downloaded file into data/universe/raw/<list>.csv|xlsx.
UNIVERSE_SOURCES = {
    "cowz": {
        "label": "COWZ", "format": "pacer",
        "url": "https://www.paceretfs.com/products/COWZ/holdings.csv",
        "exchange": "US",
    },
    "cash_cows_small": {
        "label": "CALF", "format": "pacer",
        "url": "https://www.paceretfs.com/products/CALF/holdings.csv",
        "exchange": "US",
    },
    "sp400": {
        "label": "S&P 400", "format": "ishares",
        "url": "https://www.ishares.com/us/products/239763/ishares-core-sp-midcap-etf/1467271812596.ajax?fileType=csv&fileName=IJH_holdings&dataType=fund",
        "exchange": "US",
    },
    "sp600": {
        "label": "S&P 600", "format": "ishares",
        "url": "https://www.ishares.com/us/products/239774/ishares-core-sp-smallcap-etf/1467271812596.ajax?fileType=csv&fileName=IJR_holdings&dataType=fund",
        "exchange": "US",
    },
    "tsx_composite": {
        "label": "TSX Composite", "format": "ishares",
        "url": "https://www.blackrock.com/ca/investors/en/products/239837/ishares-sptsx-capped-composite-index-etf/1464253357814.ajax?fileType=csv&fileName=XIC_holdings&dataType=fund",
        "exchange": "TSX",
    },
}
MANUAL_UNIVERSE_LISTS = {"watchlist": "Watchlist", "dataroma": "Dataroma"}
