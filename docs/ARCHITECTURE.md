# Architecture

## Lifecycle

```text
User goal
  → initiating Codex task creates bootstrap plan
  → bounded App Server preflight fully exits
  → project initialization + launch request
  → trusted Stop hook atomically reserves READY frontier and causal provenance
  → exact causal predecessor Stop launches the local automatic dispatcher
  → dispatcher performs App Server thread/start at canonical cwd
  → dispatcher verifies metadata and performs production turn/start
  → dispatcher waits for authoritative completion
  → HANDOFF + new memory evidence + deterministic state transition
  → per-task App Server process fully exits
  → IMPLEMENTED + fresh independent verifier against user request/spec/every DoD
  → PASS, or REVISE/BLOCKED without dependency unlock
  → checkpoint validation + memory backup after independent PASS
  → dependencies/resources reconciled deterministically
  → the same local dispatcher adopts the exact reserved successor
  → JIT App Server process for newly READY work
  → ...
  → DONE
```

The initiating planner and production workers use model allowance. Preflight,
hooks, scheduling, resource/dependency decisions, Project Memory MCP, checkpoint
validation, incident classification, file rendering, and retry calculations are
deterministic local code. The transport actor is the one local dispatcher,
whose reservation remains bound to the exact causal predecessor. It consumes
the initiating user's durable authorization for the fixed run; no
per-transition confirmation is introduced. For an automatically owned turn,
the Stop hook is observational: the dispatcher alone consumes completion and
advances the run.

The initiating planning turn ends before its Stop continuation creates production work. Later workers may overlap only when the deterministic scheduler selects independent, resource-compatible READY tasks within `max_parallel_workers`. One resource-coordinator lock makes reservation, state transition, resource acquisition, launch identity, and descriptor persistence atomic.

## Desktop and App Server protocol boundaries

`desktop_owned` production uses an automatic local App Server dispatcher. It
creates a persistent thread at the canonical cwd, applies and reads back the
deterministic title and project metadata, starts the production turn, waits for
the authoritative completion event, closes that task's App Server process, and
continues with the exact scheduler-selected successor. Codex App task APIs and
model-mediated hook continuation are outside this transport.

Workers are persistent threads whose filesystem scope is canonical at
`thread/start`. App Server and Desktop project IDs are different namespaces;
App Server metadata alone is not proof of Desktop placement. Any asserted
Desktop visibility/editability requires an actual Desktop observation after
the worker process exits. Adaptive descriptors carry a
preflight-validated exact model and effort; Host Settings omits both. The App
Server `thread/unsubscribe` response is not an ownership release
acknowledgement: the official contract permits a last-subscriber thread to
remain loaded for 30 minutes, so it is never treated as a handoff primitive.
See [Desktop-owned runtime](DESKTOP_RUNTIME.md).

There is one execution surface: `desktop_owned`. The historical serial runner (`headless_app_server`, `HeadlessAppServerOrchestrator` and its `DesktopOrchestrator` alias) was removed in 0.8.1: it could not execute in the product, because `run`, `resume` and `_dispatch` all refused a `desktop_owned` run and every command defaulted to that surface.

## Why the Stop hook remains

`start-skill` knows the explicit target repository, but the initiating turn can
run elsewhere. It writes a short-lived per-user launch request and arms project
state. The Stop hook atomically claims that target and reserves its READY
frontier. Command hooks cannot call Codex App task APIs. The initial Stop hook
starts the dispatcher under the initiating user's durable run authorization;
later worker Stop hooks do not drive continuation. The operation, destination,
payload hash, and immutable causal owner are bound before either external side
effect. DevOps may repair and re-arm that dispatcher transition but cannot
create or message the destination.
See [Pipeline Engineer · On call](PIPELINE_ENGINEER.md).

UserPromptSubmit is used only for the exact pause, resume, status, and uninstall
phrases. The installer gives lifecycle hooks the stable `current/bin/codex-autopilot` command
rather than a plugin-cache path. Before each production reservation, the trusted
hook process uses App Server `hooks/list` to require the exact selected-plugin
Stop hook, enabled, error-free, and `trusted` or `managed`. Modified/untrusted
definitions require explicit `/hooks` review; every other mismatch fails closed.
The reservation is durable scheduling and causal evidence, not a permission
token; it cannot replace a real user-authority attestation or official platform
capability.

## Canonical state and views

| Item | Role |
| --- | --- |
| `.codex-autopilot/plan.json` | Canonical versioned task graph; schema and changes are validated before atomic write |
| `.codex-autopilot/run-state.json` | Canonical atomic lifecycle/resource journals, reservations, task states, attempts, active work, and embedded launch descriptors |
| `.codex-autopilot/pipeline-incidents.json` | Crash-safe incidents, recovery slot/lock state, bounded retries, healthchecks, and authority-bound transport journal |
| `.codex-autopilot/launches/*.json` | Derived relay views of authoritative embedded descriptors |
| `.codex-autopilot/memory.sqlite3` | Canonical project knowledge, evidence, verification outcomes, conflicts, and audit trail |
| `.codex-autopilot/memory-backups/latest.sqlite3` | Online backup after a verified milestone |
| `ROADMAP.md` | Human-readable execution view |
| `.codex-autopilot/MILESTONE.md` | Current worker cache |
| `.codex-autopilot/HANDOFF.md` | Adjacent advisory note, capped at 8 KiB |
| `.codex-autopilot/PROJECT_STATE.md` | Generated memory view |
| `.codex-autopilot/DECISIONS.md` | Generated decision view |

Workers may update only `HANDOFF.md` among the prose caches. The dispatcher renders `PROJECT_STATE.md` and `DECISIONS.md` from SQLite.

## v0.9 graph and state contracts

Plan schema 3 models tasks as an acyclic graph with explicit role profiles,
verification policies, resource claims, bounded context selectors, outputs,
capabilities, priority, and execution requirements. The validator rejects
unknown fields, missing references, self-dependencies, and cycles both on first
load and before a graph-versioned plan change. See [Task graph](TASK_GRAPH.md)
for the complete executable contract.

Mutable task status is not stored in the plan. Run-state schema 5 uses the
explicit lifecycle in `task_state.py`; in particular an implementer can only
finish at `IMPLEMENTED`, and any required verification must separately reach
`VERIFIED` before dependents are eligible. Resource declarations live in the
plan while lock ownership and its acquisition/release journal live in durable
run state. Ambiguous crash owners remain locked until worker state is
authoritative. See [Resource coordination and the shared working tree](RESOURCES.md).
Evidence and verified knowledge continue to live only in Project Memory.

Schema-2 v0.8 milestones load as a chain-shaped DAG. Missing v0.9 runtime
configuration defaults to `serial` with one worker and one Computer Use slot,
so migration never enables concurrency implicitly. See
[Migration from v0.8 to v0.9](MIGRATION_0.8_TO_0.9.md).

The deterministic scheduler computes READY state using the same
verification-aware dependency gate, records logical READY age in run state,
and admits a bounded frontier using stable resource, priority, critical-path,
fan-out, age, and declaration-order rules. Capability and Computer Use limits
are independent of model routing. See [Scheduler](SCHEDULER.md).

After implementation, verification policy is resolved by deterministic code.
Exhaustive declared checks can verify without another model; independent work
always receives a fresh Desktop verifier task and selective evidence IDs rather
than the implementer response or transcript. A structured `REVISE` verdict
creates a numbered fresh revision task and then a fresh verifier. See
[Verification and revision lifecycle](VERIFICATION_LIFECYCLE.md).

The audited candidate does not yet implement that policy split: it reserves a
fresh verifier after every successful implementation, including passing
deterministic checks and `self` policies. This is a release blocker, not an
alternative architecture; see `RELEASE_VERIFICATION_0.9.0-beta.md`.

## Fresh context with bounded memory

The stateless AI Studio Runtime rebuilds implementation, verification,
revision, planning, and replanning prompts from the same structured role/task
envelope. It retains no worker sessions and accepts no transcript or
conversation input. Every role field is behavioral metadata; task and verifier
capability continue to select the model independently.

Planner-selected Project Memory records and direct dependency outputs are
bounded twice: by the task contract and by hard runtime ceilings. Only verified
Truth, accepted Decisions, and active Constraints enter relevant state.
Dependency outputs are injected only after rechecking the dependency's own
verification gate; text excerpts, evidence selectors, and the complete prompt
all have deterministic size limits. Independent verifiers receive only the
current task, verifier role/capability, and structural selectors for evidence
from the immediately preceding implementation or revision. Revision workers
receive only the task contract and schema-validated issues. Full memory, past
worker responses, `HANDOFF.md` prose, old summaries, and concurrent
conversations are outside the API boundary. See
[AI Studio role and context runtime](AI_STUDIO_RUNTIME.md).

One validated BCP-47 language tag is persisted in `config.toml` when the run is initialized. Bootstrap views, reservation instructions, worker prompts, commentary, and final reports inherit it. Protocol tokens remain language-neutral and exact, so localization cannot change dispatcher parsing. Missing language metadata loads as `en` for v0.8 compatibility.

Before accepting `ROTATE` or `DONE`, the Desktop lifecycle requires a changed handoff and at least one new evidence item linked to that task. It then records milestone completion and advances dependencies once. A bare status marker cannot advance the plan.

Typed plan evolution adds no manager loop. A requester is recorded, current
workers drain, one fresh bounded replanner returns a full next-version graph,
and deterministic validation plus a crash-safe redo transaction update the
plan and run state. Pause persists a no-admission marker and drains rather than
cancelling existing workers. Resume performs graph-transaction and ownership
reconciliation before clearing that marker. Global rate-limit deadlines block
new admission without cancelling independent work. See
[Plan evolution and recovery](PLAN_EVOLUTION_AND_RECOVERY.md).

## Recovery

The lifecycle journals create/start/wait/interrupt/completion actions with a reservation token, operation identity, task, attempt, and the best available thread/turn identity. The App acknowledgement binds `thread_id`; the authoritative Stop/Interrupt hook binds `turn_id`. On restart, CREATE_REQUESTED, ACTIVE, and AMBIGUOUS reservations remain locked and are never recreated merely because time passed. A definite failure releases only its task and schedules a deterministic retry; independent active workers remain untouched.

Memory startup runs SQLite integrity and semantic invariant checks. Explicit snapshot/`BEGIN IMMEDIATE` transactions plus a bounded project-local lock coordinate simultaneous MCP processes and serialize rendering, backup, and restore with writers. Verification outcomes retain task/check/thread/turn provenance in a separate evidence-linked ledger and never become Truth by agreement. A corrupt database is quarantined and restored atomically from the latest verified milestone backup when available. If no valid backup exists, the run blocks instead of inventing state. Reboot recovery is manual through Resume.

Infrastructure incidents use the separate deterministic on-call state machine
`HEALTHY -> DEGRADED -> AUTO_RECOVERY -> RECOVERED`, or
`AUTO_RECOVERY_FAILED -> PIPELINE_ENGINEER -> RESOLVED/ESCALATE_TO_USER`.
Only affected tasks are withheld from scheduling. Recovery has its own lock,
slot, retry budget, and backoff, and no task resumes without a recorded passing
healthcheck. Unknown recovery ownership and unknown transport outcomes remain
locked for authoritative reconciliation. Production and policy failures are
outside Pipeline Engineer authority.
