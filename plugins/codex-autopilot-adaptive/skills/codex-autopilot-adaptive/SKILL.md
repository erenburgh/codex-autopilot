---
name: codex-autopilot-adaptive
description: Run a long local Codex project as verified milestones in fresh visible workers. Use for requests to start, execute, pause, resume, inspect, or uninstall Codex Autopilot with deterministic Sol/Astra model routing, adaptive reasoning, and evidence-backed Project Memory.
---

# Codex Autopilot — Adaptive

Use this workflow only with an existing Git repository. The target repository may differ from the initiating task's working directory. The scheduler, dependency/resource decisions, reservations, journal, and recovery are deterministic and non-LLM. In `desktop_owned` mode one initially authorized Stop event launches the local dispatcher. As in v0.7, that dispatcher continuously performs `thread/start` → `turn/start` → `turn/completed` → next `thread/start`; each task uses a fresh bounded App Server subprocess which fully exits before successor adoption. The v0.8 scheduler, Project Memory MCP, resources, and independent-verification gates determine each successor. It never calls Codex App `create_thread` or `send_message_to_thread`, and neither hook feedback nor repeated hook trust drives continuation.

## Autopilot worker

When the prompt identifies this task as a Codex Autopilot Desktop-owned worker,
complete exactly its current task. Inspect the repository, evaluate every
Definition of Done item, perform real verification, update the short
`.codex-autopilot/HANDOFF.md`, and end with exactly one status line allowed by
the worker prompt. Do not manually create, fork, message, or start another Codex
task during production work. After the result, the already-running dispatcher
records the authoritative turn, advances deterministic state, reserves the next
phase, and creates the next task itself. A Stop hook may observe the same
completion, but it is not a continuation mechanism. The user's instruction to
run Autopilot is durable authorization for every scheduler-selected task in
that run; no transition asks the user to repeat it. If transport fails,
Pipeline Engineer may diagnose and repair the pipeline and re-arm this exact
causal owner's dispatcher, but DevOps must never perform destination
`thread/start` or `turn/start`. Never answer an unrelated approval,
change trust or permissions, expand scope, repeat an ambiguous side effect, or
operate Codex UI.

Project Memory is the canonical knowledge store. Use the built-in `memory` MCP tool and its allowlisted operations before relying on important historical claims. `HANDOFF.md`, model confidence, summaries, and agent prose are not evidence. Record hypotheses as Observations. Record Decisions and Constraints with explicit origin. A verified fact requires existing validated evidence: **NO EVIDENCE → NO TRUTH**. Before `ROTATE` or `DONE`, record new verification evidence linked to the current milestone. Do not edit `PROJECT_STATE.md` or `DECISIONS.md`; the dispatcher renders those human views from Project Memory.

Use Computer Use only when the worker prompt says `Effective execution mode: computer_use`. A code worker uses repository, shell, code, logs, and non-GUI tools even when the work is difficult. A computer-use worker performs the real GUI interaction required by the Definition of Done and never targets Codex itself.

In AUTO, a Sol code worker may discover that completion truly requires GUI interaction. Keep the milestone incomplete, update the handoff with the concrete GUI requirement, add exactly one `COMPUTER_USE_REASON: <specific reason>` line, and finish with `AUTOPILOT_STATUS: REQUIRE_COMPUTER_USE`. Complexity is not a valid reason.

## Start a run

Resolve the user's target Git repository explicitly, even when it is outside this task's current directory. Inspect it and the user's goal or `ROADMAP.md`. Select one model strategy for the run:

- `auto` for `Use Codex Autopilot for this project` and requests without an override.
- `sol-only` for `Use Codex Autopilot with Sol only for this project`.
- `astra-only` for `Use Codex Autopilot with Astra only for this project`.

Infer one BCP-47 response language from the initiating user's request (for example `ru` or `en`; use the user's explicit language preference when present). Write the goal, milestone titles, objectives, Definition of Done items, execution reasons, and every user-facing reservation/update in that language. Pass the same tag with `--language`; it is durable run metadata inherited by every implementation, verification, revision, planner, and replanner task. Protocol identifiers such as `AUTOPILOT_STATUS`, `AUTOPILOT_SLOT_READY`, file names, code, and tool names remain exact and are never translated.

Do not rely on the initiating task to expose or probe Project Memory. The `start-skill` command below creates a dedicated, visible preflight task and performs one harmless real model-to-MCP call with `operation=current` before it creates run-state or Worker 1. Autopilot never answers approval on the user's behalf, changes MCP approval configuration, or bypasses trust.

Create one independently verifiable outcome per milestone. Preserve the initiating
request verbatim in `user_request`. Assign every milestone a concrete structured
`RoleProfile` with a human-readable specialist name such as `Resilience Engineer`,
`DevOps`, or `UX Designer`, and store its role ID on the task. Never replace a
known specialist with `legacy-worker`, derive a role from task prose, or wait
until launch time to guess one. For every milestone ask whether its Definition
of Done requires Computer Use. Use `computer_use` only for required browser or
desktop GUI interaction that files, code, shell tools, or programmatic interfaces
cannot replace. Coding, architecture, debugging, networking, tests, Git, builds,
HTML/CSS, documentation, and file-based asset edits remain `code` regardless of
difficulty.

Assign reasoning independently: `medium` for routine execution, `high` for difficult implementation or debugging, `xhigh` for cross-system root-cause work, and `max` only for rare foundational architecture or research. Duration alone does not raise reasoning. Model choice never depends on reasoning complexity.

Write `<target-root>/.codex-autopilot/bootstrap-plan.json`:

```json
{"schema_version":3,"graph_version":1,"goal":"...","user_request":"<verbatim initiating user request>","model_strategy":"auto","execution_strategy":"serial","max_parallel_workers":1,"computer_use_slots":1,"roles":[{"id":"resilience-engineer","name":"Resilience Engineer","responsibilities":["Own recovery and resilience outcomes."]},{"id":"acceptance-reviewer","name":"Independent Acceptance Reviewer","responsibilities":["Judge results against the original user request and DoD."]}],"tasks":[{"id":"M1","title":"...","objective":"...","definition_of_done":["..."],"execution_mode":"code","execution_mode_reason":"Repository files and tests are sufficient.","reasoning":"medium","role":"resilience-engineer","depends_on":[],"priority":0,"verification":{"policy":"independent","required":true,"verifier_role":"acceptance-reviewer","max_revision_attempts":2},"resources":[],"required_capabilities":[],"context":{},"outputs":[],"tags":[]}]}
```

`execution_mode_reason` must state the concrete capability boundary. For `computer_use`, name the GUI application or browser interaction required.
The verifier must compare the result independently with `user_request`, the run
goal, the structured task contract, and every Definition of Done item. Tests
written by the implementation worker are evidence, not the source of acceptance
criteria.

Before invoking the helper, keep the Desktop project ID separate from the App
Server project ID resolved by preflight. Do not pre-create worker slots. The deterministic scheduler
atomically changes only the bounded READY frontier to RUNNING, acquires its
resources, writes one reservation token and launch descriptor per task, and
journals `create_requested` before any side effect. The initial authorized Stop
event launches the dispatcher once; the dispatcher
calls App Server `thread/start` with a persistent task, the
canonical cwd, canonical workspace root, App Server project ID, deterministic
title, and selected model, verifies returned ID/cwd/title/project metadata,
calls production `turn/start`, waits for completion, fully exits that task's
App Server subprocess, and continues to the next reserved task in the same
local loop. UI project
membership is accepted only after the task is actually exposed in Desktop; the two project
ID namespaces must never be compared as equal or substituted for one another.

Command hooks never call Codex App task APIs and never request a model
continuation. The dispatcher makes no planning, dependency, retry, resource, or
recovery decision. Its launch accepts only the exact causal predecessor recorded
on the reservation. An uncertain create or turn start is retained as `AMBIGUOUS` and is
never retried; authoritative reconciliation is required.

Every reservation records the exact causal Codex predecessor that produced it.
`relay_owner_thread_id` is immutable provenance. The already-authorized local
dispatcher may perform or retry transport only when its PID and reservation
match durable state. DevOps re-arms that exact dispatcher transition and never
becomes the destination transport actor. `CODEX_SESSION_ID`, caller-supplied
identities, unrelated tasks, and the destination worker thread are not accepted
as substitutes. Never reassign or impersonate causal ownership.

A definite App Server `thread/start` error before a returned thread is a known
failed create: it must match the claimed contract and causal owner, open or reuse
one stable `app_server_thread_start_failed` Pipeline Engineer incident, move the
destination to `RETRY_WAIT`, and stop without another create attempt. If a thread
ID was returned or the outcome is otherwise unknown, retain the bound task as
`AMBIGUOUS`; never create a replacement or fake a retry.

For a migrated serial run stranded after a definitive dispatcher failure, Pipeline Engineer recovery may re-arm the due retry without asking the user to type in the completed predecessor task. Recovery is allowed only when durable retry and journal evidence identify the exact owner, no work or dispatcher is active, and the retry deadline has passed. A non-empty owner written by the ownership-enforcing runtime remains authoritative; a legacy null owner requires adjacent completion-to-reservation journal proof from the direct verified predecessor. The recovered reservation remains owned by that causal predecessor across repeated retries and READY deferrals. Foreign tasks, manually supplied identities, ordinary READY work, missing retry provenance, and non-serial graphs fail closed.

If App Server creation or metadata attestation fails, preserve any known bound
task and escalate through Pipeline Engineer
recovery. Never use `thread/unsubscribe`, `thread/resume`, archive/unarchive, or
a no-op turn as an ownership-transfer protocol, and never create a replacement
for an ambiguous task.

Never claim that `thread/unsubscribe` immediately returns Desktop ownership. The official App Server contract says it only removes the current connection's subscription; after the last subscriber, the thread may remain loaded for a 30-minute inactivity grace period. The automatic dispatcher owns the App Server turn through completion and then fully exits. Desktop visibility/editability remains a separate live observation.

Resolve `scripts/codex-autopilot` relative to this `SKILL.md`, then run:

```text
scripts/codex-autopilot start-skill --project <target-root> --plan-file <target-root>/.codex-autopilot/bootstrap-plan.json --replace --language <BCP-47> --desktop-project-id <desktop-project-id>
```

This command performs preflight before it creates run-state. It checks the target, Git, installed runtime, official Codex App Server, `:workspace`, target cwd, reserved task readiness, App Server project metadata, the exact selected-plugin Stop hook through `hooks/list`, built-in Project Memory MCP, SQLite FTS5, a real model-to-MCP trust probe, and model metadata. The Stop hook must be unique, enabled, error-free, `trusted` or `managed`, and use the stable installed-runtime command. If it is `modified` or `untrusted`, stop before worker creation and ask for exactly one action: open `/hooks` and trust the current Autopilot Stop hook. Missing, disabled, duplicated, erroneous, unknown-status, or mismatched definitions fail closed. Never bypass or edit hook trust. Desktop project membership was already verified through the Codex app task listing; App Server cannot validate that external-ID namespace. App Server exposes no read-only API for the persistent per-tool choice. If the command reports `Project Memory MCP: APPROVAL REQUIRED`, show the user the exact diagnostic task title and thread ID and ask one explicit question: whether they approve `codex_autopilot_memory.memory` with Always. Explain that the bundled project-scoped tool has no shell or network access and covers all memory operations. Only after an explicit yes, repeat the same command with `--approve-project-memory-always`; this answers only the verified installed-plugin request that advertises `always` through App Server, then requires a second fresh task to call memory without another approval. Never infer approval from a general request, use the flag before confirmation, initialize or launch a worker after a decline, or edit trust state. If the approval instead names `CODEX_HOME`, request only normal read/write access to that exact App Server state directory. Never request global/full access.

Show the user the short task list and selected routes, then finish the initiating
turn. The trusted Stop hook claims the explicit target, atomically reserves only
the READY frontier, and launches the automatic App Server dispatcher under the
already granted run authorization. When Pipeline Engineer recovery is needed,
DevOps fixes the fault, records a passing healthcheck, and re-arms the same
causal owner's dispatcher; it must not perform destination `thread/start` or
`turn/start` itself.

If the target is not Git, state that this beta requires a Git repository and suggest `git init`; do not initialize it or make a commit without explicit user authorization.

## Controls

Exact pause, resume, status, and uninstall prompts are handled by the plugin hook without a model request. If control reaches this skill, run the bundled helper with `stop`, `status`, or `uninstall --yes`. Project state is removed only with explicit `--purge-project-state --project <root>`.

Never run the helper with `resume` on a `desktop_owned` run. Only the trusted
Stop hook may resume it, on the user's own command; the helper refuses a model
caller by design. Attempting it wastes the turn and reports a failure that is
not one. On a resume prompt your whole job is the reporting protocol below.

## Reporting a launch while it happens

After a resume is armed, the turn stays open while the dispatcher works. Do not
fill that time with reasoning about the pipeline. Report what is happening
instead, one line at a time, so the user watches progress rather than silence:

1. Run `scripts/codex-autopilot timeline --project <target-root>`, resolving
   `scripts/codex-autopilot` relative to this `SKILL.md` exactly as the start
   command above does. It is always there; never search the filesystem for it
   and never report its location — that hunt wastes the user's turn.
2. Print only the lines that are new since your previous run of it, verbatim.
3. Wait a few seconds and repeat. Stop at the first of these, whichever comes
   first — never later:
   - the ladder shows the task started;
   - a line marked `[✗]` appears;
   - the ladder came back unchanged twice in a row;
   - you have run the command five times.
4. Finish with one short line: started, or stopped at which step.

Five runs is a hard ceiling, not a target. An unchanged ladder means nothing is
happening and more polling will not change that: report the stall and stop.
Looping past this burns the user's usage for no new information.

The final visible line carries the verdict. If the ladder stopped, say so there,
naming the step — never end on progress counts while the stall sits in collapsed
reasoning. "10 of 11 verified, M11 active" is not a verdict when the launch did
not start; "остановилось на проверке видимости" is.

Rules for this reporting:

- Never invent a step, a checkmark, or a result. Print only what the command
  returned. The ladder is the observation; your summary is not.
- A `[✗]` line is not yours to fix. Repairing the pipeline is Pipeline Engineer
  work on a ticket, never an improvisation from this session.
- Never create or message a task to work around a stalled launch.

## Status protocol

- `ROTATE`: current milestone verified with recorded evidence; another milestone remains.
- `DONE`: the complete roadmap is verified with recorded evidence.
- `BLOCKED`: user input, an unavailable required model, or an approval is required.
- `ESCALATE`: retry the same incomplete milestone in a fresh worker on the same model at the next reasoning level. At `max`, the dispatcher records `BLOCKED`.
- `REQUIRE_COMPUTER_USE`: AUTO-only Sol→Astra capability escalation for the same incomplete milestone. It never advances the roadmap.
