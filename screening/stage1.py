"""Stage 1: a cheap pre-screen from the yfinance info call plus the run's batch price.

- Info fields are converted to the trading currency first (Rule 5) and go
  through the Rule 2b sign checks.
- Market cap = info shares outstanding × batch price, never info marketCap,
  so a cached info can't supply a stale price.
- Thresholds are loosened by STAGE1_SLACK (see screening.metrics).
- Financials and REITs skip the leverage test (EBITDA doesn't apply) and the
  FCF test (FCF isn't meaningful for them, Rule 5).
- Cuts only on a clear fail: a missing field never cuts, and a Graham n/m
  (negative EPS or book value) never cuts.
Stage 1 only decides what gets fetched; the final status comes from stage 2.
"""

from __future__ import annotations

import config
from data.ratios import market_cap
from data.sector import SectorRoute
from data.values import Datum
from screening import metrics as mx
from screening.models import FAIL, NM, SLOT_MOS, Stage1Result

STAGE1_FIELDS = ["trailing_eps", "book_value_per_share", "info_free_cashflow", "info_ebitda", "info_total_debt",
                 "info_total_cash", "shares_outstanding"]


def stage1(info_values: dict[str, Datum], price: Datum, rf: Datum, route: SectorRoute,
           ticker: str = "") -> Stage1Result:
    v = {k: info_values.get(k, Datum.missing()) for k in STAGE1_FIELDS}
    mcap = market_cap(v["shares_outstanding"], price, ticker)
    slack = config.STAGE1_SLACK
    res = Stage1Result(market_cap=mcap, field_statuses={f"info.{k}": d.status for k, d in v.items()})
    res.metrics.append(mx.margin_of_safety(v["trailing_eps"], v["book_value_per_share"], price, slack))
    if route.sector_adjusted:
        res.notes.append(f"{route.label}: stage-1 FCF and leverage tests skipped (not meaningful)")
    else:
        fcf = v["info_free_cashflow"]
        if fcf.ok and fcf.value < 0:
            runway = mx.cash_runway(v["info_total_cash"], fcf)
            res.metrics.append(mx.runway_metric(runway, v["info_total_cash"], fcf, slack,
                                                label="Estimated cash runway (stage 1, info totalCash)"))
            if caveat := mx.fleet_runway_caveat(route.industry):
                res.metrics[-1].notes.append(caveat)
        else:
            res.metrics.append(mx.fcf_yield(fcf, None, mcap, rf, slack))
        res.metrics.append(mx.leverage_metric(v["info_total_debt"], v["info_total_cash"], v["info_ebitda"], slack))
    for m in res.metrics:
        if m.outcome == FAIL or (m.outcome == NM and m.slot != SLOT_MOS):
            res.cut_reasons.append(f"{m.name}: {m.display} (needs {m.threshold})")
    res.survives = not res.cut_reasons
    return res
