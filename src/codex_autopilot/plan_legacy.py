"""Parsing a v0.8 plan - apart from the canonical schema.

The format is no longer supported as a way to submit new work and lives
only as the migration path of an existing run. Keeping it in `plan.py`
would mix the current contract with the historical one; here it does not
get in the way of reading the main one and does not push the module past
the section-0 limit.

The module is imported lazily, from a call body in `plan.py`, so its own
imports from there are safe: by the first use `plan` is fully loaded.
"""

from __future__ import annotations

from typing import Any

from .plan import (
    LEGACY_PLAN_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    Plan,
    RoleProfile,
    Task,
    _identifier,
    _plan_header,
    _reject_unknown,
    _role_from_raw,
    _task_from_raw,
    _validate_graph,
    _validate_unique,
)


def validate_legacy_plan(
    data: dict[str, Any],
    profile: str,
    *,
    migrated_milestone_ids: frozenset[str] | None = None,
) -> Plan:
    """Accept a v0.8 plan - but only as the migration of an existing run.

    The exemption from the acceptance floor is historical: the contract of
    a run already under way cannot be rewritten. A fresh project has no
    history, nothing to rewrite, and no source for an exemption. The format
    by itself used to be a bypass: anyone could submit schema-2 for a new
    project and free every task from independent verification -
    self-acceptance (R8).

    So provenance is proven not by the format but by the run being
    migrated: `migrated_milestone_ids` carries the milestones that already
    existed in it. A milestone that was not there is new work, and the v0.8
    format cannot express independent acceptance for it at all: it has no
    `verification` field. New work is added after migration by a canonical
    plan change.
    """

    if migrated_milestone_ids is None:
        raise ValueError(
            "a v0.8 plan is accepted only when migrating an existing run; "
            "this project has none, so its milestones must use the canonical "
            f"v{PLAN_SCHEMA_VERSION} schema with independent verification"
        )
    _reject_unknown(
        data,
        {"schema_version", "goal", "user_request", "model_strategy", "roles", "milestones"},
        "plan",
    )
    goal, user_request, strategy = _plan_header(data, profile)
    raw_milestones = data.get("milestones")
    if not isinstance(raw_milestones, list) or not raw_milestones:
        raise ValueError("plan.milestones must be a non-empty array")
    raw_roles = data.get("roles")
    if raw_roles is None:
        roles = (
            RoleProfile(
                id="legacy-worker",
                name="Legacy serial worker",
                responsibilities=("Execute one migrated v0.8 milestone at a time.",),
                context_priorities=("Current milestone and bounded Project Memory records.",),
                verification_expectations=("Record new milestone evidence before completion.",),
            ),
        )
    else:
        if not isinstance(raw_roles, list) or not raw_roles:
            raise ValueError("plan.roles must be a non-empty array")
        roles = tuple(_role_from_raw(raw, index) for index, raw in enumerate(raw_roles, 1))
        _validate_unique((role.id for role in roles), "role id")
        if any(
            role.id == "legacy-worker" or role.name.casefold() == "legacy serial worker"
            for role in roles
        ):
            raise ValueError(
                "structured legacy roles must use a concrete RoleProfile, not generic legacy-worker"
            )
    tasks: list[Task] = []
    previous_id: str | None = None
    for index, raw in enumerate(raw_milestones, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"milestone {index} must be an object")
        _reject_unknown(
            raw,
            {
                "id",
                "title",
                "objective",
                "definition_of_done",
                "execution_mode",
                "execution_mode_reason",
                "reasoning",
                "role",
            },
            f"milestone {index}",
        )
        if raw_roles is None and "role" in raw:
            raise ValueError(
                f"milestone {index}.role requires plan.roles; role identity is never inferred"
            )
        if raw_roles is not None and "role" not in raw:
            raise ValueError(
                f"milestone {index}.role is required when plan.roles preserves structured roles"
            )
        task_id = _identifier(raw.get("id", f"M{index}"), f"milestone {index}.id")
        if task_id not in migrated_milestone_ids:
            known = ", ".join(sorted(migrated_milestone_ids)) or "none"
            raise ValueError(
                f"milestone {task_id} is not part of the run being migrated "
                f"(it has: {known}); new work is added by a canonical plan "
                "change, not under the legacy format"
            )
        role_id = (
            _identifier(raw.get("role"), f"milestone {index}.role")
            if raw_roles is not None
            else "legacy-worker"
        )
        tasks.append(
            _task_from_raw(
                raw,
                profile,
                f"milestone {index}",
                canonical=False,
                task_id=task_id,
                role=role_id,
                depends_on=(previous_id,) if previous_id else (),
            )
        )
        previous_id = task_id
    _validate_unique((task.id for task in tasks), "milestone id")
    plan = Plan(
        goal=goal,
        user_request=user_request,
        model_strategy=strategy,
        tasks=tuple(tasks),
        roles=roles,
        graph_version=1,
        execution_strategy="serial",
        max_parallel_workers=1,
        computer_use_slots=1,
        source_schema_version=LEGACY_PLAN_SCHEMA_VERSION,
        legacy_serial=True,
    )
    _validate_graph(plan)
    return plan
