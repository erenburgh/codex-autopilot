# Changelog

## 0.8.0-beta

- Added clean-machine preflight for target root, Git, installed runtime, official App Server, `:workspace`, target cwd, built-in Project Memory MCP, SQLite FTS5, and Adaptive model metadata.
- Added an explicit code-77 approval path for official App Server access to its exact `CODEX_HOME`, with no project run-state created on failure.
- Added a short-lived per-user launch registry so an initiating task can safely start Autopilot for a different target repository after `turn/completed`.
- Added project-local evidence-backed Project Memory using SQLite/FTS5 and a bundled stdio MCP server bound to each worker's target cwd. The public MCP surface is one user-approved `memory` tool with 14 strict operations.
- Separated Truth, Decisions, Constraints, Questions, Observations, Evidence, and Conflicts. Truth requires validated non-migration evidence.
- Added bounded retrieval, stable IDs, pagination, audit history, milestone evidence gates, integrity checks, online backups, and recovery from the latest verified milestone backup.
- Made `PROJECT_STATE.md` and `DECISIONS.md` generated views; made `HANDOFF.md` advisory and capped at 8 KiB.
- Added conservative v0.7 migration with a complete backup and zero automatic promotion of old agent prose to Truth.
- Preserved serial visible worker rotation, deterministic Sol/Astra routing, Host Settings omission, rate-limit waiting, and approval fail-closed behavior.
- Added an explicit first-use Project Memory trust probe. The user chooses persistent `Always` trust in Codex; production code never answers that approval.

## 0.7.0-beta

- Added deterministic AUTO routing between GPT-5.6 Sol and GPT-6 Astra, explicit execution modes, model metadata validation, and AUTO-only capability escalation.

## 0.6.0-beta

- Introduced the model-neutral App Server core, trusted lifecycle hooks, serial visible workers, deterministic controls, installer, and clean release package.
