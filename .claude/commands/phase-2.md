---
description: Build phase 2 of the value stock analyzer: Value screener
---
Phase 2: Value screener.

Before planning, read CLAUDE.md (standing rules) and the docs/SPEC.md sections this phase touches. "The spec" below means those two files together. docs/BUILD_PROMPTS.md has the full six-phase plan for context. Plan first and wait for approval before writing code.

Build /screening using the thresholds in config.py:
1. Margin of safety: Graham Number vs current price. It's a cheap first pass; the full DCF runs only in Phase 3 for a single ticker.
2. SBC-adjusted FCF yield: (FCF − stock-based comp) / market cap, compared with the 10-year government yield for the stock's trading currency (RISK_FREE_SOURCES). Only for companies that aren't structurally FCF-negative (FCF_NEGATIVE_RULE). For those, compute cash runway instead, using the exact definition in "Thresholds and defaults" table in the spec (cash + equivalents + short-term investments, divided by raw FCF burn, not SBC-adjusted). Companies with fewer than 2 fiscal years of history get "Insufficient data - too little history" for the FCF-negative test.
3. Net debt/EBITDA.
4. Share-count trend: annualised % change of the adjusted share-count history from Phase 1 (split-adjusted, latest segment only; state the span), rewarding reduction and flagging dilution above DILUTION_FLAG_PER_YEAR.
5. Sector-adjusted metrics for Financial Services and Real Estate tickers, as defined in the spec Rule 5.
5b. In stage 2, compute from /signals: Piotroski F-score, Altman Z'' zone, Beneish flag and EV/EBIT earnings yield (the spec "Value-trap, valuation and ownership signals"), each n/m for financials and REITs, and the trap-risk flag (shown, not a Fail, unless TRAP_RISK_FAILS_SCREEN). EV/EBIT becomes a screen metric only when USE_EARNINGS_YIELD_IN_SCREEN is on. An EV ≤ 0 ticker gets the "net cash exceeds market cap" flag.
5c. Also in stage 2, the asset floor from the spec ("Asset floor" in Value-trap, valuation and ownership signals): TBV, P/TBV, NCAV, NNWC, asset coverage with its band, and the net-net flag with the burn-duration line. Information only: it never changes the screen status or the quality score. Add any field-map entries these need (goodwill, other intangibles, preferred equity, total current assets, receivables, inventory) if Phase 1 didn't already.
6. A quality score exactly as defined in the spec Scoring conventions (ROIC spread, FCF margin, leverage, share-count trend; nothing divided by price), and a screen status following the Screen status rule: Pass, Fail (shown as "Fail (manual only)"), or Incomplete.

The screener runs over the universe lists the user selects (any combination of COWZ, small-cap Cash Cows, S&P 400, S&P 600, TSX Composite, watchlist and Dataroma), and every result carries its source list(s).

Run it as described in the "Screener execution" section of the spec:
- batch prices first, with market cap computed as shares × batch price (so cached info never supplies a stale price);
- a two-stage screen (stage 1 from the yfinance info call with currency conversion, sign checks and STAGE1_SLACK, financials and REITs skipping the stage-1 leverage test, and the officer snapshot saved from the same call; stage 2 full statements for survivors only; final status from stage 2);
- scripts/run_screen.py writing to the screen_runs and screen_results tables, resumable with --resume;
- retry with back-off, and "failed to load" tracked separately from "Fail";
- stage-2 metrics that differ from their stage-1 estimate by more than STAGE_DIVERGENCE logged in the run with both values;
- the health check before every run (a failure records the run as "blocked: health check failed" and doesn't start it);
- the circuit breaker (CIRCUIT_BREAKER_WINDOW / CIRCUIT_BREAKER_FAIL_RATE) pausing the run as "stopped: source failing", resumable with --resume;
- a per-field N/A count after each run, flagging any field above FIELD_NA_SPIKE as "likely renamed upstream".
Every screener result carries its fundamentals as-of date and the stale flag. A manually entered ticker always bypasses the screen and goes straight to analysis; the screener is for discovery, not a gate.

All ratios go through the sign-checking helper from the spec Rule 2b, with "n/m - <reason>" kept distinct from "N/A - Data Incomplete". A negative-EBITDA company with net debt must fail the leverage screen, never pass it.

Output one pydantic result per ticker with raw inputs, computed metrics, pass/fail per metric, inputs-available counts, and any N/A reasons.

UI: show the latest completed run's results as a plain sortable table with its run summary (when, which lists, counts per stage, failed to load, and how many tickers were refetched because they reported vs served from cache), and a "Run new screen" button that launches the script in the background and shows its progress from the database. Styling comes in Phase 5.

Tests: the signal cases listed in the spec Golden test tickers (Piotroski exactly 9 and with missing checks, Altman and Beneish against hand-computed values, JPM n/m on all three and on EV/EBIT), the asset-floor cases listed there (net-net flag with burn duration, negative TBV giving n/m and coverage "none", NNWC weights, JPM keeping P/TBV with NCAV n/m), the asset floor never changing screen status or quality score, EV ≤ 0 raises the net-cash flag, the trap-risk flag fires on F ≤ 3 and on the distress zone without failing the screen by default, turning TRAP_RISK_FAILS_SCREEN on makes it a Fail, turning USE_EARNINGS_YIELD_IN_SCREEN on adds a fifth metric, cash runway counts short-term investments and uses raw (not SBC-adjusted) FCF burn, a company not burning cash gets runway n/m, a company with 3 fiscal years and 2 negative is FCF-negative while one with a single year is "too little history", market cap comes from shares × batch price and not from cached info, a second run after no earnings dates passed makes no fundamentals calls (mocked), banks, insurers and REITs are routed by industry string and an unmatched industry is logged, the Screen status rule (all available pass with 3+ available → Pass; any fail → Fail; nothing failed but only 2 available → Incomplete; an n/m counts as failing), the quality score never uses market cap or price (assert on its inputs) and reports "n of 4", stage-1 FX conversion on a USD-reporting TSX fixture, a bank skips the stage-1 leverage test, a screen run writes officer snapshots for the tickers it screened, a failed health check blocks the run, the circuit breaker trips at the configured rate and the run resumes cleanly afterwards, a field that is N/A for most tickers is flagged as a likely rename, every row of the Rule 2b table (negative EBITDA with net debt fails the leverage screen; negative EBITDA with net cash; negative EPS and negative book value give a Graham n/m and still reach stage 2; negative equity; zero or missing market cap), n/m and N/A reported distinctly, a stage-1/stage-2 divergence is logged, a stale ticker still screens but carries the flag, stage 1 lets a borderline ticker through thanks to STAGE1_SLACK and cuts a clear failure, a ticker missing stage-1 fields goes to stage 2, the final result comes from stage 2 even when stage 1 was optimistic, --resume continues an interrupted run without redoing finished tickers, a mocked yfinance refusal is retried and then recorded as "failed to load" (not "Fail"), each metric against hand-computed fixture inputs, the FCF-negative branch on LCID, the financials branch on JPM, MELI returns "Fail (manual only)", and missing fields return N/A without exceptions.

Finish with the phase-completion checklist.
