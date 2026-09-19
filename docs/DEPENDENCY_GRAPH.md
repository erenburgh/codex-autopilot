# Dependency graph

The schema-3 plan in `src/codex_autopilot/plan.py` is the canonical v0.9 task
graph. A task carries its ID, title, objective, role, execution mode and reason,
reasoning level, verification policy, dependencies, priority, resource claims,
Definition of Done, optional capabilities, context selectors, outputs, and tags.
Roles are declared separately and referenced by ID.

`validate_plan()` rejects unknown references, self-dependencies, duplicate IDs,
invalid dependency-output selectors, and cycles. `validate_plan_change()` applies
the same checks to a replacement graph and additionally requires an exact
one-step `graph_version` increment while preserving the run goal, original user
request, and model strategy. `topological_order()` is stable for equal choices.

The scheduler derives READY work from the graph; declaration order is only the
last deterministic tie-breaker and does not create implicit dependencies.
Downstream output context is admitted only after the dependency gate succeeds.

## Verification gate status

`IMPLEMENTED` is distinct from `VERIFIED`, and only `VERIFIED` unlocks a
dependent: `dependency_state_satisfies()` in `task_state.py` accepts no other
state. The graph has no weaker path, because `validate_plan()` refuses a
canonical task whose verification is not `independent` and required. `self`,
`deterministic`, and `auto` stay in the schema — a migrated v0.8 task carries
`self` — and cannot be declared for new canonical work.

See also `TASK_GRAPH.md`, `SCHEDULER.md`, and
`PLAN_EVOLUTION_AND_RECOVERY.md` for the implementation-level schema and state
transitions.
