from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .acceptance import AcceptanceClass
from .department_acceptance import DepartmentContract
from .goal_contract import GoalContract, is_persisted_goal_contract_compatibility
from .plan_fields import PLAN_FIELDS
from .plan_graph import topological_order
from .role_specification import ROLE_SPECIFICATIONS_FILE, RoleProfile, RoleSpecificationError, prepare_role_specifications, role_profile_to_dict
from .skill_packs import SkillAttestation, SkillPack, SkillReference, validate_plan_skill_qualifications, validate_trusted_skill_promotions


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

# M10-REV-004: a new schema-3 run enters the declared v0.9 mode by
# default. Migrated v0.8 plans are untouched: they carry
# execution_strategy="serial", max_parallel_workers=1 and
# legacy_serial=True explicitly, and validation keeps them from slipping
# into parallelism implicitly.
DEFAULT_EXECUTION_STRATEGY = "auto"

# Values for a CONFIG WITHOUT a [runtime] section, i.e. a project created
# before v0.9. Such a project stays serial and single-lane explicitly,
# rather than drifting into parallelism because a new run's default
# changed.
COMPAT_EXECUTION_STRATEGY = "serial"
COMPAT_MAX_PARALLEL_WORKERS = 1
# A conservative but genuinely parallel limit: two workers give real
# parallelism with minimal growth in load and spend. The user's decision
# of 14 Sep 2026. Two stood here as the default and got into the plan
# template, from where the planner copied it blindly: a 24-task graph with
# four independent branches ran two at a time. The Computer Use limit is
# held by a separate slot and does not depend on this number.
DEFAULT_MAX_PARALLEL_WORKERS = 10
DEFAULT_COMPUTER_USE_SLOTS = 1
DEFAULT_MAX_MEMORY_RECORDS = 8
DEFAULT_MAX_DEPENDENCY_OUTPUTS = 8

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
    produces_outcomes: tuple[str, ...] = ()
    acceptance_class: AcceptanceClass = AcceptanceClass.MIXED
    loaded_skills: tuple[SkillReference, ...] = ()
    skill_attestation: SkillAttestation | None = None


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
    departments: tuple[DepartmentContract, ...] = ()
    graph_version: int = 1
    execution_strategy: str = DEFAULT_EXECUTION_STRATEGY
    max_parallel_workers: int = DEFAULT_MAX_PARALLEL_WORKERS
    computer_use_slots: int = DEFAULT_COMPUTER_USE_SLOTS
    source_schema_version: int = PLAN_SCHEMA_VERSION
    legacy_serial: bool = False
    goal_contract: GoalContract | None = None
    skill_packs: tuple[SkillPack, ...] = ()

    @property
    def milestones(self) -> tuple[Task, ...]:
        """Compatibility alias for callers that use milestone terminology."""

        return self.tasks

    @property
    def task_map(self) -> dict[str, Task]:
        return {item.id: item for item in self.tasks}

    @property
    def role_map(self) -> dict[str, RoleProfile]:
        return {item.id: item for item in self.roles}


def validate_plan(data: dict[str, Any], profile: str, *, promotion_evidence_store: Any | None = None) -> Plan:
    """Validate a submitted canonical plan.

    A submitted payload cannot supply its own migration provenance.  Existing
    v0.8 runs use :func:`validate_migrating_plan`, which derives that provenance
    from the adjacent durable plan and run-state.
    """

    if isinstance(data, dict) and data.get("compatibility") is not None:
        raise ValueError(
            "plan.compatibility is produced by migration, never declared: a "
            "submitted schema-3 plan cannot claim migrated provenance"
        )
    plan = _validate_plan_payload(
        data,
        profile,
        inherited=None,
        migrated_milestone_ids=None,
        require_goal_contract=True,
        require_acceptance_class=True,
    )
    validate_trusted_skill_promotions(plan.skill_packs, evidence_store=promotion_evidence_store)
    validate_plan_skill_qualifications(plan, evidence_store=promotion_evidence_store)
    return plan


def validate_migrating_plan(
    data: dict[str, Any],
    profile: str,
    *,
    state_dir: Path,
    state_payload: dict[str, Any] | None = None,
) -> Plan:
    """Validate initial input while preserving a proven existing v0.8 run."""

    if isinstance(data, dict) and data.get("compatibility") is not None:
        raise ValueError(
            "plan.compatibility is produced by migration, never declared: a "
            "submitted schema-3 plan cannot claim migrated provenance"
        )
    migrated_ids = persisted_legacy_milestone_ids(
        state_dir,
        state_payload=state_payload,
    )
    persisted_compatibility = is_persisted_goal_contract_compatibility(
        data,
        state_dir=state_dir,
        schema_version=PLAN_SCHEMA_VERSION,
        state_payload=state_payload,
        plan_filename=PLAN_FILE,
    )
    plan = _validate_plan_payload(
        data,
        profile,
        inherited=None,
        migrated_milestone_ids=migrated_ids,
        require_goal_contract=not persisted_compatibility,
        require_acceptance_class=not persisted_compatibility,
    )
    validate_trusted_skill_promotions(
        plan.skill_packs, project_root=state_dir.expanduser().resolve().parent
    )
    validate_plan_skill_qualifications(plan, project_root=state_dir.expanduser().resolve().parent)
    return plan


def validate_persisted_plan(
    data: dict[str, Any],
    profile: str,
    *,
    state_dir: Path | None = None,
) -> Plan:
    """Validate a plan written by the runtime itself.

    Differs from `validate_plan` in one thing: `compatibility` is allowed
    here, because it was not written from outside. A migrated v0.8 plan
    reaches this form only through `_validate_legacy_plan`, and the target
    plan of a plan-change transaction is pinned by hash to a candidate that
    already passed `validate_plan_change`.

    A submitted plan goes through another entrance and cannot declare its
    provenance: otherwise a fresh schema-3 plan would call itself migrated
    and step out from under independent acceptance - self-acceptance (R8).
    """

    migrated_ids = _legacy_milestone_ids(data)
    if migrated_ids is not None:
        if state_dir is None or not _legacy_run_state_proves_migration(state_dir):
            raise ValueError(
                "persisted legacy plan requires an adjacent existing v0.8 run-state; "
                "migration provenance cannot be declared by plan content alone"
            )
    elif isinstance(data, dict) and data.get("compatibility") is not None:
        raise ValueError(
            "persisted plan.compatibility is valid only for a proven migrated v0.8 run"
        )
    persisted_compatibility = is_persisted_goal_contract_compatibility(
        data,
        state_dir=state_dir,
        schema_version=PLAN_SCHEMA_VERSION,
        plan_filename=PLAN_FILE,
    )
    plan = _validate_plan_payload(
        data,
        profile,
        inherited=None,
        migrated_milestone_ids=(
            frozenset(migrated_ids) if migrated_ids is not None else None
        ),
        require_goal_contract=not persisted_compatibility,
        require_acceptance_class=not persisted_compatibility,
    )
    project_root = state_dir.expanduser().resolve().parent if state_dir is not None else None
    validate_trusted_skill_promotions(plan.skill_packs, project_root=project_root)
    validate_plan_skill_qualifications(plan, project_root=project_root)
    return plan


def persisted_legacy_milestone_ids(
    state_dir: Path,
    *,
    state_payload: dict[str, Any] | None = None,
) -> frozenset[str] | None:
    """Return task IDs only for an existing persisted legacy run.

    The format of a submitted plan is not provenance.  The compatibility
    exception exists only for a run that was already present on disk: its
    run-state must carry the v0.7/v0.8 lineage and its persisted plan must be
    either the original schema-2 milestones or the exact schema-3
    ``legacy_serial`` representation produced by this runtime.
    """

    resolved = state_dir.expanduser().resolve()
    if not _legacy_run_state_proves_migration(
        resolved,
        state_payload=state_payload,
    ):
        return None
    plan_path = resolved / PLAN_FILE
    if not plan_path.is_file():
        return None
    try:
        previous = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ids = _legacy_milestone_ids(previous)
    if ids is not None and previous.get("schema_version") == PLAN_SCHEMA_VERSION:
        # A migrated graph can later acquire new canonical tasks.  Only the
        # exact historical v0.8 acceptance signature identifies tasks that
        # existed before independent verification; a later independent task
        # must never be downgraded by submitting it again as schema 2.
        ids = tuple(
            task_id
            for task_id, item in zip(ids, previous["tasks"], strict=True)
            if _is_legacy_verification_payload(item.get("verification"))
        )
    return frozenset(ids) if ids is not None else None


def _legacy_run_state_proves_migration(
    state_dir: Path,
    *,
    state_payload: dict[str, Any] | None = None,
) -> bool:
    if state_payload is None:
        state_path = state_dir / "run-state.json"
        if not state_path.is_file():
            return False
        try:
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
    if not isinstance(state_payload, dict):
        return False
    run_id = state_payload.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        return False
    schema = state_payload.get("schema_version")
    if schema in {3, 4}:
        return True
    if schema != 5:
        return False
    migrated_from = state_payload.get("migrated_from_schema")
    if migrated_from in {3, 4}:
        return True
    # Early v0.9 builds persisted the converted plan before they acquired a
    # dedicated lineage marker.  Preserve those real runs only when their
    # durable scheduler shape is the legacy serial shape; a fresh schema-5
    # run with ordinary defaults does not qualify.
    return (
        state_payload.get("execution_strategy") == "serial"
        and state_payload.get("max_parallel_workers") == 1
    )


def _legacy_milestone_ids(data: Any) -> tuple[str, ...] | None:
    if not isinstance(data, dict):
        return None
    schema = data.get("schema_version")
    raw_legacy = schema in {None, LEGACY_PLAN_SCHEMA_VERSION} and isinstance(
        data.get("milestones"), list
    )
    converted_legacy = (
        schema == PLAN_SCHEMA_VERSION
        and data.get("compatibility")
        == {"migrated_from_schema": LEGACY_PLAN_SCHEMA_VERSION, "legacy_serial": True}
        and data.get("execution_strategy") == "serial"
        and data.get("max_parallel_workers") == 1
        and data.get("computer_use_slots", DEFAULT_COMPUTER_USE_SLOTS) == 1
        and isinstance(data.get("tasks"), list)
    )
    if not raw_legacy and not converted_legacy:
        return None
    entries = data["milestones" if raw_legacy else "tasks"]
    if not entries:
        return None
    result: list[str] = []
    for index, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            return None
        value = item.get("id", f"M{index}" if raw_legacy else None)
        if not isinstance(value, str) or not value.strip():
            return None
        result.append(value.strip())
    return tuple(result)


def _is_legacy_verification_payload(raw: Any) -> bool:
    return raw == {
        "policy": "self",
        "required": True,
        "deterministic_checks": [],
        "max_revision_attempts": 0,
    }


def _is_legacy_verification(policy: VerificationPolicy) -> bool:
    return policy == VerificationPolicy(
        policy="self",
        required=True,
        deterministic_checks=(),
        max_revision_attempts=0,
    )


def _validate_plan_payload(
    data: dict[str, Any],
    profile: str,
    *,
    inherited: "Plan | None",
    require_goal_contract: bool,
    require_acceptance_class: bool,
    migrated_milestone_ids: frozenset[str] | None = None,
) -> Plan:
    if profile not in {"adaptive", "host-settings"}:
        raise ValueError("profile must be adaptive or host-settings")
    if not isinstance(data, dict):
        raise ValueError("plan must be an object")
    schema = data.get("schema_version")
    if schema in {None, LEGACY_PLAN_SCHEMA_VERSION} and "milestones" in data and inherited is None:
        from .plan_legacy import validate_legacy_plan

        return validate_legacy_plan(
            data, profile, migrated_milestone_ids=migrated_milestone_ids
        )
    # Any other schema_version is one violation among the rest
    # (``plan_admission.graph_plan``, stage "fields"). It was a pregate here
    # that raised alone and hid every other defect of the graph.
    return _validate_graph_plan(
        data,
        profile,
        inherited=inherited,
        require_goal_contract=require_goal_contract,
        require_acceptance_class=require_acceptance_class,
    )


def validate_plan_change(
    current: Plan,
    data: dict[str, Any],
    profile: str,
    *,
    promotion_evidence_store: Any | None = None,
) -> Plan:
    """Validate a complete replacement graph before any durable write.

    Every violation in one pass (``plan_admission.plan_change_candidate``):
    the schema, the graph, and the fields a change may not replace - goal,
    model_strategy, the Goal Contract, graph_version + 1, the role
    transition and skill promotions. user_request is carried over from the
    current plan, never taken from the reply (M11: a 35 234-character echo
    was the measured cause of a legitimate change refused wholesale).
    """

    from .plan_admission import plan_change_candidate
    from .plan_issues import IssueCollector

    if profile not in {"adaptive", "host-settings"}:
        raise ValueError("profile must be adaptive or host-settings")
    if not isinstance(data, dict):
        raise ValueError("plan must be an object")
    c = IssueCollector()
    read = plan_change_candidate(
        c, current, data, profile, promotion_evidence_store=promotion_evidence_store
    )
    c.raise_if_any()
    return read.plan


def load_plan(state_dir: Path, profile: str) -> Plan:
    data = json.loads((state_dir / PLAN_FILE).read_text(encoding="utf-8"))
    return validate_persisted_plan(data, profile, state_dir=state_dir)


def save_plan(state_dir: Path, plan: Plan) -> None:
    try:
        role_registry, _hiring = prepare_role_specifications(state_dir, plan.roles)
    except RoleSpecificationError as exc:
        raise ValueError(str(exc)) from exc
    atomic_json(state_dir / PLAN_FILE, plan_to_dict(plan))
    atomic_json(state_dir / ROLE_SPECIFICATIONS_FILE, role_registry)


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "graph_version": plan.graph_version,
        "goal": plan.goal,
        "user_request": plan.user_request,
        **({"goal_contract": plan.goal_contract.to_dict()} if plan.goal_contract is not None else {}),
        "model_strategy": plan.model_strategy,
        "execution_strategy": plan.execution_strategy,
        "max_parallel_workers": plan.max_parallel_workers,
        "computer_use_slots": plan.computer_use_slots,
        "roles": [role_profile_to_dict(item) for item in plan.roles],
        **({"skill_packs": [item.to_dict() for item in plan.skill_packs]} if plan.skill_packs else {}),
        "tasks": [_task_to_dict(item) for item in plan.tasks],
    }
    if plan.departments:
        payload["departments"] = [item.to_dict() for item in plan.departments]
    if plan.legacy_serial:
        payload["compatibility"] = {
            "migrated_from_schema": plan.source_schema_version,
            "legacy_serial": True,
        }
    return payload


# The single list of allowed plan fields, named to the model in the
# replanner prompt; it lives in plan_fields with every nested set.
GRAPH_PLAN_FIELDS = frozenset(PLAN_FIELDS)


def _validate_graph_plan(
    data: dict[str, Any],
    profile: str,
    *,
    require_goal_contract: bool,
    require_acceptance_class: bool,
    inherited: "Plan | None" = None,
) -> Plan:
    """Every violation of the graph in one pass (``plan_admission``)."""

    from .plan_admission import graph_plan
    from .plan_issues import IssueCollector

    c = IssueCollector()
    read = graph_plan(
        c,
        data,
        profile,
        inherited=inherited,
        require_goal_contract=require_goal_contract,
        require_acceptance_class=require_acceptance_class,
    )
    c.raise_if_any()
    return read.plan


def _task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id, "title": task.title, "objective": task.objective,
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
        "acceptance_class": task.acceptance_class.value,
        **({"loaded_skills": [item.to_dict() for item in task.loaded_skills]} if task.loaded_skills else {}),
        **({"skill_attestation": task.skill_attestation.to_dict()} if task.skill_attestation else {}),
        **(
            {"produces_outcomes": list(task.produces_outcomes)}
            if task.produces_outcomes
            else {}
        ),
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
