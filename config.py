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
# Fair value per share = (PV of stage-1 FCF + PV of terminal value + cash − debt) / shares.
# False → the cash − debt bridge is left out (levered FCF treated as fully net of debt).
DCF_ADD_NET_CASH = True
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
STAGE1_SLACK = 0.25  # loosens the hurdle each stage-1 metric is compared against
STAGE_DIVERGENCE = 0.30
SCATTER_LABEL_TOP_N = 10
GRAHAM_MULTIPLIER = 22.5  # Graham Number = sqrt(22.5 × EPS × BVPS) (15× earnings × 1.5× book)
MONTHS_PER_YEAR = 12  # unit constant: monthly burn = annual burn / 12
QUARTERS_PER_YEAR = 4  # unit constant: quarterly burn = TTM burn / 4

# Sector-adjusted screen slots (Financial Services / Real Estate, Rule 5).
# Slot "fcf" → ROE spread (banks, insurers, other) or FFO yield vs risk-free (REITs);
# slot "leverage" → P/TBV (banks), P/B (insurers, other), not applicable (REITs).
MIN_ROE_SPREAD = 0.0  # ROE − COST_OF_CAPITAL must be at least this
MAX_P_TBV_BANK = 1.5
MAX_P_B = 1.5

# NOPAT for ROIC: effective tax rate (tax provision / pretax income) clamped to
# TAX_RATE_BOUNDS; when it is n/m (pretax ≤ 0 or missing) the fallback is used and labelled.
STATUTORY_TAX_RATE_FALLBACK = 0.21
TAX_RATE_BOUNDS = (0.0, 0.5)

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

# Quant lens confidence (SPEC "Score mapping"): "low" when peak earnings are
# normalised or the Graham Number and the DCF disagree on direction.
QUANT_CONFIDENCE_NORMAL = "normal"
QUANT_CONFIDENCE_LOW = "low"
# Financials (banks, insurers, other): excess-return shortcut, fair P/B = ROE / COST_OF_CAPITAL
# (zero growth); the ROE spread replaces the ROIC bonus/penalty. REITs: the DCF on an FFO base.

# --------------------------------------------------------------------------
# Score mapping — Macro lens
# --------------------------------------------------------------------------
NET_DEBT_EBITDA_BREAKPOINTS = [(0.0, 10), (1.0, 8), (2.0, 6), (3.0, 4), (5.0, 1)]
INTEREST_COVERAGE_BREAKPOINTS = [(1.5, 1), (3.0, 4), (5.0, 7), (8.0, 10)]
NO_INTEREST_EXPENSE_SCORE = 10.0  # only when EBIT > 0 (Rule 2b)
EBIT_NONPOSITIVE_COVERAGE_SCORE = 1.0
ALTMAN_BREAKPOINTS = [(0.0, 1), (1.1, 3), (2.6, 7), (4.0, 10)]
LEVERAGE_TREND_SCORES = {"falling": 8, "flat": 5, "rising": 2}
LEVERAGE_TREND_FLAT_BAND = 0.3  # ±0.3x counts as flat (net debt / EBITDA)
# Financials and REITs: the trend is on liabilities / equity, which runs ~10x for banks,
# so ±0.3x would call a 3% move a trend; ±1.0x is flat instead.
LEVERAGE_TREND_FLAT_BAND_FINANCIALS = 1.0

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
# Per-million-token prices for ANTHROPIC_MODEL (claude-sonnet-5: $2 in / $10 out,
# checked against Anthropic pricing 2026-09). Update when the model changes.
LLM_PRICE_PER_MTOK_IN = 2.0
LLM_PRICE_PER_MTOK_OUT = 10.0
TOKENS_PER_MTOK = 1_000_000  # unit constant
# Lowest temperature, sent only to models that accept sampling parameters.
# Models matching these prefixes reject `temperature` with a 400, so it is omitted;
# repeatability then rests on the response cache (same payload → same cached answer).
LLM_TEMPERATURE = 0.0
LLM_NO_SAMPLING_MODEL_PREFIXES = ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-7", "claude-opus-4-8",
                                  "claude-fable", "claude-mythos")
LLM_EFFORT = "medium"  # output_config.effort for the scoring calls; None omits it (models without effort)
LLM_MAX_TOKENS = 16000
LLM_VALIDATION_RETRIES = 1  # one retry after a schema / evidence failure, then "Insufficient data"
LLM_TIMEOUT_SECONDS = 300
LLM_CACHE_DB_PATH = DATA_DIR / "cache" / "llm_cache.db"

# LLM backend (CLAUDE.md Rule 4). "auto": the Anthropic API when ANTHROPIC_API_KEY is set,
# otherwise the Claude Code CLI (`claude -p`, the user's Claude subscription) when it can be
# found. "api" / "claude_code" force one; "none" disables the LLM lenses.
LLM_BACKEND = os.getenv("LLM_BACKEND", "auto").strip().lower() or "auto"
LLM_BACKENDS = ("auto", "api", "claude_code", "none")
# The CLI: CLAUDE_CODE_CLI in .env wins; else `claude` on PATH; else these globs (newest match),
# which cover the binary bundled with the VS Code extension (including Flatpak VS Code).
CLAUDE_CODE_CLI = os.getenv("CLAUDE_CODE_CLI", "")
CLAUDE_CODE_CLI_GLOBS = [
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude",
    "~/.var/app/com.visualstudio.code/data/vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude",
]
CLAUDE_CODE_TIMEOUT_SECONDS = 600  # one structured call, including the CLI's own start-up

# Moat lens: the prompt first names the single most relevant threat for the business.
# Hints are matched on the yfinance industry by prefix first, then on the sector.
INDUSTRY_THREAT_HINTS = {
    "Software": "AI disruption (AI-native competitors or AI features commoditising the product)",
    "Information Technology Services": "AI disruption (automation of billable services work)",
    "Internet Content": "AI disruption of search, content and advertising",
    "Apparel Retail": "brand erosion and private-label or direct-to-consumer competition",
    "Apparel Manufacturing": "brand erosion and private-label competition",
    "Internet Retail": "price competition and platform disintermediation",
    "Department Stores": "private-label erosion and the shift to online retail",
    "Discount Stores": "private-label and price competition",
    "Grocery Stores": "private-label erosion and price competition",
    "Specialty Retail": "brand erosion and private-label or online competition",
    "Packaged Foods": "private-label erosion and GLP-1 driven changes in demand",
    "Beverages": "private-label erosion and changing consumer health preferences",
    "Banks": "regulation and interest-rate compression of net interest margins",
    "Insurance": "catastrophe losses, pricing cycles and regulation",
    "Asset Management": "fee compression and the shift to passive products",
    "Auto Manufacturers": "EV transition costs and price competition",
    "Airlines": "fuel costs, capacity cycles and price competition",
    "Rental & Leasing Services": "fleet cost cycles and residual-value risk",
    "Railroads": "volume cyclicality and regulation of pricing",
    "Gold": "commodity price cycles and reserve depletion",
    "REIT": "interest rates, refinancing costs and tenant demand",
}
SECTOR_THREAT_HINTS = {
    "Technology": "AI disruption",
    "Communication Services": "AI disruption and shifting advertising and content consumption",
    "Consumer Cyclical": "brand erosion, private-label or online competition, and demand cyclicality",
    "Consumer Defensive": "private-label erosion and price competition",
    "Financial Services": "regulation and interest rates",
    "Real Estate": "interest rates and tenant demand",
    "Healthcare": "drug-pricing regulation, patent expiries and reimbursement pressure",
    "Industrials": "demand cyclicality and input-cost pressure",
    "Energy": "commodity price cycles and the energy transition",
    "Basic Materials": "commodity price cycles",
    "Utilities": "rate regulation and interest rates",
}

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
# 6-K pre-filter: a filing goes to the LLM check only when a departure word
# occurs within LEADERSHIP_KEYWORD_WINDOW_WORDS words of a CEO/CFO title, or a
# "near_role" word sits right next to one ("interim CEO"). Entries are regexes,
# matched case-insensitively except all-caps role acronyms. The narrow patterns
# keep quarterly-report 6-Ks ("interim financial statements", "retirement
# benefits", "succession planning") out of the LLM step.
LEADERSHIP_KEYWORDS = {
    "role": [r"Chief Executive", r"Chief Financial", r"CEO", r"CFO"],
    "departure": [r"resign\w*", r"retire(?!ment)\w*", r"step(?:s|ping|ped)? down", r"succession(?! plan)",
                  r"succeed\w*", r"departure", r"leav(?:e|es|ing) the company"],
    "near_role": [r"interim"],
}
LEADERSHIP_KEYWORD_WINDOW_WORDS = 12
LEADERSHIP_NEAR_ROLE_WORDS = 2

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
SCREEN_PROGRESS_POLL_SECONDS = 5  # the Screener page re-reads run progress this often
SCREEN_LOG_DIR = ROOT / "storage" / "logs"

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
# Altman Z'' (non-manufacturer / emerging-market model, Altman 1995):
# X1 working capital/TA, X2 retained earnings/TA, X3 EBIT/TA, X4 book equity/total liabilities.
ALTMAN_COEFFICIENTS = {"X1": 6.56, "X2": 3.26, "X3": 6.72, "X4": 1.05}
BENEISH_THRESHOLD = -1.78
REVERSE_DCF_SEARCH_RANGE = (-0.30, 0.50)
REVERSE_DCF_TOLERANCE = 1e-6  # bisection stops when the growth bracket is this narrow
REVERSE_DCF_MAX_ITERATIONS = 200
SENSITIVITY_RATE_STEPS = [-0.02, -0.01, 0.0, 0.01, 0.02]
SENSITIVITY_GROWTH_STEPS = [-0.05, -0.025, 0.0, 0.025, 0.05]
USE_EARNINGS_YIELD_IN_SCREEN = False
MIN_EARNINGS_YIELD = 0.08
MIN_METRICS_FOR_PASS_WITH_EARNINGS_YIELD = 4  # "4 of 5" when the earnings yield is a screen metric
# Beneish (1999), "The Detection of Earnings Manipulation", Financial Analysts
# Journal 55(5), 8-variable model coefficients.
BENEISH_COEFFICIENTS = {
    "intercept": -4.84, "DSRI": 0.920, "GMI": 0.528, "AQI": 0.404, "SGI": 0.892,
    "DEPI": 0.115, "SGAI": -0.172, "TATA": 4.679, "LVGI": -0.327,
}
# Asset floor (information only; never changes a score or the screen status).
NNWC_RECEIVABLES_WEIGHT = 0.75
NNWC_INVENTORY_WEIGHT = 0.5
# Asset coverage = TBV / market cap; (lower bound inclusive, band), checked from the top.
ASSET_COVERAGE_BANDS = [(1.0, "fully covered"), (0.5, "partly covered"), (0.2, "thin"),
                        (float("-inf"), "negligible")]
TRAP_RISK_FAILS_SCREEN = False
PEAK_MARGIN_RATIO = 1.5
INSIDER_LOOKBACK_MONTHS = 6
INSIDER_CLUSTER_MIN = 3
INSIDER_CLUSTER_DAYS = 90
DIVIDEND_FCF_PAYOUT_MAX = 1.0
DIVIDEND_CUT_THRESHOLD = 0.10
# Financials and REITs: FCF is not meaningful (Rule 5), so dividend safety uses the earnings
# payout instead — above this (or net income ≤ 0 while paying) → "dividend at risk".
DIVIDEND_EARNINGS_PAYOUT_MAX_FINANCIALS = 1.0
# Estimate revisions (context only): EPS consensus now vs 90 days ago; within ±1% → "flat".
ESTIMATE_REVISION_FLAT_BAND = 0.01
ESTIMATE_REVISION_PERIODS = ["0y", "+1y", "0q"]  # eps_trend rows tried in order

# --------------------------------------------------------------------------
# Portfolio (Phase 6)
# --------------------------------------------------------------------------
ALERT_PIOTROSKI_DROP = 2
THESIS_TRIGGER_FIELDS: list[str] = []  # filled in Phase 6

# --------------------------------------------------------------------------
# Screener universe (issuers change these URLs; keep them here, not in code)
# --------------------------------------------------------------------------
# Each list tries its sources in order; a manually downloaded file in
# data/universe/raw/<list>.csv|xlsx is used when all fail or when it is newer.
# - S&P 400/600: SSGA SPDR daily holdings (iShares US CSVs now return an HTML gate).
# - COWZ / CALF: paceretfs.com blocks automated requests (Cloudflare), so the
#   holdings come from the funds' SEC N-PORT-P filings, which are public about
#   60 days after the period end; as_of records that period. Small-cap Cash
#   Cows ticker confirmed as CALF (2026-09).
_SSGA = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{fund}.xlsx"
UNIVERSE_SOURCES = {
    "cowz": {"label": "COWZ", "exchange": "US", "sources": [
        {"format": "nport", "fund": "COWZ", "name": "SEC N-PORT (COWZ)"},
    ]},
    "cash_cows_small": {"label": "CALF", "exchange": "US", "sources": [
        {"format": "nport", "fund": "CALF", "name": "SEC N-PORT (CALF)"},
    ]},
    "sp400": {"label": "S&P 400", "exchange": "US", "sources": [
        {"format": "holdings", "name": "SSGA SPMD", "url": _SSGA.format(fund="spmd")},
        {"format": "holdings", "name": "iShares IJH",
         "url": "https://www.ishares.com/us/products/239763/ishares-core-sp-midcap-etf/1467271812596.ajax?fileType=csv&fileName=IJH_holdings&dataType=fund"},
    ]},
    "sp600": {"label": "S&P 600", "exchange": "US", "sources": [
        {"format": "holdings", "name": "SSGA SPSM", "url": _SSGA.format(fund="spsm")},
        {"format": "holdings", "name": "iShares IJR",
         "url": "https://www.ishares.com/us/products/239774/ishares-core-sp-smallcap-etf/1467271812596.ajax?fileType=csv&fileName=IJR_holdings&dataType=fund"},
    ]},
    "tsx_composite": {"label": "TSX Composite", "exchange": "TSX", "sources": [
        {"format": "holdings", "name": "iShares XIC",
         "url": "https://www.blackrock.com/ca/investors/en/products/239837/ishares-sptsx-capped-composite-index-etf/1464253357814.ajax?fileType=csv&fileName=XIC_holdings&dataType=fund"},
    ]},
}
MANUAL_UNIVERSE_LISTS = {"watchlist": "Watchlist", "dataroma": "Dataroma"}
