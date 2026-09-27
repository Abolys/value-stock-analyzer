# Value Stock Analyzer — Claude Code build kit

A personal research app: screens for value stocks, runs a four-lens analysis (Quant, Macro & balance sheet, Moat, Devil's Advocate) with value-trap signals, estimates a historical turnaround window, and tracks holdings against your written thesis. Built with Claude Code, one phase at a time.

## What's in the kit

| Path | What it is |
|---|---|
| `CLAUDE.md` | Standing rules. Claude Code loads it every session. |
| `docs/SPEC.md` | The detailed specification: thresholds, data sources, scoring, signals, charts, tests. |
| `docs/BUILD_PROMPTS.md` | The six build phases, with setup and after-build notes. |
| `docs/ui-mockup.html` | The visual target. Open it in a browser. Fictional data. |
| `.claude/commands/phase-1.md` … `phase-6.md` | Each phase as a slash command: `/phase-1` … `/phase-6`. |
| `.gitignore`, `.env.example` | Starters. `.env` is ignored so your keys never get committed. |

## Start

1. Unzip into an empty folder and run `git init`.
2. `cp .env.example .env`, then fill in `ANTHROPIC_API_KEY` and `SEC_USER_AGENT` (your name and email; the SEC requires it). FMP and SMTP are optional. The Anthropic key is billed through the Claude Console, separately from your Claude subscription.
3. Run `claude` in the folder.
4. Switch to plan mode (Shift+Tab), type `/phase-1`, review the plan, approve.
5. When Phase 1 passes its checklist and commits, start a fresh session and run `/phase-2`. Continue through `/phase-6`.

Each phase ends with the same checklist: tests pass, the test tickers (MELI, HTZ, LCID, LULU, JPM) hit their branches, the app starts, and a commit. If a phase breaks something, roll back to the previous phase's commit.

## Things to confirm while building

Flagged in the spec as "verify when building":

- the small-cap Cash Cows ETF ticker (believed to be CALF);
- the Bank of Canada Valet series code for the 10-year yield;
- yfinance's exact sector and industry names;
- the Piotroski, Altman Z'' and Beneish coefficients against their original sources;
- which analyst-estimate properties exist in the pinned yfinance version;
- current Anthropic API prices for the cost log.

## After the build

See "After the build" in `docs/BUILD_PROMPTS.md`: the weekly screen schedule, monthly universe refresh, the yfinance upgrade script, calibration after prompt changes, and the manual CSVs (Dataroma, leadership events, insider events).

Personal research tool, not investment advice.
