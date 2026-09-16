"""Разбор плана v0.8 - отдельно от канонической схемы.

Формат снят с поддержки как способ ставить новую работу и живёт только
как путь миграции существующего прогона. Держать его в `plan.py` значит
смешивать действующий контракт с историческим; здесь он не мешает читать
основной и не растит модуль сверх предела раздела 0.

Модуль импортируется лениво, из тела вызова в `plan.py`, поэтому его
собственные импорты оттуда безопасны: к моменту первого обращения
`plan` уже загружен целиком.
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
    """Принять план v0.8 - но только как миграцию существующего прогона.

    Исключение из порога приёмки историческое: переписывать контракт уже
    идущего прогона нельзя. У свежего проекта истории нет, переписывать
    нечего, и исключению неоткуда взяться. Прежде формат сам по себе был
    обходом: любой мог подать schema-2 для нового проекта и освободить
    все задачи от независимой верификации - это самопринятие (R8).

    Поэтому происхождение доказывается не форматом, а прогоном, который
    мигрируют: `migrated_milestone_ids` несёт вехи, уже существовавшие в
    нём. Веха, которой там не было, - это новая работа, и формат v0.8
    выразить для неё независимую приёмку не может вовсе: поля
    `verification` в нём нет. Новая работа добавляется после миграции
    канонической сменой плана.
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
