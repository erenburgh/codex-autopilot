# Verification status

Development target: macOS 26.6.2 arm64, Codex CLI/App Server 0.153.4, ChatGPT Desktop 26.901.51231, Python 3.14.7, GPT-5.6 Sol, and GPT-6 Astra.

## Deterministic suite

The repository suite covers App Server request shape, serial lifecycle and completion gates, launch from outside the target cwd, clean first run, explicit permission denial with no state creation, missing MCP, Adaptive and Host Settings preflight, SQLite/FTS5 records, evidence and conflicts, no-evidence rejection, path/symlink isolation, pagination, MCP protocol and restart, backup/recovery, 20-milestone memory behavior, bounded prompt growth, v0.7 migration, installer/reinstall, contamination rules, and release invariants.

Run it with:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Plugin manifests and skill directories are also checked with the official bundled plugin and skill validators. Release ZIPs are rebuilt deterministically and scanned for local paths, caches, logs, databases, virtual environments, test output, `.git`, and known legacy strings.

## v0.8 live acceptance results

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

All live workers had distinct durable thread IDs. The test harness checked the App Server event order and found no overlapping active turns. The browser scenario required the Astra task to be foreground so Codex could execute the visible Computer Use call.

The live acceptance harness is source-only. Its explicit developer flags can answer the bundled memory-tool elicitation for one App Server session and the exact browser test surface; production cannot do either.

## Deterministic acceptance results

| Scenario | Result |
| --- | --- |
| Initiating cwd differs from explicit target | PASS |
| Memory schema, FTS5, IDs, bounded pagination, and project isolation | PASS |
| No-evidence rejection and evidence-backed Truth | PASS |
| Decision/Constraint/Question/Observation lifecycles and user correction | PASS |
| Conflict history without destructive overwrite | PASS |
| Path traversal and symlink escape rejection | PASS |
| MCP one-tool/14-operation schema, protocol, binding, and restart | PASS |
| Corrupt memory quarantine and verified-backup recovery | PASS |
| Conservative v0.7 migration | PASS |
| 20-milestone integrity and summary-drift resistance | PASS |
| Pause/resume, crash reconciliation, and rate-limit state machine | PASS |
| Release contamination and installer footprint | PASS |

The final repository suite contains 75 passing tests. The reproducible context benchmark is in [CONTEXT_BENCHMARK.md](CONTEXT_BENCHMARK.md).

## Confirmed foundations from v0.7

v0.7 real App Server runs observed Sol-only three-worker rotation, mixed Sol/Astra rotation, and Sol-to-Astra capability escalation with distinct durable thread IDs and no overlapping model turns. Computer Use required the Astra task to be foreground and the test surface permission to be available. Production remains fail-closed on approvals.

## Current limits

- App Server is experimental.
- Saved Project placement is not live-verified.
- Host Settings proves field omission; cross-build inheritance of another task's UI choice is unverified.
- Deterministic rate-limit recovery is tested. A v0.8 live run observed a real rate-limit error, an exact reset timestamp, automatic waiting, and a fresh retry of the same milestone. A complete multi-hour wait and weekly exhaustion remain unverified.
- Reboot recovery requires explicit Resume.
- Transparent recovery of an MCP process inside the same active turn is not promised; a new worker starts a new server.
- A separate external clean Mac has not yet validated the v0.8 package. Local clean-state App Server probes and a real restricted permission-denial run cover the discovered failure path.
- App Server exposes the `Always` choice for MCP tool trust, but a human has not yet repeated the final packaged first-use flow on an external Mac. This remains an onboarding acceptance item.

The implementation is clean-room. No source code from Agent Memory Engine is copied, linked, or packaged; see [Project Memory](PROJECT_MEMORY.md).
