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
2. `cp .env.example .env`, then fill in `ANTHROPIC_API_KEY` and `SEC_USER_AGENT` (your name and email; the SEC requires it). SMTP is optional. The Anthropic key is billed through the Claude Console, separately from your Claude subscription.
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

### Portfolio and alerts

- Add a holding from a ticker's Stock page with **Add to portfolio**. The analysis on screen is frozen as the purchase snapshot, together with your reasons, levels and sell triggers.
- Alerts for holdings and the watchlist are checked at three points:
  - at the end of every completed `scripts/run_screen.py` run;
  - on app start, as a background process at most every `ALERT_CHECK_MIN_INTERVAL_MINUTES`;
  - on demand, with `python scripts/check_alerts.py` or **Check alerts now** on the Portfolio page.
- To have the weekly screen check alerts too, schedule it with cron. For example, every Sunday at 02:00:

  ```
  0 2 * * 0  cd /path/to/value-stock-analyzer && .venv/bin/python scripts/run_screen.py --lists cowz,cash_cows_small,sp400,sp600,tsx_composite,watchlist,dataroma
  ```

- Canadian coverage: after refreshing the universe, run `python scripts/map_sec_ciks.py` so cross-listed TSX names get their SEC history (valuation-based recovery, debt maturities, 6-K leadership check). Review the `auto:` rows it writes to `data/sec_cik_overrides.csv`; add by hand any company it misses because it is named differently on each exchange.
- Email is off unless `SMTP_HOST` and `ALERT_EMAIL_TO` are set in `.env`. `SMTP_PORT` defaults to 587 (STARTTLS). `SMTP_USER` and `SMTP_PASSWORD` are used when set, and `ALERT_EMAIL_FROM` defaults to `SMTP_USER`. For Gmail, use an app password.


### Sharing the app for feedback

`python scripts/share.py` prints a public https link to your running app (a free Cloudflare quick tunnel; the app itself still listens on localhost only). Visitors must sign in:

- **viewer password** (`APP_VIEWER_PASSWORD` in `.env`, generated on first run): give this to your friend. Everything can be browsed, including your portfolio, but nothing is edited or saved, no screen or alert check starts, and the Moat / Devil's Advocate lenses show cached answers only (no calls on your API key or Claude subscription).
- **owner password** (`APP_OWNER_PASSWORD`): full access from anywhere.

Your friend can leave comments with **💬 Feedback on the app** in the sidebar; you'll find them under **📥 Feedback received**. The link lasts while the script runs (Ctrl-C closes it) and changes each time.

Personal research tool, not investment advice.
