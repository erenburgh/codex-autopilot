# v0.9 task graph contract

`src/codex_autopilot/plan.py` is the executable schema for plan schema 3.
`src/codex_autopilot/task_state.py` is the executable state-machine and
dependency-gate contract. Unknown fields are rejected instead of ignored. All
references and the complete graph are validated when a plan is loaded and again
before a plan change is atomically saved.

## Ownership boundaries

The three durable concerns remain separate:

- `plan.json` contains the versioned, declarative task graph. It has no mutable
  task status, lock owner, thread ID, or verification result.
- `run-state.json` contains mutable task states, attempts, revisions, active
  task IDs, graph version, execution strategy, concurrency limits, and logical
  READY-age sequences used by the deterministic scheduler.
- the existing Project Memory SQLite database remains the only canonical store
  for evidence, Truth, Observations, Decisions, Constraints, Questions, and
  conflicts. The DAG does not introduce another knowledge or evidence store.

This separation makes graph updates validate-able before they affect execution
and preserves the Project Memory rule **NO EVIDENCE → NO TRUTH**.

## Plan schema 3

Required plan fields:

| Field | Contract |
| --- | --- |
| `schema_version` | Integer `3`. |
| `goal` | Non-empty string. A plan change cannot replace it. |
| `user_request` | Verbatim initiating request used by the independent acceptance gate. New planners must write it and plan changes cannot replace it. |
| `model_strategy` | `auto`, `sol-only`, `astra-only`, or `host-settings`, constrained by the selected profile. |
| `roles` | Non-empty array of role profiles with unique IDs. |
| `tasks` | Non-empty array of tasks with unique IDs. |

Optional plan fields and safe defaults:

| Field | Default | Contract |
| --- | --- | --- |
| `graph_version` | `1` | Positive integer. A plan change must increment exactly once. |
| `execution_strategy` | `serial` | `serial`, `parallel`, or `auto`. |
| `max_parallel_workers` | `1` | Positive integer; scheduler ceiling. |
| `computer_use_slots` | `1` | Positive integer; independent Computer Use ceiling. |
| `compatibility` | absent | Migration provenance only. A `legacy_serial` plan must remain `serial` with one worker. |

For backward readability, pre-existing schema-3 files that lack `user_request`
load with `goal` as their acceptance source. New plan creation never relies on
that fallback.

The conservative defaults intentionally do not opt a plan into concurrency.
The scheduler may use parallel execution only when the canonical plan and
runtime configuration explicitly allow it.

## Role profile

Every role requires `id`, `name`, and a non-empty `responsibilities` array. The
optional `domain_focus`, `preferred_tools`, `context_priorities`, and
`verification_expectations` arrays describe temporary specialist behavior.
Role metadata never names or selects a model; model routing continues to follow
the task's required capability and the run's model strategy.

## Task

Required fields:

| Field | Contract |
| --- | --- |
| `id` | Stable identifier matching `[A-Za-z][A-Za-z0-9._-]{0,63}`. |
| `title` | Non-empty human-readable title. |
| `objective` | Non-empty bounded unit of work. |
| `definition_of_done` | Non-empty array of non-empty criteria. |
| `execution_mode` | `code` or `computer_use`. |
| `execution_mode_reason` | Concrete non-empty capability reason. |
| `reasoning` | Required and normalized for Adaptive; forbidden for Host Settings. |
| `role` | Existing role ID. |
| `verification` | Structured verification policy described below. |

Optional fields:

| Field | Default | Contract |
| --- | --- | --- |
| `depends_on` | `[]` | Unique task IDs; no self-reference or cycle. |
| `priority` | `0` | Integer from -1,000,000 through 1,000,000. |
| `resources` | `[]` | Structured resource claims. |
| `required_capabilities` | `[]` | Unique capability names used independently of role metadata. |
| `context` | bounded empty context | Selective Project Memory queries/IDs and direct dependency outputs. |
| `outputs` | `[]` | Structured output IDs, descriptions, optional paths, and required flags. |
| `tags` | `[]` | Unique task labels. |

`context.dependency_outputs` may reference only direct dependencies. Context
also carries `memory_queries`, `memory_record_ids`, `max_memory_records`, and
`max_dependency_outputs`; the defaults are eight apiece. These are selectors
into Project Memory and dependency results, not copies of either store.

At prompt construction time, the AI Studio Runtime applies hard ceilings of 20
memory records and 20 dependency outputs even when a task requests larger
limits. It rejects missing explicit memory IDs, excludes unverified memory from
established state, rechecks dependency verification, confines output paths to
the project root, and includes only bounded text excerpts. See
[AI Studio role and context runtime](AI_STUDIO_RUNTIME.md).

## Verification policy

`verification.policy` is one of `self`, `deterministic`, `independent`, or
`auto`. The required v0.9 behavior is: `self` accepts the implementer's
evidence-backed self-check; `deterministic` promotes an exhaustive all-pass
result without a second LLM; `independent` always uses a fresh verifier; and
`auto` selects the least costly sufficient route from risk and deterministic
coverage. `required` defaults to `true`; when explicitly false, an implemented
result may satisfy the dependency gate.
`max_revision_attempts` defaults to two.
Optional verifier routing fields are `verifier_role`, `execution_mode` plus its
required `execution_mode_reason`, and Adaptive-only `reasoning`.

`deterministic_checks` is an array of unique check IDs. For a deterministic
policy, an exhaustive all-pass result may produce `VERIFIED`; a failure creates
structured revision work. Check kinds
are:

- `command`: requires an argument vector in `argv`; shell command strings are
  deliberately not part of the schema. It may set `timeout_seconds` and
  `expected_exit_code`.
- `artifact`: requires `path`.
- `evidence`: names required evidence without embedding or duplicating it.

A required deterministic policy must declare at least one check. Evidence
produced by any policy is still recorded through Project Memory.

The current candidate parser supports this schema, but the lifecycle still
forces every policy through an independent verifier and ignores
`required=false` for dependency satisfaction. That mismatch is tracked as a
release blocker in `RELEASE_VERIFICATION_0.9.0-beta.md`.

Runtime semantics, the strict verifier result protocol, fresh-context boundary,
and revision-attempt behavior are specified in
[Verification and revision lifecycle](VERIFICATION_LIFECYCLE.md).

## Resource claim

Each claim requires a unique task-local `id`, a `kind`, a non-empty `target`,
and an `access` mode. Kinds are `path`, `directory`, `glob`, `application`,
`environment`, `browser`, `device`, `external_sandbox`, and `logical`. Access is
`read`, `write`, or `exclusive`. `description` is optional. This milestone
defines the declaration; normalized conflict and durable lock semantics build
on it without changing the plan shape.

## State machine

The durable task states are:

`WAITING`, `READY`, `RUNNING`, `IMPLEMENTED`, `VERIFYING`,
`REVISION_REQUIRED`, `REVISING`, `RETRY_WAIT`, `VERIFIED`, `BLOCKED`,
`FAILED`, and `CANCELLED`.

Legal successful flow is explicit:

```text
WAITING → READY → RUNNING → IMPLEMENTED
                                  └─ fresh independent verifier → VERIFYING → VERIFIED
                                                                       └→ REVISION_REQUIRED
                                                                                → REVISING
                                                                                → IMPLEMENTED
```

Retry, block, failure, and cancellation edges are enumerated in
`TASK_TRANSITIONS`. `RUNNING → VERIFIED` and `IMPLEMENTED → VERIFIED` are never
legal. Thus an implementer's success, self-report, or passing
implementer-authored tests cannot mechanically masquerade as an accepted
result.

A dependency is satisfied only when its task is `VERIFIED` by a fresh
independent verifier.

Transitions into `READY` or `RUNNING` re-check this gate. Complete state maps
also reject unknown states, unknown task IDs, and active/advanced tasks whose
dependencies are not satisfied.

The scheduler is specified separately in [Deterministic dependency
scheduler](SCHEDULER.md). It is the only component that promotes newly eligible
`WAITING` tasks to `READY`; it does not launch workers or turn selections into
`RUNNING` without the runtime's durable external-action journal.

## Plan changes

A plan change is a complete schema-3 replacement, not an unchecked patch.
`validate_plan_change` requires the same original `user_request`, goal, and model
strategy and exactly the next graph version. It runs all nested schema, role,
dependency, output-context, and cycle checks. `save_plan_change` performs no
write until validation has succeeded, then uses the existing atomic JSON
replacement path.

At runtime, workers request one of four typed changes—`prerequisite`,
`dependency`, `resource`, or `verification`—through the final
`PLAN_CHANGE_REQUEST` protocol. The active frontier drains before exactly one
fresh bounded replanner returns a complete replacement graph. Existing task IDs
and verified task contracts are immutable. A durable redo transaction commits
the plan and reconciled run state as one recoverable graph-version change. See
[Plan evolution and recovery](PLAN_EVOLUTION_AND_RECOVERY.md).

## v0.8 compatibility

Schema-2 plans (and bootstrap plans that omit a version and use `milestones`)
load additively:

- milestone IDs and order are preserved;
- each milestone after the first depends on its predecessor, producing a valid
  chain-shaped DAG;
- structured `roles` and each milestone's explicit `role` are preserved when
  present; the migration refuses to infer a missing role or collapse a concrete
  role into `legacy-worker`;
- only a truly role-less legacy input receives the compatibility-only
  `legacy-worker` role and readable legacy verification metadata; the runtime
  still requires a fresh independent verifier before `VERIFIED`;
- execution is pinned to `serial`, `max_parallel_workers=1`, and
  `computer_use_slots=1`;
- a canonical save writes schema 3 plus explicit `compatibility` provenance.

A v0.8 `config.toml` without `[runtime]` loads with the same serial limits. A
schema-4 `run-state.json` migrates in memory to schema 5 with one active task at
most and preserves verified history, current attempt, and retry/block state.
Legacy cursor-to-complete-state conversion is deterministic. No legacy input
silently enables parallel execution.
