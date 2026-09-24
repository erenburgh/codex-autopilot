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

Every violation is reported in one round (`plan_admission.admit_replanner_result`).
The replanner has three attempts, and the validator used to stop at its first
violation, with Goal Contract coverage and the state conditions (a removed task,
a rewritten `VERIFIED` task) each costing a round of their own - the last of them
only at the commit, after the plan verifier's PASS. One pass now collects, in a
fixed order: the protocol line itself, unknown fields (each refusal names the
accepted set, `plan_fields.ALLOWED_FIELDS`), every field of every role,
department and task, outcome bindings and the R29 acceptance floor per task, the
graph references, the fields a change may not replace, coverage, and the
run-state conditions that can only get worse while the change drains (removed
task, requester gone, `VERIFIED` or `CANCELLED` task rewritten). A check that
depends on another runs only when that one is clean, and no wider. One violation
reads exactly as before; several read `plan has N issues:` and a numbered list.

The refusal is recorded in `rejections` with its structured `issues`, and the next
replanner's prompt lists every refused attempt (`rejected_attempts`) and the last
one as a numbered list of `path: message (accepted: ...)`, marking an issue that
repeats an earlier one; its
constraints carry `allowed_fields` and `allowed_values`. Each text goes in once:
an attempt carries its issues without the reason rendered from them, and an issue
an earlier attempt already listed is `{"path", "repeated_from_attempt"}`. Nothing
is cut: a replanner prompt that still does not fit the ceiling is not launched -
the reservation builds it before counting the attempt, and the refusal is a
`context_budget` stop for the on-call, the same as the plan verifier's; the
refusal that led to it is recorded first. A graph that moved under
the replanner is the runtime's state, not its mistake: the change is rebased and
a fresh replanner is raised without spending an attempt. A semantic `REVISE` from
the plan verifier, and a commit conflict after its PASS (an advanced task
rewritten), return to the replanner the same way instead of taking the dispatcher
down. A PASS the commit refused is journaled as the outcome, not the verdict: the
verifier's session and its `turn_completed` read `PLAN_REVISION_REQUIRED` with the
conflict in `plan_commit_conflict`, and Project Memory keeps the verifier's PASS
with a runtime note beside it (`PLAN-v<N>`, role `plan-commit`) that the graph was
not committed and why. A commit refused on the run's own state (the run state's graph version
moved, a worker other than the requester still active, a lock still held) is
not the replanner's either: the change is rebased, a fresh replanner is raised,
and no semantic revision is spent (`RUNTIME_CONFLICT` in
`plan_verification_history`). The same refusal twice in a row does not clear by
waiting, so the second one goes to the on-call through the same door as an
exhausted budget. A wrong `schema_version` is one violation among the rest, for
a fresh plan as for a change. When the three attempts are spent, only the requester is held and the
on-call is called; it can raise a new change (`devops-request-plan-change`) with a
fresh budget that carries the old refusals as `inherited_rejections`, and so does
her `unblock --option replan`.

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
