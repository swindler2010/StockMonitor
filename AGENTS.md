# Stock Monitor — canonical task entrypoint for Work / ChatGPT

Before accepting or implementing ANY Stock Monitor / CandlePilot task, read the current canonical master task registry from **main**:

- **Single authoritative file:** [docs/MASTER_TASK_REGISTRY_RU_2026-11-11.md](docs/MASTER_TASK_REGISTRY_RU_2026-11-11.md)
- **Full raw content:** https://raw.githubusercontent.com/swindler2010/StockMonitor/main/docs/MASTER_TASK_REGISTRY_RU_2026-11-11.md
- **GitHub API:** https://api.github.com/repos/swindler2010/StockMonitor/contents/docs/MASTER_TASK_REGISTRY_RU_2026-11-11.md

Only this MASTER is the task/requirements/status register. It contains complete 16-Issue / 713-comment text history, original agent rules and supplementary reports, historic PDF text, active PR descriptions, semantic alias map, and checked status limitations.

**MANDATORY workflow**
1. Read MASTER section **0** and the newest accepted update before acting. Choose exactly one G-ID and preserve every dependent acceptance condition (N/D/T/CP).
2. Inspect actual code, Git commits, tests and the live Windows TEST/Bridge. Historical PASS, a code change or CORE READY alone is not E2E DONE.
3. Update only the same MASTER for task progress, decisions, results, evidence, open blockers and version SHA. Never open or maintain another parallel GitHub Issue/backlog, independent task spreadsheet or MASTER file.
4. Existing GitHub Issues **#1–#16 are historical evidence only**, not the active backlog. Do not write new work there. Keep draft **PR #17 and PR #18**, working code, release/rollback data and branches intact.
5. Do not permit simultaneous Work1/Work2 edits of the canonical file or shared SQLite; use Git SHA conflict checks and refresh on conflict.

**SAFETY:** Do not overwrite Store 0.15, production/local SQLite, tokens, user data, live brokerage money or trading guards. Confirm GETTEX/EUR source/provenance and closed-bar DQ; Apple/NVIDIA first, Amazon next. No bulk preparation of other instruments. Before any deletion, confirm archive, uses, backup and recovery. Original detailed agent rules are preserved verbatim in MASTER section X.

This AGENTS.md is a **small discovery pointer**, not a second set of tasks.
