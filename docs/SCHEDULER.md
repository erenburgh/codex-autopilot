# Deterministic dependency scheduler

`src/codex_autopilot/scheduler.py` is the non-LLM scheduling layer for the
schema-3 graph. It reads the validated `Plan` and durable `RunState`, updates
newly eligible tasks to `READY`, and returns a launch recommendation. It never
starts a model, creates a task, acquires a lock, or performs an external call.
The runtime must journal a selected task's transition to `RUNNING` separately.

## Readiness and verification gate

A task can enter `READY` only from `WAITING`, and only when every direct
dependency is `VERIFIED`. `dependency_state_satisfies()` in `task_state.py`
accepts no other state, so `IMPLEMENTED` unlocks nothing; a canonical task
cannot declare a weaker rule for itself, because `validate_plan()` requires
`independent` and required verification. The scheduler reuses that
state-machine gate rather than maintaining a second interpretation. It also
rejects plan/run-state graph-version mismatches and an active-task journal
that does not exactly match active task states.

For the reference graph

```text
A     B
|\   /
| \ /
C  D
 \ /
  E
```

where `C <- A`, `D <- A+B`, and `E <- C+D`, the READY sets are:

| Verified state | READY set |
| --- | --- |
| none | `A`, `B` |
| `A` is only `IMPLEMENTED` | `B` |
| `A` | `B`, `C` |
| `A`, `B` | `C`, `D` |
| `A`, `B`, `C` | `D` |
| `A`, `B`, `C`, `D` | `E` |

## Stable priority policy

Every READY frontier uses this total order:

1. tasks admitted by the current resource-availability snapshot before tasks
   whose resources are unavailable;
2. higher explicit task `priority`;
3. longer remaining static critical path, including the task itself;
4. larger transitive fan-out;
5. older logical READY age;
6. earlier plan declaration, then task ID as a final total-order guard.

READY age is not wall-clock time. `RunState.scheduler_sequence` increases when a
task newly enters READY, and `task_ready_since` stores that sequence. This makes
fairness reproducible across restarts and ensures equal-priority/equal-relevance
tasks that have waited longer win. Resource availability is supplied as a
deterministic task-level snapshot. Normalized matching, durable acquisition,
crash reconciliation, and the shared-working-tree contract are specified in
[Resource coordination](RESOURCES.md).

Admission is a stable greedy pass through that order. A task is deferred if its
resource snapshot is unavailable, a required named capability is unavailable,
a capability has reached its configured limit, or all worker slots are full.
Selecting a task consumes one unit of every required named capability for the
rest of that decision. A `computer_use` task also consumes one independent
`computer_use` capability slot. Missing named capability limits mean unbounded;
an explicit limit of zero disables admission for that capability.

## Strategies and limits

- `serial` always has an effective worker limit of one, even if another layer
  contains a larger numeric limit. More than one active task is rejected.
- `parallel` greedily fills the safe READY frontier up to the worker and
  capability limits.
- `auto` uses the same bounded safe admission and naturally runs one task when
  only one fits, or multiple independent tasks when the frontier and limits
  allow it. Model choice and reasoning complexity never affect scheduling.

The effective worker limit is the lower of the plan and durable run-state
limits. Either layer can tighten execution to `serial`; neither can make the
other more permissive. `max_parallel_workers` defaults to `10` and `computer_use_slots` to `1`, so a
fresh run is genuinely parallel while two Computer Use workers never overlap. A
config without a `[runtime]` section, and every migrated v0.8 project, stay at
one worker instead.
