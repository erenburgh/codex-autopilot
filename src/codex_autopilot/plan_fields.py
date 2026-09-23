"""The field sets of a schema-3 plan: one source for the parser and the prompt.

The parser in ``plan.py`` enforced its nested field sets as inline literals,
and the replanner's prompt named only the top-level list plus the department
and rubric sets. For a task, a verification, a check, a resource, an output
and a context the model saw no list at all: a refusal "task 3 has unknown
fields" told it what was wrong and nothing of what is right, so the next
attempt guessed again (R31). A real run spent its whole budget of three
attempts that way on a department field.

So the sets live here, the parser rejects by them, and the prompt hands the
model ``ALLOWED_FIELDS`` whole. A field accepted by the parser and missing
from here, or the other way round, is a drift the tests catch.

No imports from ``plan``: this module is read by it at import time.
"""

from __future__ import annotations

from .department_acceptance import DEPARTMENT_FIELDS, RUBRIC_REFERENCE_FIELDS

PLAN_FIELDS: tuple[str, ...] = (
    "schema_version", "graph_version", "goal", "user_request", "goal_contract",
    "model_strategy", "execution_strategy", "max_parallel_workers", "computer_use_slots",
    "roles", "departments", "skill_packs", "tasks", "compatibility",
)
COMPATIBILITY_FIELDS: tuple[str, ...] = ("migrated_from_schema", "legacy_serial")
ROLE_FIELDS: tuple[str, ...] = (
    "id", "name", "version", "responsibilities", "domain_focus", "preferred_tools",
    "context_priorities", "verification_expectations", "skill_requirements",
)
TASK_FIELDS: tuple[str, ...] = (
    "id", "title", "objective", "definition_of_done", "execution_mode",
    "execution_mode_reason", "reasoning", "role", "depends_on", "priority",
    "verification", "resources", "required_capabilities", "context", "outputs", "tags",
    "produces_outcomes", "acceptance_class", "loaded_skills", "skill_attestation",
)
VERIFICATION_FIELDS: tuple[str, ...] = (
    "policy", "required", "deterministic_checks", "verifier_role", "execution_mode",
    "execution_mode_reason", "reasoning", "max_revision_attempts",
)
CHECK_FIELDS: tuple[str, ...] = (
    "id", "kind", "description", "argv", "path", "timeout_seconds", "expected_exit_code",
)
RESOURCE_FIELDS: tuple[str, ...] = ("id", "kind", "target", "access", "description")
OUTPUT_FIELDS: tuple[str, ...] = ("id", "description", "path", "required")
CONTEXT_FIELDS: tuple[str, ...] = (
    "memory_queries", "memory_record_ids", "dependency_outputs", "max_memory_records",
    "max_dependency_outputs",
)

# Where each set applies, by the path a refusal names. The prompt carries
# this mapping whole.
ALLOWED_FIELDS: dict[str, tuple[str, ...]] = {
    "plan": PLAN_FIELDS,
    "plan.compatibility": COMPATIBILITY_FIELDS,
    "plan.roles[]": ROLE_FIELDS,
    "plan.departments[]": tuple(DEPARTMENT_FIELDS),
    "plan.departments[].rubric": tuple(RUBRIC_REFERENCE_FIELDS),
    "plan.tasks[]": TASK_FIELDS,
    "plan.tasks[].verification": VERIFICATION_FIELDS,
    "plan.tasks[].verification.deterministic_checks[]": CHECK_FIELDS,
    "plan.tasks[].resources[]": RESOURCE_FIELDS,
    "plan.tasks[].outputs[]": OUTPUT_FIELDS,
    "plan.tasks[].context": CONTEXT_FIELDS,
}
