# Pipeline Engineer · On call

Codex Autopilot has a permanent deterministic on-call capability and creates a
fresh model-backed Pipeline Engineer only after an infrastructure incident
exhausts bounded automatic recovery. The implementation is
`src/codex_autopilot/pipeline_engineer.py`; its state is independent from
production resource locks and worker slots.

## Deterministic incident taxonomy

Incident routing uses structured signal fields, never keyword interpretation of
free-form prose:

| Class | Owner | Automatic repair |
| --- | --- | --- |
| `PRODUCTION` | production task / product owner | forbidden |
| `PIPELINE` | on-call supervisor | allowlisted runbook only |
| `RUNTIME` | on-call supervisor | allowlisted runbook only |
| `INTEGRATION` | on-call supervisor | allowlisted runbook only |
| `TOOLING` | on-call supervisor | allowlisted runbook only |
| `POLICY` | user | forbidden |
| `AMBIGUOUS_SIDE_EFFECT` | authoritative reconciliation, then user if unresolved | forbidden |

`AMBIGUOUS_SIDE_EFFECT` overrides the nominal surface when a structured signal
reports an unknown outcome for App Server `thread/start` or `turn/start`.
These operations are not retried from elapsed time, a missing response, a new
process, or agent judgment.

An explicit App Server `thread/start` RPC error is different from an unknown
outcome: the side effect is `KNOWN_FAILED`. Runtime matches the exact creation
contract, causal owner, and reservation, then opens one stable
`app_server_thread_start_failed`/`PIPELINE` incident. Legacy structured Codex
App create rejections remain readable through `transport_policy_rejected`, but
new reservations never use that create path.

The Pipeline Engineer does not fix production quality failures. It cannot
bypass hook/tool trust, answer an approval, impersonate the user, change global
Codex settings, delete project state, perform destructive or unbounded repair,
or turn causal provenance into task-management authority.

## Lifecycle and task isolation

The lifecycle is:

```text
HEALTHY
  -> DEGRADED
  -> AUTO_RECOVERY
     -> RECOVERED -> RESOLVED
     -> DEGRADED (bounded retry with backoff)
     -> AUTO_RECOVERY_FAILED
        -> PIPELINE_ENGINEER
           -> RESOLVED
           -> ESCALATE_TO_USER
```

Production, policy, and ambiguous-side-effect incidents route from `DEGRADED`
to `ESCALATE_TO_USER`; they never create a Pipeline Engineer. An infrastructure
signal without an allowlisted runbook reaches `AUTO_RECOVERY_FAILED`, after
which one Pipeline Engineer lane receives the incident package. Re-delivery of
the same signal returns that same incident and lane without duplicate journal
events.

Each incident names exact `affected_task_ids`. The scheduler marks only those
tasks unavailable and continues independent work. `context_task_id` only
anchors the engineer's thread (cwd and title) for a ticket that holds no task
- a stop of the run itself, a reservation that found no successor - and pauses
nothing; a ticket with neither is anchored to the first unfinished task and no
longer breaks every reservation with "names no task". Only the engineer's own
escalation with `scope: run` (`blocks_run`) holds every task. Existing active work is not
silently declared stopped; an authoritative interrupt/terminal event remains
required. A task may resume only after every incident affecting it is
`RECOVERED` with a recorded passing healthcheck or `RESOLVED`.

## Every stop reaches the on-call

Every place that stops a task goes through one door, `blocked_runs.stop_run`:
the hiring ladder, a worker's own `BLOCKED`/`ESCALATE`, three unreadable
verdicts, an unroutable verifier, an exhausted replanner, both refusals of
plan verification (holding the requester), a reservation that finds state that
cannot be, a plan change waiting on locks no live session holds, and an
engineer that leaves no successor. The door opens a fresh `RUNTIME` ticket per
stop (the signal carries `stop_kind`, the plan change and an ordinal, so a
second stop after an answered one is never handed the old `RESOLVED` ticket),
records `stop_kind` in `system_state`, always routes the ticket to the
engineer's lane, and does not touch the run's status. A structural test lists
every transition into `TaskState.BLOCKED` with its door.

R3 decides what a stop does to its task. A stop that is hers (the ladder, a
refused plan, a worker's `PRODUCT_DECISION`, `ARCHITECTURE_DECISION` or
`DANGEROUS_PERMISSION`) blocks at once. Every other stop only holds its task by
the ticket's pause: any other worker code - `ENVIRONMENT_FAILURE`,
`MISSING_RESOURCE`, `DEPENDENCY_DEFECT`, `CONTRADICTORY_CONTRACT`,
`RECOVERY_EXHAUSTED` or none - returns the task to `READY`; three unreadable
verdicts and an unroutable verifier leave it `IMPLEMENTED`. When the ticket
closes, the same action runs again. The task becomes `BLOCKED` only when the
on-call hands the ticket up (`stop_holds.block_escalated_tasks`) or the same
stop exhausts the on-call (below).

R23 bounds every stop in the door (`stop_repeats`). The signature is the run,
the stop kind, its plan change, the tasks it holds and the reason code. When
the on-call has closed two tickets with that signature and the stop comes back,
the third ticket is not given to a third engineer: it goes to the owner as
`RECOVERY_EXHAUSTED` with a report (the signature, the attempt count, what
each closure did - its actions, note and healthcheck), and the tasks the stop
only held become `BLOCKED`. Her answer to a ticket that was handed to her
starts the count again; a ticket Resume swept shut while it still sat in the
on-call's lane counts as a closure. The orphan sweep and the plan gate use this
bound instead of their own copies.

An engineer that leaves no successor files `no_successor` only when a `READY`
task that no open ticket holds has no session at all. Tasks held by a ticket
that waits for the owner are not idle work: counting them made every engineer
completion after the plan gate's escalation call another engineer, forever.

The plan gate is a stop as well. When the canonical plan has no valid
verification receipt, the reservation no longer raises (that rolled back the
completion that called it, the engineer's own included, and left its session
`ACTIVE` forever). It builds nothing from the graph, files one `plan_unverified`
ticket holding every unfinished task, and reserves the on-call for it. After
two closures that did not fix the plan, the third ticket goes to the owner as
`RECOVERY_EXHAUSTED`, and the run derives `BLOCKED` (`AWAITING_OWNER`).

The on-call is reserved next to the work, never instead of it: at the end of
every reservation pass (so a ticket filed in that pass gets its engineer at
once), outside the worker slots, at most one per run. It is reserved above the
plan-verification gate - its descriptor builds nothing from the graph but an
anchor - and below the `CODEX_THREAD_ID` and external-dispatcher guards.
Every pass also sweeps the journal: tickets in `DEGRADED` that no runbook will
replay (stop tickets never are) and in `AUTO_RECOVERY_FAILED` go to the lane,
and a `BLOCKED` task that no open ticket holds and no plan change explains gets
an `orphan_block` ticket, bounded like every stop.

The engineer's `ESCALATE_TO_USER` moves its own ticket to the owner with the
diagnosis, repair, decision and recommendation from its `AUTOPILOT_ESCALATION`
line (the bounded end of its message when the line is missing) and holds only
that ticket's tasks; the run continues around it. The run's status is derived
in `run_status`: `RUNNING` while any session is pending, `WAITING`
(`PIPELINE_ENGINEER_PENDING`) while a ticket waits for an engineer, and
`BLOCKED` (`AWAITING_OWNER`) only when nothing can be taken and what is left
waits for the owner. The wake-up raises a stranded run whatever its status
says, except a paused or finished one.

A session whose dispatcher died is never left pending. An engineer whose
completion raised - an unreadable status line, `RESOLVED` without
`devops-resolve-incident` - used to stay `ACTIVE` with a dead dispatcher, and
one engineer per run kept every later one out. The wake-up now counts any
pending session without a live dispatcher as stranded, asks the server about
its thread, and retires a turn that is over, so the next engineer takes the
ticket in the same wake. A create in doubt has no thread to ask about: the
reservation pass files a `lost_create` ticket for a worker's, and retires an
engineer's, which never started a turn. An engineer whose own turn failed
(the turn ended non-completed, `turn/start` was rejected) retires only its
session: its task is an anchor it never executes, possibly running under a
worker of its own, so no task state, active slot or retry time is touched.
Her pause and the account's limit retire it without counting. Two engineers
lost on one ticket - failed, or dead with the turn over - send that ticket to
the owner with what happened to them, not to a third (R23).

The frontier's own refusals are stops too, not raises that roll back the
completion calling it: task states that do not fit the graph
(`task_states_mismatch`, holding every unfinished task), and a plan change
whose proposal no longer validates, whose digest moved, whose verification
mode is unknown or whose requester is not `READY` (`inconsistent_state`,
holding the requester). Under a refused graph the engineer's descriptor takes
no model or effort from that graph - it runs on the owner's Codex settings.

## What the on-call may do about a stopped task

An infrastructure ticket's package lists the whole vocabulary
(`RECOVERY_ACTIONS`), not only the diagnostics. A stop ticket also carries
`stop_context` (`stop_diagnosis`): the kind of stop, the worker's reason code,
the verifier's last issues, hires and effort, the replanner's refusals, the
bounded end of the stopped session's final message, `means` for this kind of
stop, and, for a permission request, the request next to the durable
authorization run-state holds for the run (R4: a versioned list of covered
operations), the operation of it that covers the request if one does, and the
permission profile. `owner_answer` is the exact
`unblock` command that would be her answer.

Two actions act on the task itself (`engineer_stop_actions`):

- `devops-return-task --incident-id <id> --task <task>` returns a task the
  ticket holds from `BLOCKED` to `READY`, or to `IMPLEMENTED` when a verdict was
  ever given - the same rule as her unblock. `VERIFIED` is unreachable.
- `devops-request-plan-change --incident-id <id> --task <task> --reason <text>`
  asks the replanner on the task's behalf, exactly as its worker would.

Both run only from the thread of the on-call session reserved for that very
incident (`CODEX_THREAD_ID` must be its thread), only as far as the means table
in `engineer_authority.STOP_MEANS` allows - which the engineer cannot edit - and
never on a stop that is hers (`PRODUCT_DECISION`, `ARCHITECTURE_DECISION`,
`DANGEROUS_PERMISSION`, and a worker's own `RECOVERY_EXHAUSTED`: their means
are empty - a diagnosis and an escalation with the same code). A task at the
top of its hiring ladder - judged by its ladder, whatever ticket holds it -
returns only after a runtime patch on the ticket that changed the acceptance
path, or through a plan change (R23: the cause must change). The acceptance
path is an explicit list in `engineer_authority`: the modules
`verification.py`, `acceptance.py`, `acceptance_floor.py`,
`department_acceptance.py`, `department_runtime.py`, `department_gate.py`,
and the verifier's own parts of the prompt
builders (`LADDER_RESET_DEFINITIONS`: whole verifier-only definitions, and in
the shared builders only the body of an `if phase == "verification"` branch).
What a patch changed is read from the staged pair of texts, compared as syntax
trees, never from the ticket's record (`ladder_grants`). One patch is good for
one grant, and only while it stands: a patch withdrawn, refused at install or
reverted buys nothing, and a grant it already bought is revoked in the same
transaction - the tally goes back, a task that has not started goes back to
`BLOCKED`, and a ticket holds it. The grant is a fresh hire at the effort the
task already reached, not the whole ladder again. A return that does not hold comes
back as the same stop, and the door's R23 bound sends the third to her with
what each return did.

An infrastructure stop only holds its task, so closing its ticket is the
task's return, and `devops-resolve-incident` on a stop ticket is bound like
one: only the on-call of that ticket from its own thread (`CODEX_THREAD_ID`);
every repairing action it names must be among the means for that stop - a
stop whose means are empty (hers, a worker's `RECOVERY_EXHAUSTED` included)
is not closed at all, only escalated; and each named repair must have
happened: `repair_runtime_code` needs a live runtime patch of the ticket,
`request_plan_change` a plan change it asked for, `return_stopped_task` a
return or a held task the closure itself returns. Nor is a stop ticket closed
with diagnostics alone, or while a task it holds is still `BLOCKED` without the
plan change it asked for: that closure used to leave the task waiting for
nobody.

An on-call whose answer the runtime refuses - no readable status line,
`RESOLVED` on a ticket still open - no longer raises before its transaction.
The session completes as a protocol error (an R13 violation is recorded), the
lane is free at once, and the second such engineer on one ticket sends it to
her as `RECOVERY_EXHAUSTED` with the end of its own message. A failed turn of
the on-call retires only its session; its anchor task is never touched.

`arm` runs under the run's transaction and refuses while any other session is
pending or has a live dispatcher (`run_arming`); the calling engineer's own
session does not count.

A runtime patch is proven inside the project and staged there, the run
drains, and the wake-up installs it atomically outside the sandbox when no
dispatcher of this run is alive, scheduled or running (`runtime_install`, see
`docs/SECURITY.md`). Other runs are not waited for: their processes keep the
tree they started from. The drain is not a stop: while a patch is staged no
task counts as reservable work, so the on-call's completion files no
`NO_SUCCESSOR` over the tasks it just returned. The drain is bounded: past
`turn_timeout_seconds + reconcile_timeout_seconds` and a margin since staging,
a dispatcher still alive is not finishing a turn, so the patch is refused,
what it bought revoked, and a `runtime_patch_refused` ticket naming the live
pids goes to the on-call - the run never stands drained with nobody told.
`devops-repair-runtime` and `devops-revert-runtime-patch --incident-id <id>`
answer only to the engineer of that ticket, from its own thread, within the
means table; a staged patch is withdrawn only by the ticket that staged it.

## Advisory tickets and permission requests

Production, policy and an ambiguous side effect no longer go to the owner
directly: `route_incident` puts them in the on-call's lane like every other
ticket. There they get diagnostics only - `thread/read` through `server_view`
and the journal - and the brief says so: the engineer cannot close them
(`RESOLVE_FORBIDDEN_CLASSES`) and hands them up with its diagnosis and
recommendation. `FORBIDDEN_ACTIONS` are unchanged; an ambiguous create or send
is never repeated.

A thread Desktop files outside the project is an R5 placement defect, not a
stop: one ticket per cause per run (stop kind `placement_defect`; the cause is
part of the signal id, so a second cause is never swallowed by an open ticket
of the first) holds no task and carries the cause, the two separate facts (App
Server `projectId`, Desktop's rule and its reason, the Desktop version), a
diagnosis, a recommendation and every thread of the run outside the project by
id and title (`stop_context.placement`). A wrong cwd, or a staged permission
profile that did not keep the root read-only (`isolation_not_proven`, with
`.codex-autopilot/isolation-probe.json`), is a runtime defect the on-call
repairs - isolation is never a choice handed to her; a Desktop project root
(R6) goes to her. The same door carries `runtime_roots_widened` (a thread came
back with roots wider than its workspace) and
`canonical_changed_outside_manifest` (after a promotion, one ticket per task).
The on-call's own thread is never stopped by its placement, so the ticket
always reaches it.

A permission request inside a turn (`ApprovalRequired`) is never answered and
never retried. Its ticket counts the requests of the run
(`approvals_in_run`) and names the thread's placement contract: requests that
keep coming from threads filed at the read-only root are a runtime defect. It is its own failure code, `approval_required`, not counted
towards the retry ceiling; a stop ticket holds the task with the request in
it and goes to the on-call, which compares it with the run's durable
authorization and permission profile (`stop_context.approval`; the on-call's
prompt carries its own paragraph for this kind of stop). The authorization is
R4's record in run-state (`durable_authorization`): written when she arms the
run - backfilled at the first request for a run armed earlier - with the
version and the covered operations of `engineer_authority`, which the engineer
cannot edit. `run_authorization.covering_operation` reads from the request
itself whether an operation covers it: a file change whose targets lie inside
the project, or the plugin's own `codex-autopilot` command with no shell around
it, run from a cwd inside the project, aimed at this project, and one of the
subcommands the run recorded (`RUN_AUTHORIZED_CLI_SUBCOMMANDS`, version 2: the
read-only views, the relay protocol and the on-call's own actions). Her own
commands are not on it - `unblock`, `authorize-project-root`, `arm`, `stop`,
`resume`, `revoke-skill`, `uninstall`: arming a run does not authorize an answer
on her behalf or a mutation of her saved projects, so such a request goes to
her as a permission. What it cannot prove is not covered. A covered
request is an R4 violation - the runtime asked for what the run already holds -
and an escalation of it as `DANGEROUS_PERMISSION` is refused as a protocol
error: that would be the very confirmation request R4 forbids. A runtime that asked
for more than the run needs is a defect it repairs, and such a ticket closes
only with a live runtime patch of that ticket - without one the same request
would come straight back. Otherwise it hands the ticket up as
`DANGEROUS_PERMISSION` with a recommendation. Her answer is applied without a
loop: `--option replan` changes the plan so the task does not need the
operation, `--option retry` (she granted it herself) runs it once more, and the
same request after her retry is not retried again. Resume counts as `retry`
with the request's signature recorded, so the same rule holds on that door: a
Resume after the same request came back leaves the ticket with her and says
which answer closes it.

## Her answer

`codex-autopilot unblock --project <root> --task <id> [--option <code>] --reason <text>`
(`owner_answers.answer_task`) is one transaction. A ticket that holds no task -
the on-call's own permission request, anchored to a task only by
`context_task_id`, or a run-level stop - is answered by its id instead
(`--incident-id <ticket>`, the form the status card prints for it); naming the
anchored task answers it too. It is one transaction: the task leaves `BLOCKED`, the
open tickets that hold it are closed as answered by her, an answer at the top
of the ladder grants a fresh hire, the decision and its option are recorded in
`user_unblocks` where the next worker reads them, and the run is raised the way
the wake-up raises it (`derive_owner`, `ensure_wake`, the same hook-trust gate).
It never asks for Resume, and it says "the run continues by itself" only when
that is so: with no completed turn to continue from, nothing raises the run -
the sweep only signals such a run - and she is told to start it once with its
phrase. Resume on a `BLOCKED` run still answers what was
handed to her and now also lifts the stops of those tickets' tasks by the same
transition, a fresh hire at the top of the ladder included (by the task's
ladder, not the ticket's kind); tickets the on-call never looked at are routed
to its lane, never closed.

The status card shows every ticket handed to her with the on-call's diagnosis,
the decision needed, its recommendation, the options and the answer command.
Revoked hook trust is the one signal that goes to her without the on-call -
raising the engineer passes the same trust gate, and going around it is her
boundary - so the wake-up records the refusal with its diagnosis and
recommendation, and the card shows it. A system banner for her decisions
exists (`runtime.escalation_notifications`), off by default like every banner.

## Bounded recovery

Automatic recovery has its own file lock, one logical recovery slot, ownership
token, attempt counter, retry budget, and exponential backoff capped by a
configured maximum. It receives a fixed runbook of symbolic actions rather than
arbitrary shell commands. The current allowlist covers only owned runtime child
restart, owned local channel reopen, ephemeral project metadata refresh, and
owned ephemeral tool-cache rebuild. Every action is checked against the selected
runbook and journaled.

The recovery process ending is not evidence that the system recovered. A
non-empty passing healthcheck is mandatory. On crash reconciliation, a missing
or `UNKNOWN` owner retains the recovery lock and slot. Even a terminal-success
process state without a healthcheck returns to degraded recovery or exhausts the
budget; it never resumes affected tasks optimistically.

## Incident package and Studio role

AI Studio always exposes the system role `Pipeline Engineer · On call`, separate
from planner-defined temporary roles. It can build a fresh prompt only for an
incident already in `PIPELINE_ENGINEER` - infrastructure, or advisory with
diagnostics only. The package is bounded
and contains:

- structured incident identity, classification, affected tasks, and phase;
- captured system state and recent journal events;
- the exact allowed and forbidden actions;
- runbook, recovery slot/lock, attempt budget, backoff, and healthcheck gate.

The rules block rides with it whole, statement and check. When the package and
the rules do not fit the prompt ceiling together, the package gives way: its
diagnostic parts (`server_view`, `recent_events`, `stop_context`, `system_state`)
are replaced, largest first, by `{"truncated": true, "original_chars": N, "head": ...}`;
the ticket's identity, class, phase and actions never are
(`engineer_package_budget`). The ticket's own copies of diagnostics (its summary
with the stop's reason, `system_state`, `recent_events`, an earlier escalation's
detail) give way next, the same way. If even the ticket's identity, the action
lists and the rules cannot fit, the on-call cannot be called for that ticket: it
goes to the owner as `ESCALATE_TO_USER` / `RECOVERY_EXHAUSTED` with the refusal
as its diagnosis and a recommendation - room for the whole block (a higher prompt
ceiling or a model with a larger window), never a split or shortened rules block,
which R17 forbids - the event `pipeline_engineer_unpromptable`
is journaled, and the next ticket in the lane is taken - at reservation and at
the dispatcher alike (`engineer_reservation.hand_unpromptable_ticket_to_owner`).

No worker transcript or forwarded authorization prose is accepted by this
entry point. Status output shows the role, incident phase, paused task IDs,
recovery-slot ownership, and pending authority-bound transport.

## Causal relay recovery

The initiating user authorizes the complete fixed Autopilot run. Each scheduler
reservation records the exact causal predecessor as `relay_owner_thread_id`;
only its automatic dispatcher may consume the one-shot App Server launch. A
claimed launch cannot be claimed again.

If App Server definitively rejects `thread/start`, the bounded create command
records the known-failed result before its caller can retry. The affected reservation moves
from `RELAYING` to `RETRY_WAIT`, resources are released, and one stable incident
pauses only the destination task. Pipeline Engineer may repair local routing,
but may not create, fork, start, or message the destination task.

After the repair, `devops-rearm-relay-owner --incident-id ...` checks the exact
incident/reservation hash, successful predecessor completion, elapsed backoff,
absence of another active or pending destination, and a concrete role-based
title. A passing healthcheck resolves the incident, creates one retry
reservation, and launches the dispatcher bound to the original causal
predecessor. There is no chat message or model continuation. Re-running recovery
reuses the same incident and reservation; DevOps does not perform destination
`thread/start` or `turn/start` itself.

## When a person is required

The on-call hands a ticket to the user for a dangerous permission, global Codex
configuration, potentially destructive repair, production/product or
architecture choice, ambiguous create/turn outcome that cannot be reconciled,
or a failed Pipeline Engineer recovery after the bounded automatic attempts -
always after looking at it first, with a diagnosis and a recommendation.

The deterministic regression coverage is in
`tests/test_pipeline_engineer.py`, with transport CLI enforcement and Stop-hook
behavior in `tests/test_desktop_lifecycle.py` and Studio boundaries in
`tests/test_ai_studio.py`.

## Department lead stops (R30)

A verifier is reserved only with its department's lead and rubric
(`department_gate.admit_verifier`, under the coordinator lock). When the
task's profession names no lead, or its department's rubric history is
ambiguous, that task alone stops (`department_lead`): it stays `IMPLEMENTED`,
held by the ticket, and its neighbours go on. The ticket's diagnosis names the
cause and its recommendation the remedy. No lead: `devops-request-plan-change`
- the change is marked `requires_lead`, and the replanner's graph is refused
until the requester's profession names one. A stray record in the rubric
scope: `devops-supersede-rubric --record <id>` retires it - only a record
outside the canonical history (the first verified record of each version
1..n; a refusal names the stray ones), never the first of its version, even
when the runtime wrote both - audited in Project Memory, and the task returns. A follow-up
whose prompt cannot be built stops the same way (`launch_refused`), with what
its reservation took given back. A turn not started by the runtime still
running in a finished lead's thread is a `lead_outlived` ticket that holds no
task; a finished extra turn is only recorded (an R30 violation and a
verification result), never a stop.
