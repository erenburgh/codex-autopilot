# Migration from v0.8 to v0.9

v0.9 loads v0.8 plan, configuration, and run-state files additively. The
migration is deliberately fail-closed for concurrency.

## Plan

The v0.8 schema-2 `milestones` array becomes a schema-3 `tasks` graph. The first
task has no dependency and every later task depends on its immediate
predecessor. The result is semantically identical to the original serial order
and is a valid DAG.

On canonical save, v0.9 writes:

```json
{
  "schema_version": 3,
  "execution_strategy": "serial",
  "max_parallel_workers": 1,
  "computer_use_slots": 1,
  "compatibility": {
    "migrated_from_schema": 2,
    "legacy_serial": true
  }
}
```

The abbreviated object above shows migration fields only. The saved plan also
contains the goal, model strategy, synthesized legacy role, and complete tasks.
The compatibility validator rejects a migrated plan that claims parallel
execution. Moving such a project to a parallel graph requires an explicit
schema-3 plan change with a new graph version and full revalidation.

## Configuration

The new `[runtime]` section carries `execution_strategy`,
`max_parallel_workers`, `computer_use_slots`, and `worker_surface`. If the
section is absent, as it is in v0.8, the loader supplies `serial`, `1`, `1`, and
`headless_app_server`; this preserves the historical runner rather than
silently changing ownership semantics. User-facing `start-skill` with a saved
Desktop project explicitly writes `desktop_owned`.

## Run state

Schema-4 state loads as schema 5. Existing single-worker fields remain during
the additive transition, while the loader adds graph version, scheduler limits,
task states, attempts, revisions, active task IDs, logical READY-age sequences,
an empty lifecycle journal/session list, and per-task retry timestamps.
Completed `ROTATE`/`DONE` history becomes `VERIFIED`; an active
current milestone becomes the sole `RUNNING` task; rate-limit and blocked states
remain retry-waiting and blocked. State validation rejects more than one active
task under `serial`.

This in-memory migration is deterministic and non-destructive. The atomic state
writer persists schema 5 on the next ordinary save. v0.7 projects must first use
the existing v0.7-to-v0.8 conservative migration so their evidence and prose
provenance rules remain intact.

## Project Memory

There is no memory migration to another subsystem. v0.9 continues to use the
existing project-scoped SQLite/MCP store and its evidence requirements. Plan
context fields select bounded records; they do not duplicate them in plan or
run-state files.
