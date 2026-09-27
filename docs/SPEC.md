# Value Stock Analyzer — Specification

This file is the detailed specification. `CLAUDE.md` holds the standing rules and is loaded every session; this file is read on demand. Before planning any phase, read the sections here that the phase touches. Section names used in the build prompts (e.g. "Screener execution", "Score mapping", "Charts") refer to sections in either file.

## Thresholds and defaults

Every constant below lives in `config.py` (Rule 1 in CLAUDE.md). Further constants are defined inline in the sections that follow and belong in `config.py` too.

| Constant | Default | Used for |
|---|---|---|
| `COST_OF_CAPITAL` | 0.09 | The single discount rate: DCF discounting AND the ROIC comparison. Never use two different rates. |
| `TERMINAL_GROWTH` | 0.025 | DCF terminal stage |
| `STAGE1_GROWTH_CAP` / `STAGE1_GROWTH_FLOOR` | 0.15 / -0.05 | Clamp on the DCF stage-1 growth rate, which is the revenue CAGR over the available fiscal years (not FCF growth, which is too noisy over ~4 years) |
| `DCF_STAGE1_YEARS` | 5 | DCF explicit stage |
| `DCF_BASE_YEARS` | 3 | DCF starting FCF = average of this many fiscal years (see Rule 2b) |
| `FCF_NEGATIVE_RULE` | negative FCF in a majority of the available fiscal years (up to the last 4), with at least 2 years required | Defines "structurally FCF-negative". Fewer than 2 years (recent IPOs, spin-offs) → "Insufficient data - too little history" |
| Cash runway (definition) | (cash + cash equivalents + short-term investments) ÷ monthly burn, where burn = −TTM raw FCF (not SBC-adjusted, since stock comp isn't cash out) | Used wherever the plan says "cash runway". Burn ≤ 0 → runway n/m "not burning cash" |
| `MIN_MARGIN_OF_SAFETY` | 0.20 | Graham Number at least 20% above price to pass |
| `MIN_FCF_SPREAD_OVER_10Y` | 0.0 | SBC-adjusted FCF yield must beat the 10-year government bond yield of the stock's trading currency |
| `RISK_FREE_SOURCES` | USD → yfinance `^TNX`; CAD → Bank of Canada Valet API, 10-year benchmark bond yield series `BD.CDN.10YR.DQ.YLD` (verified 2026-09) | 10-year yield per trading currency |
| `MIN_METRICS_FOR_PASS` | 3 | A screen Pass needs every available metric to pass and at least 3 of the 4 metrics available; fewer available → "Incomplete" |
| `MAX_NET_DEBT_EBITDA` | 3.0 | Leverage ceiling |
| `MAX_SHARE_GROWTH_PER_YEAR` | 0.0 | Pass if share count flat or shrinking |
| `DILUTION_FLAG_PER_YEAR` | 0.03 | Flag dilution above 3%/yr |
| `MIN_CASH_RUNWAY_MONTHS` | 24 | FCF-negative names |
| `LENS_WEIGHTS` | 0.25 each | Aggregate verdict (weighted mean of available lenses) |
| `CONTROVERSY_GAP` | 3.0 | Flag "high controversy" when Devil's Advocate is ≥3 points below the mean of the other three |
| `DRAWDOWN_THRESHOLD` | 0.25 | Drawdown episode = ≥25% below rolling 52-week high |
| `RECOVERY_BAND` | 0.10 | "Recovered" = back within 10% of the prior high |
| `MIN_EPISODES` | 3 | Below this, fall back to peers; also the minimum same-type episodes before falling back to all types |
| `BENCHMARKS` | US → `SPY`, Canada → `^GSPTSE` | Market benchmark per listing country (verify the TSX symbol in yfinance) |
| `MARKET_DRIVEN_RATIO` | 0.5 | An episode is "market-driven" if the benchmark fell by ≥ 50% of the stock's drop over the same window |
| `PEER_COUNT` | 5 | Peers = same yfinance industry, nearest 5 by market cap, chosen from the combined universe lists (the app has no other source of companies); the output lists the peers used and says they come from the universe |
| `ROLLING_HIGH_DAYS` | 252 | Trading days in the rolling 52-week high (computed within one corporate-action segment only) |
| `LISTING_COUNTRY_SUFFIXES` / `LISTING_COUNTRY_DEFAULT` | `.TO`, `.V`, `.NE`, `.CN` → CA / US | Listing country from the ticker suffix; picks the `BENCHMARKS` index |
| `RECOVERY_CLOCK_START` | `trough` | Recovery time runs from the episode's trough (alternatives: `threshold`, `peak`) to the first close back within `RECOVERY_BAND` of the prior high |
| `RECOVERY_CLOCK_SECONDARY` | `threshold` | A second range on the same episodes, from the first close `DRAWDOWN_THRESHOLD` below the high ("once it's down 25%, how long until it's back?"); `""` hides it |
| `TURNAROUND_STRUCTURAL_ACTION` | `withhold` | Devil's Advocate says "structural": withhold the range (`downgrade` → show it at Low confidence, "may not be mean-reverting") |
| `TURNAROUND_CONFIDENCE_LEVELS` / `TURNAROUND_HIGH_MIN_EPISODES` / `TURNAROUND_CONFIDENCE_RULE` | Low, Medium, High / 6 / text | ≥ 6 recovered episodes → High, ≥ `MIN_EPISODES` → Medium, else Low; −1 level for the all-types fallback, −1 for peer-based; structural flag per `TURNAROUND_STRUCTURAL_ACTION`. The rule text is printed with every estimate |
| `TURNAROUND_SURVIVORSHIP_CAVEAT` | the caveat in "Turnaround estimate integrity" | Always shown |
| `RATIO_COMPARE_TOLERANCE` | 1e-9 | Float slack so a value exactly at a ratio threshold (e.g. `MARKET_DRIVEN_RATIO`) counts as meeting it |
| `DAYS_PER_MONTH` | 30.4375 | Unit constant for recovery months |
| `MACD_FAST` / `MACD_SLOW` / `MACD_SIGNAL` / `MACD_CROSSOVER_LOOKBACK_DAYS` | 12 / 26 / 9 / 5 | Bullish MACD crossover within the last 5 trading days (turnaround near-term signal) |
| `WILLIAMS_R_PERIOD` / `WILLIAMS_R_OVERSOLD` / `WILLIAMS_R_LOOKBACK_DAYS` | 14 / −80 / 5 | Williams %R rising out of oversold: ≤ −80 within the last 5 days, above it now. Uses daily highs and lows scaled onto the adjusted closes; falls back to close-based (labelled) when a provider has no highs and lows |
| `DOUBLE_BOTTOM_WINDOW_DAYS` / `_PIVOT_DAYS` / `_TOLERANCE` / `_MIN_SEPARATION_DAYS` / `_MIN_BOUNCE` | 120 / 5 / 3% / 20 / 10% | Forming double bottom: two pivot lows within 3% of each other, ≥ 20 trading days apart, a neckline ≥ 10% above them, price now between the second low and the neckline |
| `VALUATION_RECOVERY_YEARS` | 5 | Valuation-based recovery: time for a ratio to return to its own 5-year median (needs FMP) |
| `LEADERSHIP_LOOKBACK_MONTHS` | 24 | Window for CEO/CFO departure flag |
| `LEADERSHIP_KEYWORDS` | `role`, `departure` and `near_role` groups (see Data sources) | 6-K keyword pre-filter before the LLM check, applied by proximity (`LEADERSHIP_KEYWORD_WINDOW_WORDS`) |
| `LEADERSHIP_HIGH_COUNT` | 2 | Departures in the window that make the flag "high" (1 = "flagged") |
| `CORP_ACTION_PRICE_GAP` / `CORP_ACTION_SHARE_CHANGE` | 0.70 / 0.50 | Heuristic break detection (see Data sources) |
| `CACHE_TTL_PRICES` | 1 day | Price cache expiry |
| Fundamentals cache (rule) | Valid until the company's next earnings date + `EARNINGS_REFETCH_GRACE_DAYS` (3), capped at `CACHE_TTL_FUNDAMENTALS_MAX` (100 days) | Fundamentals only change when a company reports, so they're refetched after a report, not on a fixed timer. No known earnings date → the cap applies |
| `OFFICER_REFRESH_DAYS` | 30 | The `info` call (and its officer snapshot) is refetched at least this often, even if fundamentals are still cached |
| `SCATTER_LABEL_TOP_N` | 10 | Screener scatter labels only this many points |
| `STALE_FUNDAMENTALS_DAYS` | 120 | Latest reported period older than this → "fundamentals may be stale" |
| `MIXED_PERIOD_DAYS` | 100 | A ratio whose inputs' period ends differ by more than this is marked "mixed periods" |
| `STAGE_DIVERGENCE` | 0.30 | Stage-2 metric differing from its stage-1 estimate by more than 30% is logged |
| `STAGE1_SLACK` | 0.25 | Stage-1 screen thresholds are loosened by 25% so borderline names reach stage 2 |
| `FETCH_MAX_RETRIES` / `FETCH_BACKOFF_SECONDS` | 3 / 30 (doubling) | Retry and back-off when yfinance refuses or throttles requests |
| `CANARY_TICKERS` | `SPY`, `MSFT`, `CNR.TO` | Health check before runs (a US ETF, a US company, a Canadian company) |
| `CIRCUIT_BREAKER_WINDOW` / `CIRCUIT_BREAKER_FAIL_RATE` | 50 / 0.5 | Pause a run if more than half of the last 50 tickers failed to load |
| `FIELD_NA_SPIKE` | 0.8 | Flag a likely field rename when one field is N/A for more than 80% of a run's tickers |
| `YF_MIN_SECONDS_BETWEEN_CALLS` | 1.0 | yfinance throttle |
| `EDGAR_MAX_REQUESTS_PER_SECOND` | 10 | SEC fair-access limit |
| `BATCH_PRICE_CHUNK_SIZE` | 100 | Tickers per yfinance multi-ticker download in the batch price step |
| `CORP_ACTION_SHARE_WINDOW_DAYS` | 120 | How close to a price gap the share-count change must fall for the corporate-action heuristic (also the window for matching a split to a share jump) |
| `SPLIT_MATCH_TOLERANCE` | 0.15 | A share-count jump within ±15% of a recorded split ratio is treated as that split |
| `LEADERSHIP_ROLE_TERMS` / `DEPARTURE_TERMS` | CEO/CFO titles; resign, retire, step down, … | 8-K Item 5.02 parsing: a departure needs a role term and a departure term in the same sentence (divisional titles such as "CEO of CCB" don't count) |
| `LEADERSHIP_KEYWORD_WINDOW_WORDS` / `LEADERSHIP_NEAR_ROLE_WORDS` | 12 / 2 | 6-K pre-filter: a departure keyword must fall within 12 words of a CEO/CFO title ("interim" within 2), so quarterly reports that merely mention the CEO, "interim" statements and "retirement" benefits don't reach the LLM |
| `QUARTER_GAP_DAYS` | 80–100 | Consecutive quarters for the TTM sum must be this far apart; otherwise the latest fiscal year is used ("annual, not TTM") |
| `HEALTH_CHECK_TTL_MINUTES` | 60 | A health-check result is reused this long at app start |
| `RISK_FREE_QUOTE_RANGE` | 0–20 | Sanity range for a quoted 10-year yield in percent (`^TNX` is quoted in percent, verified 2026-09) |
| `GRAHAM_MULTIPLIER` | 22.5 | Graham Number = √(22.5 × EPS × book value per share) |
| `STAGE1_SLACK` (how it applies) | hurdle × (1 − 0.25), ceiling × (1 + 0.25) | Stage 1 loosens what each metric is compared against: Graham ≥ price × (1 + `MIN_MARGIN_OF_SAFETY`) × 0.75; FCF yield ≥ (10-year + spread) × 0.75; net debt/EBITDA ≤ 3.0 × 1.25; negative info FCF → estimated runway ≥ 24 × 0.75 months |
| `MIN_ROE_SPREAD` | 0.0 | Sector-adjusted screen (banks, insurers, other financials): ROE − `COST_OF_CAPITAL` must be at least this |
| `MAX_P_TBV_BANK` / `MAX_P_B` | 1.5 / 1.5 | Sector-adjusted valuation slot: banks on price to tangible book, insurers and other financials on price to book. REITs use FFO yield vs the 10-year yield in the FCF slot and have no book slot |
| SBC not reported | raw FCF, labelled | When the cash-flow statement has no stock-based-comp row, the FCF yield uses raw FCF and is labelled "SBC not reported; unadjusted FCF yield" |
| `STATUTORY_TAX_RATE_FALLBACK` / `TAX_RATE_BOUNDS` | 0.21 / 0–50% | NOPAT for ROIC: effective tax rate clamped to the bounds; fallback rate (labelled) when the effective rate is n/m |
| `MIN_METRICS_FOR_PASS_WITH_EARNINGS_YIELD` | 4 | Replaces `MIN_METRICS_FOR_PASS` when `USE_EARNINGS_YIELD_IN_SCREEN` is on (4 of 5) |
| `BENEISH_COEFFICIENTS` | Beneish (1999) 8-variable model | M-score coefficients |
| `ALTMAN_COEFFICIENTS` | X1 6.56, X2 3.26, X3 6.72, X4 1.05 | Altman Z'' coefficients (zones in `ALTMAN_ZONES`) |
| `NNWC_RECEIVABLES_WEIGHT` / `NNWC_INVENTORY_WEIGHT` | 0.75 / 0.5 | Net-net working capital |
| `ASSET_COVERAGE_BANDS` | ≥100% fully covered, 50–100% partly, 20–50% thin, <20% negligible | Asset coverage band |
| `MONTHS_PER_YEAR` / `QUARTERS_PER_YEAR` | 12 / 4 | Unit constants for monthly runway burn and quarterly net-net burn |
| `SCREEN_PROGRESS_POLL_SECONDS` | 5 | Screener page progress refresh |
| `SCREEN_ETA_MIN_DONE` | 5 | Tickers finished before the Screener shows an estimated time left |
| `CHANGES_LIST_MAX` | 8 | Tickers named per "Changes since last screen" card before "+n more" |
| `HEATMAP_NEUTRAL_BAND` | 0.05 | Sensitivity heatmap: fair values within ±5% of the actual latest price are shaded neutral grey |
| `INSIDER_MARKER_SIZE_RANGE` | 8–22 px | Insider markers on the price panel, scaled by trade value |
| `ESTIMATE_SCORING_EDGE` | `p75` | A past turnaround estimate's window runs from its run date to run date + the upper end of its interquartile range (`median` is the alternative). Recovered = a close within `RECOVERY_BAND` of the episode's prior high inside the window; missed = the window ended first; not yet = still open |
| `ACCURACY_MIN_SCORED` | 10 | Below this many scored estimates, the Estimate accuracy page shows "Not enough scored estimates yet" |
| `APP_VERSION` | 0.6.0 | Shown in the export footer |
| `DCF_ADD_NET_CASH` | True | DCF bridge: fair value per share = (PV stage 1 + PV terminal + cash − debt) ÷ shares |
| `REVERSE_DCF_TOLERANCE` / `REVERSE_DCF_MAX_ITERATIONS` | 1e-6 / 200 | Reverse-DCF bisection stopping rule |
| Quant method for financials | Banks, insurers, other financials: fair P/B = ROE ÷ `COST_OF_CAPITAL` (zero-growth excess-return shortcut) × book value per share; the ROE spread replaces the ROIC adjustment. REITs: the same DCF on a TTM FFO base | Quant lens for sector-adjusted tickers (the reverse DCF and grid are n/m for the excess-return method) |
| `QUANT_CONFIDENCE_NORMAL` / `QUANT_CONFIDENCE_LOW` | "normal" / "low" | Quant confidence labels (low on peak-earnings normalisation or a Graham/DCF disagreement) |
| `ESTIMATE_REVISION_FLAT_BAND` / `ESTIMATE_REVISION_PERIODS` | 0.01 / `0y`, `+1y`, `0q` | EPS consensus now vs 90 days ago within ±1% → "flat"; eps_trend rows tried in order (context only) |
| `LLM_PRICE_PER_MTOK_IN` / `_OUT` | 2.0 / 10.0 | `claude-sonnet-5` pricing per million tokens (checked 2026-09); update with the model |
| `LLM_TEMPERATURE` / `LLM_NO_SAMPLING_MODEL_PREFIXES` | 0.0 / Sonnet 5, Opus 5, Opus 4.7/4.8, Fable, Mythos | Lowest temperature, sent only to models that accept sampling parameters (the listed ones reject `temperature`; the response cache keeps their scores stable) |
| `LLM_EFFORT` / `LLM_MAX_TOKENS` / `LLM_TIMEOUT_SECONDS` | "medium" / 16000 / 300 | LLM request settings |
| `LLM_VALIDATION_RETRIES` | 1 | One retry (with the validation error fed back) after a schema or evidence failure, then "Insufficient data" |
| `LLM_MIN_EVIDENCE_FACTS` | 2 | Moat and Devil's Advocate must cite at least 2 payload fields |
| `LLM_CACHE_DB_PATH` | `data/cache/llm_cache.db` | Permanent LLM response cache keyed by (ticker, lens, hash of model + payload, prompt version) |
| `LEVERAGE_TREND_FLAT_BAND_FINANCIALS` | 1.0 | Macro leverage trend for financials and REITs (liabilities ÷ equity): ±1.0x is flat |
| `DIVIDEND_EARNINGS_PAYOUT_MAX_FINANCIALS` | 1.0 | Dividend at risk for financials and REITs when earnings payout exceeds this (FCF payout is n/m for them) |
| `LLM_BACKEND` | `auto` | `auto`: the Anthropic API when `ANTHROPIC_API_KEY` is set, else the Claude Code CLI (`claude -p`, Claude subscription); `api` / `claude_code` force one; `none` disables the LLM lenses |
| `CLAUDE_CODE_CLI` / `CLAUDE_CODE_CLI_GLOBS` / `CLAUDE_CODE_TIMEOUT_SECONDS` | `.env` path, else `claude` on PATH, else the VS Code extension's bundled binary / 600 | Locating and running the CLI fallback; its calls log a billed cost of $0 plus the list-price equivalent |
| `INDUSTRY_THREAT_HINTS` / `SECTOR_THREAT_HINTS` | e.g. Apparel Retail → brand / private-label erosion; Software → AI disruption; Banks → regulation and rates | Moat prompt: the sector-threat hint (industry prefix first, then sector) |
| `ALERT_PIOTROSKI_DROP` | 2 | Alert when Piotroski falls this far below its baseline (the purchase snapshot for holdings, the first value seen for watchlist names); the baseline resets after an alert and rises with the score |
| `THESIS_TRIGGER_FIELDS` | price, thesis levels, the four lens scores and the aggregate, piotroski, altman_z / altman_zone, beneish_flag, net_debt_ebitda, interest_coverage, fcf_yield, cash_runway_months, margin_of_safety, dcf_fair_value / dcf_upside, share_trend, drawdown, leadership.flag, insider_cluster_buy, dividend_at_risk, stale | The only fields a sell trigger may use, each with its type (number, bool, enum with choices) and direction (colours "then vs now") |
| `THESIS_LEVEL_FIELDS` | target_price, buy_below_price, intrinsic_value | Thesis levels a numeric trigger can compare against (e.g. `price >= target_price`) |
| `TRIGGER_OPERATORS` / `TRIGGER_EQUALITY_OPERATORS` | `<`, `<=`, `>`, `>=`, `==`, `!=` / `==`, `!=` | Trigger operators; bool and enum fields take only the equality ones |
| `TRIGGER_NEAR_BAND` | 0.10 | Traffic light: a numeric trigger within 10% of its threshold (relative) is "near" (amber); a trigger whose value is N/A or n/m is amber too ("can't evaluate"), never a silent pass or fire |
| `THEN_VS_NOW_FIELDS` | lens scores, aggregate, Piotroski, Altman Z'', net debt/EBITDA, FCF yield, margin of safety, DCF fair value, leadership flag, dividend at risk, price | Rows of the Portfolio "then vs now" comparison |
| `COST_BASIS_METHOD` | `average` | Realised / unrealised gain on average cost (Canada's adjusted cost base rule); sells realise shares × (price − average cost) − fees |
| `ALERT_CHECK_MIN_INTERVAL_MINUTES` / `ALERT_CHECK_LAUNCH_GRACE_SECONDS` | 60 / 60 | The app-start alert check (a background process) runs at most this often; a just-launched check counts as running for the grace period before its process records its pid |
| `ALERT_INBOX_MAX` | 100 | Alerts listed in the Portfolio inbox, newest first |
| `ALERT_KINDS` | buy_below, target, earnings, leadership, insider_cluster, piotroski_drop, trigger, stale | Alert types and their labels |
| `SMTP_DEFAULT_PORT` / `SMTP_TIMEOUT_SECONDS` | 587 / 30 | Alert email over STARTTLS; off unless `SMTP_HOST` and `ALERT_EMAIL_TO` are set in `.env` |


## Data sources and their limits

- **yfinance:** daily prices, splits, ~4 years of annual and ~5 quarters of financials, `info` (sector, industry, business summary, currencies), `calendar` (next earnings date), `get_shares_full()` for share-count history (fall back to annual diluted shares), `^TNX` for the US 10-year Treasury yield (verify the scale when implementing).
- **Bank of Canada Valet API (free, no key):** the Government of Canada 10-year benchmark bond yield for CAD-priced stocks. Cache it with the price TTL.
- **Share-count history must be adjusted before any trend is computed:**
  - Adjust for splits and reverse splits using yfinance's split history, so a 1-for-10 reverse split is never read as a 90% buyback.
  - Cut the series at corporate-action breaks (same breaks as prices) and compute the trend only within the latest segment, stating its span.
  - A single-period change above `CORP_ACTION_SHARE_CHANGE` that isn't explained by a recorded split is flagged in the rationale, not scored as a buyback or dilution.
- **Known yfinance limits:** only ~4 years of fundamentals. The 5-year valuation median and valuation-based recovery timing need FMP. When FMP isn't configured, those features are switched off and labelled as such. The 3-5 year share-count trend uses whatever history exists and states the span.
- **SEC EDGAR (free):** filings and filing dates. Requests must send the `SEC_USER_AGENT` header from `.env` (name plus contact email) and respect `EDGAR_MAX_REQUESTS_PER_SECOND`.
- **Leadership-turnover flag — layered sources.** A CEO or CFO departure within `LEADERSHIP_LOOKBACK_MONTHS` raises the flag. Use every layer that applies to the ticker and merge the events (dedupe by person and month):
  1. **8-K Item 5.02** (US domestic filers): structured officer-change filings; full lookback history. Coverage label: `"8-K, full history"`.
  2. **6-K filings** (cross-listed Canadian companies filing 40-F/6-K, and foreign ADRs filing 20-F/6-K): free-form press releases, so two steps. First a cheap keyword filter over the last `LEADERSHIP_LOOKBACK_MONTHS` of 6-K text and exhibit descriptions using `LEADERSHIP_KEYWORDS`: a departure word ("resign", "retire" but not "retirement", "step down", "succession" but not "succession planning", "succeed", "departure") within `LEADERSHIP_KEYWORD_WINDOW_WORDS` words of a role ("Chief Executive", "Chief Financial", "CEO", "CFO"), or "interim" right next to one. Document-wide co-occurrence is not enough: quarterly-report 6-Ks mention the CEO, "interim" statements and "retirement" benefits on different pages. Then the LLM confirms only the keyword matches, returning structured JSON: `{departure: bool, role, person, effective_date}`. Cache by filing accession number. Coverage label: `"6-K, keyword + LLM check"`.
  3. **Officer snapshots** (every ticker, and the only automated source for TSX-only companies): save yfinance `companyOfficers` to the `officer_snapshots` table. The stage-1 `info` call already returns it, so every screen run snapshots every screened ticker at no extra cost; manual analyses snapshot too. `scripts/snapshot_officers.py` only covers tickers not in any screened list (e.g. watchlist names you haven't screened). A change in the CEO or CFO name between two snapshots is an event dated to the later snapshot. It only sees changes after the first snapshot. Coverage label: `"officer tracking since <first snapshot date>"`.
  4. **Manual events:** `/data/leadership_events.csv` (ticker, date, role, person, note, source) for departures the user reads about. Coverage label: `"manual"`.
  - **Never automate SEDAR+.** Its terms of use prohibit scraping and automated monitoring, and the site blocks bots. SEDAR+ is for the user to read by hand, feeding the manual file.
  - The flag result carries the event list, which layers were used, and the coverage window actually checked. If coverage is shorter than the lookback (e.g. officer tracking started 2 months ago), the result says `"partial coverage"` and is never reported as a clean "no turnover".
  - A ticker with no layers available returns `"N/A - no leadership data source"`.
- **Insider transactions:**
  - US filers: SEC Form 4 via the existing EDGAR client (structured XML). Count only open-market purchases (transaction code `P`) and sales (`S`); ignore grants, option exercises and other codes. Record whether a sale was under a pre-arranged 10b5-1 plan when the filing says so. Coverage label: `"Form 4, full history"`.
  - Everyone else (including TSX-only companies, whose insider filings are on SEDI, which, like SEDAR+, must never be automated): `/data/insider_events.csv` (ticker, date, insider, role, type buy/sell, shares, price, source) filled by hand. Coverage label: `"manual"`, or `"N/A - no insider data source"` when empty.
- **Ownership and sentiment context (yfinance `info`, often US-only, N/A otherwise):** `heldPercentInsiders`, `shortPercentOfFloat`.
- **Analyst estimates (yfinance analyst-estimate properties such as EPS trend and revisions; verify which exist in the pinned version):** forward EPS and revenue consensus and whether estimates were revised up or down over the last 30/90 days. Coverage is patchy, especially for small caps and TSX names; missing → N/A. Context only, never scored.
- **Dividends:** yfinance dividend history (long, split-adjusted) plus dividends paid from the cash-flow statement.
- **Debt maturities:** free sources don't give a reliable maturity schedule. Use the current vs long-term debt split as a proxy and say so. Never display "specific debt maturity dates" unless a real source (FMP) supplied them.
- **Corporate-action breaks:** yfinance records splits but not bankruptcy emergence. Detect breaks with a heuristic (single-day price move ≥ `CORP_ACTION_PRICE_GAP` plus a reported share-count change ≥ `CORP_ACTION_SHARE_CHANGE`) and merge with a manual override list in `/data/corporate_actions.csv` (ticker, date, type, note). Seed it with HTZ's 2021 Chapter 11 emergence; confirm the exact break date from the price series.
- **Screener universe:** one CSV per list in `/data/universe/`, each with columns `ticker, name, source, as_of`. The screener UI lets the user choose which lists to screen.
  - Pre-filtered value lists, built from ETF holdings files:
    - `cowz.csv` — Pacer US Cash Cows 100 (COWZ): top 100 Russell 1000 companies by FCF yield.
    - `cash_cows_small.csv` — Pacer US Small Cap Cash Cows ETF (CALF, confirmed 2026-09).
    - paceretfs.com blocks automated downloads (Cloudflare), so both Pacer lists come from the funds' **SEC N-PORT-P filings** via the EDGAR client (fund ticker → series via `company_tickers_mf.json`; each holding carries its ticker; only long common equity is kept). N-PORT holdings are public about 60 days after the period end, so these lists can lag a quarterly rebalance by up to ~5 months; `as_of` records the N-PORT period. A Pacer file saved by hand into `/data/universe/raw/` wins when it is newer.
  - Wider nets, from iShares holdings files:
    - `sp400.csv` and `sp600.csv` — US mid- and small-cap, from the SSGA SPDR SPMD and SPSM daily holdings files (same S&P indexes), with iShares IJH/IJR as fallbacks (the iShares US CSV links return an HTML gate as of 2026-09).
    - `tsx_composite.csv` (XIC) — about 220 Canadian stocks; yfinance tickers need the `.TO` suffix.
  - Hand-maintained lists:
    - `watchlist.csv` — the user's own tickers.
    - `dataroma.csv` — names copied by hand each quarter from Dataroma's superinvestor holdings. Dataroma has no official API; never scrape it. Holdings come from 13F filings, so they can be up to ~45 days stale and cover US long positions only; the `as_of` column records the quarter.
  - `scripts/refresh_universe.py` downloads the ETF holdings files, normalises tickers to yfinance format, drops cash, futures and other non-equity lines, and writes the CSVs.
    - The sources live in `config.py` (`UNIVERSE_SOURCES`), not in code, because issuers change them. Each list has an ordered list of sources, tried in turn.
    - If a download fails or its format changed, keep the previous CSV, print which list is stale and since when, and accept a manually downloaded file dropped into `/data/universe/raw/` instead.
  - When lists are combined, remove duplicate tickers but keep every source: a ticker in COWZ and Dataroma shows `source = "COWZ, Dataroma"`.
- **Caching:** all provider calls go through a disk cache (SQLite or parquet) and are throttled. `st.cache_data` sits on top for in-session speed only.
  - Prices: `CACHE_TTL_PRICES`.
  - Fundamentals and `info`: the earnings-driven rule in the table (refetch after the next earnings date passes, capped), with `info` also refreshed at least every `OFFICER_REFRESH_DAYS`.
  - Each cache entry records when it was fetched and why it expires, so the run summary can say how many tickers were refetched because they reported.

## Data source resilience

yfinance is unofficial: it reads Yahoo's website data and breaks when Yahoo changes things (renamed fields, empty statements, blanket request blocks). Design for the whole source failing, not just one ticker.

- **Field map.** Every yfinance `info` key and statement row label the app uses is defined once in `/data/field_map.py`, each with known alternative names. Code never reads a raw yfinance label directly. A label not found under any alias returns `"N/A - field not found: <name>"`, distinct from an ordinary missing value.
- **Health check.** `data/health.py` runs at app start and before every screen run. It fetches `CANARY_TICKERS` (prices, `info`, quarterly statements) and checks that every field in the field map resolves, and that values are sane (price > 0, market cap > 0, statements not empty).
  - On failure the screen does not start. The run is recorded as "blocked: health check failed" with the specific failures.
  - The app shows a banner: "Data source not responding correctly — try updating yfinance", listing what failed.
  - Cached data stays usable during an outage, shown with its age.
- **Circuit breaker.** During a run, if more than `CIRCUIT_BREAKER_FAIL_RATE` of the last `CIRCUIT_BREAKER_WINDOW` tickers failed to load, the run pauses and is marked "stopped: source failing". It can continue later with `--resume`.
- **Field N/A report.** After each run, count N/A per field. A field that is N/A for more than `FIELD_NA_SPIKE` of tickers is flagged "likely renamed upstream" in the run summary and the UI.
- **Pinned version.** `requirements.txt` pins the exact yfinance version. Upgrading is deliberate: `scripts/upgrade_yfinance.py` upgrades it, reruns the health check and `pytest`, and prints a pass/fail summary. The pin is updated only if both pass.
- **FMP fallback.** When `FMP_API_KEY` is set, a ticker that yfinance fails to load is retried through the FMP provider. Every value records which provider it came from, shown in rationales and exports.

## Screener execution

The universe is roughly 1,400 tickers, and a cold full fetch at the yfinance throttle takes hours. So:

- **Batch prices first.** Before stage 1, fetch latest prices for every ticker in the run with yfinance's multi-ticker batch download, in chunks (e.g. 100 tickers per call). Market cap is computed as latest shares outstanding × this price, so price-dependent numbers are fresh even when the `info` and fundamentals caches are not refetched. The batch price is the "actual latest price" from Rule 5.
- **Two stages.**
  - **Stage 1** uses only the yfinance `info` call (one call per ticker, usually served from cache under the earnings-driven rule), combined with the batch price: `trailingEps` and `bookValue` for a rough Graham Number, `freeCashflow` / computed market cap for an unadjusted FCF yield, and `ebitda`, `totalDebt` and `totalCash` for net debt/EBITDA.
    - Apply Rule 5 currency conversion to these fields first, and the Rule 2b sign checks.
    - Apply the screen thresholds loosened by `STAGE1_SLACK`.
    - Financials and REITs skip the stage-1 leverage test explicitly (EBITDA doesn't apply to them) and are judged on the other stage-1 tests only.
    - Tickers missing stage-1 fields go on to stage 2 rather than being cut on missing data.
    - Save the `companyOfficers` snapshot from the same call.
  - **Stage 2** fetches the full statements and share-count history only for stage-1 survivors, and computes the exact metrics (SBC-adjusted FCF yield, cash runway, share-count trend, sector-adjusted metrics).
  - The final status always comes from stage 2. Stage 1 only decides what gets fetched.
- **Screen status rule.** Each of the four screen metrics (margin of safety, FCF yield vs risk-free, leverage, share-count trend; or their sector-adjusted equivalents) is pass, fail, N/A or n/m.
  - **Pass:** every available metric passes and at least `MIN_METRICS_FOR_PASS` are available. An n/m counts as available and failing.
  - **Fail:** any available metric fails. Shown as "Fail (manual only)" in the UI, since any ticker can still be analysed manually.
  - **Incomplete:** nothing failed, but fewer than `MIN_METRICS_FOR_PASS` metrics were available. Never shown as Pass or Fail.
- **Runs happen outside Streamlit.** `scripts/run_screen.py --lists cowz,tsx_composite,...` does the work and writes to the `screen_runs` and `screen_results` tables in SQLite.
  - It is resumable: an interrupted run continues from the last completed ticker when restarted with `--resume`.
  - The Streamlit page never runs a full screen in the request. It reads the latest completed run, and its "Run new screen" button launches the script as a background process and polls the run's progress from the database.
- **Every run records its own gaps.** `screen_runs` stores:
  - start and end time, and which lists were used;
  - tickers attempted, passed stage 1, passed stage 2, and failed to load (with the reason).
  - Requests that yfinance refuses are retried with back-off (`FETCH_MAX_RETRIES`, `FETCH_BACKOFF_SECONDS`). Tickers still failing are listed as "failed to load" and are never counted as a screen "Fail".
- **Manual tickers skip all of this** and run on demand in the page, since they need only a handful of calls.

## Turnaround estimate integrity

- **Episode type.** Every drawdown episode (past and current) is classified against the listing country's benchmark over the same peak-to-trough window: `market-driven` if the benchmark fell by at least `MARKET_DRIVEN_RATIO` × the stock's drop, otherwise `company-specific`. The estimate uses past episodes of the same type as the current drop. With fewer than `MIN_EPISODES` of that type, it uses all episodes, says so, and lowers confidence one level.
- **Only when there's a drop.** If the current drawdown from the rolling 52-week high is smaller than `DRAWDOWN_THRESHOLD`, there is nothing to time: output "Not in a qualifying drawdown (currently −X% from 52-week high)", give no range, and still return the episode history for the chart.
- **Scoring past estimates.** For the History and Estimate accuracy views, only the first estimate made during each drawdown episode counts, measured from that analysis run's date. Later runs during the same episode are kept in history but not scored again, so one drop can't be counted many times.
- **Survivorship bias.** Every stock and peer the app can read is still trading, so its history only contains drops it survived; companies that fell and were delisted are missing. Free data can't fix this. Every turnaround output carries the caveat: "Based on companies still trading; ones that fell and were delisted aren't included, so real-world recovery odds are lower than this suggests." Unrecovered episodes are still counted, never dropped.

## Value-trap, valuation and ownership signals

Computed in `/signals`, deterministic, following Rules 2, 2b and 3b (N/A vs n/m, periods recorded). Financials and REITs get n/m for Piotroski, Altman, Beneish and EV/EBIT (the formulas aren't built for them). Formula coefficients and zone cut-offs below are the published ones as commonly cited; verify each against the original source when building and record the source in a code comment. Constants go in `config.py`.

**Value-trap scores** (need two consecutive fiscal years):
- **Piotroski F-score** (0–9), the nine standard checks: ROA > 0; operating cash flow > 0; ROA improved; operating cash flow > net income; long-term debt/assets fell; current ratio rose; no new shares issued (split-adjusted); gross margin rose; asset turnover rose.
  - A check without data is N/A. Report as "7 / 9 (9 checks available)". With fewer than `PIOTROSKI_MIN_CHECKS` (7) available, the score is "Insufficient data".
  - `PIOTROSKI_STRONG` = 7, `PIOTROSKI_WEAK` = 3.
- **Altman Z''-score** (the non-manufacturer version, usable across sectors): 6.56 × (working capital / total assets) + 3.26 × (retained earnings / total assets) + 6.72 × (EBIT / total assets) + 1.05 × (book equity / total liabilities). Zones (`ALTMAN_ZONES`): < 1.10 distress, 1.10–2.60 grey, > 2.60 safe.
- **Beneish M-score** (8-variable): −4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI + 0.115 DEPI − 0.172 SGAI + 4.679 TATA − 0.327 LVGI. Above `BENEISH_THRESHOLD` (−1.78) → "possible earnings manipulation" flag. A flag only, never a score input; it's a probabilistic model with real false positives, and the UI says so.

**Valuation extras:**
- **Reverse DCF.** Using the same DCF structure and assumptions as the Quant lens, solve (bisection) for the stage-1 growth rate that makes fair value equal the actual latest price. Report it next to the historical revenue CAGR: "Price implies −4%/yr growth; history shows +6%/yr". If the solution falls outside `REVERSE_DCF_SEARCH_RANGE` (−30% to +50%), report "beyond range" with the side. FCF-negative or unstable-base companies → n/m.
- **Sensitivity grid.** Fair value for `COST_OF_CAPITAL` ± 1 and ± 2 points × stage-1 growth ± 2.5 and ± 5 points (a 5 × 5 grid). The displayed fair-value range is the min–max of the central 3 × 3. The Quant score still uses the base case; the range sits beside it.
- **EV/EBIT earnings yield.** EV = market cap + total debt + preferred and minority interest where available − cash − short-term investments. Earnings yield = TTM EBIT / EV.
  - EBIT ≤ 0 → n/m.
  - EV ≤ 0 → n/m "net cash exceeds market cap", shown as a highlighted flag rather than an error, since that's often a deep-value signal worth a look.
  - Shown in the screener table and used for sorting. It becomes a screen metric only if `USE_EARNINGS_YIELD_IN_SCREEN` (default False) is on, with `MIN_EARNINGS_YIELD` (0.08); then raise `MIN_METRICS_FOR_PASS` to 4 of 5.
- **Peak-earnings check (cyclicals).** For tickers whose cyclicality score is 3 (sector or industry override): if TTM operating margin ≥ `PEAK_MARGIN_RATIO` (1.5) × the average operating margin of the available fiscal years, flag "possibly peak earnings". For flagged tickers the DCF base FCF is normalised to TTM revenue × the average FCF margin over the available years, and both the raw and normalised fair values are shown.

**Insider activity.** Trades are loaded over `INSIDER_FETCH_MONTHS` (12), so the turnaround can check the whole current drawdown for a cluster buy (its 52-week high is at most a year old). The summary below covers `INSIDER_LOOKBACK_MONTHS` (6): number of distinct insiders buying and selling, net shares and value, and a **cluster-buy flag** when at least `INSIDER_CLUSTER_MIN` (3) distinct insiders made open-market purchases within `INSIDER_CLUSTER_DAYS` (90). 10b5-1 plan sales are counted separately from discretionary sales. Coverage is always shown, like the leadership flag.

**Dividend safety** (dividend payers only; non-payers → N/A "no dividend"):
- Trailing dividend yield.
- FCF payout = dividends paid ÷ raw TTM FCF. Above `DIVIDEND_FCF_PAYOUT_MAX` (1.0), or FCF ≤ 0 while paying, → "dividend at risk".
- Earnings payout = dividends ÷ net income.
- Years of uninterrupted payments, and any cuts (a year-over-year drop in the annual per-share total of more than `DIVIDEND_CUT_THRESHOLD`, 10%) in the available history.
- Financials and REITs (FCF not meaningful, Rule 5): FCF payout is n/m; "dividend at risk" when the earnings payout exceeds `DIVIDEND_EARNINGS_PAYOUT_MAX_FINANCIALS` (1.0), or net income ≤ 0 while paying.

**Context fields (never scored):** insider ownership %, short interest % of float, analyst estimates and revision direction.

**Asset floor (downside signal, information only).** How much of the market cap is backed by net assets if the earnings case fails. These never change a score, the quality score or the screen status; they're shown and passed on.
- **Tangible book value (TBV)** = total shareholders' equity − goodwill − other intangible assets − preferred equity where reported. **P/TBV** = market cap ÷ TBV.
- **Net current asset value (NCAV)** = total current assets − total liabilities (all of them, including long-term debt) − preferred equity where reported.
- **Net-net working capital (NNWC)** = cash and short-term investments + `NNWC_RECEIVABLES_WEIGHT` (0.75) × receivables + `NNWC_INVENTORY_WEIGHT` (0.5) × inventory − total liabilities. A rough liquidation value.
- **Asset coverage** = TBV ÷ market cap, shown as a percentage, with a band from `ASSET_COVERAGE_BANDS`: ≥ 100% "fully covered", 50–100% "partly covered", 20–50% "thin", < 20% "negligible".
- **Net-net flag:** when NCAV ≥ market cap, highlight "trades below net current assets" (like the EV ≤ 0 flag: a signal worth a look, not a verdict). If the company is burning cash, add how long the discount lasts at the current burn: (NCAV − market cap) ÷ quarterly raw FCF burn, e.g. "discount gone in ~5 quarters at current burn".
- Sign and sector rules (Rule 2b style): TBV ≤ 0 → P/TBV and asset coverage n/m "negative tangible book", with coverage shown as "none". NCAV and NNWC may be negative and are shown as negative numbers, never n/m, since a negative value is the answer. Financials and REITs get NCAV and NNWC n/m (their balance sheets aren't split into current and non-current); banks keep P/TBV, which is already their sector-adjusted valuation metric.
- All inputs follow Rule 3b (latest quarter, with its date) and Rule 5 (currency conversion, actual latest price for market cap).

**Where the signals go:**
- Screener (stage 2): Piotroski, Altman zone, Beneish flag, EV/EBIT, P/TBV and asset coverage per ticker, plus the net-net flag when it fires. A **trap-risk flag** fires when F ≤ `PIOTROSKI_WEAK` or the Altman zone is distress. It is shown, not a Fail, unless `TRAP_RISK_FAILS_SCREEN` (default False) is on.
- Quant lens: Piotroski adjustment, reverse DCF, sensitivity range, peak-earnings normalisation (see Score mapping).
- Macro lens: Altman Z'' sub-score (see Score mapping).
- Devil's Advocate payload: all of the above plus insider activity, dividend safety, the asset floor (TBV, P/TBV, NCAV, NNWC, asset coverage and band, net-net flag) and the context fields, each with coverage or N/A reasons. The Devil's Advocate must say how much of the price the asset floor covers if the earnings case fails.
- Turnaround: an insider cluster buy during the current drawdown is listed as a near-term signal, next to the technical signals; it never overrides the structural-impairment rule. The output also states the asset floor as context (e.g. "Asset floor: 85% of the price covered by tangible book"); it doesn't change the range or the confidence.

## Portfolio and thesis tracking (Phase 6)

The app also has to be useful after a purchase, where long holds make "thesis drift" the main risk.

- **Holdings** (`holdings` table): ticker, account label (free text, e.g. "TFSA"), buy date, shares, price, currency, and sells as separate dated rows. Show position value, gain/loss, and return vs the ticker's `BENCHMARKS` index over the same period.
- **Thesis at purchase:** when a holding is added, save a full analysis snapshot (all lens scores, key metrics, signals, turnaround estimate) and a thesis record:
  - the reasons, as a short list of free-text points;
  - intrinsic value estimate, buy-below price and target price;
  - **sell triggers** as structured rules on named metric fields, e.g. `piotroski < 5`, `net_debt_ebitda > 3.5`, `leadership.flag == "high"`, `price >= target_price`, `dividend_at_risk == true`. Only fields in an allowed list (`THESIS_TRIGGER_FIELDS`) can be used, and rules are validated on save. No free-text rule evaluation.
- **Thesis check:** on every refresh, evaluate each trigger and compare current lens scores and key metrics with the purchase snapshot. Show which original reasons still hold (the user ticks them in the journal) and what has changed since purchase.
- **Journal:** dated free-text entries per holding, plus automatic entries when a trigger fires or an alert is raised. Exportable with the per-ticker report.
- **Alerts** are evaluated at the end of every scheduled screen run and on app start, for holdings and the watchlist:
  - price at or below a buy-below price, or at or above a target;
  - new earnings reported (a fundamentals refetch after an earnings date);
  - a new leadership departure or insider cluster buy;
  - Piotroski dropping by `ALERT_PIOTROSKI_DROP` (2) or more;
  - any sell trigger firing;
  - fundamentals going stale on a holding.
  - Each alert fires once per event. Conditions (price levels, triggers, cluster buy, stale) fire when they turn on and re-arm once they clear; a value that is N/A or n/m leaves them unchanged. Events (earnings, departures, Piotroski drops) carry a unique key. The first check of a ticker records the event baselines without alerting.
  - A check recomputes the deterministic lenses (Quant, Macro) only; Moat, Devil's Advocate and the aggregate come from the latest full analysis, labelled with its date.
  - Return vs benchmark is index-equivalent: each buy's cash buys index units on the same day and each sell sells the same fraction of them; both sides are price returns on actual closes (dividends excluded, labelled).
  Alerts go to an in-app inbox (unread count in the sidebar). Optional email via SMTP settings in `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO`), off unless configured. Each alert fires once per event, not on every run.

## Golden test tickers

Capture each once with `scripts/capture_fixtures.py` into `/tests/fixtures/` so tests run offline. Assert the branch taken, not live numbers.

| Ticker | Branch it must exercise |
|---|---|
| MELI | Fails the screener (`Fail (manual only)`) but the full pipeline runs when entered manually |
| HTZ | Corporate-action break detected; no drawdown, rolling high or share-count trend is computed across it; leadership flag evaluated from EDGAR |
| LCID | Structurally FCF-negative: cash runway replaces FCF yield and DCF; dilution risk flagged |
| LULU | Moat lens receives the sector-specific threat instruction (brand/private-label for retail), not a default AI-disruption framing |
| JPM | Financials branch: EBITDA-based metrics replaced by sector-adjusted ones |

Signal tests use hand-built two-year statement fixtures with known answers: a Piotroski case scoring exactly 9 and one with missing checks, Altman and Beneish against hand-computed values, JPM getting n/m on all three and on EV/EBIT, a reverse DCF that recovers a known growth rate, a synthetic cyclical at peak margins being flagged and normalised, a Form 4 fixture with a cluster buy (3 insiders, code P) plus grants and option exercises that must be ignored, a dividend payer with a cut, and asset-floor fixtures: a company with NCAV above market cap (net-net flag fires, with the burn-duration line when FCF-negative), one with heavy goodwill giving negative TBV (P/TBV n/m, coverage "none"), NNWC against hand-computed weights, and JPM with NCAV n/m but P/TBV kept.

Leadership-flag tests use saved filing fixtures and synthetic snapshots rather than a golden ticker: an 8-K Item 5.02 departure, a 6-K CEO-change press release (keyword hit, LLM mocked to confirm), a 6-K with a keyword but no departure (LLM mocked to reject), two officer snapshots with a changed CFO, a manual CSV event, and a ticker whose tracking is shorter than the lookback (must report partial coverage).

## Scoring conventions

- Every lens scores 1 (bearish) to 10 (bullish).
- Devil's Advocate score = how well the bull case survives the attack: 1 = the bear case wins decisively, 10 = the bull case holds up.
- Aggregate verdict = weighted mean of available lenses (`LENS_WEIGHTS`), shown with the count of lenses behind it and a label band: ≤3.9 bearish, 4.0–5.9 neutral, 6.0–7.4 lean bullish, ≥7.5 bullish (make the bands config constants too).
- Quality score (screener scatter y-axis) measures the business, not the price. It must not use anything divided by price or market cap (so no FCF yield and no margin of safety), or the scatter would plot price against price. Inputs, each mapped to 1–10 by breakpoints and averaged over those available (reported as "n of 4"):
  - ROIC minus `COST_OF_CAPITAL` (`QUALITY_ROIC_SPREAD_BREAKPOINTS`): −5 pts → 2, 0 → 5, +5 pts → 7, +15 pts → 10. Return on total assets substitutes when ROIC is n/m (Rule 2b).
  - FCF margin = SBC-adjusted FCF ÷ revenue (`QUALITY_FCF_MARGIN_BREAKPOINTS`): ≤ 0% → 1, 5% → 5, 10% → 7, ≥ 20% → 10.
  - Leverage: `NET_DEBT_EBITDA_BREAKPOINTS`, with the Rule 2b negative-EBITDA cases.
  - Share-count trend, split-adjusted (`QUALITY_SHARE_TREND_BREAKPOINTS`): +5%/yr → 1, +2%/yr → 4, 0% → 6, −3%/yr → 10.
  - Financials and REITs use ROE minus `COST_OF_CAPITAL` in place of ROIC, and drop FCF margin and leverage.

## Score mapping

Every lens turns its metrics into a score by a fixed, visible rule. Scores are clamped to 1–10 and shown to one decimal. All breakpoints below are config constants (`*_BREAKPOINTS` as lists of `(value, score)` pairs, interpolated linearly between points and flat beyond the ends). The defaults are starting points, not rules to defend.

**Rule: every rationale shows its mapping**, e.g. `"DCF upside +18% → 6.5; ROIC spread +6 pts → +1; score 7.5"`. A score whose working can't be shown is a bug.

### Quantitative Fundamental (deterministic)
- `QUANT_UPSIDE_BREAKPOINTS`: DCF implied upside → base score: −30% → 2, 0% → 5, +30% → 7.5, +60% → 10.
- `ROIC_SPREAD_BONUS`: +1 if ROIC exceeds `COST_OF_CAPITAL` by ≥ 5 percentage points (`ROIC_SPREAD_BONUS_THRESHOLD`); −1 if ROIC is below `COST_OF_CAPITAL`.
- `PIOTROSKI_ADJUSTMENT`: +0.5 if F ≥ `PIOTROSKI_STRONG`, −1 if F ≤ `PIOTROSKI_WEAK`, 0 otherwise or when Piotroski is "Insufficient data". Applied after the ROIC adjustment, and shown in the mapping line.
- Peak-earnings: for flagged cyclicals, the base score uses the normalised DCF (see Value-trap, valuation and ownership signals) and `confidence` is set to "low".
- The reverse DCF and sensitivity range are shown beside the score but don't change it.
- Graham Number cross-check: if it and the DCF disagree on direction (one says undervalued, the other overvalued), set `confidence = "low"` and say so. It never changes the score.
- FCF-negative names: `RUNWAY_BREAKPOINTS` replace the upside mapping: < 12 months → 1, 12–24 → 3, 24–36 → 4, > 36 → 5. `RUNWAY_SCORE_CAP` = 5: a cash-burning company can't score bullish on numbers alone. No ROIC bonus.

### Macro & Balance Sheet (deterministic)
Average of the sub-scores that have data (report which were used):
- `NET_DEBT_EBITDA_BREAKPOINTS`: net cash (≤ 0x) → 10, 1x → 8, 2x → 6, 3x → 4, ≥ 5x → 1. Applies only when EBITDA > 0; negative-EBITDA cases follow Rule 2b.
- `INTEREST_COVERAGE_BREAKPOINTS` (EBIT / interest expense): < 1.5x → 1, 3x → 4, 5x → 7, ≥ 8x → 10. No interest expense → 10 only when EBIT > 0; EBIT ≤ 0 → 1 (Rule 2b).
- `ALTMAN_BREAKPOINTS` (Z''-score → sub-score): ≤ 0 → 1, 1.1 → 3, 2.6 → 7, ≥ 4.0 → 10. n/m for financials and REITs.
- Leverage trend over the available years: falling → 8, flat (within ±0.3x) → 5, rising → 2 (`LEVERAGE_TREND_SCORES`).
- Cyclicality from `SECTOR_CYCLICALITY`, keyed on yfinance's own sector names (verify against real `info` data when building):
  - Defensive → 8: `Consumer Defensive`, `Utilities`, `Healthcare`.
  - Mixed → 5: `Technology`, `Communication Services`, `Industrials`, `Consumer Cyclical`, `Financial Services`, `Real Estate`.
  - Highly cyclical → 3: `Energy`, `Basic Materials`.
  - `INDUSTRY_CYCLICALITY_OVERRIDES` take precedence, keyed on yfinance industry names: e.g. `Airlines`, `Auto Manufacturers`, `Auto Parts`, `Travel Services`, `Lodging`, `Resorts & Casinos`, `Steel`, `Oil & Gas E&P` → 3.
  - A sector or industry not in either table → 5, and it is logged so the tables can be extended.
- Financials and REITs: the net debt/EBITDA and interest-coverage sub-scores are N/A; the lens uses trend and cyclicality only and is marked "reduced data". Their leverage trend is liabilities ÷ equity, with its own flat band `LEVERAGE_TREND_FLAT_BAND_FINANCIALS` (±1.0x), since bank balance sheets run near 10x.

### Business Moat (LLM)
The prompt includes a fixed rubric (`MOAT_RUBRIC`, versioned with the prompt):
- **2** — no pricing power; commoditised product or service; the main sector threat is already eroding margins or share.
- **5** — a real advantage (brand, switching costs, scale, network, cost position) that is narrowing or only partly defends against the main threat.
- **8** — a durable advantage that clearly holds up against the main threat, visible in stable or rising margins and returns.
Scores between anchors are allowed. The response must list at least 2 specific facts from the payload that justify the score; if it doesn't, reject and retry once, then return "Insufficient data".

### Devil's Advocate (LLM)
Score = how well the bull case survives the attack. Rubric (`DA_RUBRIC`, versioned):
- **2** — the attack finds a likely permanent impairment or a balance-sheet risk the bull case can't answer.
- **5** — real risks, but the bull case holds if one named assumption holds.
- **8** — the attack finds only minor risks, or risks the current price already reflects.
Same evidence rule: at least 2 specific facts from the payload.

### Calibration
- `tests/test_calibration.py`, marked `@pytest.mark.live` (calls the configured LLM backend: the API, or the Claude Code CLI without a key; run on demand, not in the phase checklist): scores the golden tickers with the current prompts and saves the results to `tests/calibration/<prompt_version>.json`.
- It builds every LLM payload from the saved offline fixtures, never live data. Otherwise a new quarter's numbers would show up as a score shift and be blamed on the prompt. Only the prompt may differ between calibration runs.
- When a Moat or Devil's Advocate prompt version changes, the test compares against the previous version's file and fails on any ticker whose score moved by more than `CALIBRATION_MAX_SHIFT` (2.0), listing each shift. A deliberate re-rating is accepted by committing the new file.

## Charts

- Plotly for all charts. One y-axis per chart; no dual-axis charts.
- A ticker keeps the same colour everywhere; sorting or filtering never repaints.
- Nothing is silently dropped from a chart: excluded items are listed in a caption with the reason.
- **Never index a series that can be zero or negative.** Indexing to 100 is only allowed for series that stay positive (prices, benchmarks). Profit, FCF and anything else that can be negative is plotted in its own units.
- **Fundamentals over time are small multiples, not one overlaid chart:** separate small panels for price, revenue, net income and total debt, stacked on a shared time axis, each on its own scale. Fundamentals are plotted at their quarterly period ends, not stretched to daily points.
- **Lens scores are a dot strip, not a radar.** One horizontal 1–10 scale per lens with a dot for its score, a vertical line at the aggregate, and the gap from the Devil's Advocate to the others' mean shaded when the controversy flag fires. A lens with insufficient data shows a hollow marker labelled with the reason, never a dot at zero. (Radar charts exaggerate differences by area and hide the gap that matters most.)
- **Peer strip:** for a ticker's key metrics (margin of safety, FCF yield vs risk-free, net debt/EBITDA, ROIC), one row per metric with the peers as grey dots and the ticker highlighted, with each peer labelled on hover. N/A and n/m peers are listed below the strip.
- **Asset floor panel:** horizontal bars on one shared scale for market cap, tangible book, NCAV and NNWC, with a vertical line at the market cap so it's obvious which asset measures reach it. Negative values extend left of zero. The asset coverage percentage and band sit above the bars, and the net-net flag (with the burn-duration line) below when it fires.
- **Sensitivity heatmap:** the 5 × 5 fair-value grid with discount rate on one axis and growth on the other, each cell showing fair value. Colour is diverging around the actual latest price (cells above price in one hue, below in the other, neutral grey near price), with the base case outlined and the reverse-DCF growth marked on the growth axis.
- **Trap-score panel:** three small meters (Piotroski 0–9, Altman Z'' with its three zones shaded, Beneish with the threshold marked), each labelled with its value, zone and "n of 9 checks" where relevant. The Beneish meter carries a one-line note that it's probabilistic.
- **Insider markers:** on the price panel of the small multiples, open-market buys as up-triangles and sales as down-triangles (10b5-1 sales hollow), sized by value, with the insider and amount on hover.
- **Dividend panel** (payers only): annual dividend per share as bars over the available history, cuts highlighted, with the FCF payout ratio as a separate small line chart below (never a second axis).
- **Portfolio view (Phase 6):** a holdings table (value, gain/loss, return vs benchmark, trigger status as a traffic light, unread alerts), and per holding a "then vs now" comparison: the purchase snapshot and today's values side by side for each lens score and key metric, with changes coloured by direction.
- **52-week range bar** in every per-ticker header: a thin bar from the 52-week low to the 52-week high (adjusted closes), a marker at the latest price, and the current drawdown from the high labelled (e.g. "−31% from high").
- **Exports** carry a footer on every page: price date, fundamentals as-of date, data providers used, app version, and "Personal research output, not investment advice."
