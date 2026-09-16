from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")


class GoalContractError(ValueError):
    """A Goal Contract is malformed or a task is not bound to its outcomes."""


@dataclass(frozen=True, slots=True)
class GoalOutcome:
    id: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "description": self.description}


@dataclass(frozen=True, slots=True)
class GoalDeliverable:
    id: str
    description: str
    path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            **({"path": self.path} if self.path else {}),
        }


@dataclass(frozen=True, slots=True)
class GoalConstraint:
    id: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "description": self.description}


@dataclass(frozen=True, slots=True)
class GlobalAcceptanceCriterion:
    id: str
    description: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "description": self.description}


@dataclass(frozen=True, slots=True)
class GoalContract:
    """Structured authority for what a run must produce and satisfy."""

    required_outcomes: tuple[GoalOutcome, ...]
    deliverables: tuple[GoalDeliverable, ...]
    constraints: tuple[GoalConstraint, ...]
    global_acceptance: tuple[GlobalAcceptanceCriterion, ...]

    @property
    def outcome_ids(self) -> frozenset[str]:
        return frozenset(item.id for item in self.required_outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_outcomes": [item.to_dict() for item in self.required_outcomes],
            "deliverables": [item.to_dict() for item in self.deliverables],
            "constraints": [item.to_dict() for item in self.constraints],
            "global_acceptance": [
                item.to_dict() for item in self.global_acceptance
            ],
        }


def validate_goal_contract(
    raw: Any,
    *,
    label: str = "goal_contract",
) -> GoalContract:
    """Load a Goal Contract from strict structured data.

    All four sections must be declared.  Outcomes, deliverables, and global
    acceptance are non-empty because an empty list would make a successful run
    vacuous.  Constraints may be empty, but the explicit array distinguishes
    "none" from an omitted part of the contract.
    """

    if not isinstance(raw, dict):
        raise GoalContractError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {"required_outcomes", "deliverables", "constraints", "global_acceptance"},
        label,
    )
    for field in (
        "required_outcomes",
        "deliverables",
        "constraints",
        "global_acceptance",
    ):
        if field not in raw:
            raise GoalContractError(f"{label}.{field} must be declared")

    outcomes = tuple(
        _outcome(item, index, label)
        for index, item in enumerate(
            _array(raw["required_outcomes"], f"{label}.required_outcomes"), 1
        )
    )
    deliverables = tuple(
        _deliverable(item, index, label)
        for index, item in enumerate(
            _array(raw["deliverables"], f"{label}.deliverables"), 1
        )
    )
    constraints = tuple(
        _constraint(item, index, label)
        for index, item in enumerate(
            _array(raw["constraints"], f"{label}.constraints"), 1
        )
    )
    acceptance = tuple(
        _acceptance(item, index, label)
        for index, item in enumerate(
            _array(raw["global_acceptance"], f"{label}.global_acceptance"), 1
        )
    )
    if not outcomes:
        raise GoalContractError(f"{label}.required_outcomes must be non-empty")
    if not deliverables:
        raise GoalContractError(f"{label}.deliverables must be non-empty")
    if not acceptance:
        raise GoalContractError(f"{label}.global_acceptance must be non-empty")
    _unique((item.id for item in outcomes), f"{label} required outcome id")
    _unique((item.id for item in deliverables), f"{label} deliverable id")
    _unique((item.id for item in constraints), f"{label} constraint id")
    _unique((item.id for item in acceptance), f"{label} global acceptance id")
    return GoalContract(
        required_outcomes=outcomes,
        deliverables=deliverables,
        constraints=constraints,
        global_acceptance=acceptance,
    )


def validate_outcome_bindings(
    contract: GoalContract,
    task_outcomes: Mapping[str, Iterable[str]],
) -> None:
    """Require every graph task to produce at least one declared outcome."""

    known = contract.outcome_ids
    for task_id, raw_ids in task_outcomes.items():
        outcome_ids = tuple(raw_ids)
        if not outcome_ids:
            raise GoalContractError(
                f"task {task_id} must declare at least one produces_outcomes entry"
            )
        _unique(outcome_ids, f"task {task_id} produced outcome id")
        unknown = sorted(set(outcome_ids) - known)
        if unknown:
            raise GoalContractError(
                f"task {task_id} produces unknown Goal Contract outcomes: "
                + ", ".join(unknown)
            )


def is_persisted_goal_contract_compatibility(
    data: Any,
    *,
    state_dir: Path | None,
    schema_version: int,
    state_payload: dict[str, Any] | None = None,
    plan_filename: str = "plan.json",
) -> bool:
    """Recognize a pre-v1 canonical plan from durable project state.

    Merely pointing at a directory that contains some run is not enough: the
    payload must be the exact persisted plan or the exact target of the
    crash-safe plan-change transaction.  Thus a new bootstrap remains subject
    to the Goal Contract even when ``--replace`` sees stale v0.9 state.
    """

    if (
        not isinstance(data, dict)
        or data.get("schema_version") != schema_version
        or data.get("goal_contract") is not None
        or state_dir is None
    ):
        return False
    resolved = state_dir.expanduser().resolve()
    if not _existing_run_state(resolved, state_payload=state_payload):
        return False

    try:
        persisted = json.loads(
            (resolved / plan_filename).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        persisted = None
    if persisted == data:
        return True

    try:
        transaction = json.loads(
            (resolved / "plan-change-transaction.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(transaction, dict) or transaction.get("schema_version") != 1:
        return False
    if transaction.get("status") not in {"PREPARED", "PLAN_WRITTEN", "COMMITTED"}:
        return False
    if transaction.get("target_plan") != data:
        return False
    expected = transaction.get("target_plan_sha256")
    base_expected = transaction.get("base_plan_sha256")
    if not isinstance(expected, str) or not isinstance(base_expected, str):
        return False
    if not isinstance(persisted, dict) or _json_digest(persisted) != base_expected:
        return False
    return _json_digest(data) == expected


def _json_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _existing_run_state(
    state_dir: Path,
    *,
    state_payload: dict[str, Any] | None = None,
) -> bool:
    if state_payload is None:
        try:
            state_payload = json.loads(
                (state_dir / "run-state.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return False
    if not isinstance(state_payload, dict):
        return False
    run_id = state_payload.get("run_id")
    return (
        state_payload.get("schema_version") in {3, 4, 5}
        and isinstance(run_id, str)
        and bool(run_id.strip())
    )


def _outcome(raw: Any, index: int, parent: str) -> GoalOutcome:
    label = f"{parent}.required_outcomes[{index}]"
    _entry(raw, label, {"id", "description"})
    return GoalOutcome(
        id=_identifier(raw.get("id"), f"{label}.id"),
        description=_required_string(raw.get("description"), f"{label}.description"),
    )


def _deliverable(raw: Any, index: int, parent: str) -> GoalDeliverable:
    label = f"{parent}.deliverables[{index}]"
    _entry(raw, label, {"id", "description", "path"})
    path = raw.get("path")
    if path is not None:
        path = _required_string(path, f"{label}.path")
    return GoalDeliverable(
        id=_identifier(raw.get("id"), f"{label}.id"),
        description=_required_string(raw.get("description"), f"{label}.description"),
        path=path,
    )


def _constraint(raw: Any, index: int, parent: str) -> GoalConstraint:
    label = f"{parent}.constraints[{index}]"
    _entry(raw, label, {"id", "description"})
    return GoalConstraint(
        id=_identifier(raw.get("id"), f"{label}.id"),
        description=_required_string(raw.get("description"), f"{label}.description"),
    )


def _acceptance(raw: Any, index: int, parent: str) -> GlobalAcceptanceCriterion:
    label = f"{parent}.global_acceptance[{index}]"
    _entry(raw, label, {"id", "description"})
    return GlobalAcceptanceCriterion(
        id=_identifier(raw.get("id"), f"{label}.id"),
        description=_required_string(raw.get("description"), f"{label}.description"),
    )


def _entry(raw: Any, label: str, allowed: set[str]) -> None:
    if not isinstance(raw, dict):
        raise GoalContractError(f"{label} must be an object")
    _reject_unknown(raw, allowed, label)


def _array(raw: Any, label: str) -> list[Any]:
    if not isinstance(raw, list):
        raise GoalContractError(f"{label} must be an array")
    return raw


def _identifier(raw: Any, label: str) -> str:
    value = _required_string(raw, label)
    if not _IDENTIFIER.fullmatch(value):
        raise GoalContractError(
            f"{label} must start with a letter and contain only letters, "
            "digits, dot, underscore, or hyphen (max 64 characters)"
        )
    return value


def _required_string(raw: Any, label: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise GoalContractError(f"{label} must be a non-empty string")
    return raw.strip()


def _reject_unknown(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise GoalContractError(f"{label} has unknown fields: {', '.join(unknown)}")


def _unique(values: Iterable[str], label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise GoalContractError(f"duplicate {label}: {value}")
        seen.add(value)
