---
name: codex-autopilot-host-settings
description: Run a long local Codex project as verified milestones in fresh visible workers with evidence-backed Project Memory while leaving model and reasoning selection to host defaults.
---

# Codex Autopilot — Host Settings

Use this workflow only with an existing Git repository. The target repository may differ from the initiating task's working directory. Scheduling, reservations, journaling, and recovery are deterministic. In `desktop_owned` mode the initially authorized Stop hook launches one local dispatcher. That dispatcher creates a persistent task, starts and waits for its production turn through App Server at the canonical cwd, fully exits the task's App Server subprocess, and continues with the deterministic successor. Codex App task APIs and model-mediated hook continuation are not used. This profile performs no Sol/Astra routing and sends neither a model nor a reasoning override.

## Autopilot worker

When the prompt identifies this task as a Codex Autopilot Desktop-owned worker,
complete exactly its current task, verify it, update
`.codex-autopilot/HANDOFF.md`, and end with exactly one allowed status line. Do
not manually create, start, or message another task during production work. The
already-running dispatcher records the authoritative Desktop turn, reserves the
next phase, and launches it in the same local loop. The Stop hook is only an
observer for an automatically owned production turn. The user's instruction to run
Autopilot is durable authorization for every scheduler-selected task in that
run. Pipeline Engineer may repair transport and re-arm that exact causal
owner's dispatcher but must never perform destination `thread/start` or
`turn/start`. Never answer an unrelated
approval, expand scope, or operate Codex UI.

A definite App Server `thread/start` error before a returned thread is a known
failed create: it must match the claimed contract and causal owner, open or reuse
one stable `app_server_thread_start_failed` Pipeline Engineer incident, move the
destination to `RETRY_WAIT`, and stop without another create attempt. If a thread
ID was returned or the outcome is otherwise unknown, retain the bound task as
`AMBIGUOUS`; never create a replacement or fake a retry.

Project Memory is the canonical knowledge store. Use the built-in `memory` MCP tool and its allowlisted operations before relying on important historical claims. `HANDOFF.md`, model confidence, summaries, and agent prose are not evidence. Record hypotheses as Observations and keep Decisions, Constraints, and verified facts distinct. A verified fact requires existing validated evidence: **NO EVIDENCE → NO TRUTH**. Before `ROTATE` or `DONE`, record new verification evidence linked to the current milestone. Do not edit `PROJECT_STATE.md` or `DECISIONS.md`; the dispatcher generates those human views.

The execution mode documents what the Definition of Done requires, but this profile does not select a capability. Use Computer Use only when the current host-selected model provides it and the prompt says `Effective execution mode: computer_use`. Otherwise return `BLOCKED` if the Definition of Done cannot be completed.

## Start a run

The target is the Codex project you are working in. Resolve it as that project's own root, and take the Desktop project id from the same place: the two always belong together, because every created task is placed in that project and verified there.

A different directory is accepted only when it lies inside some Codex project's roots, and then that project's id is the one to pass. A path that belongs to no Codex project cannot be a target: the run would have no project to place its tasks in, and the user would see nothing. Say so immediately, in one sentence, naming the path and the fix - create a Codex project for that directory, or work in the project you already have. Never start a run that will fail later for this reason, and never ask the user to add the project by hand mid-run. Inspect it and the user's goal or `ROADMAP.md`. Create one independently verifiable outcome per milestone. Set `model_strategy` to `host-settings`. Do not add a reasoning field: the dispatcher omits both model and effort, and each fresh thread uses whatever defaults the host applies.

Infer one BCP-47 response language from the initiating user's request (for example `ru` or `en`; use the user's explicit language preference when present). Write the goal, milestone titles, objectives, Definition of Done items, execution reasons, reservation prompts, and user-facing updates in that language. Pass the same tag with `--language`; every fresh worker inherits it. Keep protocol identifiers such as `AUTOPILOT_STATUS`, `AUTOPILOT_SLOT_READY`, file names, code, and tool names exact. Role names are the exception and stay in English always, whatever the run language is: `Resilience Engineer`, `DevOps`, `UX Designer`, `Release Engineer`. A role is a profession, and the whole environment names professions in English; the thread-title format also appends the English words `Verifier` and `Verify`, so a translated role produces a half-translated title like `Инженер основания Verifier | M1 | Verify ...`. Task titles, objectives, DoD items and every user-facing line keep the run language.

Do not rely on the initiating task to expose or probe Project Memory. The `start-skill` command below creates a dedicated, visible preflight task and performs one harmless real model-to-MCP call with `operation=current` before it creates run-state or Worker 1. Autopilot never answers approval for the user, changes MCP approval configuration, or bypasses trust.

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

Classify each milestone as `code` or `computer_use` based only on whether its Definition of Done requires real GUI interaction that files, code, shell tools, or programmatic interfaces cannot replace. Include a concrete `execution_mode_reason`.

Write `<target-root>/.codex-autopilot/bootstrap-plan.json`; preserve the initiating
request verbatim and require a fresh independent verifier for every milestone:

```json
{"schema_version":3,"graph_version":1,"goal":"...","user_request":"<verbatim initiating user request>","model_strategy":"host-settings","execution_strategy":"auto","max_parallel_workers":10,"computer_use_slots":1,"roles":[{"id":"implementer","name":"Implementation Specialist","responsibilities":["Implement the milestone contract."]},{"id":"acceptance-reviewer","name":"Independent Acceptance Reviewer","responsibilities":["Judge the result against the original request, specification, and every DoD item."]}],"tasks":[{"id":"M1","title":"...","objective":"...","definition_of_done":["..."],"execution_mode":"code","execution_mode_reason":"Repository files and tests are sufficient.","role":"implementer","depends_on":[],"priority":0,"verification":{"policy":"independent","required":true,"verifier_role":"acceptance-reviewer","max_revision_attempts":2},"resources":[],"required_capabilities":[],"context":{},"outputs":[],"tags":[]}]}
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

Resolve `scripts/codex-autopilot` relative to this `SKILL.md`, then run it as direct argv - the program and its flags as separate arguments, with no `sh -lc`/`zsh -lc` wrapper and no quoting of the whole line into one string. The installer registers this exact program in Codex execpolicy, and execpolicy matches argv tokens: wrapped in a shell the whole command becomes a single opaque token, matches nothing, and Codex raises an approval that the dispatcher will never answer. Run:

```text
scripts/codex-autopilot start-skill --project <target-root> --plan-file <target-root>/.codex-autopilot/bootstrap-plan.json --replace --language <BCP-47> --desktop-project-id <desktop-project-id>
```

This command performs preflight before it creates run-state. It checks the target, Git, installed runtime, official Codex App Server, `:workspace`, target cwd, App Server project metadata, actual Desktop placement behavior, the exact selected-plugin Stop hook through `hooks/list`, built-in Project Memory MCP, SQLite FTS5, and a real model-to-MCP trust probe. The Stop hook must be unique, enabled, error-free, `trusted` or `managed`, and use the stable installed-runtime command. If it is `modified` or `untrusted`, stop before worker creation and ask for exactly one action: open `/hooks` and trust the current Autopilot Stop hook. Missing, disabled, duplicated, erroneous, unknown-status, or mismatched definitions fail closed. Never bypass or edit hook trust. App Server exposes no read-only API for the persistent per-tool choice. If it reports `Project Memory MCP: APPROVAL REQUIRED`, show the exact diagnostic task title and thread ID and ask whether the user approves `codex_autopilot_memory.memory` with Always. Explain the one local project-scoped tool. Only after an explicit yes, repeat the command with `--approve-project-memory-always`; it answers only the verified installed-plugin request that advertises `always`, then requires a second fresh task to call memory without another approval. Never infer approval, use the flag before confirmation, launch Worker 1 after a decline, or edit trust state. Never attach a permission request to `start-skill` and never re-run it to obtain access. The dispatcher refuses every approval it observes, so a command approval raised this way can only deadlock: it waits in a task the user is not looking at while the initiating turn shows nothing. A non-zero exit is a finished answer, not a permissions hint - print its last lines as the visible verdict and stop. `CODEX_HOME` access is requested only when preflight itself printed `Worker access: APPROVAL REQUIRED`, and then it is the user who grants that exact directory; a handshake timeout, a missing file, or any other failure is never answered with a permission request.

Do not pre-create worker slots. After preflight fully exits, the Stop hook
atomically reserves only the bounded READY frontier and launches the trusted
dispatcher exactly once. The dispatcher calls App Server `thread/start` for a
persistent task using the canonical cwd/workspace root and the App Server
project ID, verifies returned metadata, calls production `turn/start`, waits for
completion, fully exits that task's App Server subprocess, and continues with
the deterministic successor. App Server and
Desktop project IDs are separate namespaces; actual Desktop project visibility
must be observed before claiming it. Never use App Server `thread/resume`,
`thread/unsubscribe`, archive/unarchive, or a no-op turn as an ownership-transfer
protocol. Desktop visibility/editability is a separate live observation. Show
the short task list and finish the initiating turn; the dispatcher continues
without another user message.

If creation or metadata attestation cannot reach the full-exit barrier, preserve
any known bound task and escalate through Pipeline Engineer recovery. Never
create a replacement for an ambiguous task.

If the target is not Git, state that this beta requires a Git repository and suggest `git init`; do not initialize it or make a commit without explicit user authorization.

## Controls

Exact pause, resume, status, and uninstall prompts are handled by the plugin hook without a model request. If control reaches this skill, run the bundled helper with `stop`, `resume`, `status`, or `uninstall --yes`. Project state is removed only with explicit `--purge-project-state --project <root>`.

## Status protocol

- `ROTATE`: current milestone verified with recorded evidence; another milestone remains.
- `DONE`: the complete roadmap is verified with recorded evidence.
- `BLOCKED`: user input, a missing host capability, or an approval is required.
