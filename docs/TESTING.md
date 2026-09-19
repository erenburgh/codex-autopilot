# Testing

Run the deterministic unit suite from the repository root:

```text
PYTHONPATH=src python3 -m unittest discover -s tests
```

Add `-v` to print test names. On this tree the suite is 1019 tests and ends
`OK`.

## The acceptance floor

A canonical task cannot accept its own work, and it cannot declare a partial
run as its evidence. `validate_plan()` refuses any canonical task whose
`verification.policy` is not `independent`, whose `verification.required` is
not true, whose `max_revision_attempts` is below two, or which declares no
full-suite deterministic check (`src/codex_autopilot/plan.py`).

A check counts as the full suite only when it is a `command` expecting exit
code 0, launched through `env` with `CODEX_THREAD_ID`, `CODEX_TURN_ID` and
`CODEX_SESSION_ID` set to empty, and invoking a whole-suite runner directly
(`src/codex_autopilot/acceptance_floor.py`). A shell wrapper, an inline
`-c` expression, `unittest discover` outside a `tests` root, a pytest run
narrowed by `-k`, `-m`, `--last-failed` or one test file, and an unknown
runner whose name merely contains the word "suite" are all rejected.
Execution, not this validation, proves the outcome.

Deterministic checks are admission evidence for a fresh independent judge,
never acceptance by themselves (`src/codex_autopilot/acceptance.py`). The
only exception is a migrated v0.8 plan, and only for the tasks that already
existed in the run being migrated (`src/codex_autopilot/plan_legacy.py`).

## The installed copy carries the suite

`install.sh` copies `tests` next to `src` into the installed runtime, and
the release archive lists it among the items the user receives
(`scripts/build_release.py`). The on-call engineer needs it: a runtime
repair is applied only after the reproduction test fails on the current code,
passes with the patch, and the whole suite stays green in a staged copy
(`src/codex_autopilot/runtime_repair.py`). An installation that ships no
test suite refuses repairs instead of applying them.

## What this suite does not prove

Tests with fake App Server and Desktop clients prove request payloads and
fail-closed logic; they are not live UI evidence. `scripts/live_acceptance.py`
is developer-only and is excluded from the user archive
(`scripts/build_release.py`). A live multi-worker run, Desktop sidebar
title readback, Desktop project grouping, and Computer Use serialization
alongside code work are outside this suite.
