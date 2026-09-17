from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable

from .acceptance import AcceptanceClass, acceptance_class_from_raw
from .acceptance_floor import CLEAN_IDENTITY_ASSIGNMENTS, is_clean_suite_command
from .department_acceptance import (
    DepartmentAcceptanceError,
    DepartmentContract,
    department_contract_from_raw,
    task_department_binding,
    resolve_task_department,
    validate_department_contracts,
)
from .goal_contract import (
    GoalContract,
    GoalContractError,
    is_persisted_goal_contract_compatibility,
    validate_goal_contract,
    validate_outcome_bindings,
)
from .models import EXECUTION_MODES, STRATEGIES
from .plan_graph import topological_order
from .reasoning import normalize
from .role_specification import DEFAULT_ROLE_SPECIFICATION_VERSION, ROLE_SPECIFICATIONS_FILE, RoleProfile, RoleSpecificationError, prepare_role_specifications, role_profile_to_dict, validate_role_specification_transition, validate_role_version
from .skill_packs import SkillAttestation, SkillPack, SkillPackError, SkillReference, skill_attestation_from_raw, skill_packs_from_raw, skill_references_from_raw, validate_plan_skill_bindings, validate_plan_skill_qualifications, validate_trusted_skill_promotions


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

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")


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
    if schema in {None, LEGACY_PLAN_SCHEMA_VERSION} and "milestones" in data:
        if inherited is not None:
            raise ValueError("plan changes must use the canonical v0.9 schema")
        from .plan_legacy import validate_legacy_plan

        return validate_legacy_plan(
            data, profile, migrated_milestone_ids=migrated_milestone_ids
        )
    if schema != PLAN_SCHEMA_VERSION:
        raise ValueError(
            f"plan.schema_version must be {PLAN_SCHEMA_VERSION}; "
            f"v0.8 serial plans may use {LEGACY_PLAN_SCHEMA_VERSION} or omit it"
        )
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
    """Validate a complete replacement graph before any durable write."""

    # user_request is carried over from the current plan, not taken from
    # the replanner's reply. A verbatim echo used to be required, and the
    # prompt honestly asked "preserve user_request verbatim" - but on a live
    # run that is 35 234 characters. A model rewriting the graph does not
    # reproduce such a string, and a legitimate plan change was rejected
    # wholesale.
    #
    # Measured on M11: the replanner's turn completed successfully, the
    # result was rejected with "plan changes must not replace the original
    # user request", the run stood, a ticket opened.
    #
    # Carrying over is stricter than the old check: an echo could be forged,
    # while a field not taken from the reply cannot be changed at all. goal
    # (542 characters) and model_strategy stay strict - the model repeats
    # them reliably, and a discrepancy there means intent, not a copy error.
    data = dict(data)
    data["user_request"] = current.user_request
    if data.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("plan changes must use the canonical v0.9 schema")
    # A migrated plan's provenance is inherited from the current plan and
    # never declared by the submitted body: otherwise a replacement would
    # "declare" itself legacy and step out from under the acceptance floor,
    # which is self-acceptance (R8).
    candidate = _validate_plan_payload(
        data,
        profile,
        inherited=current,
        require_goal_contract=current.goal_contract is not None,
        require_acceptance_class=True,
    )
    if candidate.graph_version != current.graph_version + 1:
        raise ValueError(
            "plan change graph_version must increment exactly once "
            f"({current.graph_version} -> {current.graph_version + 1})"
        )
    if candidate.goal != current.goal:
        raise ValueError("plan changes must not replace the run goal")
    if candidate.model_strategy != current.model_strategy:
        raise ValueError("plan changes must not replace model_strategy")
    if candidate.goal_contract != current.goal_contract:
        raise ValueError("plan changes must not replace the Goal Contract")
    try:
        validate_role_specification_transition(current.roles, candidate.roles)
    except RoleSpecificationError as exc:
        raise ValueError(str(exc)) from exc
    existing = set(current.skill_packs)
    new_promotions = tuple(pack for pack in candidate.skill_packs if pack not in existing)
    validate_trusted_skill_promotions(new_promotions, evidence_store=promotion_evidence_store)
    validate_plan_skill_qualifications(candidate, evidence_store=promotion_evidence_store)
    # validate_plan already performs all role, output, dependency, and cycle
    # checks. Keeping this wrapper mandatory prevents a plan-change path from
    # accidentally treating initial-load validation as optional.
    return candidate


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


def _validate_canonical_acceptance(
    plan: "Plan",
    *,
    inherited: "Plan | None" = None,
) -> None:
    """Enforce the acceptance floor for every new canonical task.

    R8/R29 make deterministic checks admission evidence for a fresh judge,
    never acceptance by themselves.  The argv gate can prove the identity
    environment is reset and that a direct command exists; executing the
    declared repository-wide check, not this lint, proves its outcome (R25).

    Migrated v0.8 ``legacy_serial`` plans remain the sole compatibility
    exception because rewriting their historical acceptance contract would
    mutate an existing run.

    The exception is historical, so it rests on provenance, not on a claim.
    On a plan change only a task whose acceptance contract is not rewritten
    keeps it: a migrated run may fix a task's composition (resources,
    dependencies, text), while a new task or one with rewritten verification
    passes the floor in full. Otherwise a replacement would smuggle in, under
    the guise of migration, a task that accepts its own work (R8).
    """

    if not plan.legacy_serial:
        exempt: frozenset[str] = frozenset()
    elif inherited is None:
        # A persisted migrated graph may also contain tasks added after the
        # migration.  Those tasks were born under the canonical acceptance
        # floor and remain independent; only the exact synthesized v0.8
        # verification contract receives the historical exception.
        exempt = frozenset(
            task.id
            for task in plan.tasks
            if _is_legacy_verification(task.verification)
        )
    else:
        def contract(task: "Task") -> tuple:
            """The task's essence: what we do, when it counts as done, how it is accepted.

            The exception is historical, so it rests on history. Only the
            verification used to be compared - and under an old id the work
            itself could be swapped while the weak acceptance contract stayed
            untouched. New work under someone else's number is the same
            self-acceptance (R8).

            Resources and dependencies are deliberately not included: a
            migrated task's composition may be fixed, its meaning may not be
            rewritten.
            """

            return (
                task.objective,
                tuple(task.definition_of_done),
                task.execution_mode,
                task.verification,
            )

        before = {task.id: contract(task) for task in inherited.tasks}
        exempt = frozenset(
            task.id
            for task in plan.tasks
            if task.id in before and before[task.id] == contract(task)
        )

    for task in plan.tasks:
        if task.id in exempt:
            continue
        verification = task.verification
        if verification.policy != "independent":
            raise ValueError(
                "R8/R29: canonical task "
                f"{task.id} verification.policy must be \"independent\"; "
                f"got {verification.policy!r}. Deterministic checks admit work "
                "to independent judgement and never replace it"
            )
        if not verification.required:
            raise ValueError(
                f"R8/R29: canonical task {task.id} verification.required must be true"
            )
        if verification.max_revision_attempts < 2:
            raise ValueError(
                "R29: canonical task "
                f"{task.id} verification.max_revision_attempts must be at least 2"
            )
        suite = next(
            (
                check
                for check in verification.deterministic_checks
                if is_clean_suite_command(check)
            ),
            None,
        )
        if suite is None:
            names = ", ".join(f"{name}=" for name in CLEAN_IDENTITY_ASSIGNMENTS)
            raise ValueError(
                "R29: canonical task "
                f"{task.id} must declare at least one full-suite deterministic "
                "check as a successful command argv launched through env, reset "
                f"{names}, and invoke the command directly"
            )

# The single list of allowed plan fields. It is also named to the model in
# the replanner prompt: otherwise the refusal "plan has unknown fields"
# does not say which fields exist at all, and the redo goes blind.
GRAPH_PLAN_FIELDS = frozenset({
    "schema_version", "graph_version", "goal", "user_request", "goal_contract",
    "model_strategy", "execution_strategy", "max_parallel_workers", "computer_use_slots",
    "roles", "departments", "skill_packs", "tasks", "compatibility",
})


def _validate_graph_plan(
    data: dict[str, Any],
    profile: str,
    *,
    require_goal_contract: bool,
    require_acceptance_class: bool,
    inherited: "Plan | None" = None,
) -> Plan:
    _reject_unknown(data, set(GRAPH_PLAN_FIELDS), "plan")
    goal, user_request, strategy = _plan_header(
        data,
        profile,
        require_user_request=True,
    )
    raw_goal_contract = data.get("goal_contract")
    if require_goal_contract and raw_goal_contract is None:
        raise ValueError(
            "plan.goal_contract must be declared as a structured Goal Contract"
        )
    try:
        goal_contract = (
            validate_goal_contract(raw_goal_contract, label="plan.goal_contract")
            if raw_goal_contract is not None
            else None
        )
    except GoalContractError as exc:
        raise ValueError(str(exc)) from exc
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
    if inherited is not None:
        # A replacement may only repeat the current plan's provenance - so a
        # copy of the plan from plan_to_dict passes unchanged - but cannot
        # introduce or rewrite it. Declaring itself migrated and stepping out
        # from under independent verification is not allowed (R8).
        expected = (
            {
                "migrated_from_schema": inherited.source_schema_version,
                "legacy_serial": True,
            }
            if inherited.legacy_serial
            else None
        )
        if compatibility != expected:
            raise ValueError(
                "plan.compatibility is inherited from the current plan, never "
                "declared by a plan change"
            )
        legacy_serial = inherited.legacy_serial
        source_schema = inherited.source_schema_version
        if legacy_serial and (
            execution_strategy != "serial"
            or max_parallel_workers != 1
            or computer_use_slots != 1
        ):
            raise ValueError(
                "a migrated v0.8 legacy_serial plan must remain serial with "
                "max_parallel_workers=1 and computer_use_slots=1"
            )
    elif compatibility is not None:
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
            or computer_use_slots != 1
        ):
            raise ValueError(
                "a migrated v0.8 legacy_serial plan must remain serial with "
                "max_parallel_workers=1 and computer_use_slots=1"
            )

    raw_roles = data.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise ValueError("plan.roles must be a non-empty array")
    roles = tuple(_role_from_raw(raw, index) for index, raw in enumerate(raw_roles, 1))
    _validate_unique((role.id for role in roles), "role id")

    try:
        skill_packs = skill_packs_from_raw(data.get("skill_packs", []))
    except SkillPackError as exc:
        raise ValueError(str(exc)) from exc

    raw_departments = data.get("departments", [])
    if not isinstance(raw_departments, list):
        raise ValueError("plan.departments must be an array")
    try:
        departments = tuple(
            department_contract_from_raw(raw, f"department {index}")
            for index, raw in enumerate(raw_departments, 1)
        )
        validate_department_contracts(
            departments,
            role_ids=(role.id for role in roles),
        )
    except DepartmentAcceptanceError as exc:
        raise ValueError(str(exc)) from exc

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("plan.tasks must be a non-empty array")
    tasks: list[Task] = []
    for index, raw in enumerate(raw_tasks, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"task {index} must be an object")
        tasks.append(
            _task_from_raw(
                raw,
                profile,
                f"task {index}",
                canonical=True,
                require_acceptance_class=require_acceptance_class,
            )
        )
    _validate_unique((task.id for task in tasks), "task id")
    plan = Plan(
        goal=goal,
        user_request=user_request,
        model_strategy=strategy,
        tasks=tuple(tasks),
        roles=roles,
        departments=departments,
        graph_version=graph_version,
        execution_strategy=execution_strategy,
        max_parallel_workers=max_parallel_workers,
        computer_use_slots=computer_use_slots,
        source_schema_version=source_schema,
        legacy_serial=legacy_serial,
        goal_contract=goal_contract,
        skill_packs=skill_packs,
    )
    try:
        if goal_contract is not None:
            validate_outcome_bindings(
                goal_contract,
                {task.id: task.produces_outcomes for task in plan.tasks},
            )
        else:
            orphan = next(
                (task for task in plan.tasks if task.produces_outcomes),
                None,
            )
            if orphan is not None:
                raise GoalContractError(
                    f"task {orphan.id} declares produces_outcomes without "
                    "plan.goal_contract"
                )
    except GoalContractError as exc:
        raise ValueError(str(exc)) from exc
    _validate_canonical_acceptance(plan, inherited=inherited)
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
            "version",
            "responsibilities",
            "domain_focus",
            "preferred_tools",
            "context_priorities",
            "verification_expectations",
            "skill_requirements",
        },
        label,
    )
    return RoleProfile(
        id=_identifier(raw.get("id"), f"{label}.id"),
        name=_required_string(raw.get("name"), f"{label}.name"),
        responsibilities=_nonempty_strings(raw.get("responsibilities"), f"{label}.responsibilities"),
        version=validate_role_version(
            raw.get("version", DEFAULT_ROLE_SPECIFICATION_VERSION),
            f"{label}.version",
        ),
        domain_focus=_strings(raw.get("domain_focus", []), f"{label}.domain_focus"),
        preferred_tools=_strings(raw.get("preferred_tools", []), f"{label}.preferred_tools"),
        context_priorities=_strings(raw.get("context_priorities", []), f"{label}.context_priorities"),
        verification_expectations=_strings(
            raw.get("verification_expectations", []),
            f"{label}.verification_expectations",
        ),
        skill_requirements=skill_references_from_raw(
            raw.get("skill_requirements", []), f"{label}.skill_requirements"
        ),
    )


def _task_from_raw(
    raw: dict[str, Any],
    profile: str,
    label: str,
    *,
    canonical: bool,
    require_acceptance_class: bool = False,
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
                "produces_outcomes",
                "acceptance_class",
                "loaded_skills",
                "skill_attestation",
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
    produces_outcomes = _identifiers(
        raw.get("produces_outcomes", []),
        f"{label}.produces_outcomes",
    )
    _validate_unique(produces_outcomes, f"{label} produced outcome id")
    acceptance_class = acceptance_class_from_raw(
        raw.get("acceptance_class"),
        f"{label}.acceptance_class",
        default=None if require_acceptance_class else AcceptanceClass.MIXED,
    )
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
        produces_outcomes=produces_outcomes,
        acceptance_class=acceptance_class,
        loaded_skills=skill_references_from_raw(
            raw.get("loaded_skills", []), f"{label}.loaded_skills"
        ),
        skill_attestation=skill_attestation_from_raw(raw.get("skill_attestation"), f"{label}.skill_attestation"),
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
    if policy == "independent" and "max_revision_attempts" not in raw:
        raise ValueError(
            f"{label}.max_revision_attempts must be declared and be at least 2"
        )
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
    try:
        validate_department_contracts(plan.departments, role_ids=role_ids)
    except DepartmentAcceptanceError as exc:
        raise ValueError(str(exc)) from exc
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
        try:
            department_binding = task_department_binding(task)
        except DepartmentAcceptanceError as exc:
            raise ValueError(f"task {task.id}: {exc}") from exc
        if department_binding is not None:
            try:
                department = resolve_task_department(
                    plan.departments,
                    task,
                    role_names={item.id: item.name for item in plan.roles},
                )
            except DepartmentAcceptanceError as exc:
                raise ValueError(f"task {task.id}: {exc}") from exc
            if department is None:
                raise ValueError(f"task {task.id}: department binding disappeared")
            if not task.context.dependency_outputs:
                raise ValueError(
                    f"task {task.id} department acceptance requires a selected "
                    "dependency output carrying the pinned rubric reference"
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
    try:
        validate_plan_skill_bindings(plan)
    except SkillPackError as exc:
        raise ValueError(str(exc)) from exc
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
