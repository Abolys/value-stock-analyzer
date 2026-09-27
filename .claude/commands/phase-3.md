---
description: Build phase 3 of the value stock analyzer: Four-lens analysis engine
---
Phase 3: Four-lens analysis engine.

Before planning, read CLAUDE.md (standing rules) and the docs/SPEC.md sections this phase touches. "The spec" below means those two files together. docs/BUILD_PROMPTS.md has the full six-phase plan for context. Plan first and wait for approval before writing code.

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
