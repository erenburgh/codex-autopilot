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

## Onboarding: what the user must hear before the first worker starts

The first run is the only moment the user is looking. Before the first worker
is created - after preflight, in the same initiating turn - tell them the
following, in the run language, as a short block they can come back to. Do not
paraphrase it into three lines and do not pad it into a lecture; every point
below answers a question users actually asked.

1. **Two decisions are Codex's, not Autopilot's.** Trust the Autopilot **Stop**
   hook once in Codex `/hooks`, and answer **Always** when the preflight task
   asks to approve the bundled `memory` tool. Both are asked before the first
   milestone, never in the middle of one. Autopilot never answers an approval
   for the user, never changes trust and never edits global Codex settings. If
   Codex keeps asking for approvals during the run, the user can switch that
   project to the approval mode that lets Codex act for them - that is a Codex
   setting the user changes, and it is worth saying so here.
2. **How to look.** Tasks of the run appear in the Desktop sidebar only after
   the hook answers, so the way to see them is to ask: `статус` / `status` for
   a short card, `подробный статус` / `detailed status` for the full report,
   `задачи` / `tasks` for the same card when the word that comes to mind is
   "tasks". `останови` / `stop` pauses after the current turn; `продолжи` /
   `resume` continues; `удали Codex Autopilot` / `uninstall Codex Autopilot`
   removes the plugin and keeps the project - removal needs the full name.
3. **What a running task means.** While a milestone runs, its thread is held by
   Autopilot until the task ends with a status line; do not type into it. Thread
   titles say who is working and on what: `<Role> | <task id> | <phase>`, with
   `Verifier` and `Verify` for the independent check. When a task is finished
   or rejected, that thread is ordinary: the user may open it and correct the
   result by hand, and the next verification judges the corrected state.
4. **The plan is not frozen.** New work that was not in the plan is added as a
   plan change, not by editing files in `.codex-autopilot/`: ask for it in a
   fresh task, and the planner records the change with its provenance. Never
   hand-edit `plan.json`, `run-state.json` or `pipeline-incidents.json`.
5. **What happens when something breaks.** A rejected result is revised by a
   fresh worker and, when revisions run out, re-hired one effort step up; only
   an exhausted ladder stops a task, and then the run says so with the issues
   named. An infrastructure fault - a dispatcher that died, a thread that
   drifted, a defect in Autopilot's own code - becomes a ticket for the
   on-call Pipeline Engineer, which repairs it, proves the repair with tests,
   records what it did and returns the task to work. A run that ran out of
   Codex limits resumes by itself when the window resets. The user is asked
   only for the things listed in point 1 and for decisions that are genuinely
   theirs: an unknown side effect on the Codex side, a product or architecture
   choice, a dangerous permission.
6. **Where to read more.** `GETTING_STARTED.md` in the installed runtime, and
   `docs/` next to it, hold the same explanations at length.

## Start a run

The target is the Codex project you are working in. Resolve it as that project's own root, and take the Desktop project id from the same place: the two always belong together, because every created task is placed in that project and verified there.

A different directory is accepted only when it lies inside some Codex project's roots, and then that project's id is the one to pass. A path that belongs to no Codex project cannot be a target: the run would have no project to place its tasks in, and the user would see nothing. Say so immediately, in one sentence, naming the path and the fix - create a Codex project for that directory, or work in the project you already have. Never start a run that will fail later for this reason, and never ask the user to add the project by hand mid-run. Inspect it and the user's goal or `ROADMAP.md`. Select one model strategy for the run:

- `auto` for `Use Codex Autopilot for this project` and requests without an override.
- `sol-only` for `Use Codex Autopilot with Sol only for this project`.
- `astra-only` for `Use Codex Autopilot with Astra only for this project`.

Infer one BCP-47 response language from the initiating user's request (for example `ru` or `en`; use the user's explicit language preference when present). Write the goal, milestone titles, objectives, Definition of Done items, execution reasons, and every user-facing reservation/update in that language. Pass the same tag with `--language`; it is durable run metadata inherited by every implementation, verification, revision, planner, and replanner task. Protocol identifiers such as `AUTOPILOT_STATUS`, `AUTOPILOT_SLOT_READY`, file names, code, and tool names remain exact and are never translated. Role names are the exception and stay in English always, whatever the run language is: `Resilience Engineer`, `DevOps`, `UX Designer`, `Release Engineer`. A role is a profession, and the whole environment names professions in English; the thread-title format also appends the English words `Verifier` and `Verify`, so a translated role produces a half-translated title like `Инженер основания Verifier | M1 | Verify ...`. Task titles, objectives, DoD items and every user-facing line keep the run language.

Do not rely on the initiating task to expose or probe Project Memory. The `start-skill` command below creates a dedicated, visible preflight task and performs one harmless real model-to-MCP call with `operation=current` before it creates run-state or Worker 1. Autopilot never answers approval on the user's behalf, changes MCP approval configuration, or bypasses trust.

Create one independently verifiable outcome per milestone. Preserve the initiating
request verbatim in `user_request`. Assign every milestone a concrete structured
`RoleProfile` with a human-readable English specialist name such as
`Resilience Engineer`, `DevOps`, or `UX Designer`, and store its role ID on the
task. The name is English even when the run language is not. Never replace a
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
{"schema_version":3,"graph_version":1,"goal":"...","user_request":"<verbatim initiating user request>","model_strategy":"auto","execution_strategy":"auto","max_parallel_workers":10,"computer_use_slots":1,"roles":[{"id":"resilience-engineer","name":"Resilience Engineer","responsibilities":["Own recovery and resilience outcomes."]},{"id":"acceptance-reviewer","name":"Independent Acceptance Reviewer","responsibilities":["Judge results against the original user request and DoD."]}],"tasks":[{"id":"M1","title":"...","objective":"...","definition_of_done":["..."],"execution_mode":"code","execution_mode_reason":"Repository files and tests are sufficient.","reasoning":"medium","role":"resilience-engineer","depends_on":[],"priority":0,"verification":{"policy":"independent","required":true,"verifier_role":"acceptance-reviewer","max_revision_attempts":2},"resources":[],"required_capabilities":[],"context":{},"outputs":[],"tags":[]}]}
```

`depends_on` is the real dependency, not the order in which the tasks were
written down. Two tasks that can be done without each other's result are
declared as siblings on the same predecessor, and the scheduler then runs them
side by side. Chaining independent work into one line hides parallelism that the
run was authorized to use. `execution_strategy` stays `auto` and
`max_parallel_workers` is the user's decision, not a template number. If the user named a number, use exactly it. If they did not, preflight prints a `Ёмкость:` line describing their own account - unlimited, credits, or plan tier - and you must show that line to the user and let them answer before the first worker starts. On an unlimited account there is no ceiling at all: as many tasks run at once as the graph opens. Otherwise the default is `10` unless the user asked for serial. Two Astra workers must never run at once: they share one Computer Use surface, take control from each other and burn limits. That limit is held by `computer_use_slots`, not by this number, so raising this number is safe.
execution or the plan was migrated from v0.8; a migrated plan carries
`legacy_serial` and remains serial with one worker.

Sibling tasks that write to the same files are not parallel: the resource lock
serializes them. Declare `resources` honestly - `write` for what the task
changes, `read` for what it only consults - and split the work so that siblings
own disjoint files. When a shared entry point is unavoidable, give the
foundation task the job of making it extensible, so the later tasks add their
own files instead of editing a common one.

`execution_mode_reason` must state the concrete capability boundary. For `computer_use`, name the GUI application or browser interaction required.
When a task's result talks to something outside the repository - another
application's API, a network service, a database, a CLI that speaks a protocol -
its Definition of Done must carry at least one item verified against that real
system, not only against a double. A fake answers whatever the test author
expected: the handshake, the capabilities a method requires, the exact field
names, and the shape of an error are proved only by the real thing.

Measured: `codex-thread-tools` shipped with 28 green tests on a fake transport
and a passing independent verifier, and its `projects` command failed on the
first live call - `project/list requires experimentalApi capability`. The
capability was never declared in the handshake. No fake could have caught it,
and the verifier could not either, because the contract did not ask for it.

Keep that live item as small and as safe as it can be: read-only whenever
reading proves the point, the narrowest scope that exercises the protocol, never
a destructive call, and never a write to the user's real data unless the user's
request is itself about writing. If the real system cannot be reached from the
task's environment, say so in the Definition of Done as an explicit gap instead
of replacing it with another double.

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

Resolve `scripts/codex-autopilot` relative to this `SKILL.md`, then run it as direct argv - the program and its flags as separate arguments, with no `sh -lc`/`zsh -lc` wrapper and no quoting of the whole line into one string. The installer registers this exact program in Codex execpolicy, and execpolicy matches argv tokens: wrapped in a shell the whole command becomes a single opaque token, matches nothing, and Codex raises an approval that the dispatcher will never answer. Run:

```text
scripts/codex-autopilot start-skill --project <target-root> --plan-file <target-root>/.codex-autopilot/bootstrap-plan.json --replace --language <BCP-47> --desktop-project-id <desktop-project-id>
```

This command performs preflight before it creates run-state. It checks the target, Git, installed runtime, official Codex App Server, `:workspace`, target cwd, reserved task readiness, App Server project metadata, the exact selected-plugin Stop hook through `hooks/list`, built-in Project Memory MCP, SQLite FTS5, a real model-to-MCP trust probe, and model metadata. The Stop hook must be unique, enabled, error-free, `trusted` or `managed`, and use the stable installed-runtime command. If it is `modified` or `untrusted`, stop before worker creation and ask for exactly one action: open `/hooks` and trust the current Autopilot Stop hook. Missing, disabled, duplicated, erroneous, unknown-status, or mismatched definitions fail closed. Never bypass or edit hook trust. Desktop project membership was already verified through the Codex app task listing; App Server cannot validate that external-ID namespace. App Server exposes no read-only API for the persistent per-tool choice. If the command reports `Project Memory MCP: APPROVAL REQUIRED`, show the user the exact diagnostic task title and thread ID and ask one explicit question: whether they approve `codex_autopilot_memory.memory` with Always. Explain that the bundled project-scoped tool has no shell or network access and covers all memory operations. Only after an explicit yes, repeat the same command with `--approve-project-memory-always`; this answers only the verified installed-plugin request that advertises `always` through App Server, then requires a second fresh task to call memory without another approval. Never infer approval from a general request, use the flag before confirmation, initialize or launch a worker after a decline, or edit trust state. Never attach a permission request to `start-skill` and never re-run it to obtain access. The dispatcher refuses every approval it observes, so a command approval raised this way can only deadlock: it waits in a task the user is not looking at while the initiating turn shows nothing. A non-zero exit is a finished answer, not a permissions hint - print its last lines as the visible verdict and stop. `CODEX_HOME` access is requested only when preflight itself printed `Worker access: APPROVAL REQUIRED`, and then it is the user who grants that exact directory; a handshake timeout, a missing file, or any other failure is never answered with a permission request.

Show the user the short task list and selected routes, tell them the status
phrase, then finish the initiating turn. The trusted Stop hook claims the
explicit target, atomically reserves only the READY frontier, and launches the
automatic App Server dispatcher under the already granted run authorization.
When an incident reaches `PIPELINE_ENGINEER`, follow the procedure in
"Pipeline Engineer: the actual procedure" below.

If the target is not Git, state that this beta requires a Git repository and suggest `git init`; do not initialize it or make a commit without explicit user authorization.

## Saved-project root drift

Autopilot never changes a saved Codex project on its own. When the canonical
directory is not inside any root of the configured project, the create fails
closed and names the exact authorization it needs. The user, and only the user,
grants it:

```text
scripts/codex-autopilot authorize-project-root --project <target-root> --yes
```

`--revoke` withdraws it later. The authorization names that one project and that
one root; it does not carry to another. Never run it on the user's behalf and
never infer it from a general request to continue.

## Controls

Exact pause, resume, status, and uninstall prompts are handled by the plugin hook without a model request. If control reaches this skill, run the bundled helper with `stop`, `status`, or `uninstall --yes`. Project state is removed only with explicit `--purge-project-state --project <root>`.

Never run the helper with `resume` on a `desktop_owned` run. Only the trusted
Stop hook may resume it, on the user's own command; the helper refuses a model
caller by design. Attempting it wastes the turn and reports a failure that is
not one. On a resume prompt, arm the resume and end the turn; see below.

## Reporting a launch

Finish the initiating turn as soon as the plan is shown. The dispatcher waits
for that turn to reach `completed` before it creates the worker task: it polls
the owner thread and creates nothing until the turn ends. Holding the turn open
to watch the launch therefore prevents the very launch being watched. Measured:
the turn streamed the ladder, the dispatcher read the owner thread 554 times in
two minutes, and no task was ever created.

**The launch report is not visible, and that is the accepted cost.** The Stop
hook must answer `continue`; a blocking answer leaves the initiating turn
`interrupted` forever and the dispatcher never starts. Only a blocking answer is
displayed, so the report it carries reaches no one. Do not claim the user can
see it.

Therefore the last visible line of the initiating turn must tell the user how to
look. Name the phrase:

> Запуск взведён. Чтобы увидеть ход дела, спроси `статус` или `задачи` — ответит хук, коротко и сразу; `подробный статус` даёт полный отчёт.

The status phrase runs on `UserPromptSubmit`, which is outside the causal chain
and may block safely - that is why its output is visible when the Stop hook's is
not.

`scripts/codex-autopilot timeline --project <target-root>` stays available for a
*later* turn, when the user asks what happened. Resolve it relative to this
`SKILL.md` exactly as the start command does; never search the filesystem for it
and never run it inside the initiating turn.

Rules for any such report:

- Never invent a step, a checkmark, or a result. Print only what the command
  returned. The ladder is the observation; your summary is not.
- The final visible line carries the verdict. If the ladder stopped, say so
  there, naming the step - never end on progress counts while the stall sits in
  collapsed reasoning. "10 of 11 verified, M11 active" is not a verdict when the
  launch did not start; "остановилось на проверке видимости" is.
- A `[✗]` line is not yours to fix. Repairing the pipeline is Pipeline Engineer
  work, by the procedure below, never an improvisation from this session.
- A verifier rejection is not a stall and not yours to report as one. The task
  is revised by a fresh worker carrying the verifier's structured issues, and
  when that worker's revision budget runs out the task is re-hired one step up
  the effort ladder with the plan and the Definition of Done untouched. Only an
  exhausted ladder stops a task, and then the run says so with the issues named.
  See `docs/REHIRING.md`. Never change a plan, a DoD, or an attempt budget to
  make a rejected task pass.
- Never create or message a task to work around a stalled launch.

## Pipeline Engineer

An incident routed to `PIPELINE_ENGINEER` reserves one on-call engineer and
creates it as a visible worker task in the project, exactly like any other
worker. Nothing about this lane is manual any more, and nothing in it waits for
the user by default.

The engineer holds full authority to repair the pipeline on the user's behalf.
The user does not choose the repair (R13). It never performs destination
`thread/start` or `turn/start` itself: it repairs the fault and re-arms the
causal predecessor, so that predecessor performs its own reserved transport
under the already granted run authorization.

It is reserved before any other work — a broken pipeline outranks new tasks —
and it deliberately holds none of the affected task's resources: those may still
be held by the session that failed, and the repairer must not be blocked by the
thing it came to repair.

Its tools are the helper commands, resolved relative to this `SKILL.md`:
`relay-status`, `relay-complete`, `relay-fail --failure-code <kind> --definitive`,
`devops-rearm-relay-owner`, `arm`, and `devops-resolve-incident`.

Three rules bind it:

- **Ask the server before deciding.** Run state records what Autopilot believed;
  App Server records what occurred. They differ exactly when a dispatcher died
  mid-flight.
- **An unknown side effect is a stop.** Never replace an `AMBIGUOUS` task and
  never guess. That is the one case where standing still is correct.
- **Close the ticket with evidence.** `RESOLVED` counts only when the ticket is
  actually closed through `devops-resolve-incident` with a passing healthcheck.
  The word in the final line is a claim, not an observation.

It finishes with exactly one line: `PIPELINE_ENGINEER_STATUS: RESOLVED`, or —
only when repair is genuinely outside its authority — `ESCALATE_TO_USER` with
one code from the closed list: `DANGEROUS_PERMISSION`, `GLOBAL_CONFIG_CHANGE`,
`PROJECT_DAMAGE_RISK`, `RECOVERY_EXHAUSTED`, `PRODUCT_DECISION`,
`ARCHITECTURE_DECISION`. A bare escalation is refused.

## Status protocol

- `ROTATE`: current milestone verified with recorded evidence; another milestone remains.
- `DONE`: the complete roadmap is verified with recorded evidence.
- `BLOCKED`: user input, an unavailable required model, or an approval is required.
- `ESCALATE`: retry the same incomplete milestone in a fresh worker on the same model at the next reasoning level. At `max`, the dispatcher records `BLOCKED`.
- `REQUIRE_COMPUTER_USE`: AUTO-only Sol→Astra capability escalation for the same incomplete milestone. It never advances the roadmap.
