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

The on-call is reserved next to the work, never instead of it: at the end of
every reservation pass (so a ticket filed in that pass gets its engineer at
once), outside the worker slots, at most one per run. It is reserved above the
plan-verification gate - its descriptor builds nothing from the graph but an
anchor - and below the `CODEX_THREAD_ID` and external-dispatcher guards.
Every pass also sweeps the journal: tickets in `DEGRADED` that no runbook will
replay (stop tickets never are) and in `AUTO_RECOVERY_FAILED` go to the lane,
and a `BLOCKED` task that no open ticket holds and no plan change explains gets
an `orphan_block` ticket; after two such closures the next one goes to the
owner as `RECOVERY_EXHAUSTED`.

The engineer's `ESCALATE_TO_USER` moves its own ticket to the owner with the
diagnosis, repair, decision and recommendation from its `AUTOPILOT_ESCALATION`
line (the bounded end of its message when the line is missing) and holds only
that ticket's tasks; the run continues around it. The run's status is derived
in `run_status`: `RUNNING` while any session is pending, `WAITING`
(`PIPELINE_ENGINEER_PENDING`) while a ticket waits for an engineer, and
`BLOCKED` (`AWAITING_OWNER`) only when nothing can be taken and what is left
waits for the owner. The wake-up raises a stranded run whatever its status
says, except a paused or finished one.

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
