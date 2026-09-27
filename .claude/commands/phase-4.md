---
description: Build phase 4 of the value stock analyzer: Turnaround timing
---
Phase 4: Turnaround timing.

Before planning, read CLAUDE.md (standing rules) and the docs/SPEC.md sections this phase touches. "The spec" below means those two files together. docs/BUILD_PROMPTS.md has the full six-phase plan for context. Plan first and wait for approval before writing code.

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
9. Output: a range naming the episode type it's based on, such as "8-20 months, based on 3 past company-specific drops", the current drop's type, the survivorship-bias caveat from the spec (always shown), a confidence level (High/Medium/Low) with the rule that produced it written in config (sample size plus the structural flag), catalysts, and active technical signals. Never a single date.

Return the episode list (start, trough, recovery dates) as structured data so Phase 5 can shade them on a chart.

UI: show the estimate and the episode list as a plain section.

Tests: an insider cluster buy inside the current drawdown appears as a near-term signal and one before the drawdown doesn't, a stock 8% off its high returns "Not in a qualifying drawdown" with no range but with its episode history, a stock 30% off returns a range, episode classification on synthetic series (stock and benchmark fall together → market-driven; stock falls alone → company-specific; exactly at MARKET_DRIVEN_RATIO), the estimate uses only same-type episodes, the fallback to all types lowers confidence and says so, Canadian tickers use the Canadian benchmark, the survivorship caveat is always present, no episode or rolling high crosses the HTZ break, unrecovered episodes are counted, the peer fallback triggers below MIN_EPISODES, a structural flag withholds or downgrades the estimate, technical signals on synthetic price series, and valuation-based recovery is labelled unavailable without FMP.

Finish with the phase-completion checklist.
