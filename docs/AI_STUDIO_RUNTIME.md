# AI Studio role and context runtime

`src/codex_autopilot/ai_studio.py` is the common runtime boundary between the
validated task graph and every model-backed phase. It represents
implementation, independent verification, revision, planning, and replanning
with the same structured envelope. It is not an agent manager and retains no
thread, turn, transcript, conversation, or worker-session state.

## Temporary specialist roles

A role is planner-defined metadata, not a model alias. Its complete profile is
placed in the phase envelope:

- arbitrary `id` and human-readable `name`;
- responsibilities and domain focus;
- preferred tools and context priorities; and
- verification expectations.

The task independently declares execution mode, required capabilities, and
reasoning. In Adaptive `auto`, code routes to Sol and required Computer Use
routes to Astra. Changing a task's role cannot change that route. Independent
verification may use a different role, but its model is still selected from
the verification capability override rather than the role name.

## Permanent on-call role

Studio also exposes one system role outside planner-defined role metadata:
`Pipeline Engineer · On call`. The role is permanently discoverable, but a
fresh prompt can be built only for a structured infrastructure incident already
routed to `PIPELINE_ENGINEER` after bounded automatic recovery failed. The
runtime rejects production, policy, and ambiguous-side-effect incidents.

Its context is a bounded incident package with system state, recent events,
allowed/forbidden actions, recovery ownership, retry budget/backoff, and a
mandatory healthcheck. It receives no worker transcript and no task-transport
authority. See [Pipeline Engineer · On call](PIPELINE_ENGINEER.md).

## Selective context contract

Each fresh phase prompt is rebuilt from canonical inputs. The
`AUTOPILOT_CONTEXT` record contains only:

- the assigned task metadata, role, Definition of Done, resources, and
  verification contract;
- an acceptance gate containing the immutable original `user_request`, run goal,
  task DoD, and the explicit rule that implementation-authored tests are evidence
  rather than acceptance requirements;
- planner-declared memory queries and eligible Project Memory records;
- outputs from planner-selected direct dependencies whose dependency gate is
  satisfied; and
- phase-specific evidence selectors, deterministic results, or structured
  revision/replanning issues.

Project Memory selection accepts verified Truth, accepted Decisions, and active
Constraints. Explicit missing record IDs fail closed. Search and explicit IDs
are de-duplicated deterministically. Unverified Observations and open Questions
are never injected as established state.

Dependency output selection is allowed only for direct dependencies named in
`task.context.dependency_outputs`. The runtime rechecks the dependency's own
verification requirement against the current task-state snapshot. Each selected
output carries its declaration, dependency state, and bounded evidence IDs.
Existing UTF-8 file outputs add size, SHA-256, and at most a 2,000-character
excerpt; binary outputs add metadata without copying bytes.

The runtime never accepts a transcript or conversation parameter. Full worker
responses, HANDOFF prose, concurrent conversations, and session history cannot
enter the envelope through its API. Workers retrieve referenced evidence by ID
from Project Memory when the selector alone is insufficient.

An independent verifier must compare the delivered result with that acceptance
gate and may return `PASS` only after checking the original request and every DoD
item independently. This prevents an implementation and its own tests from
silently redefining what the user asked for.

## Cost and safety bounds

Task-level `max_memory_records` and `max_dependency_outputs` are further capped
by hard runtime ceilings of 20 each. Memory statements and output excerpts have
per-item limits, evidence selectors are structural and bounded, and the final
prompt has one fail-closed ceiling, `ai_studio.MAX_PROMPT_CHARS` (a quarter of the
measured context window). Every phase - worker, verifier, planner, replanner, plan
verifier, on-call - carries the rules block first, each rule with its statement
and its check verbatim (R17); the block is never cut, and a prompt that cannot hold
it is refused, never trimmed. The plan verifier's and the replanner's refusals
become a `context_budget` stop for the on-call; the on-call's own package is fitted around
the rules instead (`engineer_package_budget`: its diagnostic parts are replaced,
largest first, by a marker naming their size), so a large stop never keeps the
on-call away. Output paths are resolved under the canonical project root; an
escaping path is rejected.

The Desktop lifecycle still owns reservation tokens, fresh task creation,
resource locks, canonical-cwd preparation, and authoritative completion. The AI
Studio runtime builds one prompt at a time and owns none of that mutable state.

## Synthetic structures

The same schema and runtime represent all required shapes:

- code/integration tasks: `execution_mode=code`, repository capabilities, and
  file or logical resource claims;
- research/fact-verification tasks: a research specialist role plus explicit
  sources/tools in role metadata and `required_capabilities`; and
- mixed code/Computer Use graphs: ordinary dependency edges between code and
  `execution_mode=computer_use` tasks, with resource and Computer Use slot
  coordination remaining deterministic.

The focused regression suite is `tests/test_ai_studio.py`.
