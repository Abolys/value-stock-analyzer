"""Golden tickers run through everything built so far and hit their branches."""

import pytest

from scripts.golden_check import CHECKS, run_all


@pytest.mark.parametrize("ticker", list(CHECKS))
def test_golden_ticker_branch(ticker, fx_provider, db_path):
    CHECKS[ticker](run_all(fx_provider, ticker, db_path))
