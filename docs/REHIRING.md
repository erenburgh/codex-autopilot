# Re-hiring · what happens when acceptance refuses

## Short answer

A verifier's refusal never stops the run. The task gets a new attempt with
the full list of complaints, and when this executor's attempts run out — a
new executor. It stops only at the top of the hiring ladder, and then that is
a conscious decision of the product owner, not a silent death of the pipeline.

## The revision cycle

1. The verifier returns `REVISE` with structured issues. Each has exactly
   four fields: `code`, `summary`, `details`, `dod_refs` — that is, not
   "bad", but what exactly and which Definition of Done item is violated.
2. The task moves to `REVISION_REQUIRED`; the issues are saved in the state.
3. The dispatcher's next pass reserves a **fresh** revision worker `R{n}` and
   puts the issues into its prompt. Starting without issues is forbidden at
   two levels: by the run-state schema and by the reservation itself.
4. Revision closed → `IMPLEMENTED` → a fresh independent verifier again.

The cycle's budget is the task's `verification.max_revision_attempts`.

## The hiring ladder

When the current executor's revision budget is exhausted, what changes is
neither the plan nor the bar, but **the way the result is reached and who
reaches it**:

```
medium -> high -> xhigh -> max
```

Each step is a new hire: a fresh worker, raised effort, the whole accumulated
list of complaints, a new revision budget. The revision counter stays
continuous, so that the `R{n}` numbering and the attempt history are not
lost.

What does **not** change on a re-hire: the graph, the Definition of Done, the
deterministic checks, the plan version. Otherwise acceptance would move to
meet the work, rather than the other way round.

State: `task_rehires[task_id]` — how many times the task was re-hired;
`task_effort[task_id]` — the assigned step on top of the one recorded in the
plan. Journal: the `task_rehired` event with the fields `hire`,
`effort_from`, `effort_to`.

## Why the model is not a step

In the `auto` strategy the model is tied hard to the task's `execution_mode`:
`astra` means a declared Computer Use capability, `sol` means its absence.
Swapping the model for quality would swap the declared capability, not the
diligence, so the ladder raises only the effort.

## The top of the ladder

When no steps remain, the task moves to `BLOCKED`, the journal receives the
`hiring_ladder_exhausted` event, and `last_error` names the number of hires,
the step reached and the number of attempts. The task status additionally
prints the acceptance complaints themselves — otherwise the owner has
nothing to decide with.

The stop goes through the one stop door (`blocked_runs.stop_run`) like every
other: a `RUNTIME` ticket with `stop_kind=ladder_exhausted` that holds this
task only, routed to the on-call at once. The on-call looks first - whether the
refusals are about the work or about a gate, rubric or runtime defect. A
judgement about the work stays the owner's: accepting it is never automated,
and when the call is hers the engineer hands the ticket up with its diagnosis
and recommendation.

Until 0.13.1 this ticket was deliberately not routed, because the engineer then
came instead of the work and would have frozen the neighbours; it was to be
routed when the run went idle, and in practice nobody came.

## Neighbouring tasks

A re-hire does not touch the shared graph, merges nothing and does not change
the plan version, so tasks that do not depend on the stalled one keep going.
The on-call is reserved next to them, in the same completion, and takes no
work slot. The run's status is derived: `BLOCKED` (`AWAITING_OWNER`) appears
only when nothing can be taken and what is left waits for the owner.

## Extension point

Today a ladder step is effort, because that is the only lever that exists in
the runtime. When versioned competence profiles appear, the step becomes a
change of the executor's profile rather than a raise of effort. The content of
the step changes; the event, the counters, the boundaries and the guarantee
that the Definition of Done stays untouched remain the same.

The attachment point is `_rehire_or_block_on_revision_limit` in
`src/codex_autopilot/lifecycle_base.py` and `next_effort_step` in
`src/codex_autopilot/models.py`.
