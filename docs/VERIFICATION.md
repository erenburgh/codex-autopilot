# Verification status

Development target: macOS 26.6.2 arm64, Codex CLI/App Server 0.153.4, ChatGPT Desktop 26.901.51231, Python 3.14.7, GPT-5.6 Sol, and GPT-6 Astra.

## Deterministic suite

The repository suite covers App Server request shape and serial compatibility plus the Desktop-owned JIT lifecycle: bounded parallel descriptors, atomic duplicate prevention, resource/dependency blocking, exact App Server `thread/start` and production `turn/start` in the canonical project, metadata attestation, automatic Stop-owned dispatch without chat relay, exact causal-owner enforcement, one stable known-failure incident, DevOps healthcheck plus predecessor-only re-arm, independent acceptance, one-worker retry isolation, ambiguous crash recovery, and process exit after completion. It also covers clean first run, permissions, Project Memory, migration, installation, contamination, and release invariants.

Run it with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Plugin manifests and skill directories are also checked with the official bundled plugin and skill validators. Release ZIPs are rebuilt deterministically and scanned for local paths, caches, logs, databases, virtual environments, test output, `.git`, and known legacy strings.

## Historical v0.8 headless App Server acceptance

The final local acceptance runs used the installed v0.8 runtime and the unified `memory` tool contract:

| Scenario | Result | Evidence |
| --- | --- | --- |
| Brand-new Git target and full preflight | PASS | Real App Server 0.153.4 verified `:workspace`, exact target cwd, SQLite 3.53.4 with FTS5, connected MCP/project binding, and model metadata. |
| Missing initiating-task access to `CODEX_HOME` | PASS | Real restricted run exited 77 with `Worker access: APPROVAL REQUIRED`, named the exact user `~/.codex` directory, and created no project state. |
| Sol-only, three milestones | PASS | Three fresh visible Sol tasks returned ROTATE, ROTATE, DONE; every worker used memory, no worker used Computer Use, and no turns overlapped. |
| AUTO mixed route | PASS | Three fresh visible tasks routed Sol/code → Astra/computer_use → Sol/code; tool logs show Computer Use only in the Astra worker and memory use in all three. |
| AUTO capability escalation | PASS | A Sol code worker returned REQUIRE_COMPUTER_USE and a fresh Astra worker retried the same milestone, used real browser Computer Use, and returned DONE. |
| Host Settings omission | PASS | `thread/start` omitted `model` and `turn/start` omitted `effort`; the tested App Server applied its current `gpt-6-astra` / `medium` defaults. |
| Real account rate limit | PARTIAL | App Server supplied an exact reset timestamp, the limited worker was retired, and the dispatcher automatically created a fresh worker for the same milestone after reset. The retry worker was manually interrupted, so end-to-end milestone completion was not part of this run. |

All those live workers had distinct durable thread IDs and the harness checked App Server event order. Desktop visibility/editability is a separate live acceptance observation and is never inferred from an App Server project ID.

The live acceptance harness is source-only. Its explicit developer flags can answer the bundled memory-tool elicitation for one App Server session and the exact browser test surface; production cannot do either.

## Deterministic acceptance results

The table below now distinguishes the original candidate's internal checks from
the independent M10 contract audit. A green implementation-authored assertion
does not override a contradictory source requirement.

| Scenario | Result |
| --- | --- |
| Initiating cwd differs from explicit target | PASS |
| Memory schema, FTS5, IDs, bounded pagination, and project isolation | PASS |
| No-evidence rejection and evidence-backed Truth | PASS |
| Decision/Constraint/Question/Observation lifecycles and user correction | PASS |
| Conflict history without destructive overwrite | PASS |
| Path traversal and symlink escape rejection | PASS |
| MCP one-tool/16-operation schema, protocol, binding, and restart | PASS |
| Corrupt memory quarantine and verified-backup recovery | PASS |
| Eight simultaneous MCP writers: evidence, observations, decisions, verification results, conflicts, unique IDs, links, and integrity | PASS |
| Concurrent rendering/backups, bounded busy waits, killed transaction rollback, and writer-behind-recovery serialization | PASS |
| Evidence-linked task/check/thread/turn verification ledger; idempotent causal replay and no agreement-to-Truth promotion | PASS |
| Conservative v0.7 migration | PASS |
| 20-milestone integrity and summary-drift resistance | PASS |
| Pause/resume, crash reconciliation, and rate-limit state machine | PASS |
| Release contamination and installer footprint | PASS |
| Two bounded parallel Desktop-owned Sol reservations with disjoint resources | PASS |
| Conflicting resources and unverified dependencies remain waiting | PASS |
| Atomic automatic-dispatch claims prevent duplicate task creation and duplicate production turns | PASS |
| App Server `thread/start` uses the canonical cwd/App Server project namespace and verifies returned metadata | PASS |
| Exact predecessor Stop → automatic App Server task creation → automatic production turn → process exit | PASS |
| Known create failure → one stable incident → DevOps healthcheck → exact predecessor dispatcher re-arm; DevOps never launches the destination itself | PASS |
| `self`, `deterministic`, `independent`, and `auto` implement their distinct acceptance/cost semantics | **Withdrawn — a canonical task is refused any policy but `independent` (`validate_plan`), so every task reserves a fresh verifier by design** |
| Authoritative Desktop Stop/Interrupt journal identities | PASS |
| One-worker rate limit preserves independent active work and deterministic retry | PASS |
| Desktop mode rejects model/chat relay; automatic App Server production remains causal and bounded | PASS |
| Typed prerequisite/dependency/resource/verification request parsing | PASS |
| Fresh replanner applies a dynamic prerequisite and rejects a cyclic graph without writes | PASS |
| Interrupted plan/run-state commit completes from its durable redo record | PASS |
| Multi-worker drain pause and reconcile-before-resume | PASS |
| Account rate barrier preserves independent active work and exact retry timing | PASS |
| Crash reconciliation retains unknown locks and retries absent owners idempotently | PASS |

The contract regressions that were red when the table above was written are
green in this tree: `PYTHONPATH=src python3 -m unittest discover -s tests` runs
1194 tests and ends `OK`. The single FAIL row was closed by dropping the
requirement it measured: a canonical task must now declare `independent`
verification, and a `deterministic` policy is rejected before execution
(`src/codex_autopilot/plan.py`). How to run the suite and what it does not
cover is in [TESTING.md](TESTING.md); the reproducible context benchmark is in
[CONTEXT_BENCHMARK.md](CONTEXT_BENCHMARK.md).

## Confirmed foundations from v0.7

v0.7 real App Server runs observed Sol-only three-worker rotation, mixed Sol/Astra rotation, and Sol-to-Astra capability escalation with distinct durable thread IDs and no overlapping model turns. Computer Use required the Astra task to be foreground and the test surface permission to be available. Production remains fail-closed on approvals.

## Current limits

- App Server is experimental.
- A complete multi-worker Desktop-owned live run remains for final acceptance; deterministic M4 tests exercise the exact Codex App launch payload and ownership boundary.
- Host Settings proves field omission; cross-build inheritance of another task's UI choice is unverified.
- Deterministic rate-limit recovery is tested. A v0.8 live run observed a real rate-limit error, an exact reset timestamp, automatic waiting, and a fresh retry of the same milestone. A complete multi-hour wait and weekly exhaustion remain unverified.
- Reboot recovery is automatic for a task waiting on a rate-limit retry: the wake-up launch agent sweeps the armed projects at login and arms the wake-up again (`install.sh`, `src/codex_autopilot/wake.py`). A run with nothing due, or one a human paused or stopped, is not woken and still needs an explicit Resume. The multi-hour wait itself remains unverified live.
- Transparent recovery of an MCP process inside the same active turn is not promised; a new worker starts a new server.
- A separate external clean Mac has not yet validated the packaged build. Local clean-state App Server probes and a real restricted permission-denial run cover the discovered failure path.
- App Server exposes the `Always` choice for MCP tool trust, but a human has not yet repeated the final packaged first-use flow on an external Mac. This remains an onboarding acceptance item.

The implementation is clean-room. No source code from Agent Memory Engine is copied, linked, or packaged; see [Project Memory](PROJECT_MEMORY.md).
