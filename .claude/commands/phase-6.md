---
description: Build phase 6 of the value stock analyzer: Portfolio, thesis journal and alerts
---
Phase 6: Portfolio, thesis journal and alerts.

Before planning, read CLAUDE.md (standing rules) and the docs/SPEC.md sections this phase touches. "The spec" below means those two files together. docs/BUILD_PROMPTS.md has the full six-phase plan for context. Plan first and wait for approval before writing code.

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
