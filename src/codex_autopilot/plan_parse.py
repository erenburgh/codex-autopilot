"""Reading one plan entity from its raw object: roles, tasks and their parts.

Moved out of ``plan.py``, where each reader stopped at its first
``raise ValueError``: a task missing both ``execution_mode_reason`` and
``verification.max_revision_attempts`` reported the first, and the second
came back one replanner round later. Every reader now takes a collector
``c``. Independent fields go through ``c.check`` and are all reported;
conditions that depend on each other stay sequential inside one check
(``policy`` before ``max_revision_attempts``, ``kind`` before
``argv``/``path``). A reader whose object had any failed field returns
``FAILED`` - the caller builds no half-read entity from it.

The default collector is fail-fast, which is the old behaviour: the legacy
v0.8 reader (``plan_legacy``) and any other caller get the first error, as
before.

Imported lazily from ``plan``'s function bodies (as ``plan_legacy`` is), so
the imports from ``plan`` below are safe: by then it is fully loaded.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .acceptance import AcceptanceClass, acceptance_class_from_raw
from .models import EXECUTION_MODES, STRATEGIES
from .plan import (
    DEFAULT_MAX_DEPENDENCY_OUTPUTS,
    DEFAULT_MAX_MEMORY_RECORDS,
    RESOURCE_ACCESS_MODES,
    RESOURCE_KINDS,
    VERIFICATION_CHECK_KINDS,
    VERIFICATION_POLICIES,
    ResourceClaim,
    Task,
    TaskContext,
    TaskOutput,
    VerificationCheck,
    VerificationPolicy,
)
from .plan_fields import (
    CHECK_FIELDS,
    CONTEXT_FIELDS,
    OUTPUT_FIELDS,
    RESOURCE_FIELDS,
    ROLE_FIELDS,
    TASK_FIELDS,
    VERIFICATION_FIELDS,
)
from .plan_issues import FAIL_FAST, FAILED, UnknownFieldsError
from .reasoning import normalize
from .role_specification import (
    DEFAULT_ROLE_SPECIFICATION_VERSION,
    RoleProfile,
    validate_role_version,
)
from .skill_packs import skill_attestation_from_raw, skill_references_from_raw

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")

# The fields whose absence a refusal of unknown fields also names. A model
# that wrote `lead_role` usually also left out `lead_role_id`; saying both in
# one line is what lets it fix both in one round.
REQUIRED_ROLE_FIELDS = ("id", "name", "responsibilities")
REQUIRED_TASK_FIELDS = (
    "id", "title", "objective", "definition_of_done", "execution_mode",
    "execution_mode_reason", "role", "verification",
)
REQUIRED_VERIFICATION_FIELDS = ("policy",)
REQUIRED_CHECK_FIELDS = ("id", "kind", "description")
REQUIRED_RESOURCE_FIELDS = ("id", "kind", "target", "access")
REQUIRED_OUTPUT_FIELDS = ("id", "description")


def _object(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    return raw


def _failed_since(c: Any, before: int) -> bool:
    return c.count() > before


def plan_header(
    data: dict[str, Any],
    profile: str,
    *,
    require_user_request: bool = False,
) -> tuple[str, str, str]:
    """goal, user_request and model_strategy - fail-fast, as the legacy reader wants."""

    goal = required_string(data.get("goal"), "plan.goal")
    user_request = required_string(
        data.get("user_request") if require_user_request else data.get("user_request", goal),
        "plan.user_request",
    )
    return goal, user_request, model_strategy(data, profile)


def model_strategy(data: dict[str, Any], profile: str) -> str:
    strategy = str(data.get("model_strategy") or ("auto" if profile == "adaptive" else "host-settings"))
    if strategy not in STRATEGIES:
        raise ValueError(f"model_strategy must be one of {sorted(STRATEGIES)}")
    if profile == "adaptive" and strategy == "host-settings":
        raise ValueError("Adaptive profile requires auto, sol-only, or astra-only model_strategy")
    if profile == "host-settings" and strategy != "host-settings":
        raise ValueError("Host Settings profile requires model_strategy=host-settings")
    return strategy


def role_from_raw(raw: Any, index: int, c: Any = FAIL_FAST, stage: str = "roles") -> Any:
    label = f"role {index}"
    if c.check(stage, label, _object, raw, label) is FAILED:
        return FAILED
    before = c.count()
    c.check(stage, label, reject_unknown, raw, ROLE_FIELDS, label, REQUIRED_ROLE_FIELDS)
    fields = {
        "id": c.check(stage, f"{label}.id", identifier, raw.get("id"), f"{label}.id"),
        "name": c.check(stage, f"{label}.name", required_string, raw.get("name"), f"{label}.name"),
        "responsibilities": c.check(
            stage, f"{label}.responsibilities", nonempty_strings,
            raw.get("responsibilities"), f"{label}.responsibilities",
        ),
        "version": c.check(
            stage, f"{label}.version", validate_role_version,
            raw.get("version", DEFAULT_ROLE_SPECIFICATION_VERSION), f"{label}.version",
        ),
    }
    for name in ("domain_focus", "preferred_tools", "context_priorities", "verification_expectations"):
        fields[name] = c.check(stage, f"{label}.{name}", strings, raw.get(name, []), f"{label}.{name}")
    fields["skill_requirements"] = c.check(
        stage, f"{label}.skill_requirements", skill_references_from_raw,
        raw.get("skill_requirements", []), f"{label}.skill_requirements",
    )
    if _failed_since(c, before):
        return FAILED
    return RoleProfile(**fields)


def task_from_raw(
    raw: dict[str, Any],
    profile: str,
    label: str,
    *,
    canonical: bool,
    require_acceptance_class: bool = False,
    task_id: str | None = None,
    role: str | None = None,
    depends_on: tuple[str, ...] | None = None,
    c: Any = FAIL_FAST,
    stage: str = "tasks",
) -> Any:
    """One task, every independent field checked; FAILED when any failed."""

    before = c.count()

    def field(name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        return c.check(stage, f"{label}.{name}", fn, *args, **kwargs)

    if canonical:
        c.check(stage, label, reject_unknown, raw, TASK_FIELDS, label, REQUIRED_TASK_FIELDS)
    title = field("title", required_string, raw.get("title"), f"{label}.title")
    objective = field("objective", required_string, raw.get("objective"), f"{label}.objective")
    done = field(
        "definition_of_done", nonempty_strings, raw.get("definition_of_done"), f"{label}.definition_of_done"
    )
    execution_mode = field("execution_mode", _execution_mode, raw.get("execution_mode", ""), label)
    execution_mode_reason = field(
        "execution_mode_reason", required_string,
        raw.get("execution_mode_reason"), f"{label}.execution_mode_reason",
    )
    reasoning = field("reasoning", _reasoning, raw, profile, label)
    resolved_id = task_id or field("id", identifier, raw.get("id"), f"{label}.id")
    resolved_role = role or field("role", identifier, raw.get("role"), f"{label}.role")
    resolved_dependencies = depends_on
    if resolved_dependencies is None:
        resolved_dependencies = field(
            "depends_on", identifiers, raw.get("depends_on", []), f"{label}.depends_on"
        )
    priority = field(
        "priority", bounded_int, raw.get("priority", 0), f"{label}.priority", -1_000_000, 1_000_000
    )
    verification = (
        verification_from_raw(raw.get("verification"), profile, f"{label}.verification", c, stage)
        if canonical
        else VerificationPolicy(policy="self", required=True, max_revision_attempts=0)
    )
    resources = _items(
        c, stage, raw.get("resources", []), f"{label}.resources", resource_from_raw, label,
        f"{label} resource id",
    )
    context = context_from_raw(raw.get("context"), label, c, stage) if canonical else TaskContext()
    outputs = _items(
        c, stage, raw.get("outputs", []), f"{label}.outputs", output_from_raw, label,
        f"{label} output id",
    )
    produces_outcomes = field(
        "produces_outcomes", _unique_identifiers,
        raw.get("produces_outcomes", []), f"{label}.produces_outcomes", f"{label} produced outcome id",
    )
    acceptance_class = field(
        "acceptance_class", acceptance_class_from_raw, raw.get("acceptance_class"),
        f"{label}.acceptance_class",
        default=None if require_acceptance_class else AcceptanceClass.MIXED,
    )
    required_capabilities = field(
        "required_capabilities", strings,
        raw.get("required_capabilities", []), f"{label}.required_capabilities",
    )
    tags = field("tags", strings, raw.get("tags", []), f"{label}.tags")
    loaded_skills = field(
        "loaded_skills", skill_references_from_raw, raw.get("loaded_skills", []), f"{label}.loaded_skills"
    )
    skill_attestation = field(
        "skill_attestation", skill_attestation_from_raw,
        raw.get("skill_attestation"), f"{label}.skill_attestation",
    )
    if _failed_since(c, before):
        return FAILED
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
        required_capabilities=required_capabilities,
        context=context,
        outputs=outputs,
        tags=tags,
        produces_outcomes=produces_outcomes,
        acceptance_class=acceptance_class,
        loaded_skills=loaded_skills,
        skill_attestation=skill_attestation,
    )


def _execution_mode(value: Any, label: str) -> str:
    mode = str(value).strip()
    if mode not in EXECUTION_MODES:
        raise ValueError(f"{label}.execution_mode must be one of {sorted(EXECUTION_MODES)}")
    return mode


def _items(
    c: Any, stage: str, raw: Any, label: str, reader: Any, parent: str, unique_name: str
) -> Any:
    """An array of nested objects: each item on its own, then unique ids."""

    array = c.check(stage, label, _array, raw, label)
    if array is FAILED:
        return FAILED
    before = c.count()
    items = tuple(reader(item, index, parent, c, stage) for index, item in enumerate(array, 1))
    if _failed_since(c, before):
        return FAILED
    if c.check(stage, label, validate_unique, (item.id for item in items), unique_name) is FAILED:
        return FAILED
    return items


def _unique_identifiers(value: Any, label: str, unique_name: str) -> tuple[str, ...]:
    result = identifiers(value, label)
    validate_unique(result, unique_name)
    return result


def verification_from_raw(
    raw: Any, profile: str, label: str, c: Any = FAIL_FAST, stage: str = "tasks"
) -> Any:
    if c.check(stage, label, _object, raw, label) is FAILED:
        return FAILED
    before = c.count()
    c.check(stage, label, reject_unknown, raw, VERIFICATION_FIELDS, label, REQUIRED_VERIFICATION_FIELDS)
    policy = c.check(stage, f"{label}.policy", _policy, raw, label)
    required = c.check(stage, f"{label}.required", _boolean, raw.get("required", True), f"{label}.required")
    checks = _items(
        c, stage, raw.get("deterministic_checks", []), f"{label}.deterministic_checks",
        check_from_raw, label, f"{label} check id",
    )
    if policy == "deterministic" and required is True and checks is not FAILED and not checks:
        c.add(stage, f"{label}.deterministic_checks",
              f"{label}.deterministic_checks is required for deterministic policy")
    verifier_role = raw.get("verifier_role")
    if verifier_role is not None:
        verifier_role = c.check(
            stage, f"{label}.verifier_role", identifier, verifier_role, f"{label}.verifier_role"
        )
    pair = c.check(stage, f"{label}.execution_mode", _execution_pair, raw, label)
    reasoning = None
    if "reasoning" in raw:
        reasoning = c.check(stage, f"{label}.reasoning", _verification_reasoning, raw, profile)
    max_attempts = c.check(
        stage, f"{label}.max_revision_attempts", bounded_int,
        raw.get("max_revision_attempts", 2), f"{label}.max_revision_attempts", 0, 100,
    )
    if _failed_since(c, before):
        return FAILED
    return VerificationPolicy(
        policy=policy,
        required=required,
        deterministic_checks=checks,
        verifier_role=verifier_role,
        execution_mode=pair[0],
        execution_mode_reason=pair[1],
        reasoning=reasoning,
        max_revision_attempts=max_attempts,
    )


def _policy(raw: dict[str, Any], label: str) -> str:
    """The policy, then what it demands: dependent, so one sequential check."""

    policy = str(raw.get("policy", "")).strip()
    if policy not in VERIFICATION_POLICIES:
        raise ValueError(f"{label}.policy must be one of {sorted(VERIFICATION_POLICIES)}")
    if policy == "independent" and "max_revision_attempts" not in raw:
        raise ValueError(f"{label}.max_revision_attempts must be declared and be at least 2")
    return policy


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _execution_pair(raw: dict[str, Any], label: str) -> tuple[str | None, str | None]:
    execution_mode = raw.get("execution_mode")
    if execution_mode is not None:
        execution_mode = str(execution_mode).strip()
        if execution_mode not in EXECUTION_MODES:
            raise ValueError(f"{label}.execution_mode must be one of {sorted(EXECUTION_MODES)}")
    reason = raw.get("execution_mode_reason")
    if reason is not None:
        reason = required_string(reason, f"{label}.execution_mode_reason")
    if bool(execution_mode) != bool(reason):
        raise ValueError(f"{label}.execution_mode and execution_mode_reason must be provided together")
    return execution_mode, reason


def _verification_reasoning(raw: dict[str, Any], profile: str) -> str:
    if profile != "adaptive":
        raise ValueError("Host Settings verification policies must omit reasoning")
    return normalize(str(raw["reasoning"]))


def check_from_raw(raw: Any, index: int, parent: str, c: Any = FAIL_FAST, stage: str = "tasks") -> Any:
    label = f"{parent}.deterministic_checks[{index}]"
    if c.check(stage, label, _object, raw, label) is FAILED:
        return FAILED
    before = c.count()
    c.check(stage, label, reject_unknown, raw, CHECK_FIELDS, label, REQUIRED_CHECK_FIELDS)
    shape = c.check(stage, f"{label}.kind", _check_shape, raw, label)
    check_id = c.check(stage, f"{label}.id", identifier, raw.get("id"), f"{label}.id")
    description = c.check(
        stage, f"{label}.description", required_string, raw.get("description"), f"{label}.description"
    )
    timeout = c.check(
        stage, f"{label}.timeout_seconds", bounded_int,
        raw.get("timeout_seconds", 300), f"{label}.timeout_seconds", 1, 86_400,
    )
    exit_code = c.check(
        stage, f"{label}.expected_exit_code", bounded_int,
        raw.get("expected_exit_code", 0), f"{label}.expected_exit_code", -255, 255,
    )
    if _failed_since(c, before):
        return FAILED
    kind, argv, path = shape
    return VerificationCheck(
        id=check_id, kind=kind, description=description, argv=argv, path=path,
        timeout_seconds=timeout, expected_exit_code=exit_code,
    )


def _check_shape(raw: dict[str, Any], label: str) -> tuple[str, tuple[str, ...], str | None]:
    """kind, then argv and path by kind: dependent, so one sequential check."""

    kind = str(raw.get("kind", "")).strip()
    if kind not in VERIFICATION_CHECK_KINDS:
        raise ValueError(f"{label}.kind must be one of {sorted(VERIFICATION_CHECK_KINDS)}")
    argv = strings(raw.get("argv", []), f"{label}.argv")
    path = raw.get("path")
    if path is not None:
        path = required_string(path, f"{label}.path")
    if kind == "command" and not argv:
        raise ValueError(f"{label}.argv is required for command checks")
    if kind != "command" and argv:
        raise ValueError(f"{label}.argv is only valid for command checks")
    if kind == "artifact" and not path:
        raise ValueError(f"{label}.path is required for artifact checks")
    if kind != "artifact" and path:
        raise ValueError(f"{label}.path is only valid for artifact checks")
    return kind, argv, path


def resource_from_raw(raw: Any, index: int, parent: str, c: Any = FAIL_FAST, stage: str = "tasks") -> Any:
    label = f"{parent}.resources[{index}]"
    if c.check(stage, label, _object, raw, label) is FAILED:
        return FAILED
    before = c.count()
    c.check(stage, label, reject_unknown, raw, RESOURCE_FIELDS, label, REQUIRED_RESOURCE_FIELDS)
    kind = c.check(stage, f"{label}.kind", _one_of, raw.get("kind", ""), RESOURCE_KINDS, f"{label}.kind")
    access = c.check(
        stage, f"{label}.access", _one_of, raw.get("access", ""), RESOURCE_ACCESS_MODES, f"{label}.access"
    )
    description = raw.get("description")
    if description is not None:
        description = c.check(
            stage, f"{label}.description", required_string, description, f"{label}.description"
        )
    resource_id = c.check(stage, f"{label}.id", identifier, raw.get("id"), f"{label}.id")
    target = c.check(stage, f"{label}.target", required_string, raw.get("target"), f"{label}.target")
    if _failed_since(c, before):
        return FAILED
    return ResourceClaim(id=resource_id, kind=kind, target=target, access=access, description=description)


def _one_of(value: Any, allowed: Iterable[str], name: str) -> str:
    text = str(value).strip()
    if text not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}")
    return text


def output_from_raw(raw: Any, index: int, parent: str, c: Any = FAIL_FAST, stage: str = "tasks") -> Any:
    label = f"{parent}.outputs[{index}]"
    if c.check(stage, label, _object, raw, label) is FAILED:
        return FAILED
    before = c.count()
    c.check(stage, label, reject_unknown, raw, OUTPUT_FIELDS, label, REQUIRED_OUTPUT_FIELDS)
    path = raw.get("path")
    if path is not None:
        path = c.check(stage, f"{label}.path", required_string, path, f"{label}.path")
    required = c.check(stage, f"{label}.required", _boolean, raw.get("required", True), f"{label}.required")
    output_id = c.check(stage, f"{label}.id", identifier, raw.get("id"), f"{label}.id")
    description = c.check(
        stage, f"{label}.description", required_string, raw.get("description"), f"{label}.description"
    )
    if _failed_since(c, before):
        return FAILED
    return TaskOutput(id=output_id, description=description, path=path, required=required)


def context_from_raw(raw: Any, parent: str, c: Any = FAIL_FAST, stage: str = "tasks") -> Any:
    label = f"{parent}.context"
    if raw is None:
        return TaskContext()
    if c.check(stage, label, _object, raw, label) is FAILED:
        return FAILED
    before = c.count()
    c.check(stage, label, reject_unknown, raw, CONTEXT_FIELDS, label)
    fields = {
        "memory_queries": c.check(
            stage, f"{label}.memory_queries", strings, raw.get("memory_queries", []), f"{label}.memory_queries"
        ),
        "memory_record_ids": c.check(
            stage, f"{label}.memory_record_ids", strings,
            raw.get("memory_record_ids", []), f"{label}.memory_record_ids",
        ),
        "dependency_outputs": c.check(
            stage, f"{label}.dependency_outputs", identifiers,
            raw.get("dependency_outputs", []), f"{label}.dependency_outputs",
        ),
        "max_memory_records": c.check(
            stage, f"{label}.max_memory_records", bounded_int,
            raw.get("max_memory_records", DEFAULT_MAX_MEMORY_RECORDS), f"{label}.max_memory_records", 0, 100,
        ),
        "max_dependency_outputs": c.check(
            stage, f"{label}.max_dependency_outputs", bounded_int,
            raw.get("max_dependency_outputs", DEFAULT_MAX_DEPENDENCY_OUTPUTS),
            f"{label}.max_dependency_outputs", 0, 100,
        ),
    }
    if _failed_since(c, before):
        return FAILED
    return TaskContext(**fields)


def _reasoning(raw: dict[str, Any], profile: str, label: str) -> str | None:
    if profile == "adaptive":
        if raw.get("reasoning") is None:
            raise ValueError(f"{label} requires reasoning in Adaptive profile")
        return normalize(str(raw["reasoning"]))
    if "reasoning" in raw:
        raise ValueError("Host Settings plans must omit task reasoning")
    return None


def reject_unknown(
    raw: dict[str, Any], allowed: Iterable[str], label: str, required: Iterable[str] = ()
) -> None:
    """Refuse unknown fields, naming the accepted ones (R31).

    It named only the rejected key: "task 3 has unknown fields: ['lead']",
    and nothing the model could read said which fields exist. The department
    reader already named them, the plan reader did not. Now every refusal
    carries the accepted set, and - when some are absent - the required
    fields it is missing, so a misspelling is fixed in one round.
    """

    accepted = tuple(allowed)
    unknown = sorted(set(raw) - set(accepted))
    if not unknown:
        return
    missing = [name for name in required if name not in raw]
    raise UnknownFieldsError(
        f"{label} has unknown fields: {unknown}; accepted fields are {sorted(accepted)}"
        + (f"; missing required fields: {missing}" if missing else ""),
        accepted,
    )


def required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def identifier(value: Any, name: str) -> str:
    result = required_string(value, name)
    if not _IDENTIFIER.fullmatch(result):
        raise ValueError(f"{name} must match {_IDENTIFIER.pattern}")
    return result


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def strings(value: Any, name: str) -> tuple[str, ...]:
    raw = _array(value, name)
    result = tuple(required_string(item, f"{name} item") for item in raw)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def nonempty_strings(value: Any, name: str) -> tuple[str, ...]:
    result = strings(value, name)
    if not result:
        raise ValueError(f"{name} must be a non-empty array")
    return result


def identifiers(value: Any, name: str) -> tuple[str, ...]:
    raw = _array(value, name)
    result = tuple(identifier(item, f"{name} item") for item in raw)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def positive_int(value: Any, name: str) -> int:
    return bounded_int(value, name, 1, 1_000_000)


def bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def validate_unique(values: Iterable[str], name: str) -> None:
    """Unique ids; the refusal names the duplicates, not just the fact."""

    items = tuple(values)
    duplicates = sorted({item for item in items if items.count(item) > 1})
    if duplicates:
        raise ValueError(f"{name}s must be unique; duplicated: {duplicates}")
