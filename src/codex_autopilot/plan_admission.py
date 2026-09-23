"""Admitting a schema-3 plan: every violation in one pass, stage by stage.

The replanner gets three attempts (``MAX_PLAN_CHANGE_REJECTIONS``). The
validator used to stop at its first ``raise``, and two more rounds of
checking waited behind it: the Goal Contract coverage ran only after a clean
validation, and the constraints the prompt names (existing task ids stay,
VERIFIED tasks do not change) were checked only when the plan was committed,
after the plan verifier's PASS. A graph with four independent defects could
not be repaired within the budget even by a model that fixes everything it
is told - it was told one thing per round.

Now one pass collects them all (``IssueCollector``), in a fixed order, and
the replanner reads the numbered list in its next prompt. A stage whose
checks depend on another runs only when that one is clean, and no wider:

- fields, header, goal_contract, scalars - each check on its own;
- compatibility - its serial check waits for clean scalars;
- roles, skill_packs, departments, tasks - each entity and each field of it
  on its own (``plan_parse``); the department/lead cross-check waits for
  clean roles and departments; unique task ids wait for clean tasks;
- outcomes and acceptance (R29) - per successfully read task, whatever the
  other tasks look like: they depend on nothing but the task;
- graph - references of each read task against the raw id sets; cycles only
  over a fully read graph with no unknown dependency (``visit`` would
  otherwise meet an id it has no entry for);
- immutables (a plan change) - each comparison gated only by its own field,
  so a changed goal is reported next to a broken task;
- coverage and state (the replanner) - Goal Contract coverage, and the
  conditions on run state that can only get worse while the change drains:
  an existing task removed, the requester gone, a VERIFIED or CANCELLED task
  rewritten. "An advanced task rewritten" stays with the commit
  (``reconcile_plan_change_state``): a task RUNNING now may be READY again by
  then, so refusing it here would refuse a plan that could be admitted.

The collector catches ``ValueError`` only (``plan_issues``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .department_acceptance import (
    department_contract_from_raw,
    department_contract_issues,
    resolve_task_department,
    task_department_binding,
)
from .goal_contract import outcome_binding_issues, validate_goal_contract
from .plan import (
    DEFAULT_COMPUTER_USE_SLOTS,
    DEFAULT_EXECUTION_STRATEGY,
    DEFAULT_MAX_PARALLEL_WORKERS,
    EXECUTION_STRATEGIES,
    LEGACY_PLAN_SCHEMA_VERSION,
    PLAN_SCHEMA_VERSION,
    Plan,
    Task,
    _is_legacy_verification,
)
from .acceptance_floor import CLEAN_IDENTITY_ASSIGNMENTS, is_clean_suite_command
from .plan_fields import COMPATIBILITY_FIELDS, PLAN_FIELDS
from .plan_issues import FAILED, FailFastCollector, IssueCollector, PlanIssue, PlanIssues
from .plan_parse import (
    model_strategy,
    positive_int,
    reject_unknown,
    required_string,
    role_from_raw,
    task_from_raw,
    validate_unique,
)
from .resilience import (
    PLAN_CHANGE_RESULT_PREFIX,
    PlanChangeConflictError,
    PlanChangeProtocolError,
    PlanChangeResult,
    _single_final_protocol_object,
)
from .role_specification import validate_role_specification_transition
from .skill_packs import (
    skill_packs_from_raw,
    validate_plan_skill_bindings,
    validate_plan_skill_qualifications,
    validate_trusted_skill_promotions,
)

__all__ = [
    "FAILED", "FailFastCollector", "IssueCollector", "PlanIssue", "PlanIssues",
    "admit_replanner_result", "graph_plan", "plan_change_candidate", "validate_graph",
]

REQUIRED_PLAN_FIELDS = ("schema_version", "goal", "roles", "tasks")
ENVELOPE_FIELDS = ("request_id", "base_graph_version", "plan")
PLAN_STAGES = (
    "header", "goal_contract", "scalars", "compatibility", "roles", "skill_packs",
    "departments", "tasks",
)


@dataclass
class ReadPlan:
    """What one pass read: each part, or FAILED; ``plan`` only when all were read."""

    goal: Any = FAILED
    model_strategy: Any = FAILED
    graph_version: Any = FAILED
    goal_contract: Any = FAILED
    roles: Any = FAILED
    skill_packs: Any = FAILED
    tasks: tuple[Task, ...] = ()
    raw_task_ids: frozenset[str] = frozenset()
    plan: Any = FAILED


def _raw_ids(raw: Any) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        item["id"].strip()
        for item in raw
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
    )


def graph_plan(
    c: Any,
    data: dict[str, Any],
    profile: str,
    *,
    require_goal_contract: bool,
    require_acceptance_class: bool,
    inherited: Plan | None = None,
) -> ReadPlan:
    """Read and check a schema-3 graph; every violation goes to ``c``."""

    read = ReadPlan()
    c.check("fields", "plan", reject_unknown, data, PLAN_FIELDS, "plan", REQUIRED_PLAN_FIELDS)
    if inherited is not None and data.get("schema_version") != PLAN_SCHEMA_VERSION:
        c.add("fields", "plan.schema_version", "plan changes must use the canonical v0.9 schema")

    read.goal = c.check("header", "plan.goal", required_string, data.get("goal"), "plan.goal")
    user_request = c.check(
        "header", "plan.user_request", required_string, data.get("user_request"), "plan.user_request"
    )
    read.model_strategy = c.check("header", "plan.model_strategy", model_strategy, data, profile)

    raw_contract = data.get("goal_contract")
    if require_goal_contract and raw_contract is None:
        c.add("goal_contract", "plan.goal_contract",
              "plan.goal_contract must be declared as a structured Goal Contract")
    elif raw_contract is None:
        read.goal_contract = None
    else:
        read.goal_contract = c.check(
            "goal_contract", "plan.goal_contract", validate_goal_contract,
            raw_contract, label="plan.goal_contract",
        )

    read.graph_version = c.check(
        "scalars", "plan.graph_version", positive_int, data.get("graph_version", 1), "plan.graph_version"
    )
    execution_strategy = c.check("scalars", "plan.execution_strategy", _execution_strategy, data)
    max_workers = c.check(
        "scalars", "plan.max_parallel_workers", positive_int,
        data.get("max_parallel_workers", DEFAULT_MAX_PARALLEL_WORKERS), "plan.max_parallel_workers",
    )
    slots = c.check(
        "scalars", "plan.computer_use_slots", positive_int,
        data.get("computer_use_slots", DEFAULT_COMPUTER_USE_SLOTS), "plan.computer_use_slots",
    )
    compatibility = _compatibility(c, data, inherited, (execution_strategy, max_workers, slots))

    raw_roles = data.get("roles")
    roles: list[Any] = []
    if not isinstance(raw_roles, list) or not raw_roles:
        c.add("roles", "plan.roles", "plan.roles must be a non-empty array")
    else:
        roles = [role_from_raw(raw, index, c) for index, raw in enumerate(raw_roles, 1)]
        if all(role is not FAILED for role in roles):
            c.check("roles", "plan.roles", validate_unique, (role.id for role in roles), "role id")
    if c.clean("roles"):
        read.roles = tuple(roles)
    read.skill_packs = c.check("skill_packs", "plan.skill_packs", skill_packs_from_raw, data.get("skill_packs", []))

    departments = _departments(c, data.get("departments", []), roles)

    raw_tasks = data.get("tasks")
    tasks: list[Any] = []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        c.add("tasks", "plan.tasks", "plan.tasks must be a non-empty array")
    else:
        for index, raw in enumerate(raw_tasks, 1):
            if not isinstance(raw, dict):
                c.add("tasks", f"task {index}", f"task {index} must be an object")
                tasks.append(FAILED)
                continue
            tasks.append(task_from_raw(
                raw, profile, f"task {index}", canonical=True,
                require_acceptance_class=require_acceptance_class, c=c,
            ))
        if c.clean("tasks"):
            c.check("tasks", "plan.tasks", validate_unique, (task.id for task in tasks), "task id")
    read.tasks = tuple(task for task in tasks if task is not FAILED)
    read.raw_task_ids = _raw_ids(raw_tasks)
    legacy_serial = compatibility[0] if compatibility is not FAILED else False

    _outcomes(c, read.goal_contract, read.tasks)
    _acceptance(c, read.tasks, legacy_serial=legacy_serial, inherited=inherited)
    unknown_dependency = _graph(
        c, read.tasks, roles, departments, raw_roles, read.raw_task_ids, legacy_serial=legacy_serial
    )
    if c.clean(*PLAN_STAGES):
        read.plan = Plan(
            goal=read.goal,
            user_request=user_request,
            model_strategy=read.model_strategy,
            tasks=read.tasks,
            roles=read.roles,
            departments=departments,
            graph_version=read.graph_version,
            execution_strategy=execution_strategy,
            max_parallel_workers=max_workers,
            computer_use_slots=slots,
            source_schema_version=compatibility[1],
            legacy_serial=legacy_serial,
            goal_contract=read.goal_contract,
            skill_packs=read.skill_packs,
        )
        if c.clean("graph"):
            c.check("graph", "plan.skill_packs", validate_plan_skill_bindings, read.plan)
    # The one check that needs the whole graph: visit() would meet an id it
    # has no entry for.
    if c.clean("tasks") and not unknown_dependency:
        c.check("graph", "plan.tasks", _validate_cycles, read.tasks)
    return read


def _execution_strategy(data: dict[str, Any]) -> str:
    value = str(data.get("execution_strategy", DEFAULT_EXECUTION_STRATEGY)).strip()
    if value not in EXECUTION_STRATEGIES:
        raise ValueError(f"plan.execution_strategy must be one of {sorted(EXECUTION_STRATEGIES)}")
    return value


def _compatibility(c: Any, data: dict[str, Any], inherited: Plan | None, scalars: tuple) -> Any:
    """(legacy_serial, source_schema), or FAILED."""

    compatibility = data.get("compatibility")
    serial_message = (
        "a migrated v0.8 legacy_serial plan must remain serial with "
        "max_parallel_workers=1 and computer_use_slots=1"
    )

    def serial(source_ok: bool = True) -> bool:
        strategy, workers, slots = scalars
        return source_ok and strategy == "serial" and workers == 1 and slots == 1

    if inherited is not None:
        # A replacement may only repeat the current plan's provenance - so a
        # copy of the plan from plan_to_dict passes unchanged - but cannot
        # introduce or rewrite it (R8).
        expected = (
            {"migrated_from_schema": inherited.source_schema_version, "legacy_serial": True}
            if inherited.legacy_serial
            else None
        )
        if compatibility != expected:
            c.add("compatibility", "plan.compatibility",
                  "plan.compatibility is inherited from the current plan, never declared by a plan change")
            return FAILED
        if inherited.legacy_serial and c.clean("scalars") and not serial():
            c.add("compatibility", "plan.compatibility", serial_message)
            return FAILED
        return inherited.legacy_serial, inherited.source_schema_version
    if compatibility is None:
        return False, PLAN_SCHEMA_VERSION
    if not isinstance(compatibility, dict):
        c.add("compatibility", "plan.compatibility", "plan.compatibility must be an object")
        return FAILED
    before = c.count()
    c.check("compatibility", "plan.compatibility", reject_unknown,
            compatibility, COMPATIBILITY_FIELDS, "plan.compatibility")
    source = compatibility.get("migrated_from_schema", PLAN_SCHEMA_VERSION)
    if isinstance(source, bool) or not isinstance(source, int):
        c.add("compatibility", "plan.compatibility.migrated_from_schema",
              "plan.compatibility.migrated_from_schema must be an integer")
    legacy = compatibility.get("legacy_serial", False)
    if not isinstance(legacy, bool):
        c.add("compatibility", "plan.compatibility.legacy_serial",
              "plan.compatibility.legacy_serial must be a boolean")
    if c.count() > before:
        return FAILED
    if legacy and c.clean("scalars") and not serial(source == LEGACY_PLAN_SCHEMA_VERSION):
        c.add("compatibility", "plan.compatibility", serial_message)
        return FAILED
    return legacy, source


def _departments(c: Any, raw: Any, roles: list[Any]) -> tuple:
    if not isinstance(raw, list):
        c.add("departments", "plan.departments", "plan.departments must be an array")
        return ()
    departments = [
        c.check("departments", f"plan.departments[{index}]", department_contract_from_raw,
                item, f"department {index}")
        for index, item in enumerate(raw, 1)
    ]
    if not c.clean("departments"):
        return ()
    if c.clean("roles"):
        for message in department_contract_issues(departments, role_ids=(role.id for role in roles)):
            c.add("departments", "plan.departments", message)
    return tuple(departments)


def _outcomes(c: Any, contract: Any, tasks: tuple[Task, ...]) -> None:
    """Outcome bindings per read task; only the contract gates them."""

    if contract is FAILED:
        return
    if contract is None:
        for task in tasks:
            if task.produces_outcomes:
                c.add("outcomes", f"task {task.id}.produces_outcomes",
                      f"task {task.id} declares produces_outcomes without plan.goal_contract")
        return
    for task_id, message in outcome_binding_issues(
        contract, [(task.id, task.produces_outcomes) for task in tasks]
    ):
        c.add("outcomes", f"task {task_id}.produces_outcomes", message)


def acceptance_issues(
    tasks: Iterable[Task], *, legacy_serial: bool, inherited: Plan | None = None
) -> list[tuple[str, str]]:
    """The acceptance floor for every new canonical task (R8/R29).

    Deterministic checks are admission evidence for a fresh judge, never
    acceptance by themselves. The argv gate can prove the identity
    environment is reset and that a direct command exists; executing the
    declared repository-wide check, not this lint, proves its outcome (R25).

    Migrated v0.8 ``legacy_serial`` plans remain the sole compatibility
    exception, and it rests on provenance, not on a claim: on a plan change
    only a task whose acceptance contract is not rewritten keeps it - its
    objective, DoD, execution mode and verification, not merely the
    verification (new work under an old number is the same self-acceptance,
    R8). Resources and dependencies of a migrated task may be fixed.

    Each task's four conditions are independent of each other and of every
    other task, so all of them are reported. They used to stop at the first
    condition of the first task.
    """

    tasks = tuple(tasks)
    if not legacy_serial:
        exempt: frozenset[str] = frozenset()
    elif inherited is None:
        exempt = frozenset(task.id for task in tasks if _is_legacy_verification(task.verification))
    else:
        def contract(task: Task) -> tuple:
            return (task.objective, tuple(task.definition_of_done), task.execution_mode, task.verification)

        before = {task.id: contract(task) for task in inherited.tasks}
        exempt = frozenset(
            task.id for task in tasks if task.id in before and before[task.id] == contract(task)
        )
    found: list[tuple[str, str]] = []
    for task in tasks:
        if task.id in exempt:
            continue
        verification = task.verification
        if verification.policy != "independent":
            found.append((task.id,
                "R8/R29: canonical task "
                f"{task.id} verification.policy must be \"independent\"; "
                f"got {verification.policy!r}. Deterministic checks admit work "
                "to independent judgement and never replace it"))
        if not verification.required:
            found.append((task.id, f"R8/R29: canonical task {task.id} verification.required must be true"))
        if verification.max_revision_attempts < 2:
            found.append((task.id,
                f"R29: canonical task {task.id} verification.max_revision_attempts must be at least 2"))
        if not any(is_clean_suite_command(check) for check in verification.deterministic_checks):
            names = ", ".join(f"{name}=" for name in CLEAN_IDENTITY_ASSIGNMENTS)
            found.append((task.id,
                "R29: canonical task "
                f"{task.id} must declare at least one full-suite deterministic "
                "check as a successful command argv launched through env, reset "
                f"{names}, and invoke the command directly"))
    return found


def _acceptance(c: Any, tasks: tuple[Task, ...], *, legacy_serial: bool, inherited: Plan | None) -> None:
    if not c.clean("compatibility"):
        return
    for task_id, message in acceptance_issues(tasks, legacy_serial=legacy_serial, inherited=inherited):
        c.add("acceptance", f"task {task_id}.verification", message)


def _is_legacy_role(role: Any) -> bool:
    return role.id == "legacy-worker" or role.name.casefold() == "legacy serial worker"


def _graph(
    c: Any,
    tasks: tuple[Task, ...],
    roles: list[Any],
    departments: tuple,
    raw_roles: Any,
    raw_task_ids: frozenset[str],
    *,
    legacy_serial: bool,
) -> bool:
    """References of each read task. Returns whether a dependency was unknown."""

    role_ids = _raw_ids(raw_roles)
    role_map = {role.id: role for role in roles if role is not FAILED}
    departments_ready = c.clean("roles", "departments")
    unknown_dependency = False
    for task in tasks:
        path = f"task {task.id}"
        if task.role not in role_ids:
            c.add("graph", f"{path}.role", f"task {task.id} references unknown role {task.role!r}")
        elif not legacy_serial and task.role in role_map and _is_legacy_role(role_map[task.role]):
            c.add("graph", f"{path}.role",
                  f"task {task.id} requires a concrete RoleProfile, not generic legacy-worker")
        verifier = task.verification.verifier_role
        if verifier and verifier not in role_ids:
            c.add("graph", f"{path}.verification.verifier_role",
                  f"task {task.id} verification references unknown role {verifier!r}")
        elif verifier and not legacy_serial and verifier in role_map and _is_legacy_role(role_map[verifier]):
            c.add("graph", f"{path}.verification.verifier_role",
                  f"task {task.id} verifier requires a concrete RoleProfile, not generic legacy-worker")
        binding = c.check("graph", f"{path}.resources", _binding, task)
        if binding not in (None, FAILED) and departments_ready:
            c.check("graph", f"{path}.resources", _department, task, departments, roles)
        for dependency in task.depends_on:
            if dependency == task.id:
                c.add("graph", f"{path}.depends_on", f"task {task.id} cannot depend on itself")
                unknown_dependency = True
            elif dependency not in raw_task_ids:
                c.add("graph", f"{path}.depends_on",
                      f"task {task.id} references unknown dependency {dependency!r}")
                unknown_dependency = True
        invalid = sorted(set(task.context.dependency_outputs) - set(task.depends_on))
        if invalid:
            c.add("graph", f"{path}.context.dependency_outputs",
                  f"task {task.id} context.dependency_outputs must be direct dependencies; invalid={invalid}")
    return unknown_dependency


def _binding(task: Task) -> Any:
    try:
        return task_department_binding(task)
    except ValueError as exc:
        raise ValueError(f"task {task.id}: {exc}") from exc


def _department(task: Task, departments: tuple, roles: list[Any]) -> None:
    try:
        department = resolve_task_department(
            departments, task, role_names={role.id: role.name for role in roles}
        )
    except ValueError as exc:
        raise ValueError(f"task {task.id}: {exc}") from exc
    if department is None:
        raise ValueError(f"task {task.id}: department binding disappeared")
    if not task.context.dependency_outputs:
        raise ValueError(
            f"task {task.id} department acceptance requires a selected "
            "dependency output carrying the pinned rubric reference"
        )


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


def validate_graph(plan: Plan) -> None:
    """The graph checks of an already built plan, first error first (legacy v0.8)."""

    c = IssueCollector()
    for message in department_contract_issues(plan.departments, role_ids=plan.role_map):
        c.add("departments", "plan.departments", message)
    unknown = _graph(
        c, plan.tasks, list(plan.roles), plan.departments,
        [{"id": role.id} for role in plan.roles], frozenset(plan.task_map),
        legacy_serial=plan.legacy_serial,
    )
    if c.clean("graph", "departments"):
        c.check("graph", "plan.skill_packs", validate_plan_skill_bindings, plan)
    if not unknown:
        c.check("graph", "plan.tasks", _validate_cycles, plan.tasks)
    if c.issues:
        raise ValueError(c.issues[0].message)


def plan_change_candidate(
    c: Any,
    current: Plan,
    data: dict[str, Any],
    profile: str,
    *,
    promotion_evidence_store: Any | None = None,
) -> ReadPlan:
    """A complete replacement graph, checked against the current one."""

    # user_request is carried over from the current plan, never taken from
    # the reply: a verbatim echo of a 35 234-character request was the
    # measured cause of a whole plan change being refused (M11).
    data = dict(data)
    data["user_request"] = current.user_request
    read = graph_plan(
        c, data, profile, inherited=current,
        require_goal_contract=current.goal_contract is not None, require_acceptance_class=True,
    )
    if read.graph_version is not FAILED and read.graph_version != current.graph_version + 1:
        c.add("immutables", "plan.graph_version",
              "plan change graph_version must increment exactly once "
              f"({current.graph_version} -> {current.graph_version + 1})")
    if read.goal is not FAILED and read.goal != current.goal:
        c.add("immutables", "plan.goal", "plan changes must not replace the run goal")
    if read.model_strategy is not FAILED and read.model_strategy != current.model_strategy:
        c.add("immutables", "plan.model_strategy", "plan changes must not replace model_strategy")
    if read.goal_contract is not FAILED and read.goal_contract != current.goal_contract:
        c.add("immutables", "plan.goal_contract", "plan changes must not replace the Goal Contract")
    if read.roles is not FAILED:
        c.check("immutables", "plan.roles", validate_role_specification_transition, current.roles, read.roles)
    if read.skill_packs is not FAILED:
        existing = set(current.skill_packs)
        c.check(
            "immutables", "plan.skill_packs", validate_trusted_skill_promotions,
            tuple(pack for pack in read.skill_packs if pack not in existing),
            evidence_store=promotion_evidence_store,
        )
    if read.plan is not FAILED:
        c.check("immutables", "plan.tasks", validate_plan_skill_qualifications,
                read.plan, evidence_store=promotion_evidence_store)
    return read


def state_issues(
    current: Plan, read: ReadPlan, state: Any, requester_task_id: str
) -> list[tuple[str, str]]:
    """The run-state conditions a replacement can already be refused on.

    Only the monotone ones: a task removed stays removed, the requester out
    of the graph stays out, a VERIFIED task stays VERIFIED and a CANCELLED
    one stays CANCELLED (absorbing). They were checked only at the commit,
    after the plan verifier's PASS - where the conflict was never caught and
    took the dispatcher down with it.
    """

    from .task_state import TaskState

    found: list[tuple[str, str]] = []
    if read.raw_task_ids:
        removed = sorted(set(current.task_map) - read.raw_task_ids)
        if removed:
            found.append(("plan.tasks",
                f"plan changes cannot remove tasks with durable history: {removed}"))
        if requester_task_id and requester_task_id not in read.raw_task_ids:
            found.append(("plan.tasks", "plan change requester must remain in the graph"))
    final = {TaskState.VERIFIED.value: "verified", TaskState.CANCELLED.value: "cancelled"}
    states = getattr(state, "task_states", None) or {}
    for task in read.tasks:
        before = current.task_map.get(task.id)
        kind = final.get(str(states.get(task.id) or ""))
        if before is not None and kind and before != task:
            found.append((f"task {task.id}",
                f"{kind} task {task.id} is immutable during plan evolution"))
    return found


def coverage(c: Any, read: ReadPlan) -> None:
    if read.goal_contract in (FAILED, None) or not c.clean("tasks"):
        return
    from .plan_verification import coverage_issues

    for issue in coverage_issues(read.goal_contract, read.tasks):
        c.add("coverage", "plan.goal_contract",
              "proposed plan failed deterministic coverage admission: " + issue.summary)


def _envelope(c: Any, message: str, request_id: str, base_graph_version: int) -> Any:
    """The replanner's protocol line, every defect of it collected (R31)."""

    try:
        raw = _single_final_protocol_object(message, PLAN_CHANGE_RESULT_PREFIX)
    except PlanChangeProtocolError as exc:
        c.add("protocol", "protocol", str(exc))
        return FAILED
    if raw is None:
        c.add("protocol", "protocol",
              f"replanner must end with exactly one {PLAN_CHANGE_RESULT_PREFIX} object "
              f'{{"request_id":"{request_id}","base_graph_version":{base_graph_version},"plan":{{...}}}}')
        return FAILED
    unknown = sorted(set(raw) - set(ENVELOPE_FIELDS))
    missing = [name for name in ENVELOPE_FIELDS if name not in raw]
    if unknown or missing:
        c.add("protocol", "protocol",
              f"plan change result fields must be exactly {list(ENVELOPE_FIELDS)}; "
              f"unknown {unknown}, missing {missing}", ENVELOPE_FIELDS)
    if "request_id" in raw and raw["request_id"] != request_id:
        c.add("protocol", "protocol.request_id",
              f"plan change request_id must be {request_id!r}; got {raw['request_id']!r}")
    base = raw.get("base_graph_version")
    if "base_graph_version" in raw and (isinstance(base, bool) or base != base_graph_version):
        c.add("protocol", "protocol.base_graph_version",
              f"base_graph_version must be {base_graph_version}, the graph you were given; got {base!r}")
    plan = raw.get("plan")
    if not isinstance(plan, dict):
        if "plan" in raw:
            c.add("protocol", "protocol.plan", "plan change result plan must be an object")
        return FAILED
    return plan


def admit_replanner_result(
    current: Plan,
    final_message: str,
    *,
    request_id: str,
    base_graph_version: int,
    requester_task_id: str,
    profile: str,
    evidence_store: Any | None,
    state: Any,
) -> tuple[Any, Plan]:
    """The replanner's reply, admitted or refused with every reason at once.

    ``base_graph_version`` is the one its prompt carried. When the current
    graph is no longer that one, the runtime moved under the replanner -
    that is state, not a model error: ``PlanChangeConflictError`` is raised
    and the caller raises a fresh replanner on the current graph without
    spending an attempt. A reply that names another base than it was given
    is the model's error, reported with the rest.
    """

    if base_graph_version != current.graph_version:
        raise PlanChangeConflictError(
            f"the graph moved under the replanner: it was given version {base_graph_version}, "
            f"the run holds {current.graph_version}"
        )
    c = IssueCollector()
    raw_plan = _envelope(c, final_message, request_id, base_graph_version)
    if raw_plan is FAILED:
        c.raise_if_any()
    read = plan_change_candidate(c, current, raw_plan, profile, promotion_evidence_store=evidence_store)
    coverage(c, read)
    for path, message in state_issues(current, read, state, requester_task_id):
        c.add("state", path, message)
    c.raise_if_any()
    return PlanChangeResult(request_id, base_graph_version, dict(raw_plan)), read.plan

