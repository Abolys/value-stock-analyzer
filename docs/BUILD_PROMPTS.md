# Value Stock Analyzer — Claude Code Build Prompts

## Before Phase 1

1. Unzip the kit into an empty project folder and run `git init` there. The kit already contains `CLAUDE.md` (the standing rules, loaded every session), `docs/SPEC.md` (the detailed specification), `.gitignore`, `.env.example`, `docs/ui-mockup.html`, this file as `docs/BUILD_PROMPTS.md`, and one slash command per phase in `.claude/commands/`.
2. Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` and `SEC_USER_AGENT` (your name and email; the SEC requires it). Add `FMP_API_KEY` and the SMTP settings only if you want them. `.env` is already in `.gitignore`. The Anthropic key is billed through the Claude Console, separately from the subscription you use to run Claude Code.
3. Start Claude Code in the project folder.

"The spec" in the prompts means `CLAUDE.md` and `docs/SPEC.md` together; section names refer to either file.

**For each phase:** switch into plan mode (Shift+Tab cycles modes), type the phase's command (`/phase-1`, then `/phase-2` in a later session, and so on), review the plan, and approve it. The commands contain exactly the prompts below, so you don't need to paste anything. Each phase ends with the phase-completion checklist from `CLAUDE.md`: tests, golden tickers, the app starting, and a commit. If a later phase breaks something, roll back to the last phase's commit. Starting each phase in a fresh session keeps the context clean; `CLAUDE.md` carries the rules across.

## Phase 1 — Foundation, data layer and app shell

```
Read CLAUDE.md and docs/SPEC.md first; together they govern this whole project.

Build the foundation:
1. Project layout, requirements.txt, and config.py containing every constant from "Thresholds and defaults" table in the spec and the Score mapping and Scoring conventions sections. The kit already provides .gitignore and .env.example: check them against the spec, extend them if anything is missing, and make sure .env is ignored before the first commit. containing every constant from "Thresholds and defaults" table in the spec and the Score mapping and Scoring conventions sections.
2. A DataProvider interface in /data with a yfinance implementation. Keep the interface generic so an FMP implementation can be added later without touching screening or analysis code. Add an FMP stub that is only enabled when FMP_API_KEY is set.
3. A disk cache wrapping every provider call, plus the yfinance throttle. Prices expire on CACHE_TTL_PRICES. Fundamentals and info use the earnings-driven rule from "Thresholds and defaults" table in the spec (valid until the next earnings date + EARNINGS_REFETCH_GRACE_DAYS, capped at CACHE_TTL_FUNDAMENTALS_MAX), with info also refreshed every OFFICER_REFRESH_DAYS. Each entry records when it was fetched and why it expires. Also a batch price function using yfinance's multi-ticker download in chunks, returning the latest price per ticker.
4. Currency handling: detect financialCurrency vs currency mismatches and convert financials using a yfinance FX pair, as a function usable on both info fields and statements (Phase 2's stage 1 needs it). Also the two price accessors from the spec Rule 5: adjusted closes for drawdowns, signals and indexed charts; the actual latest price for valuation.
4a. Data source resilience per the spec: /data/field_map.py as the only place yfinance keys and statement row labels are named (with aliases, and "N/A - field not found" when none match); data/health.py checking CANARY_TICKERS at app start (banner on failure, cached data still usable); requirements.txt pinning the exact yfinance version; scripts/upgrade_yfinance.py (upgrade, health check, pytest, pass/fail summary); and per-ticker FMP fallback when FMP_API_KEY is set, with every value recording its provider.
4b. Period handling per the spec Rule 3b: the provider returns every fundamental value with its period end date; a TTM builder sums the last four quarters (falling back to the latest fiscal year, labelled "annual, not TTM"); fiscal-year labels use the company's own year end; a staleness check (STALE_FUNDAMENTALS_DAYS, or an earnings date passed with no newer statements); and a helper that marks a ratio "mixed periods" when its inputs are more than MIXED_PERIOD_DAYS apart.
5. The risk-free rate by trading currency (RISK_FREE_SOURCES): the US 10-year from ^TNX (check the scale) and the Government of Canada 10-year from the Bank of Canada Valet API (verify the series code before relying on it). Unknown currencies return N/A with the reason.
6. The leadership-turnover flag, using the four layers in the spec:
   - A SEC EDGAR client: look up a company's CIK, list its 8-K and 6-K filings, send SEC_USER_AGENT and respect the rate limit.
   - 8-K Item 5.02 parsing for CEO/CFO departures.
   - 6-K keyword pre-filter using LEADERSHIP_KEYWORDS. Leave the LLM confirmation step as a clearly marked stub that returns "unconfirmed"; Phase 3 wires it to the LLM layer.
   - An officer_snapshots table in SQLite with a save function that takes an already-fetched info dict (Phase 2's stage 1 will call it, so screening snapshots every screened ticker for free), and scripts/snapshot_officers.py covering only tickers that aren't in any screened list (throttled, resumable if interrupted).
   - /data/leadership_events.csv for manual events.
   - A merge step returning the events, the layers used, the coverage window, and "partial coverage" when the window is shorter than LEADERSHIP_LOOKBACK_MONTHS.
   Never access SEDAR+ programmatically.
7. Corporate-action detection: the heuristic from the spec, merged with /data/corporate_actions.csv (seed it with HTZ). Return a list of break dates per ticker. Then the adjusted share-count history per the spec: split- and reverse-split-adjusted, cut at breaks, trend computed on the latest segment only, and unexplained jumps flagged rather than scored.
8. The screener universe as described in the spec: scripts/refresh_universe.py builds the COWZ, small-cap Cash Cows, S&P 400, S&P 600 and TSX Composite lists from ETF holdings files (URLs in config.UNIVERSE_SOURCES), plus empty watchlist.csv and dataroma.csv templates for hand-entered tickers. Before building the Cash Cows lists, confirm the small-cap fund's ticker on paceretfs.com. Handle failed or changed downloads by keeping the last good CSV and reporting it as stale, and accept manually downloaded files in /data/universe/raw/. Merging lists removes duplicate tickers but keeps all their sources.
8b. Inputs for the signals in the spec "Value-trap, valuation and ownership signals":
   - Form 4 support in the EDGAR client: parse open-market purchases (code P) and sales (S), ignore all other codes, record 10b5-1 flags. Plus /data/insider_events.csv for manual (non-SEC) insider events, and coverage labels as for the leadership flag. Never automate SEDI.
   - Field-map entries for heldPercentInsiders, shortPercentOfFloat, the analyst-estimate properties available in the pinned yfinance version (check which exist; missing → N/A), dividend history and dividends paid, and every statement line the Piotroski, Altman and Beneish formulas need (working capital, retained earnings, receivables, SG&A, depreciation, PP&E, current ratio inputs, etc.).
9. scripts/capture_fixtures.py, and use it to save offline fixtures for the golden tickers (including two fiscal years of full statements, dividends and Form 4 filings where they exist).
10. A minimal Streamlit shell in app/main.py: a sidebar with ticker input and a "Run screener" option, and a page that shows the raw provider output for a ticker. Later phases replace this with real views.

Tests: Form 4 parsing keeps P and S and ignores grants and option exercises, 10b5-1 sales are marked, a non-SEC ticker gets manual or "N/A - no insider data source" coverage, missing analyst-estimate properties return N/A without errors, a fundamentals cache entry stays valid before the next earnings date and expires after it plus the grace days, the cap applies when there's no earnings date, info refreshes after OFFICER_REFRESH_DAYS even when fundamentals are still valid, the batch price function handles a chunk with one bad ticker without losing the rest, .gitignore excludes .env, a synthetic 1-for-10 reverse split does not show as a share reduction, the HTZ share-count trend starts after the break, an unexplained share jump is flagged, the CAD risk-free source is used for a .TO ticker and the US one for a US ticker, a USD-reporting TSX company's info fields are converted to CAD, the adjusted and actual price accessors return different values on a dividend payer, the field map resolves an alias when the primary label is missing and returns "field not found" when no alias matches, the health check fails on a mocked empty statement and on a missing field, the app shows the banner and still serves cached data when the health check fails, FMP fallback is used only when configured, TTM built from four quarters with the right end date, the annual fallback label, LULU's fiscal year labelled by its own year end, staleness triggered by age and separately by a passed earnings date, mixed-periods marking, cache hit/miss and expiry, universe parsing on a saved holdings file (non-equity lines dropped, TSX tickers get .TO, duplicates merged with combined sources, a failed download keeps the stale list), currency conversion on a fixture, corporate-action detection finds the HTZ break, the leadership-flag cases listed in the spec (8-K, 6-K keyword hit and miss, officer-snapshot change, manual event, partial coverage), and graceful "N/A" for missing fields.

Finish with the phase-completion checklist.
```

## Phase 2 — Value screener

```
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
```

## Phase 3 — Four-lens analysis engine

```
Build the four lenses in /analysis plus the LLM layer in /llm. Every lens returns a pydantic result: score 1-10, markdown rationale, the key figures as typed fields, the assumptions used, and a data-completeness note.

Scores follow the "Score mapping" section of the spec exactly: breakpoint tables for Quant and Macro, anchored rubrics plus the two-fact evidence rule for Moat and Devil's Advocate. Build one shared helper for breakpoint interpolation. Each rationale includes its mapping line (e.g. "DCF upside +18% → 6.5; ROIC spread +6 pts → +1; score 7.5"), and the mapping steps are also returned as a structured field.

1. Quantitative Fundamental (deterministic):
   - Two-stage DCF starting from the average FCF of the last DCF_BASE_YEARS fiscal years (not a single year; if that average is ≤ 0 or FCF changed sign in the window, return "Insufficient data - unstable FCF base" and use cash runway, per Rule 2b), then DCF_STAGE1_YEARS of growth at the revenue CAGR over the available fiscal years (not FCF growth), clamped to STAGE1_GROWTH_CAP/FLOOR, TERMINAL_GROWTH after, discounted at COST_OF_CAPITAL. Return fair value, implied upside, and every assumption.
   - ROIC compared with the same COST_OF_CAPITAL.
   - The Piotroski adjustment from Score mapping.
   - Reverse DCF (the growth rate today's price implies, next to the historical revenue CAGR) and the 5 × 5 sensitivity grid with its fair-value range, both from /signals and using exactly the same DCF function as the base case.
   - Peak-earnings check for cyclicals: when flagged, normalise the DCF base (TTM revenue × average FCF margin), show raw and normalised fair values, and set confidence to low.
   - The Graham Number as a cross-check.
   - Structurally FCF-negative companies skip the DCF and use cash runway as the quantitative read.
   - Too little or too erratic FCF history returns "Insufficient data" with the reason.
2. Macro & Balance Sheet Risk (deterministic): leverage, interest coverage, net debt/EBITDA trend, cyclical exposure by sector, and the Altman Z'' sub-score. Use the current vs long-term debt split as the maturity proxy and say so. Use sector-adjusted metrics for financials and REITs.
3. Business Moat (LLM): input is the business summary, sector, industry, gross margin and ROIC trends. The prompt first identifies the single most relevant threat for this sector (AI disruption for software/services, brand or private-label erosion for retail, regulation or rates for financials, and so on), then scores competitive advantage and pricing power against that threat. Return the identified threat as a field.
4. Devil's Advocate (LLM): runs after the other three. Build a compact JSON payload from their structured fields, for example:
   {"dcf_implied_upside": "+42%", "cash_runway_months": null, "roic_vs_cost_of_capital": "4.1% vs 9%", "net_debt_ebitda": "3.8x", "moat_threat": "private-label erosion", "leadership": {"departures": 2, "coverage": "8-K, full history"}, "fundamentals_as_of": "2026-06-30", "stale": true, "mixed_periods": ["net_debt_ebitda"], "piotroski": "4 / 9", "altman_zone": "grey", "beneish_flag": false, "implied_growth": "-4%/yr vs +6%/yr history", "fair_value_range": "$38-$61", "peak_earnings": false, "ev_ebit_yield": "9.2%", "insiders_6mo": {"buyers": 3, "sellers": 1, "cluster_buy": true, "coverage": "Form 4, full history"}, "dividend": {"fcf_payout": "118%", "at_risk": true, "cuts": 0}, "asset_floor": {"p_tbv": "0.9x", "coverage": "85% (partly covered)", "ncav_to_mcap": "-40%", "net_net": false}, "insider_ownership": "4.1%", "short_interest": "12% of float", "estimate_revisions_90d": "down"}
   plus one line per lens: the strongest bull point and the biggest risk already found. No full markdown reports.
   Persona: a ruthless short-seller looking for every structural flaw, anchored to that payload. It must name:
   - the single DCF or cash-runway assumption most likely wrong,
   - the weakest part of the moat argument,
   - accounting red flags (accrual quality, cash-conversion trend), using the Beneish flag and Piotroski's accrual check as evidence,
   - whether the price's implied growth (reverse DCF) is actually too pessimistic, or fair given the business,
   - what insiders are doing (buying into the drop, or selling), and whether a dividend is at risk,
   - how much of the price the asset floor covers if the earnings case fails (and whether those assets are likely worth their book value),
   - leadership turnover from the leadership flag, as an explicit named item, taking its coverage into account: partial coverage means "unknown", never "clean",
   - whether the weakness looks structural (permanent impairment) or cyclical/sentiment-driven, returned as a field,
   - when the payload is stale or has mixed periods, whether the upside or the risks could be an artifact of out-of-date numbers,
   - and what would have to be true for the bull case to hold.
   Its score follows the convention in the spec (how well the bull case survives).
5. Aggregate (/analysis/aggregate.py): weighted mean of available lenses, the verdict label band, the lens count, and the high-controversy flag using CONTROVERSY_GAP.

Make each lens independently callable, so Quant, Macro and Moat can run concurrently and the Devil's Advocate starts once they're done (Phase 5 shows each result as it arrives).

LLM layer: client using ANTHROPIC_MODEL at the lowest supported temperature, third-party text (business summaries, filing text) wrapped in delimited data blocks with an instruction to ignore any instructions inside them (the spec Rule 4), pydantic response schemas, one validation retry, a disk cache keyed by ticker, lens, payload hash and prompt version, and token and cost logging per call (the spec Rule 4; cache hits logged at zero cost). Also replace the Phase 1 6-K stub with the real LLM confirmation (JSON: departure, role, person, effective_date), cached by filing accession number.

UI: show the four lens results and the aggregate as plain sections for now.

Tests: the reverse DCF recovers a known growth rate and reports "beyond range" outside the search range, the sensitivity grid's centre cell equals the base-case fair value, the Piotroski adjustment applies at 7 and 3 and not with insufficient checks, a synthetic peak-margin cyclical is flagged, normalised and marked low confidence, the Altman sub-score follows its breakpoints and is n/m for JPM, the Devil's Advocate payload includes every signal field with N/A or coverage where missing (including the asset floor), the Quant and Macro scores are identical with and without the asset-floor fields present, dividend safety flags a payer with FCF payout above 100% and detects a cut, SECTOR_CYCLICALITY matches real yfinance sector names from the fixtures (every golden ticker's sector resolves to an entry, not the default), an industry override beats its sector (an airline scores 3 though Industrials is 5), an unknown sector is logged, Quant, Macro and Moat run concurrently and the Devil's Advocate only after them, each LLM call logs tokens and cost and a cache hit logs zero, DCF growth comes from revenue CAGR and is clamped, third-party text appears only inside the data blocks and never in the system prompt, a filing containing "ignore previous instructions" doesn't change the mocked request structure, the calibration test builds payloads from fixtures only, the Rule 2b lens cases (DCF base averaged over DCF_BASE_YEARS; an average ≤ 0 or a sign flip gives "unstable FCF base" and switches to runway; ROIC n/m switches to return on total assets; EBIT ≤ 0 gives an interest-coverage sub-score of 1 even with no interest expense; negative EBITDA with net cash scores leverage from runway; the Macro score never reaches 10 from a negative ratio), n/m values passed to the Devil's Advocate with their reasons, breakpoint interpolation (between points, at points, beyond both ends), Quant scoring including the ROIC bonus/penalty and the runway cap, a Graham/DCF disagreement lowering confidence without changing the score, Macro averaging only available sub-scores and the financials reduced-data path, Moat and Devil's Advocate responses rejected when they cite fewer than 2 facts (mocked), DCF and Graham Number against fixed hand-computed inputs, single cost of capital used in both places, LCID skips the DCF, JPM uses sector-adjusted metrics, the LULU moat prompt contains the sector-threat instruction, the Devil's Advocate payload is compact JSON (no markdown), the aggregate renormalises when a lens is missing, and the controversy flag fires at the threshold. LLM calls mocked. Also build tests/test_calibration.py as described in the spec (marked live), run it once, and commit the first calibration file as the baseline.

Finish with the phase-completion checklist.
```

## Phase 4 — Turnaround timing

```
Build /analysis/turnaround.py. It runs after the four lenses and consumes their structured output. Its job is to estimate how long recoveries have historically taken for this stock, from its own price history.

0. First compute the current drawdown from the rolling 52-week high. If it's smaller than DRAWDOWN_THRESHOLD, output "Not in a qualifying drawdown (currently −X% from 52-week high)" with no range, but still build the episode history below for the chart.
1. Use the adjusted-close price accessor (the spec Rule 5). Split the price history at the corporate-action breaks from Phase 1. Never compute a rolling 52-week high, a drawdown or a recovery across a break.
2. Find drawdown episodes of at least DRAWDOWN_THRESHOLD below the rolling 52-week high within each segment. For each, measure the time to get back within RECOVERY_BAND of the prior high. Episodes still unrecovered today are counted and reported separately, not dropped. Classify each episode, and the current drop, as market-driven or company-specific against the BENCHMARKS index over the same peak-to-trough window, using MARKET_DRIVEN_RATIO (see "Turnaround estimate integrity" in the spec).
3. Valuation-based recovery (time for P/E, P/B or FCF yield to return to the stock's own 5-year median) runs only when FMP is configured. Otherwise report it as "Unavailable — needs longer fundamental history (FMP)".
4. Report the median and interquartile range of recovery times and the episode count, using only past episodes of the same type as the current drop. If fewer than MIN_EPISODES match, use all episodes, say so, and lower confidence one level. Peers in step 5 are matched by type the same way.
5. With fewer than MIN_EPISODES, repeat the analysis on PEER_COUNT peers (same industry, nearest market cap, chosen from the combined universe lists) and label the result "Peer-based, lower confidence", listing the peers used and noting they come from the universe. Put peer selection in its own function; Phase 5's peer strip reuses it.
6. Read the Devil's Advocate structural-vs-cyclical field. If it says structural, withhold the estimate or label it "Low confidence — may not be mean-reverting". Never apply a cyclical recovery pattern to what could be a value trap.
7. Catalysts to watch: the next earnings date from yfinance, recent leadership events from the leadership flag (all layers, with their sources), and any catalysts the lenses surfaced. Debt maturity dates appear only if FMP supplied them.
8. Near-term signals (deterministic): a bullish MACD crossover, Williams %R rising out of oversold, a forming double-bottom, and an insider cluster buy during the current drawdown (from /signals). Make the technical parameters config constants. Report them alongside the statistical window. They never override step 6.
9. Output: a range naming the episode type it's based on, such as "8-20 months, based on 3 past company-specific drops", the current drop's type, the survivorship-bias caveat from the spec (always shown), a confidence level (High/Medium/Low) with the rule that produced it written in config (sample size plus the structural flag), catalysts, active technical signals, and the asset floor as context ("Asset floor: 85% of the price covered by tangible book"), which never changes the range or confidence. Never a single date.

Return the episode list (start, trough, recovery dates) as structured data so Phase 5 can shade them on a chart.

UI: show the estimate and the episode list as a plain section.

Tests: the asset-floor line appears in the output and the range and confidence are identical with and without it, an insider cluster buy inside the current drawdown appears as a near-term signal and one before the drawdown doesn't, a stock 8% off its high returns "Not in a qualifying drawdown" with no range but with its episode history, a stock 30% off returns a range, episode classification on synthetic series (stock and benchmark fall together → market-driven; stock falls alone → company-specific; exactly at MARKET_DRIVEN_RATIO), the estimate uses only same-type episodes, the fallback to all types lowers confidence and says so, Canadian tickers use the Canadian benchmark, the survivorship caveat is always present, no episode or rolling high crosses the HTZ break, unrecovered episodes are counted, the peer fallback triggers below MIN_EPISODES, a structural flag withholds or downgrades the estimate, technical signals on synthetic price series, and valuation-based recovery is labelled unavailable without FMP.

Finish with the phase-completion checklist.
```

## Phase 5 — Dashboard, run history and export

```
Replace the plain sections with the finished UI. Read docs/ui-mockup.html first (see "UI reference" in the spec): match its page structure, content and hierarchy, as separate Streamlit pages (Screener, Stock, Estimate accuracy), with the shared sidebar. Follow the Charts section of the spec for all charts. Loading states on every slow call, and clear error states for invalid tickers and failed API calls.

Sidebar: API spend this month (from the logged LLM costs), with the number of analyses behind it.

Screener view:
- A data-source banner at the top of every page when the health check fails, naming what failed and suggesting the upgrade script, plus a note that cached results are shown with their age.
- A run header: its status (completed / stopped: source failing / blocked: health check failed), any "likely renamed upstream" fields, when the shown screen ran, which lists it covered, counts per stage, and a "failed to load" count that expands to the list of tickers and reasons.
- A list picker (checkboxes for each universe list) showing each list's as-of date, with stale lists marked, plus the "Run new screen" button with a progress bar and estimated time left while a run is going.
- A "Changes since last screen" panel above the table: new Pass, dropped from Pass, newly Incomplete, and newly stale, each a short clickable list. This is the weekly view.
- A table that defaults to showing Pass only, with a status filter (Pass / Fail / Incomplete / All) and a source filter. Columns: Ticker, Source (e.g. "COWZ, Dataroma"), Margin of safety, FCF yield vs 10-year government yield (US or Canada, by trading currency; cash runway in months for FCF-negative names), Net debt/EBITDA, Share-count trend (%/yr), EV/EBIT earnings yield, Piotroski (x / 9), Trap risk (flag, with Altman zone and Beneish on hover), P/TBV, Asset coverage (% with band; the net-net flag as a highlighted tag), Quality score (with "n of 4"), Screen status (Pass / Fail (manual only) / Incomplete), Data as of (the fundamentals' period end, with a stale marker when flagged). Each metric cell is green or red against its config threshold; N/A and n/m cells are visibly different (e.g. grey "N/A" vs amber "n/m") and say why on hover. Sortable by any column and filterable by source. Sector-adjusted tickers are labelled and show their own metrics.
- A scatter of margin of safety (x, %) vs quality score (y, 0-10), one point per ticker, following the table's current filters (Pass only by default), with a vertical line at zero margin of safety. Only the top SCATTER_LABEL_TOP_N points by quality plus margin of safety get text labels; every point shows ticker, name and both values on hover. Tickers without a computable margin of safety are left off and listed in a caption with the reason.
- Clicking a table row or a scatter point opens that ticker's view.

Per-ticker view:
- Progressive loading: the header, 52-week bar and charts appear first; each lens result fills in as it finishes (Quant, Macro and Moat in parallel, the Devil's Advocate last), then the aggregate and turnaround. Each pending section shows what it's waiting on.
- Header: the 52-week range bar from the spec Charts, the verdict badge ("6.8 / 10 — lean bullish, 4 of 4 lenses") and tags for high controversy, leadership turnover, sector-adjusted, and "fundamentals may be stale" when they apply. Under the header, one line: "Price as of <date> · Fundamentals as of <period end> (TTM)". The leadership tag always shows its coverage (e.g. "2 departures · 8-K, full history" or "No departures found · partial coverage, tracking since Oct 2026"); hovering lists the events with their source.
- Fundamentals over time as small multiples (the spec Charts): stacked panels for price, revenue, net income and total debt on a shared time axis, each on its own scale, fundamentals at their quarterly period ends. No indexing of anything that can go negative.
- Lens scores as the dot strip from the spec Charts (one 1-10 row per lens, a line at the aggregate, the Devil's Advocate gap shaded when the controversy flag fires, hollow labelled markers for insufficient data), above the lens tabs.
- A peer strip (the spec Charts) for margin of safety, FCF yield vs risk-free, net debt/EBITDA and ROIC, using the peer-selection function from Phase 4, with the peers named on hover and their source noted.
- Valuation: the base-case fair value with the sensitivity heatmap (the spec Charts), the fair-value range, and the reverse-DCF line ("Price implies −4%/yr growth; history shows +6%/yr"). For flagged cyclicals, raw and normalised fair values side by side with the peak-earnings note.
- The asset floor panel (spec Charts): market cap vs tangible book, NCAV and NNWC on one scale, with the coverage band and the net-net flag.
- The trap-score panel (Piotroski, Altman Z'', Beneish meters), EV/EBIT with the net-cash flag when it applies, and an insider-activity summary with coverage (insider buy and sell markers also appear on the price panel of the small multiples).
- A dividend panel for payers (per-share bars with cuts highlighted, FCF payout line, "at risk" flag), and a small context strip: insider ownership, short interest, analyst estimate revisions, each N/A with its reason when missing.
- Lens tabs: each lens's rationale and assumptions, a "How this score was built" line showing the mapping steps, the Moat and Devil's Advocate evidence facts listed under their scores, a low-confidence marker when the Graham Number and DCF disagree, cash runway instead of DCF fair value for FCF-negative names, and the Devil's Advocate tab in a warning colour.
- Turnaround outlook: a range bar (median marked, interquartile range as the bar), the confidence level, catalysts, active technical signals, and below it the price history with past drawdown episodes shaded in two colours by type (market-driven vs company-specific, with a legend), the benchmark as a faint line for comparison (stock and benchmark both indexed to 100 at the chart's start, keeping one y-axis), and corporate-action breaks marked as vertical lines. The survivorship caveat sits under the range bar in small text.

Run history (/storage):
- Save every analysis run to SQLite: ticker, timestamp, input hash, each lens score, aggregate, turnaround estimate and its confidence, the drawdown episode it belongs to, and the run's LLM tokens and cost.
- A History tab per ticker: the aggregate verdict over time, and for past turnaround estimates, whether the price recovered within the estimated window (recovered / not yet / missed). Only the first estimate per drawdown episode is scored, measured from that run's date (the spec Turnaround estimate integrity); later ones are shown greyed as "same episode".
- An "Estimate accuracy" page across all tickers: one stacked bar per episode type (market-driven, company-specific) showing recovered within window / still waiting / missed, with the count of scored estimates. Show "Not enough scored estimates yet" below 10. This is the check on whether Phase 4 is worth trusting.

Export (/reports):
- One-click export of the per-ticker view to Markdown and .docx (python-docx), with charts embedded as PNGs (kaleido), all assumptions and data gaps included, and the footer from the spec Charts on every page.

Tests: the asset floor panel draws negative NCAV left of zero and shows the net-net flag only when NCAV ≥ market cap, the sensitivity heatmap colours cells relative to the current price and outlines the base case, the trap-score panel shows "Insufficient data" for a Piotroski with too few checks, insider markers render hollow for 10b5-1 sales, the dividend panel is hidden for non-payers, the 52-week bar places the marker correctly and labels the drawdown, the scatter follows the table filter and labels only the top N, the monthly spend sums logged costs, two runs in one drawdown episode count once in the accuracy scoring, the Estimate accuracy page shows the not-enough-data message below 10, the changes panel on two saved runs (a ticker that newly passed, one that dropped out, one that became stale), the table defaults to Pass only, the small-multiples chart never indexes net income (a fixture with a loss at the start), the dot strip shows a hollow marker for an insufficient-data lens and shades the controversy gap, the peer strip lists N/A and n/m peers below it, the export footer is present on every page, table colouring against thresholds, scatter exclusion caption, run history write/read, and a .docx export that opens and contains every section.

Finish with the phase-completion checklist.
```

## Phase 6 — Portfolio, thesis journal and alerts

```
Build /portfolio as described in the spec "Portfolio and thesis tracking". The Portfolio page follows the Portfolio section of docs/ui-mockup.html.

1. Holdings: holdings and transactions tables (buys and sells as dated rows, account label, currency). Position value at the actual latest price, gain/loss, and return vs the ticker's BENCHMARKS index over the holding period.
2. Thesis at purchase: adding a holding runs a full analysis (or reuses today's run) and stores it as the purchase snapshot, plus a thesis record: reasons (list), intrinsic value, buy-below and target prices, and sell triggers.
3. Sell triggers: structured rules on fields from THESIS_TRIGGER_FIELDS only (metric, operator, value), validated on save, with a form in the UI to build them (pick a field, an operator, a value). Never evaluate free text.
4. Thesis check: on every refresh, evaluate each trigger and diff current lens scores, key metrics and signals against the purchase snapshot.
5. Journal: dated free-text entries per holding, a "still holds?" tick per original reason, and automatic entries when a trigger fires or an alert is raised. Included in the per-ticker export.
6. Alerts: evaluated at the end of every scheduled screen run and on app start, for holdings and the watchlist, covering every alert type in the spec. Each fires once per event (store what has already been alerted). In-app inbox with an unread count in the sidebar; optional email via the SMTP settings in .env, off unless configured.
7. UI: the Portfolio view from the spec Charts (holdings table with value, gain/loss, return vs benchmark, trigger traffic light and unread alerts; per holding a "then vs now" comparison, the trigger list with current status, and the journal). An "Add to portfolio" button on the per-ticker view opens the thesis form pre-filled from the current analysis (fair value, buy-below at MIN_MARGIN_OF_SAFETY below it).

Tests: return vs benchmark over a known period, a trigger on an allowed field fires and one on a disallowed field is rejected at save, a trigger fires once and doesn't re-alert on the next run, each alert type from the spec fires on a crafted fixture, the then-vs-now diff reports changed scores, email is skipped cleanly when SMTP isn't configured, and a holding with sells computes realised and unrealised gain correctly.

Finish with the phase-completion checklist.
```

## After the build

- Run the app on a ticker you already have a firm view on. If the DCF, moat score or Devil's Advocate disagrees with you, check the displayed assumptions first. Every number should trace back to an input you can see.
- Tune thresholds and score breakpoints in `config.py` only, then rerun `pytest`.
- If the data-source banner appears, or a run stops with "source failing", run `python scripts/upgrade_yfinance.py`. If it passes, the pin is updated and you can resume the run. If it fails, Yahoo's change hasn't been fixed in yfinance yet: keep using cached data, and check the yfinance GitHub issues for the fix.
- Whenever you change the Moat or Devil's Advocate prompt, run `pytest -m live tests/test_calibration.py` before trusting new scores. If scores move more than 2 points, decide whether that's the improvement you wanted, then commit the new calibration file.
- Schedule `scripts/run_screen.py` to run weekly overnight (cron on Mac or Linux, Task Scheduler on Windows; ask Claude Code to set it up), so a fresh screen is waiting when you open the app. The first cold run is the slow one; later runs refetch fundamentals only for companies that have reported since, so outside earnings season they're much faster.
- Run `scripts/refresh_universe.py` monthly; the ETF holdings change as the funds rebalance.
- Officer snapshots happen automatically with every screen, so the weekly screen keeps leadership tracking current for all screened stocks. Start screening as soon as Phase 2 is done: tracking for TSX-only stocks only begins with the first snapshot. Run `scripts/snapshot_officers.py` monthly only if you keep watchlist names that aren't in any screened list.
- For TSX-only holdings or candidates, check SEDI by hand for insider buying now and then, and add anything notable to `/data/insider_events.csv`.
- When you buy something, add it in the Portfolio view the same day, so the purchase snapshot captures what you actually saw, and write the sell triggers before you own it long enough to get attached.
- When you read about a CEO or CFO exit that the app missed (news, or SEDAR+ by hand), add it to `/data/leadership_events.csv`.
- Once a quarter, after 13F filings come out (about 45 days after each quarter ends), copy new Dataroma names into `dataroma.csv` and update its `as_of` column.
