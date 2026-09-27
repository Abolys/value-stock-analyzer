"""config.py holds every threshold; .gitignore keeps secrets and local data out of git."""

import re
import subprocess
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parents[1]

SPEC_TABLE_CONSTANTS = [
    "COST_OF_CAPITAL", "TERMINAL_GROWTH", "STAGE1_GROWTH_CAP", "STAGE1_GROWTH_FLOOR", "DCF_STAGE1_YEARS",
    "DCF_BASE_YEARS", "MIN_MARGIN_OF_SAFETY", "MIN_FCF_SPREAD_OVER_10Y", "RISK_FREE_SOURCES", "MIN_METRICS_FOR_PASS",
    "MAX_NET_DEBT_EBITDA", "MAX_SHARE_GROWTH_PER_YEAR", "DILUTION_FLAG_PER_YEAR", "MIN_CASH_RUNWAY_MONTHS",
    "LENS_WEIGHTS", "CONTROVERSY_GAP", "DRAWDOWN_THRESHOLD", "RECOVERY_BAND", "MIN_EPISODES", "BENCHMARKS",
    "MARKET_DRIVEN_RATIO", "PEER_COUNT", "LEADERSHIP_LOOKBACK_MONTHS", "LEADERSHIP_KEYWORDS", "LEADERSHIP_HIGH_COUNT",
    "CORP_ACTION_PRICE_GAP", "CORP_ACTION_SHARE_CHANGE", "EARNINGS_REFETCH_GRACE_DAYS",
    "CACHE_TTL_FUNDAMENTALS_MAX_DAYS", "CACHE_TTL_PRICES_DAYS", "OFFICER_REFRESH_DAYS", "SCATTER_LABEL_TOP_N",
    "STALE_FUNDAMENTALS_DAYS", "MIXED_PERIOD_DAYS", "STAGE_DIVERGENCE", "STAGE1_SLACK", "FETCH_MAX_RETRIES",
    "FETCH_BACKOFF_SECONDS", "CANARY_TICKERS", "CIRCUIT_BREAKER_WINDOW", "CIRCUIT_BREAKER_FAIL_RATE", "FIELD_NA_SPIKE",
    "YF_MIN_SECONDS_BETWEEN_CALLS", "EDGAR_MAX_REQUESTS_PER_SECOND",
    # Score mapping and scoring conventions
    "QUANT_UPSIDE_BREAKPOINTS", "ROIC_SPREAD_BONUS", "ROIC_SPREAD_BONUS_THRESHOLD", "PIOTROSKI_ADJUSTMENT",
    "RUNWAY_BREAKPOINTS", "RUNWAY_SCORE_CAP", "NET_DEBT_EBITDA_BREAKPOINTS", "INTEREST_COVERAGE_BREAKPOINTS",
    "ALTMAN_BREAKPOINTS", "LEVERAGE_TREND_SCORES", "SECTOR_CYCLICALITY", "INDUSTRY_CYCLICALITY_OVERRIDES",
    "MOAT_RUBRIC", "DA_RUBRIC", "CALIBRATION_MAX_SHIFT", "VERDICT_BANDS", "QUALITY_ROIC_SPREAD_BREAKPOINTS",
    "QUALITY_FCF_MARGIN_BREAKPOINTS", "QUALITY_SHARE_TREND_BREAKPOINTS",
    # Signals
    "PIOTROSKI_MIN_CHECKS", "PIOTROSKI_STRONG", "PIOTROSKI_WEAK", "ALTMAN_ZONES", "BENEISH_THRESHOLD",
    "REVERSE_DCF_SEARCH_RANGE", "USE_EARNINGS_YIELD_IN_SCREEN", "MIN_EARNINGS_YIELD", "TRAP_RISK_FAILS_SCREEN",
    "PEAK_MARGIN_RATIO", "INSIDER_LOOKBACK_MONTHS", "INSIDER_CLUSTER_MIN", "INSIDER_CLUSTER_DAYS",
    "DIVIDEND_FCF_PAYOUT_MAX", "DIVIDEND_CUT_THRESHOLD", "ALERT_PIOTROSKI_DROP",
    # Turnaround (Phase 4)
    "ROLLING_HIGH_DAYS", "LISTING_COUNTRY_SUFFIXES", "LISTING_COUNTRY_DEFAULT", "RECOVERY_CLOCK_START",
    "TURNAROUND_STRUCTURAL_ACTION", "TURNAROUND_CONFIDENCE_LEVELS", "TURNAROUND_HIGH_MIN_EPISODES",
    "TURNAROUND_CONFIDENCE_RULE", "TURNAROUND_SURVIVORSHIP_CAVEAT", "RATIO_COMPARE_TOLERANCE", "DAYS_PER_MONTH",
    "MACD_FAST", "MACD_SLOW", "MACD_SIGNAL", "MACD_CROSSOVER_LOOKBACK_DAYS", "WILLIAMS_R_PERIOD",
    "WILLIAMS_R_OVERSOLD", "WILLIAMS_R_LOOKBACK_DAYS", "DOUBLE_BOTTOM_WINDOW_DAYS", "DOUBLE_BOTTOM_PIVOT_DAYS",
    "DOUBLE_BOTTOM_TOLERANCE", "DOUBLE_BOTTOM_MIN_SEPARATION_DAYS", "DOUBLE_BOTTOM_MIN_BOUNCE",
    "VALUATION_RECOVERY_YEARS", "RECOVERY_CLOCK_SECONDARY", "INSIDER_FETCH_MONTHS",
]


def test_every_spec_constant_is_in_config():
    missing = [c for c in SPEC_TABLE_CONSTANTS if not hasattr(config, c)]
    assert not missing, missing


def test_spec_defaults():
    assert config.COST_OF_CAPITAL == 0.09
    assert config.CACHE_TTL_FUNDAMENTALS_MAX_DAYS == 100 and config.EARNINGS_REFETCH_GRACE_DAYS == 3
    assert config.CANARY_TICKERS == ["SPY", "MSFT", "CNR.TO"]
    assert config.RISK_FREE_SOURCES["CAD"]["series"] == "BD.CDN.10YR.DQ.YLD"
    assert config.UNIVERSE_SOURCES["cash_cows_small"]["label"] == "CALF"


def test_model_name_comes_from_env_with_default():
    src = (ROOT / "config.py").read_text()
    assert 'os.getenv("ANTHROPIC_MODEL")' in src


def test_gitignore_excludes_env():
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in lines
    r = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT)
    assert r.returncode == 0, ".env is not ignored by git"
    for path in ["runs.db", "storage/runs.db", "data/cache/cache.db", "data/universe/raw/cowz.csv"]:
        assert subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0, path
    assert subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT).returncode != 0


def test_no_yfinance_labels_outside_field_map():
    """Raw yfinance statement labels are named only in data/field_map.py."""
    labels = ["Total Revenue", "Free Cash Flow", "Stockholders Equity", "freeCashflow", "financialCurrency"]
    offenders = []
    for p in list((ROOT / "data").glob("*.py")) + list((ROOT / "app").glob("*.py")):
        if p.name in ("field_map.py",):
            continue
        text = p.read_text()
        offenders += [f"{p.name}: {lab}" for lab in labels if re.search(re.escape(f'"{lab}"'), text)]
    assert not offenders, offenders
