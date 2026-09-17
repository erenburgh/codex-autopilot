# Changelog

## 0.10.0-beta

The first release meant for people other than the author. Everything in it
was found on a live run of the previous version: every entry below is a
place where the run stood still and a human had to step in.

### DevOps repairs the runtime itself

- The on-call engineer may now change the runtime's own code. The repair is
  not declared, it is proven: a reproduction test must fail on the current
  code and pass with the patch, the whole suite must stay green, and the
  guarded ownership, trust and classification definitions must stay
  byte-identical. Anything else and the installation is untouched. A repair
  is a set of edits — several modules, a new module — applied together;
  half a set never reaches the installation.
- The engineer's authority moved into `engineer_authority.py`, which no
  repair can touch; `pipeline_engineer.py` itself became repairable, with
  five of its definitions guarded by hash.
- The installer ships the test suite next to the sources: without it a
  repair cannot be proven, and self-repair would silently disappear on a
  user's machine while staying green in the repository.
- Recovery actions are named from a vocabulary, never described in prose.
  On the previous run the main failure signature had 18 repeats, 15
  recorded resolutions and zero learned runbooks, because the learning
  path compared free text against an enumeration. `--action` is now
  mandatory on `devops-resolve-incident`; circumstances go to `--note`.
- A repeated failure is bounded per signature (R23). `maximum_attempts`
  used to be declared and read by nobody; it is now the ceiling per
  failure signature, default 5, and reaching it opens a ticket for the
  engineer rather than stopping the run.

### The run no longer waits for a human word

- A retry due after a rate limit is raised by the runtime itself. The last
  dispatcher and the Stop hook leave a wake-up process behind; it sleeps
  until the due time and dispatches the same way an automatic successor
  would, under the same owner and the same ownership check.
- The wake-up survives a reboot: the installer adds a launchd agent that
  sweeps known projects at login and every five minutes and arms a wake
  where a retry waits. The owner is derived from the run's own journal.

### The user hears what they need before the first worker

- Both skill profiles carry an onboarding block: the two decisions that
  belong to Codex, how to look at the run, what a running task means and
  what may be done with a finished one, how the plan changes, what happens
  on a fault and who repairs it. Every phrase the onboarding promises is
  one the hook knows — that is tested.
- `tasks` / `задачи` answers with the same card as `status`; bare `stop`,
  `pause`, `resume`, `continue` and their Russian forms are accepted like
  bare `status`. Uninstalling still requires the full product name.
- The engineer writes everything a person will read in the run language,
  as the workers already did.

### The harness speaks English

- Rules, the launch ladder, hook replies, preflight, CLI output, failure
  messages and notifications are English. Worker and engineer prompts were
  already in the run language and are untouched. The short status card —
  the one thing read in chat — follows the run language. The owner's
  quotes in the rules' `source` fields stay verbatim.
- A long-standing concatenation bug glued words together inside the rules
  that reach every prompt; fixed.

### Measured on the previous run

- Every Pipeline Engineer prompt line names every flag its command
  requires — `relay-fail` had gained a required `--failure-code` while the
  runbook still showed the old invocation, and an engineer following it
  would have been refused by argparse. Tested as a class, not a case.
- The status card no longer claims the dispatcher is dead during a
  verification, and no longer prints a model and reasoning that nothing
  wrote.

## 0.9.1-beta

Fixes found on the live 0.9.0 run. Every one of them was discovered not by
reading code but by the run standing still for the user: each entry below is
a stop that had to be worked out from the journals.

### The launch no longer hangs silently

- The trust probe ran at the production worker's effort. A turn whose whole
  job is one harmless tool call was counted at `xhigh` and twice missed the
  five-minute mark. The probe now has its own effort.
- `Timed out waiting for App Server` with a healthy App Server sent people
  to repair transport and permissions. Waiting for a turn raises
  `TurnTimeout`, which says plainly that the model turn ran over.
- Five minutes of silence got a voice: before the probe it prints what is
  being checked, in which task and how long is allowed. One timeout no
  longer fails the launch — there are three attempts.

### Worker and acceptance

- The worker did not know that a command requiring permission kills the run:
  the dispatcher never answers approvals, and the dialog hangs in a task
  nobody is looking at. It now finishes with `BLOCKED DANGEROUS_PERMISSION`
  and names the command.
- An acceptance refusal exhausted the revision attempts and put the task in
  BLOCKED — with no replanner, no engineer and no command to lift the state.
  The task is now re-hired at the next effort step with a fresh executor; the
  plan and the Definition of Done are untouchable.
- The on-call engineer, having closed an incident, assigned no successor: the
  run went to READY/PREPARING and stood silently. The engineer's own turn
  became the causal link.

### Context budget

- The original request was copied whole into every prompt. On a run with a
  detailed specification that is 51 475 characters out of 62 635 under a
  64 000 ceiling — not one task would have assembled. It is now a reference
  with a length and sha256, and the text is fetched from Project Memory.
- The 64 000 ceiling had no justification against a model window of
  258 400 tokens. Derived from the window and recorded in
  `docs/CONTEXT_BENCHMARK.md`. A second copy of the same constant in the
  planner prompt was killing the relay in the middle of a run.

### Installation and hooks

- Hook trust was lost before every new task. Codex clamped the declared
  `Interrupt` timeout to its own limit and thereby rewrote our definition on
  every load; the unit of trust is the whole file, so all three hooks went
  back to review.
- The run stored the skill path together with the version number. The first
  install left the reference dangling and killed the active run — that is,
  the product could not be upgraded at all. The path now goes through the
  stable `current`, and runs of earlier versions are healed on load.
- A new run inherited the previous run's open tickets and stood on them
  before its first task.

### Failures stopped hiding the cause

- A failure before the request was sent counted as ambiguous:
  `installed_plugin_root` was computed among the call arguments, i.e. after
  the "request went out" flag. Such a failure opened an
  `AMBIGUOUS_SIDE_EFFECT` ticket, where both auto-repair and the engineer are
  forbidden.
- A repeated failure of a task already waiting raised
  `IllegalTaskTransition: RETRY_WAIT -> RETRY_WAIT`, killed the relay and
  opened a second ticket on top of the first — the real cause ended up
  hidden under the consequence.
- `NameError` instead of a clear refusal: the exception was not imported in
  the module that raises it. A test now holds this class of error for the
  whole runtime.

### The user's answer to an escalation

- Resuming closed escalations only in one phase of the run, and that phase
  is set solely by the engineer's completion. A ticket escalated by routing
  waited for a human, the human answered — and the answer was lost.

611 tests.

## 0.8.2-beta

Continuation of the 0.8.1 revision and the first really working on-call
engineer lane. Acceptance on live Codex Desktop passed: three milestones,
six workers, every task inside the project, zero tickets, DONE.

### The on-call engineer became a worker

- `ensure_pipeline_engineer` changed a field in JSON and called that an
  engineer. Now an incident in the `PIPELINE_ENGINEER` class reserves a real
  session of kind `pipeline_engineer` — ahead of all other work and without a
  single resource, because what it repairs is the very queue it stands in.
- `build_pipeline_engineer_prompt` had been removed in 0.8.1 as unused.
  There were no references to it not because it was replaced but because the
  lane was never finished. Restored and rewritten: it names real commands
  (`relay-status`, `relay-complete`, `relay-fail --definitive`,
  `devops-rearm-relay-owner`, `arm`, `devops-resolve-incident`) instead of
  invented ones.
- R13 in the prompt text: the engineer has full authority to repair, it
  chooses the method, the user takes no part in the choice. Escalation is
  exceptional and requires a code from the closed list
  (`DANGEROUS_PERMISSION`, `GLOBAL_CONFIG_CHANGE`, `PROJECT_DAMAGE_RISK`,
  `RECOVERY_EXHAUSTED`, `PRODUCT_DECISION`, `ARCHITECTURE_DECISION`); a bare
  `ESCALATE_TO_USER` is no longer accepted.
- The thread digest (`server_view`) is gathered by the dispatcher and placed
  in the incident package. Before, the engineer would have had to request
  permissions for commands it does not have; now there is nothing to ask —
  everything is already in the package. The prompt is rebuilt at the start
  of the turn, not at reservation, so the picture is fresh.
- The new command `devops-resolve-incident` closes the ticket: it requires a
  healthcheck name and observations, and `RESOLVED` happens only if the
  ticket is really closed.

### Fixed

- A plan change no longer requires a verbatim echo of `user_request`. In the
  live run that is 35 234 characters: a model rewriting the graph does not
  reproduce such a string, so **no** plan change could pass. The field is
  carried over from the current plan — stricter than an echo, which could be
  forged. `goal` and `model_strategy` stay strict.
- `turn/start` runs only on a thread loaded by this connection: if the thread
  is not in `subscribed_thread_ids`, it is first raised through
  `thread/resume`.
- A broken `dispatcher_pid` in the state is a refusal, not a guess. Before, a
  non-numeric value was silently read as "the process is alive".
- The plan template in both skills carried `execution_strategy="serial"` and
  one worker. `plan.py` declares `auto` and two as the default, but the plan
  is written by the planner from the example in `SKILL.md` — and an explicit
  value in the file cannot be overridden by a default. Not one new run
  entered parallelism. It is also stated what the default does not give:
  parallelism is created by the shape of the graph, and siblings writing to
  one file are serialized by the resource lock.
- The creation causality audit (R1) is called from the status report. The
  functions were written and called only from tests: the claim "the chain is
  checked" was not backed by a call path. The first run on live state showed
  two breaks nobody had seen.

### Removed

- Five definitions hidden by the `lifecycle` facade, two arguments with a
  single allowed value, 50 unused imports in the memory modules. The facade
  now re-exports exactly what is imported through it.

### Closed from the independent review set

- **R6.** `ensure_project_root` silently called `project/update` on every
  task creation and appended the canonical root to the user's saved
  project. Reading and writing are now separated: writing requires an
  accepted user decision naming this project and this root
  (`authorize-project-root --yes`, lifted by `--revoke`). Membership is
  fixed too — it is determined by nesting, the same rule by which preflight
  picks the project.
- **R13.** A worker's `BLOCKED` and `ESCALATE` carry a code from the closed
  list. Before, the reason went into `last_error` as the string "M9 worker
  returned BLOCKED", which holds nothing beyond the status itself.
- **R5.** A measured placement discrepancy fails the launch verdict and goes
  into one normalized ticket; before, `visible_in_desktop` was not among the
  deciding items at all, and `OUTSIDE` changed nothing. An unmeasured state
  stopped being eternal: 180 seconds after the thread's creation it becomes
  a negative result.
- **PRE-SIDE-EFFECT-FENCE.** A retired Desktop task refuses on
  `UserPromptSubmit`, before a single model or tool call. Before, the closed
  refusal came at the end of the turn, i.e. after the work.
- **R18.** Provenance of external material is mandatory on intake; the
  `external` label appeared in the memory tool schema; a Constraint on
  external material, the transition into a binding state and appending
  external support after the fact — closed. `contradicts` stays open.
- **R1.** The causality audit is called from the status report.
- **ENTRYPOINT-DEFAULTS.** The plan template enters the declared default.

### Known and not closed

`docs/M11_COMPLETION.md` lists the items of the independent review that
remain open, and why each of them is not closed here.

## 0.9.0-beta

The first version in which the 0.8.0 audit is closed entirely, and the first
whose promises are checked against the code by tests.

### The 0.8.0 audit is closed

1. **The dead pipeline is removed.** `orchestrator.py`, `smoke.py` and the
   `headless_app_server` surface — about 1165 lines no run could execute.
2. **The on-call engineer got a body.** The skill promised a role whose code
   did not exist. Now an incident in the `PIPELINE_ENGINEER` class creates a
   real visible worker; it repairs on the user's behalf, and escalation
   requires a code from the closed list.
3. **The pre-created slots mechanism is removed entirely.** It worked around
   a supposed impossibility of creating a visible task through App Server;
   the premise was refuted by a live run.
4. **The empty project binding is removed** from the creation path.
5. **CLI commands sorted out.** Each has a named consumer: user commands are
   described in the documentation, repair commands in the engineer's tools,
   internal ones carry their own help. The rest are removed.
6. **Memory refusals name what is accepted.** Before, a worker tried values
   blindly and went off to read the plugin's sources.

### Checked live

Two full runs on a real project: parallel workers on one dependency
boundary, unblocking, independent verification, an incident with a coded
escalation and the user's answer, canonical placement of every created task.
Installation from the release archive followed by a `doctor` check.

### Left unchecked

External installation by anyone but the author; a CI run on the declared
Python 3.11; the `--install-deps` path on a machine without Python and Codex
CLI; multi-hour recovery after a rate limit; Computer Use unattended;
Windows. All of this is named in the README, not hidden.

- Added independent contract regressions for the required v0.9 execution
  default, Desktop-owned start surface, exact thread-title formats, canonical
  project association, and deterministic verification promotion.
- Added separate deterministic AI Studio acceptance shapes for independent
  implementation branches plus integration, a research/analysis/fact-check
  pipeline, and mixed code/Computer Use scheduling.
- Added the required dependency-graph, parallel-execution, roles,
  resource-locks, thread-naming, project-association, and testing documents.
- Recorded release-blocking candidate gaps in
  `docs/RELEASE_VERIFICATION_0.9.0-beta.md`; no release, tag, push, or publish
  was performed.

## 0.8.1-beta

A revision after the first successful 0.8.0 acceptance: what could not
execute was removed, and what promised the impossible was fixed.

### Removed as unreachable

- `orchestrator.py` (1130 lines) and the `headless_app_server` surface.
  `run`, `resume` and `_dispatch` refused under `desktop_owned`, and the
  default of every command was exactly `desktop_owned`. The only live entry
  points were `smoke.py` and the tests. With them went `smoke.py`, the
  commands `run`, `_dispatch`, `test desktop`, `restore-app-server` and
  `restore_app_server_transport`.
- The pre-created slots mechanism: `add-worker-slot`, `append_worker_slot`,
  `worker_thread_ids`, `worker_slot_cursor`, `_validate_worker_slots`, the
  `WAITING_PROJECT_SLOT*` phases. It was a workaround for a supposed
  impossibility of creating a visible task through App Server; the premise
  was refuted — all six 0.8.0 acceptance workers turned out inside the
  project.
- Re-binding a thread to the project after creation. The line above rejects
  creation if the thread is not in the right project, so
  `thread/metadata/update` was binding the already bound. v0.7 does not call
  it.
- The `threadSource` parameter in `start_thread`: the value
  `agent_created_thread` marked the task as created by another application.
- Eight functions mentioned nowhere: `system_roles`,
  `build_pipeline_engineer_prompt`, `send_message_payload`, `_transport`,
  `_require_transport_claim`, `_healthcheck_passed`, `_sha256`,
  `_validate_owner_against_state`.

### Fixed

- The Pipeline Engineer lane got a procedure. Before, the skill promised that
  DevOps would repair and re-arm, and there was no code creating the engineer
  at all: `ensure_pipeline_engineer` changes a field in JSON. Now a sequence
  of existing guarded commands is named, and it is said separately that an
  unknown side effect remains a stop.
- The launch report is no longer presented as visible. The Stop hook must
  answer `continue`, otherwise the initiating turn stays `interrupted` and
  the dispatcher does not start; so the report is not shown. The initiating
  turn must name the phrase `status`, which goes through `UserPromptSubmit`
  and is visible.
- The memory MCP server's version was taken from a hard-coded string and
  would have diverged from the package on any version bump.
- The default `worker_surface` in the config was `headless_app_server`: a
  new run got a non-working surface unless one was chosen explicitly.

### Coverage

- `test_model_routing.py` — model routing directly, without the dead
  orchestrator: the routing table and the absence of a silent model swap.
- `test_skill_promises.py` — the skill may not promise what the runtime does
  not do; it also checks that the procedure names no non-existent commands.
- Removed 24 tests of the dead path, `test_recovery.py` and
  `test_context_budget.py` entirely: the latter measured prompt growth with an
  assembly that no longer exists, while the live one has a hard
  `MAX_PROMPT_CHARS` limit.

## 0.8.0-beta

- Added clean-machine preflight for target root, Git, installed runtime, official App Server, `:workspace`, target cwd, built-in Project Memory MCP, SQLite FTS5, and Adaptive model metadata.
- Added an explicit code-77 approval path for official App Server access to its exact `CODEX_HOME`, with no project run-state created on failure.
- Added a short-lived per-user launch registry so an initiating task can safely start Autopilot for a different target repository after `turn/completed`.
- Added project-local evidence-backed Project Memory using SQLite/FTS5 and a bundled stdio MCP server bound to each worker's target cwd. The public MCP surface is one user-approved `memory` tool with 14 strict operations.
- Separated Truth, Decisions, Constraints, Questions, Observations, Evidence, and Conflicts. Truth requires validated non-migration evidence.
- Added bounded retrieval, stable IDs, pagination, audit history, milestone evidence gates, integrity checks, online backups, and recovery from the latest verified milestone backup.
- Made `PROJECT_STATE.md` and `DECISIONS.md` generated views; made `HANDOFF.md` advisory and capped at 8 KiB.
- Added conservative v0.7 migration with a complete backup and zero automatic promotion of old agent prose to Truth.
- Preserved serial visible worker rotation, deterministic Sol/Astra routing, Host Settings omission, rate-limit waiting, and approval fail-closed behavior.
- Added an explicit first-use Project Memory trust probe. The user chooses persistent `Always` trust in Codex; production code never answers that approval.

## 0.7.0-beta

- Added deterministic AUTO routing between GPT-5.6 Sol and GPT-6 Astra, explicit execution modes, model metadata validation, and AUTO-only capability escalation.

## 0.6.0-beta

- Introduced the model-neutral App Server core, trusted lifecycle hooks, serial visible workers, deterministic controls, installer, and clean release package.
