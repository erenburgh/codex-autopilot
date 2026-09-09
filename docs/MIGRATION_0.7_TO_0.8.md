# Migration from v0.7 to v0.8

Starting v0.8 with `--replace` in a project whose `run-state.json` uses v0.7 schema 3 triggers an automatic conservative migration.

Before changing state, the migrator copies the complete old `.codex-autopilot` contents to:

```text
.codex-autopilot/migrations/v0.7-to-v0.8-<timestamp>/backup/
```

It writes `MIGRATION_REPORT.md` beside that backup. Old durable worker history with `ROTATE` or `DONE` becomes migration evidence and milestone completion history. Only an identical ordered prefix of milestone IDs, titles, and objectives is skipped in the new plan. Changed or reordered work resumes from the first mismatch.

Old `DECISIONS.md` lines become agent-origin proposed Decisions because original user provenance cannot be proven. Old `HANDOFF.md` and `PROJECT_STATE.md` become low-confidence Observations. None of that prose is promoted to Truth. Migration evidence is deliberately ineligible to support Truth.

The old backup is not deleted automatically. Installing v0.8 also preserves the installed v0.7 runtime directory. Uninstalling v0.8 does not remove either one.
