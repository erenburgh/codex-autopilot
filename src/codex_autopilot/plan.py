from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable

from .models import EXECUTION_MODES, STRATEGIES
from .reasoning import normalize


PLAN_FILE = "plan.json"
PLAN_SCHEMA_VERSION = 3
LEGACY_PLAN_SCHEMA_VERSION = 2

EXECUTION_STRATEGIES = {"serial", "parallel", "auto"}
VERIFICATION_POLICIES = {"self", "deterministic", "independent", "auto"}
VERIFICATION_CHECK_KINDS = {"command", "artifact", "evidence"}
RESOURCE_KINDS = {
    "path",
    "directory",
    "glob",
    "application",
    "environment",
    "browser",
    "device",
    "external_sandbox",
    "logical",
}
RESOURCE_ACCESS_MODES = {"read", "write", "exclusive"}

# M10-REV-004: новый schema-3 прогон по умолчанию входит в заявленный
# режим v0.9. Мигрированные v0.8 планы этим не затрагиваются: они несут
# execution_strategy="serial", max_parallel_workers=1 и legacy_serial=True
# явно, и валидация не даёт им неявно уйти в параллельность.
DEFAULT_EXECUTION_STRATEGY = "auto"

# Значения для КОНФИГА БЕЗ секции [runtime], то есть для проекта,
# созданного до v0.9. Такой проект остаётся serial и одномерным явно,
# а не уезжает в параллельность из-за смены дефолта нового прогона.
COMPAT_EXECUTION_STRATEGY = "serial"
COMPAT_MAX_PARALLEL_WORKERS = 1
# Консервативный, но реально параллельный предел: два воркера дают
# настоящую параллельность при минимальном росте нагрузки и расхода.
DEFAULT_MAX_PARALLEL_WORKERS = 2
DEFAULT_COMPUTER_USE_SLOTS = 1
DEFAULT_MAX_MEMORY_RECORDS = 8
DEFAULT_MAX_DEPENDENCY_OUTPUTS = 8

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class RoleProfile:
    """Planner-defined specialist behavior; it never selects a model."""

    id: str
    name: str
    responsibilities: tuple[str, ...]
    domain_focus: tuple[str, ...] = ()
    preferred_tools: tuple[str, ...] = ()
    context_priorities: tuple[str, ...] = ()
    verification_expectations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    """A declarative check. Command checks use argv and never a shell string."""

    id: str
    kind: str
    description: str
    argv: tuple[str, ...] = ()
    path: str | None = None
    timeout_seconds: int = 300
    expected_exit_code: int = 0


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    policy: str
    required: bool
    deterministic_checks: tuple[VerificationCheck, ...] = ()
    verifier_role: str | None = None
    execution_mode: str | None = None
    execution_mode_reason: str | None = None
    reasoning: str | None = None
    max_revision_attempts: int = 2


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    id: str
    kind: str
    target: str
    access: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class TaskOutput:
    id: str
    description: str
    path: str | None = None
    required: bool = True


@dataclass(frozen=True, slots=True)
class TaskContext:
    memory_queries: tuple[str, ...] = ()
    memory_record_ids: tuple[str, ...] = ()
    dependency_outputs: tuple[str, ...] = ()
    max_memory_records: int = DEFAULT_MAX_MEMORY_RECORDS
    max_dependency_outputs: int = DEFAULT_MAX_DEPENDENCY_OUTPUTS


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    title: str
    objective: str
    definition_of_done: tuple[str, ...]
    execution_mode: str
    execution_mode_reason: str
    reasoning: str | None
    role: str
    depends_on: tuple[str, ...]
    priority: int
    verification: VerificationPolicy
    resources: tuple[ResourceClaim, ...]
    required_capabilities: tuple[str, ...]
    context: TaskContext
    outputs: tuple[TaskOutput, ...]
    tags: tuple[str, ...]


# v0.8 callers use the old name. A milestone is now a graph task, not a second
# contract, so this alias is intentionally additive.
Milestone = Task


@dataclass(frozen=True, slots=True)
class Plan:
    goal: str
    user_request: str
    model_strategy: str
    tasks: tuple[Task, ...]
    roles: tuple[RoleProfile, ...]
    graph_version: int = 1
    execution_strategy: str = DEFAULT_EXECUTION_STRATEGY
    max_parallel_workers: int = DEFAULT_MAX_PARALLEL_WORKERS
    computer_use_slots: int = DEFAULT_COMPUTER_USE_SLOTS
    source_schema_version: int = PLAN_SCHEMA_VERSION
    legacy_serial: bool = False

    @property
    def milestones(self) -> tuple[Task, ...]:
        """Compatibility view for the v0.8 serial orchestrator."""

        return self.tasks

    @property
    def task_map(self) -> dict[str, Task]:
        return {item.id: item for item in self.tasks}

    @property
    def role_map(self) -> dict[str, RoleProfile]:
        return {item.id: item for item in self.roles}


def validate_plan(data: dict[str, Any], profile: str) -> Plan:
    """Load either a canonical v0.9 graph or a v0.8 serial plan.

    A v0.8 plan is represented in memory as a valid chain-shaped DAG. It is
    explicitly pinned to serial execution with one worker and one Computer Use
    slot; migration never opts a legacy project into parallel execution.
    """

    if profile not in {"adaptive", "host-settings"}:
        raise ValueError("profile must be adaptive or host-settings")
    if not isinstance(data, dict):
        raise ValueError("plan must be an object")
    schema = data.get("schema_version")
    if schema in {None, LEGACY_PLAN_SCHEMA_VERSION} and "milestones" in data:
        return _validate_legacy_plan(data, profile)
    if schema != PLAN_SCHEMA_VERSION:
        raise ValueError(
            f"plan.schema_version must be {PLAN_SCHEMA_VERSION}; "
            f"v0.8 serial plans may use {LEGACY_PLAN_SCHEMA_VERSION} or omit it"
        )
    return _validate_graph_plan(data, profile)


def validate_plan_change(current: Plan, data: dict[str, Any], profile: str) -> Plan:
    """Validate a complete replacement graph before any durable write."""

    candidate = validate_plan(data, profile)
    # Migration provenance can remain schema 2 inside a canonical schema-3
    # graph. Check the submitted format, preserving its serial compatibility.
    if data.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("plan changes must use the canonical v0.9 schema")
    if candidate.graph_version != current.graph_version + 1:
        raise ValueError(
            "plan change graph_version must increment exactly once "
            f"({current.graph_version} -> {current.graph_version + 1})"
        )
    if candidate.goal != current.goal:
        raise ValueError("plan changes must not replace the run goal")
    if candidate.user_request != current.user_request:
        raise ValueError("plan changes must not replace the original user request")
    if candidate.model_strategy != current.model_strategy:
        raise ValueError("plan changes must not replace model_strategy")
    # validate_plan already performs all role, output, dependency, and cycle
    # checks. Keeping this wrapper mandatory prevents a plan-change path from
    # accidentally treating initial-load validation as optional.
    return candidate


def save_plan_change(
    state_dir: Path,
    current: Plan,
    data: dict[str, Any],
    profile: str,
) -> Plan:
    candidate = validate_plan_change(current, data, profile)
    save_plan(state_dir, candidate)
    return candidate


def load_plan(state_dir: Path, profile: str) -> Plan:
    data = json.loads((state_dir / PLAN_FILE).read_text(encoding="utf-8"))
    return validate_plan(data, profile)


def save_plan(state_dir: Path, plan: Plan) -> None:
    atomic_json(state_dir / PLAN_FILE, plan_to_dict(plan))


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "graph_version": plan.graph_version,
        "goal": plan.goal,
        "user_request": plan.user_request,
        "model_strategy": plan.model_strategy,
        "execution_strategy": plan.execution_strategy,
        "max_parallel_workers": plan.max_parallel_workers,
        "computer_use_slots": plan.computer_use_slots,
        "roles": [_role_to_dict(item) for item in plan.roles],
        "tasks": [_task_to_dict(item) for item in plan.tasks],
    }
    if plan.legacy_serial:
        payload["compatibility"] = {
            "migrated_from_schema": plan.source_schema_version,
            "legacy_serial": True,
        }
    return payload


def topological_order(plan: Plan) -> tuple[str, ...]:
    """Return a stable dependency order, using declaration order for ties."""

    rank = {task.id: index for index, task in enumerate(plan.tasks)}
    indegree = {task.id: len(task.depends_on) for task in plan.tasks}
    dependents: dict[str, list[str]] = {task.id: [] for task in plan.tasks}
    for task in plan.tasks:
        for dependency in task.depends_on:
            dependents[dependency].append(task.id)
    ready = sorted((task_id for task_id, count in indegree.items() if count == 0), key=rank.get)
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for dependent in sorted(dependents[current], key=rank.get):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=rank.get)
    if len(result) != len(plan.tasks):
        # This is defensive; all Plan instances created by validate_plan have
        # already passed the more descriptive DFS cycle validator.
        raise ValueError("plan task graph contains a cycle")
    return tuple(result)


def _validate_legacy_plan(data: dict[str, Any], profile: str) -> Plan:
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


def _reject_self_acceptance(plan: "Plan") -> None:
    """Правило R8 (ENFORCED): каноническая задача не принимает сама себя.

    policy="self" означает, что вердикт выносит тот же воркер, который
    делал работу. Именно так восемь задач из девяти получили VERIFIED
    в ту же секунду, что и IMPLEMENTED, и именно поэтому неверные
    реализации проходили дальше по графу.

    Мигрированный план v0.8 (legacy_serial) - единственное исключение:
    он предшествует появлению верификации, и менять его задним числом
    значило бы переписывать историю чужого прогона. Такой план остаётся
    serial и новую работу в этом режиме не принимает.
    """

    if plan.legacy_serial:
        return
    offenders = [task.id for task in plan.tasks if task.verification.policy == "self"]
    if offenders:
        raise ValueError(
            "R8: policy=\"self\" запрещена для канонических задач "
            f"{', '.join(offenders)}; задача не может принимать сама себя. "
            "Используй deterministic, independent или auto"
        )


def _validate_graph_plan(data: dict[str, Any], profile: str) -> Plan:
    _reject_unknown(
        data,
        {
            "schema_version",
            "graph_version",
            "goal",
            "user_request",
            "model_strategy",
            "execution_strategy",
            "max_parallel_workers",
            "computer_use_slots",
            "roles",
            "tasks",
            "compatibility",
        },
        "plan",
    )
    goal, user_request, strategy = _plan_header(
        data,
        profile,
        require_user_request=True,
    )
    graph_version = _positive_int(data.get("graph_version", 1), "plan.graph_version")
    execution_strategy = str(data.get("execution_strategy", DEFAULT_EXECUTION_STRATEGY)).strip()
    if execution_strategy not in EXECUTION_STRATEGIES:
        raise ValueError(f"plan.execution_strategy must be one of {sorted(EXECUTION_STRATEGIES)}")
    max_parallel_workers = _positive_int(
        data.get("max_parallel_workers", DEFAULT_MAX_PARALLEL_WORKERS),
        "plan.max_parallel_workers",
    )
    computer_use_slots = _positive_int(
        data.get("computer_use_slots", DEFAULT_COMPUTER_USE_SLOTS),
        "plan.computer_use_slots",
    )
    compatibility = data.get("compatibility")
    legacy_serial = False
    source_schema = PLAN_SCHEMA_VERSION
    if compatibility is not None:
        if not isinstance(compatibility, dict):
            raise ValueError("plan.compatibility must be an object")
        _reject_unknown(compatibility, {"migrated_from_schema", "legacy_serial"}, "plan.compatibility")
        raw_source_schema = compatibility.get("migrated_from_schema", PLAN_SCHEMA_VERSION)
        if isinstance(raw_source_schema, bool) or not isinstance(raw_source_schema, int):
            raise ValueError("plan.compatibility.migrated_from_schema must be an integer")
        source_schema = raw_source_schema
        raw_legacy_serial = compatibility.get("legacy_serial", False)
        if not isinstance(raw_legacy_serial, bool):
            raise ValueError("plan.compatibility.legacy_serial must be a boolean")
        legacy_serial = raw_legacy_serial
        if legacy_serial and (
            source_schema != LEGACY_PLAN_SCHEMA_VERSION
            or execution_strategy != "serial"
            or max_parallel_workers != 1
        ):
            raise ValueError(
                "a migrated v0.8 legacy_serial plan must remain serial with max_parallel_workers=1"
            )

    raw_roles = data.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise ValueError("plan.roles must be a non-empty array")
    roles = tuple(_role_from_raw(raw, index) for index, raw in enumerate(raw_roles, 1))
    _validate_unique((role.id for role in roles), "role id")

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("plan.tasks must be a non-empty array")
    tasks: list[Task] = []
    for index, raw in enumerate(raw_tasks, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"task {index} must be an object")
        tasks.append(_task_from_raw(raw, profile, f"task {index}", canonical=True))
    _validate_unique((task.id for task in tasks), "task id")
    plan = Plan(
        goal=goal,
        user_request=user_request,
        model_strategy=strategy,
        tasks=tuple(tasks),
        roles=roles,
        graph_version=graph_version,
        execution_strategy=execution_strategy,
        max_parallel_workers=max_parallel_workers,
        computer_use_slots=computer_use_slots,
        source_schema_version=source_schema,
        legacy_serial=legacy_serial,
    )
    _reject_self_acceptance(plan)
    _validate_graph(plan)
    return plan


def _plan_header(
    data: dict[str, Any],
    profile: str,
    *,
    require_user_request: bool = False,
) -> tuple[str, str, str]:
    goal = _required_string(data.get("goal"), "plan.goal")
    user_request = _required_string(
        data.get("user_request") if require_user_request else data.get("user_request", goal),
        "plan.user_request",
    )
    strategy = str(data.get("model_strategy") or ("auto" if profile == "adaptive" else "host-settings"))
    if strategy not in STRATEGIES:
        raise ValueError(f"model_strategy must be one of {sorted(STRATEGIES)}")
    if profile == "adaptive" and strategy == "host-settings":
        raise ValueError("Adaptive profile requires auto, sol-only, or astra-only model_strategy")
    if profile == "host-settings" and strategy != "host-settings":
        raise ValueError("Host Settings profile requires model_strategy=host-settings")
    return goal, user_request, strategy


def _role_from_raw(raw: Any, index: int) -> RoleProfile:
    label = f"role {index}"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {
            "id",
            "name",
            "responsibilities",
            "domain_focus",
            "preferred_tools",
            "context_priorities",
            "verification_expectations",
        },
        label,
    )
    return RoleProfile(
        id=_identifier(raw.get("id"), f"{label}.id"),
        name=_required_string(raw.get("name"), f"{label}.name"),
        responsibilities=_nonempty_strings(raw.get("responsibilities"), f"{label}.responsibilities"),
        domain_focus=_strings(raw.get("domain_focus", []), f"{label}.domain_focus"),
        preferred_tools=_strings(raw.get("preferred_tools", []), f"{label}.preferred_tools"),
        context_priorities=_strings(raw.get("context_priorities", []), f"{label}.context_priorities"),
        verification_expectations=_strings(
            raw.get("verification_expectations", []),
            f"{label}.verification_expectations",
        ),
    )


def _task_from_raw(
    raw: dict[str, Any],
    profile: str,
    label: str,
    *,
    canonical: bool,
    task_id: str | None = None,
    role: str | None = None,
    depends_on: tuple[str, ...] | None = None,
) -> Task:
    if canonical:
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
                "depends_on",
                "priority",
                "verification",
                "resources",
                "required_capabilities",
                "context",
                "outputs",
                "tags",
            },
            label,
        )
    title = _required_string(raw.get("title"), f"{label}.title")
    objective = _required_string(raw.get("objective"), f"{label}.objective")
    done = _nonempty_strings(raw.get("definition_of_done"), f"{label}.definition_of_done")
    execution_mode = str(raw.get("execution_mode", "")).strip()
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"{label}.execution_mode must be one of {sorted(EXECUTION_MODES)}")
    execution_mode_reason = _required_string(
        raw.get("execution_mode_reason"),
        f"{label}.execution_mode_reason",
    )
    reasoning = _reasoning(raw, profile, label)
    resolved_id = task_id or _identifier(raw.get("id"), f"{label}.id")
    resolved_role = role or _identifier(raw.get("role"), f"{label}.role")
    resolved_dependencies = depends_on
    if resolved_dependencies is None:
        resolved_dependencies = _identifiers(raw.get("depends_on", []), f"{label}.depends_on")
    priority = _bounded_int(raw.get("priority", 0), f"{label}.priority", -1_000_000, 1_000_000)
    verification = (
        _verification_from_raw(raw.get("verification"), profile, f"{label}.verification")
        if canonical
        else VerificationPolicy(policy="self", required=True, max_revision_attempts=0)
    )
    resources = tuple(
        _resource_from_raw(item, index, label)
        for index, item in enumerate(_array(raw.get("resources", []), f"{label}.resources"), 1)
    )
    _validate_unique((item.id for item in resources), f"{label} resource id")
    context = _context_from_raw(raw.get("context"), label) if canonical else TaskContext()
    outputs = tuple(
        _output_from_raw(item, index, label)
        for index, item in enumerate(_array(raw.get("outputs", []), f"{label}.outputs"), 1)
    )
    _validate_unique((item.id for item in outputs), f"{label} output id")
    return Task(
        id=resolved_id,
        title=title,
        objective=objective,
        definition_of_done=done,
        execution_mode=execution_mode,
        execution_mode_reason=execution_mode_reason,
        reasoning=reasoning,
        role=resolved_role,
        depends_on=resolved_dependencies,
        priority=priority,
        verification=verification,
        resources=resources,
        required_capabilities=_strings(
            raw.get("required_capabilities", []),
            f"{label}.required_capabilities",
        ),
        context=context,
        outputs=outputs,
        tags=_strings(raw.get("tags", []), f"{label}.tags"),
    )


def _verification_from_raw(raw: Any, profile: str, label: str) -> VerificationPolicy:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {
            "policy",
            "required",
            "deterministic_checks",
            "verifier_role",
            "execution_mode",
            "execution_mode_reason",
            "reasoning",
            "max_revision_attempts",
        },
        label,
    )
    policy = str(raw.get("policy", "")).strip()
    if policy not in VERIFICATION_POLICIES:
        raise ValueError(f"{label}.policy must be one of {sorted(VERIFICATION_POLICIES)}")
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise ValueError(f"{label}.required must be a boolean")
    checks = tuple(
        _check_from_raw(item, index, label)
        for index, item in enumerate(
            _array(raw.get("deterministic_checks", []), f"{label}.deterministic_checks"),
            1,
        )
    )
    _validate_unique((item.id for item in checks), f"{label} check id")
    if policy == "deterministic" and required and not checks:
        raise ValueError(f"{label}.deterministic_checks is required for deterministic policy")
    verifier_role = raw.get("verifier_role")
    if verifier_role is not None:
        verifier_role = _identifier(verifier_role, f"{label}.verifier_role")
    execution_mode = raw.get("execution_mode")
    if execution_mode is not None:
        execution_mode = str(execution_mode).strip()
        if execution_mode not in EXECUTION_MODES:
            raise ValueError(f"{label}.execution_mode must be one of {sorted(EXECUTION_MODES)}")
    execution_mode_reason = raw.get("execution_mode_reason")
    if execution_mode_reason is not None:
        execution_mode_reason = _required_string(execution_mode_reason, f"{label}.execution_mode_reason")
    if bool(execution_mode) != bool(execution_mode_reason):
        raise ValueError(f"{label}.execution_mode and execution_mode_reason must be provided together")
    reasoning = None
    if "reasoning" in raw:
        if profile != "adaptive":
            raise ValueError("Host Settings verification policies must omit reasoning")
        reasoning = normalize(str(raw["reasoning"]))
    return VerificationPolicy(
        policy=policy,
        required=required,
        deterministic_checks=checks,
        verifier_role=verifier_role,
        execution_mode=execution_mode,
        execution_mode_reason=execution_mode_reason,
        reasoning=reasoning,
        max_revision_attempts=_bounded_int(
            raw.get("max_revision_attempts", 2),
            f"{label}.max_revision_attempts",
            0,
            100,
        ),
    )


def _check_from_raw(raw: Any, index: int, parent: str) -> VerificationCheck:
    label = f"{parent}.deterministic_checks[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {"id", "kind", "description", "argv", "path", "timeout_seconds", "expected_exit_code"},
        label,
    )
    kind = str(raw.get("kind", "")).strip()
    if kind not in VERIFICATION_CHECK_KINDS:
        raise ValueError(f"{label}.kind must be one of {sorted(VERIFICATION_CHECK_KINDS)}")
    argv = _strings(raw.get("argv", []), f"{label}.argv")
    path = raw.get("path")
    if path is not None:
        path = _required_string(path, f"{label}.path")
    if kind == "command" and not argv:
        raise ValueError(f"{label}.argv is required for command checks")
    if kind != "command" and argv:
        raise ValueError(f"{label}.argv is only valid for command checks")
    if kind == "artifact" and not path:
        raise ValueError(f"{label}.path is required for artifact checks")
    if kind != "artifact" and path:
        raise ValueError(f"{label}.path is only valid for artifact checks")
    return VerificationCheck(
        id=_identifier(raw.get("id"), f"{label}.id"),
        kind=kind,
        description=_required_string(raw.get("description"), f"{label}.description"),
        argv=argv,
        path=path,
        timeout_seconds=_bounded_int(raw.get("timeout_seconds", 300), f"{label}.timeout_seconds", 1, 86_400),
        expected_exit_code=_bounded_int(
            raw.get("expected_exit_code", 0),
            f"{label}.expected_exit_code",
            -255,
            255,
        ),
    )


def _resource_from_raw(raw: Any, index: int, parent: str) -> ResourceClaim:
    label = f"{parent}.resources[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(raw, {"id", "kind", "target", "access", "description"}, label)
    kind = str(raw.get("kind", "")).strip()
    if kind not in RESOURCE_KINDS:
        raise ValueError(f"{label}.kind must be one of {sorted(RESOURCE_KINDS)}")
    access = str(raw.get("access", "")).strip()
    if access not in RESOURCE_ACCESS_MODES:
        raise ValueError(f"{label}.access must be one of {sorted(RESOURCE_ACCESS_MODES)}")
    description = raw.get("description")
    if description is not None:
        description = _required_string(description, f"{label}.description")
    return ResourceClaim(
        id=_identifier(raw.get("id"), f"{label}.id"),
        kind=kind,
        target=_required_string(raw.get("target"), f"{label}.target"),
        access=access,
        description=description,
    )


def _output_from_raw(raw: Any, index: int, parent: str) -> TaskOutput:
    label = f"{parent}.outputs[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(raw, {"id", "description", "path", "required"}, label)
    path = raw.get("path")
    if path is not None:
        path = _required_string(path, f"{label}.path")
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise ValueError(f"{label}.required must be a boolean")
    return TaskOutput(
        id=_identifier(raw.get("id"), f"{label}.id"),
        description=_required_string(raw.get("description"), f"{label}.description"),
        path=path,
        required=required,
    )


def _context_from_raw(raw: Any, parent: str) -> TaskContext:
    label = f"{parent}.context"
    if raw is None:
        return TaskContext()
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {
            "memory_queries",
            "memory_record_ids",
            "dependency_outputs",
            "max_memory_records",
            "max_dependency_outputs",
        },
        label,
    )
    return TaskContext(
        memory_queries=_strings(raw.get("memory_queries", []), f"{label}.memory_queries"),
        memory_record_ids=_strings(raw.get("memory_record_ids", []), f"{label}.memory_record_ids"),
        dependency_outputs=_identifiers(raw.get("dependency_outputs", []), f"{label}.dependency_outputs"),
        max_memory_records=_bounded_int(
            raw.get("max_memory_records", DEFAULT_MAX_MEMORY_RECORDS),
            f"{label}.max_memory_records",
            0,
            100,
        ),
        max_dependency_outputs=_bounded_int(
            raw.get("max_dependency_outputs", DEFAULT_MAX_DEPENDENCY_OUTPUTS),
            f"{label}.max_dependency_outputs",
            0,
            100,
        ),
    )


def _reasoning(raw: dict[str, Any], profile: str, label: str) -> str | None:
    if profile == "adaptive":
        if raw.get("reasoning") is None:
            raise ValueError(f"{label} requires reasoning in Adaptive profile")
        return normalize(str(raw["reasoning"]))
    if "reasoning" in raw:
        raise ValueError("Host Settings plans must omit task reasoning")
    return None


def _validate_graph(plan: Plan) -> None:
    task_map = plan.task_map
    role_ids = set(plan.role_map)
    for task in plan.tasks:
        if task.role not in role_ids:
            raise ValueError(f"task {task.id} references unknown role {task.role!r}")
        role = plan.role_map[task.role]
        if not plan.legacy_serial and (
            role.id == "legacy-worker" or role.name.casefold() == "legacy serial worker"
        ):
            raise ValueError(
                f"task {task.id} requires a concrete RoleProfile, not generic legacy-worker"
            )
        if task.verification.verifier_role and task.verification.verifier_role not in role_ids:
            raise ValueError(
                f"task {task.id} verification references unknown role "
                f"{task.verification.verifier_role!r}"
            )
        if task.verification.verifier_role and not plan.legacy_serial:
            verifier_role = plan.role_map[task.verification.verifier_role]
            if (
                verifier_role.id == "legacy-worker"
                or verifier_role.name.casefold() == "legacy serial worker"
            ):
                raise ValueError(
                    f"task {task.id} verifier requires a concrete RoleProfile, "
                    "not generic legacy-worker"
                )
        for dependency in task.depends_on:
            if dependency == task.id:
                raise ValueError(f"task {task.id} cannot depend on itself")
            if dependency not in task_map:
                raise ValueError(f"task {task.id} references unknown dependency {dependency!r}")
        invalid_outputs = set(task.context.dependency_outputs) - set(task.depends_on)
        if invalid_outputs:
            raise ValueError(
                f"task {task.id} context.dependency_outputs must be direct dependencies; "
                f"invalid={sorted(invalid_outputs)}"
            )
    _validate_cycles(plan.tasks)


def _validate_cycles(tasks: tuple[Task, ...]) -> None:
    dependencies = {task.id: task.depends_on for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            start = stack.index(task_id)
            cycle = stack[start:] + [task_id]
            raise ValueError(f"plan task graph contains a cycle: {' -> '.join(cycle)}")
        visiting.add(task_id)
        stack.append(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        stack.pop()
        visiting.remove(task_id)
        visited.add(task_id)

    for task in tasks:
        visit(task.id)


def _role_to_dict(role: RoleProfile) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "responsibilities": list(role.responsibilities),
        **({"domain_focus": list(role.domain_focus)} if role.domain_focus else {}),
        **({"preferred_tools": list(role.preferred_tools)} if role.preferred_tools else {}),
        **({"context_priorities": list(role.context_priorities)} if role.context_priorities else {}),
        **(
            {"verification_expectations": list(role.verification_expectations)}
            if role.verification_expectations
            else {}
        ),
    }


def _task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "objective": task.objective,
        "definition_of_done": list(task.definition_of_done),
        "execution_mode": task.execution_mode,
        "execution_mode_reason": task.execution_mode_reason,
        **({"reasoning": task.reasoning} if task.reasoning else {}),
        "role": task.role,
        "depends_on": list(task.depends_on),
        "priority": task.priority,
        "verification": _verification_to_dict(task.verification),
        "resources": [_resource_to_dict(item) for item in task.resources],
        "required_capabilities": list(task.required_capabilities),
        "context": _context_to_dict(task.context),
        "outputs": [_output_to_dict(item) for item in task.outputs],
        "tags": list(task.tags),
    }


def _verification_to_dict(policy: VerificationPolicy) -> dict[str, Any]:
    return {
        "policy": policy.policy,
        "required": policy.required,
        "deterministic_checks": [_check_to_dict(item) for item in policy.deterministic_checks],
        **({"verifier_role": policy.verifier_role} if policy.verifier_role else {}),
        **({"execution_mode": policy.execution_mode} if policy.execution_mode else {}),
        **(
            {"execution_mode_reason": policy.execution_mode_reason}
            if policy.execution_mode_reason
            else {}
        ),
        **({"reasoning": policy.reasoning} if policy.reasoning else {}),
        "max_revision_attempts": policy.max_revision_attempts,
    }


def _check_to_dict(check: VerificationCheck) -> dict[str, Any]:
    return {
        "id": check.id,
        "kind": check.kind,
        "description": check.description,
        **({"argv": list(check.argv)} if check.argv else {}),
        **({"path": check.path} if check.path else {}),
        "timeout_seconds": check.timeout_seconds,
        "expected_exit_code": check.expected_exit_code,
    }


def _resource_to_dict(resource: ResourceClaim) -> dict[str, Any]:
    return {
        "id": resource.id,
        "kind": resource.kind,
        "target": resource.target,
        "access": resource.access,
        **({"description": resource.description} if resource.description else {}),
    }


def _output_to_dict(output: TaskOutput) -> dict[str, Any]:
    return {
        "id": output.id,
        "description": output.description,
        **({"path": output.path} if output.path else {}),
        "required": output.required,
    }


def _context_to_dict(context: TaskContext) -> dict[str, Any]:
    return {
        "memory_queries": list(context.memory_queries),
        "memory_record_ids": list(context.memory_record_ids),
        "dependency_outputs": list(context.dependency_outputs),
        "max_memory_records": context.max_memory_records,
        "max_dependency_outputs": context.max_dependency_outputs,
    }


def _reject_unknown(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, name: str) -> str:
    result = _required_string(value, name)
    if not _IDENTIFIER.fullmatch(result):
        raise ValueError(f"{name} must match {_IDENTIFIER.pattern}")
    return result


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    raw = _array(value, name)
    result = tuple(_required_string(item, f"{name} item") for item in raw)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _nonempty_strings(value: Any, name: str) -> tuple[str, ...]:
    result = _strings(value, name)
    if not result:
        raise ValueError(f"{name} must be a non-empty array")
    return result


def _identifiers(value: Any, name: str) -> tuple[str, ...]:
    raw = _array(value, name)
    result = tuple(_identifier(item, f"{name} item") for item in raw)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _positive_int(value: Any, name: str) -> int:
    return _bounded_int(value, name, 1, 1_000_000)


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validate_unique(values: Iterable[str], name: str) -> None:
    items = tuple(values)
    if len(set(items)) != len(items):
        raise ValueError(f"{name}s must be unique")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
