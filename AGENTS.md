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

## PERMANENT MODEL ESCALATION AND VERIFICATION POLICY — applies to EVERY Work task

These requirements apply to every current and future Work run on Stock Monitor / CandlePilot, regardless of which G-ID, GitHub Issue, PR or module is involved. They do not override any stricter safety rule in the canonical MASTER.

1. **Default model:** use Sol for routine implementation, code fixes, UI changes, GitHub work and tests when it is available and sufficient. Reserve Astra for genuinely complex debugging, architecture, difficult candle/data-quality problems and high-risk trading-engine changes. Model choice is a recommendation, not an assurance of quality or a guarantee of lower usage; do not silently change models or claim that Work can change its own model unless that capability is actually available.
2. **Escalate clearly:** if a task cannot be completed reliably, tests fail repeatedly, evidence is insufficient, or unresolved uncertainty could affect correctness or trading safety, explicitly tell the user which part remains blocked, show concrete errors/test evidence, and recommend Astra review where appropriate. Do not guess, silently skip checks or declare success on confidence alone. A model may fail to notice its own mistakes, so independent verification remains mandatory even when no escalation is suggested.
3. **Mandatory trading safeguards:** every modification to buy/sell rules, signals, execution, orders, risk limits, position sizing, stop-loss, data-quality gates or candle handling requires relevant automated tests, including negative/edge cases and regression tests. Do not mark a task DONE or enable live execution until the required tests and acceptance conditions pass with recorded evidence. If tests cannot run, mark BLOCKED/PARTIAL and explain why; never treat an untested change as validated.
4. **Work completion report:** identify model used (if known), code/commit references, tests actually run and results, remaining risks and whether Astra escalation is advised. Never fabricate results. Do not assume that merely changing models guarantees better code.
5. **Canonical tracking:** log any durable task-specific findings or acceptance evidence only in the existing MASTER registry as required above; do not create a parallel backlog. This AGENTS.md section is the standing instruction for how ALL tasks are executed, not a new task register.
