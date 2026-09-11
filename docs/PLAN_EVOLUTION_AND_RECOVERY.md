# Plan evolution and recovery

Plan evolution, pause/resume, rate-limit coordination, and crash recovery are
deterministic lifecycle operations. They do not introduce a persistent manager
model and they never infer successful work from silence.

## Typed plan-change protocol

An implementation, revision, or verification worker that cannot safely finish
under its current contract may end with one final protocol line:

```text
PLAN_CHANGE_REQUEST: {"request_version":1,"kind":"prerequisite","target_task_id":"T44","summary":"Add schema audit","rationale":"The task cannot be verified before the schema is audited.","change":{"description":"Audit the stored schema first.","suggested_task_id":"T43A"},"evidence_ids":["EVID-123"]}
```

The exact required fields are `request_version`, `kind`, `target_task_id`,
`summary`, `rationale`, `change`, and `evidence_ids`. The requester may change
only its own task. Supported request kinds are:

- `prerequisite`: a description and optional suggested task ID;
- `dependency`: one existing dependency task ID;
- `resource`: a non-empty list of resource declarations;
- `verification`: a replacement verification object.

The Stop lifecycle validates this structure, retires the requester turn,
releases only its lock, and records `PC<n>` in `run-state.json`. Existing
workers are allowed to drain and keep their locks. No ordinary task is launched
while a plan change is active.

After the active frontier drains, the runtime reserves exactly one fresh
`Replan PC<n> · <summary>` task. Its bounded prompt contains the typed request,
the complete current graph, and evidence selectors for current `VERIFIED`
tasks. It excludes transcripts and concurrent worker responses. The replanner
may read the named Project Memory evidence, returns one complete schema-3 plan,
and exits. It must end with:

```text
AUTOPILOT_PLAN_CHANGE: {"request_id":"PC1","base_graph_version":1,"plan":{...complete schema-3 plan...}}
```

The runtime, not the replanner, validates the result. A replacement must use
exactly the next graph version, preserve the run goal, original request, model
strategy, every existing task ID, and every verified task contract. It must
also pass all role, output, dependency, resource, verification, and cycle
checks. Invalid or stale results perform no plan or run-state write.

## Crash-safe graph commit

An accepted replacement is reconciled against mutable state before it becomes
visible. Attempts, revisions, completed sessions, verification history, and
unaffected retry deadlines are preserved. The requester, changed tasks, new
tasks, and their descendants are re-evaluated through the normal dependency
gate. Already verified tasks are never downgraded or rewritten.

The plan and run state are a logical transaction protected by the resource
coordinator lock. A durable `plan-change-transaction.json` redo record contains
the validated target plan, reconciled target state, versions, and plan/state
hashes.
The runtime writes `PREPARED`, atomically replaces `plan.json`, atomically
replaces `run-state.json`, then records `COMMITTED`. Resume and reservation
finish an interrupted `PREPARED` or `PLAN_WRITTEN` transaction before reading
the graph. A digest or version mismatch fails closed; the replanner is not
called again merely because the process crashed between files.

## Pause and resume

Pause uses drain semantics:

1. The pause marker is persisted before state is changed.
2. New reservations stop immediately.
3. Existing Desktop turns continue and retain their resource and Computer Use
   locks.
4. Their authoritative Stop events may record completion and release locks,
   but cannot launch successors while paused.
5. An authoritative Interrupt retires only that attempt to `RETRY_WAIT`.

The graph, attempts, verification states, sessions, retry deadlines, and lock
journal stay durable. Status reports `PAUSED_DRAINING` while work is active and
`PAUSED` once it has drained.

Resume always reconciles before clearing the pause marker or admitting new
work. Its optional authoritative worker-state map is keyed by the reservation
or ownership token:

- `active` retains the task and lock;
- missing or `unknown` retains the task and lock fail-closed;
- `terminal` or `absent` retires the unobserved attempt to `RETRY_WAIT`, releases
  only that ownership token, and never advances verification or dependencies.

Repeated reconciliation is idempotent. Once reconciliation completes, due
retries are promoted through the ordinary dependency and resource gates and
receive fresh reservation tokens. Existing attempt numbers and histories are
not erased.

## Coordinated rate limits

A definitive rate-limit failure retires only the affected worker, releases only
its resources, and preserves every independent active worker. The retry time is
the later of normal bounded exponential backoff and the provider reset epoch
plus a five-second safety margin. That deadline also becomes the run-wide
`rate_limit_until` admission barrier, so the runtime creates no new model work
from the same account bucket before it expires. Running work is not cancelled.

After the deadline, one locked scheduling transaction clears the global
barrier, promotes due retries, and admits the normal bounded frontier. The
resilience journal records pause, resume, plan-change, rate-limit, and crash
reconciliation events separately from task and resource journals.
