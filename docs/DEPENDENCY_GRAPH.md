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

The required invariant is that `IMPLEMENTED` is distinct from `VERIFIED`, and
only a result satisfying its declared verification policy may unlock a
dependent. The candidate correctly keeps those states distinct. It does not yet
honor the different `self`, `deterministic`, `independent`, and `auto` policy
outcomes: every successful implementation is currently forced through a fresh
independent verifier. This is a release blocker recorded in
`RELEASE_VERIFICATION_0.9.0-beta.md`.

See also `TASK_GRAPH.md`, `SCHEDULER.md`, and
`PLAN_EVOLUTION_AND_RECOVERY.md` for the implementation-level schema and state
transitions.
