# Stock Monitor — instructions for development agents

Before starting any work:
1. Read the current GitHub Issues in this repository, particularly [master registry #13](https://github.com/swindler2010/StockMonitor/issues/13), and relevant issues for the active release. The 12 October 2026 PDF `Stock_Monitor_General_Plan_016_UPDATED_2026-10-12.pdf` is a historical baseline; do not treat its PASS/PARTIAL/OPEN statuses as verified current code status.
2. Inspect existing code, commits, and tests before making changes. Avoid duplicating work or rewriting functioning modules unnecessarily.
3. Prioritize v0.16 in this order: (a) finish and verify UI/MCP analytics hang fixes; (b) reliable EUR OHLCV candlesticks from verifiable exchange sources, current-session updates and all agreed timeframe history minima, with source and data-quality checks; (c) after data validation, implement safe paper trading with virtual funds, trade journal, risk controls, and no real orders. This advances the demo-trading scope originally listed under v0.17 issue #11; coordinate issue updates before declaring it implemented.
4. Complete MCP read access to the entire instrument watchlist independently of analytics readiness, chart history, quotes, and indicators. Implement safe UI controls with state readback.
5. For each issue, report what changed, tests run, results, blockers, and any regressions. Update the issue's status; close only after acceptance criteria pass. Do not label an issue complete solely because code was changed.
6. Do not alter the installed Store 0.15 fallback; develop and test v0.16-dev separately. Do not place secrets or account credentials in issues.
7. If requirements conflict or the data source cannot provide the requested candle depth/freshness, document the limitation and seek a decision rather than silently synthesizing or mixing incompatible data.

GitHub Issues are the living task tracker. This file is a standing workflow instruction, not evidence that any requirement is already implemented.
