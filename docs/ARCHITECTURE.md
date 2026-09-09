# Architecture

## Lifecycle

```text
User goal
  → initiating Codex task creates bootstrap plan
  → deterministic preflight
  → project initialization + launch request
  → trusted Stop hook sees initiating turn/completed
  → detached non-AI dispatcher
  → durable Worker 1 + model turn
  → turn/completed + HANDOFF + new memory evidence
  → checkpoint validation + memory backup
  → ROTATE
  → durable Worker 2
  → ...
  → DONE
```

Only initiating and worker turns use model allowance. Preflight, hooks, the dispatcher, Project Memory MCP, checkpoint validation, file rendering, retry timers, and process supervision are deterministic local code.

The initiating task and Worker 1 never overlap as Autopilot turns: the synchronous Stop hook launches the dispatcher only after Codex reports the initiating turn as completed, and the dispatcher independently reads that exact thread/turn before opening the Worker 1 gate. Every later worker starts only after the prior worker's `turn/completed` event and checkpoint validation. One project lock rejects a second dispatcher.

## App Server protocol

The dispatcher uses the official JSON-RPC App Server. It calls permission profile discovery, `model/list` for Adaptive, `thread/start`, `thread/name/set`, `turn/start`, `thread/read`, `turn/interrupt`, `project/list`, `mcpServerStatus/list`, `mcpServer/tool/call`, and `account/rateLimits/read`. It listens for `turn/started`, `turn/completed`, item events, and account rate-limit updates.

Workers are durable (`ephemeral: false`) visible Codex tasks with the target repository as `cwd` and `:workspace` as the permission profile. Adaptive adds a validated per-thread model and per-turn effort. Host Settings omits both. Because App Server 0.153.4 replaces a named thread-level MCP entry, the dispatcher repeats the bundled local stdio transport and sets `cwd=<target-root>`; it does not add another service or executable.

## Why the Stop hook remains

`start-skill` knows the explicit target repository, but the initiating turn can run elsewhere. It writes a short-lived per-user launch request outside the target and arms project state. Codex's Stop hook supplies the real initiating thread and turn IDs after completion. The hook atomically claims the matching request, starts one dispatcher, and passes those IDs. This closes the race between plan creation and Worker 1 while avoiding any controller model.

UserPromptSubmit is used only for the exact pause, resume, status, and uninstall phrases. Both hook commands require Codex's normal one-time trust review.

## Canonical state and views

| Item | Role |
| --- | --- |
| `.codex-autopilot/plan.json` | Canonical immutable plan for the run |
| `.codex-autopilot/run-state.json` | Canonical atomic orchestration journal and worker history |
| `.codex-autopilot/memory.sqlite3` | Canonical project knowledge, evidence, conflicts, and audit trail |
| `.codex-autopilot/memory-backups/latest.sqlite3` | Online backup after a verified milestone |
| `ROADMAP.md` | Human-readable execution view |
| `.codex-autopilot/MILESTONE.md` | Current worker cache |
| `.codex-autopilot/HANDOFF.md` | Adjacent advisory note, capped at 8 KiB |
| `.codex-autopilot/PROJECT_STATE.md` | Generated memory view |
| `.codex-autopilot/DECISIONS.md` | Generated decision view |

Workers may update only `HANDOFF.md` among the prose caches. The dispatcher renders `PROJECT_STATE.md` and `DECISIONS.md` from SQLite.

## Fresh context with bounded memory

Each worker prompt contains the current milestone, global goal, up to eight critical constraints, up to eight relevant memory IDs, and the short handoff. It does not copy the full memory, past transcripts, all completed milestones, or old summaries. Workers call the MCP tools to inspect selected records and evidence on demand.

Before accepting `ROTATE` or `DONE`, the dispatcher requires a changed handoff and at least one new evidence item linked to that milestone. It then records milestone completion, advances the roadmap once, renders views, and creates an online memory backup. A bare status marker cannot advance the plan.

## Recovery

The dispatcher stores thread/turn IDs and phases before and after each external action. On resume it reads durable App Server state and reconciles an existing completed, active, failed, or missing turn. Completed milestone records and the atomic journal prevent ordinary double advancement. An interrupted unfinished worker is retired and a fresh worker retries the same milestone.

Memory startup runs SQLite integrity and semantic invariant checks. A corrupt database is quarantined and restored from the latest verified milestone backup when available. If no valid backup exists, the run blocks instead of inventing state. Reboot recovery is manual through Resume.
