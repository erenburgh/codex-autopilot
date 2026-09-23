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
infrastructure incident already in `PIPELINE_ENGINEER`. The package is bounded
and contains:

- structured incident identity, classification, affected tasks, and phase;
- captured system state and recent journal events;
- the exact allowed and forbidden actions;
- runbook, recovery slot/lock, attempt budget, backoff, and healthcheck gate.

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
or a failed Pipeline Engineer recovery after the bounded automatic attempts.

The deterministic regression coverage is in
`tests/test_pipeline_engineer.py`, with transport CLI enforcement and Stop-hook
behavior in `tests/test_desktop_lifecycle.py` and Studio boundaries in
`tests/test_ai_studio.py`.
