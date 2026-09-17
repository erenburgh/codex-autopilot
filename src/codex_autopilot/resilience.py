from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from .plan import (
    Plan,
    atomic_json,
    plan_to_dict,
    save_plan,
    topological_order,
    validate_persisted_plan,
    validate_plan_change,
)
from .resources import release_resources_in_state
from .run_state import RunState, StateStore, utc_now
from .task_state import (
    ACTIVE_TASK_STATES,
    TaskState,
    dependencies_eligible,
    validate_task_states,
)


PLAN_CHANGE_REQUEST_PREFIX = "PLAN_CHANGE_REQUEST:"
PLAN_CHANGE_RESULT_PREFIX = "AUTOPILOT_PLAN_CHANGE:"
PLAN_CHANGE_TRANSACTION_FILE = "plan-change-transaction.json"
PLAN_CHANGE_REQUEST_KINDS = frozenset(
    {"prerequisite", "dependency", "resource", "verification"}
)
PLAN_CHANGE_ACTIVE_STATUSES = frozenset(
    {"DRAINING", "REPLANNER_RESERVED", "REPLANNING"}
)
PLAN_CHANGE_STATUSES = PLAN_CHANGE_ACTIVE_STATUSES | frozenset(
    {"APPLIED", "REJECTED", "FAILED"}
)
AUTHORITATIVE_WORKER_STATES = frozenset({"active", "terminal", "absent", "unknown"})

_REQUEST_ID = re.compile(r"^PC[1-9][0-9]*$")
_TASK_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_EVIDENCE_ID = re.compile(r"^EVID-[0-9]{3,}$")


class PlanChangeProtocolError(ValueError):
    pass


class PlanChangeConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PlanChangeRequest:
    request_version: int
    kind: str
    target_task_id: str
    summary: str
    rationale: str
    change: dict[str, Any]
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_version": self.request_version,
            "kind": self.kind,
            "target_task_id": self.target_task_id,
            "summary": self.summary,
            "rationale": self.rationale,
            "change": dict(self.change),
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True, slots=True)
class PlanChangeResult:
    request_id: str
    base_graph_version: int
    plan: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RuntimeReconciliation:
    retried_task_ids: tuple[str, ...]
    retained_task_ids: tuple[str, ...]
    unresolved_task_ids: tuple[str, ...]
    released_lock_ids: tuple[str, ...]


def parse_plan_change_request(message: str) -> PlanChangeRequest | None:
    """Parse a final typed request, or return None when no request is present."""

    raw = _single_final_protocol_object(message, PLAN_CHANGE_REQUEST_PREFIX)
    if raw is None:
        return None
    expected = {
        "request_version",
        "kind",
        "target_task_id",
        "summary",
        "rationale",
        "change",
        "evidence_ids",
    }
    _exact_keys(raw, expected, "plan change request")
    if raw["request_version"] != 1:
        raise PlanChangeProtocolError("plan change request_version must be 1")
    kind = _text(raw["kind"], "plan change kind")
    if kind not in PLAN_CHANGE_REQUEST_KINDS:
        raise PlanChangeProtocolError(
            f"plan change kind must be one of {sorted(PLAN_CHANGE_REQUEST_KINDS)}"
        )
    task_id = _identifier(raw["target_task_id"], "target_task_id")
    change = raw["change"]
    if not isinstance(change, dict):
        raise PlanChangeProtocolError("plan change change must be an object")
    _validate_change_payload(kind, change)
    evidence_ids = raw["evidence_ids"]
    if not isinstance(evidence_ids, list) or not all(
        isinstance(item, str) and _EVIDENCE_ID.fullmatch(item) for item in evidence_ids
    ):
        raise PlanChangeProtocolError("evidence_ids must contain Project Memory evidence IDs")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise PlanChangeProtocolError("evidence_ids must not contain duplicates")
    return PlanChangeRequest(
        request_version=1,
        kind=kind,
        target_task_id=task_id,
        summary=_text(raw["summary"], "plan change summary", maximum=240),
        rationale=_text(raw["rationale"], "plan change rationale", maximum=4_000),
        change=dict(change),
        evidence_ids=tuple(evidence_ids),
    )


def parse_plan_change_result(message: str) -> PlanChangeResult:
    raw = _single_final_protocol_object(message, PLAN_CHANGE_RESULT_PREFIX)
    if raw is None:
        raise PlanChangeProtocolError(
            f"replanner must end with exactly one {PLAN_CHANGE_RESULT_PREFIX} object"
        )
    _exact_keys(raw, {"request_id", "base_graph_version", "plan"}, "plan change result")
    request_id = _text(raw["request_id"], "plan change request_id")
    if not _REQUEST_ID.fullmatch(request_id):
        raise PlanChangeProtocolError("plan change request_id must match PC<number>")
    base = raw["base_graph_version"]
    if isinstance(base, bool) or not isinstance(base, int) or base <= 0:
        raise PlanChangeProtocolError("base_graph_version must be a positive integer")
    plan = raw["plan"]
    if not isinstance(plan, dict):
        raise PlanChangeProtocolError("plan change result plan must be an object")
    return PlanChangeResult(request_id, base, dict(plan))


def validate_replanner_result(
    current: Plan,
    result: PlanChangeResult,
    *,
    request_id: str,
    profile: str,
    promotion_evidence_store: Any | None = None,
) -> Plan:
    if result.request_id != request_id:
        raise PlanChangeProtocolError("replanner returned a different plan change request_id")
    if result.base_graph_version != current.graph_version:
        raise PlanChangeConflictError(
            "replanner base graph version is stale: "
            f"expected {current.graph_version}, got {result.base_graph_version}"
        )
    return validate_plan_change(
        current,
        result.plan,
        profile,
        promotion_evidence_store=promotion_evidence_store,
    )


def reconcile_plan_change_state(
    current: Plan,
    candidate: Plan,
    state: RunState,
    *,
    request_id: str,
    requester_task_id: str,
    at: str | None = None,
) -> RunState:
    """Revalidate mutable state against a candidate graph without losing history.

    Verified task contracts are immutable. Existing tasks cannot be removed, and
    advanced non-requester work cannot be rewritten. Changed READY/WAITING work,
    the requester, and every affected descendant are reset through the ordinary
    dependency gate. Attempts, revisions, completed sessions, and unaffected
    retry deadlines are preserved.
    """

    if state.graph_version != current.graph_version:
        raise PlanChangeConflictError("run state graph version is not the replanner base")
    if candidate.graph_version != current.graph_version + 1:
        raise PlanChangeConflictError("candidate graph version is not the next version")
    if requester_task_id not in current.task_map or requester_task_id not in candidate.task_map:
        raise PlanChangeConflictError("plan change requester must remain in the graph")
    removed = set(current.task_map) - set(candidate.task_map)
    if removed:
        raise PlanChangeConflictError(
            f"plan changes cannot remove tasks with durable history: {sorted(removed)}"
        )
    unexpected_active = set(state.active_task_ids) - {requester_task_id}
    if unexpected_active:
        raise PlanChangeConflictError(
            f"plan change apply requires drained workers: {sorted(unexpected_active)}"
        )
    if state.resource_locks:
        raise PlanChangeConflictError("plan change apply requires reconciled resource locks")

    changed: set[str] = set()
    for task_id, before in current.task_map.items():
        after = candidate.task_map[task_id]
        raw_state = TaskState(state.task_states[task_id])
        if raw_state is TaskState.VERIFIED and before != after:
            raise PlanChangeConflictError(
                f"verified task {task_id} is immutable during plan evolution"
            )
        if before != after:
            if task_id != requester_task_id and raw_state not in {
                TaskState.WAITING,
                TaskState.READY,
                TaskState.BLOCKED,
            }:
                raise PlanChangeConflictError(
                    f"advanced task {task_id} cannot be rewritten by a plan change"
                )
            changed.add(task_id)

    affected = set(changed)
    affected.add(requester_task_id)
    dependents: dict[str, set[str]] = {task.id: set() for task in candidate.tasks}
    for task in candidate.tasks:
        for dependency in task.depends_on:
            dependents[dependency].add(task.id)
    frontier = list(affected)
    while frontier:
        parent = frontier.pop()
        for child in dependents[parent]:
            if child not in affected:
                affected.add(child)
                frontier.append(child)

    states: dict[str, str] = {}
    for task in candidate.tasks:
        if task.id not in current.task_map or task.id in affected:
            states[task.id] = TaskState.WAITING.value
        else:
            states[task.id] = state.task_states[task.id]

    for task_id in topological_order(candidate):
        if states[task_id] == TaskState.WAITING.value and dependencies_eligible(
            candidate, task_id, states
        ):
            states[task_id] = TaskState.READY.value
    state.task_states = validate_task_states(candidate, states)
    state.active_task_ids = []
    state.graph_version = candidate.graph_version
    state.execution_strategy = candidate.execution_strategy
    state.max_parallel_workers = candidate.max_parallel_workers
    state.computer_use_slots = candidate.computer_use_slots
    state.task_attempts = {
        task.id: int(state.task_attempts.get(task.id, 0)) for task in candidate.tasks
    }
    state.task_revisions = {
        task.id: int(state.task_revisions.get(task.id, 0)) for task in candidate.tasks
    }
    state.task_retry_at = {
        task_id: retry_at
        for task_id, retry_at in state.task_retry_at.items()
        if task_id in state.task_states
        and state.task_states[task_id] == TaskState.RETRY_WAIT.value
    }
    ready = [
        task.id for task in candidate.tasks if state.task_states[task.id] == TaskState.READY.value
    ]
    kept_ready = {
        task_id: sequence
        for task_id, sequence in state.task_ready_since.items()
        if task_id in ready and task_id not in affected
    }
    for task_id in ready:
        if task_id not in kept_ready:
            state.scheduler_sequence += 1
            kept_ready[task_id] = state.scheduler_sequence
    state.task_ready_since = kept_ready
    state.active_plan_change_id = None
    record = active_plan_change(state, request_id=request_id)
    record["status"] = "APPLIED"
    record["applied_graph_version"] = candidate.graph_version
    record["completed_at"] = at or utc_now()
    state.status = "READY" if ready else "WAITING"
    state.phase = "PLAN_CHANGE_APPLIED"
    append_resilience_event(
        state,
        "plan_change_applied",
        at=record["completed_at"],
        task_id=requester_task_id,
        plan_change_id=request_id,
        detail={
            "base_graph_version": current.graph_version,
            "graph_version": candidate.graph_version,
            "affected_task_ids": sorted(affected),
        },
    )
    return state


def commit_plan_change(
    state_dir: Path,
    *,
    profile: str,
    current: Plan,
    candidate: Plan,
    state: RunState,
    request_id: str,
    fault_hook: Callable[[str], None] | None = None,
) -> None:
    """Crash-safe two-file commit using a durable redo record.

    The caller serializes this function with the resource-coordinator lock. A
    crash at either file boundary leaves enough validated data to finish the
    exact same commit on Resume without inventing or repeating model work.
    """

    from .memory import ProjectMemory

    validated = validate_plan_change(
        current,
        plan_to_dict(candidate),
        profile,
        promotion_evidence_store=ProjectMemory(state_dir.resolve().parent),
    )
    if validated != candidate or state.graph_version != candidate.graph_version:
        raise PlanChangeConflictError("plan/state commit payload is inconsistent")
    base_state_payload = asdict(StateStore(state_dir).load())
    if int(base_state_payload["graph_version"]) != current.graph_version:
        raise PlanChangeConflictError("persisted run state is not the plan-change base")
    state_payload = asdict(state)
    transaction = {
        "schema_version": 1,
        "status": "PREPARED",
        "request_id": request_id,
        "base_graph_version": current.graph_version,
        "target_graph_version": candidate.graph_version,
        "base_plan_sha256": _digest(plan_to_dict(current)),
        "target_plan_sha256": _digest(plan_to_dict(candidate)),
        "base_state_sha256": _state_digest(base_state_payload),
        "target_state_sha256": _state_digest(state_payload),
        "target_plan": plan_to_dict(candidate),
        "target_state": state_payload,
        "updated_at": utc_now(),
    }
    path = state_dir / PLAN_CHANGE_TRANSACTION_FILE
    atomic_json(path, transaction)
    if fault_hook:
        fault_hook("prepared")
    save_plan(state_dir, candidate)
    transaction["status"] = "PLAN_WRITTEN"
    transaction["updated_at"] = utc_now()
    atomic_json(path, transaction)
    if fault_hook:
        fault_hook("plan_written")
    StateStore(state_dir).save(state)
    if fault_hook:
        fault_hook("state_written")
    transaction["status"] = "COMMITTED"
    transaction["updated_at"] = utc_now()
    atomic_json(path, transaction)


def recover_plan_change_transaction(state_dir: Path, profile: str) -> bool:
    """Complete one interrupted plan/state commit from its durable redo record."""

    path = state_dir / PLAN_CHANGE_TRANSACTION_FILE
    if not path.is_file():
        return False
    raw = json.loads(path.read_text(encoding="utf-8"))
    _exact_keys(
        raw,
        {
            "schema_version",
            "status",
            "request_id",
            "base_graph_version",
            "target_graph_version",
            "base_plan_sha256",
            "target_plan_sha256",
            "base_state_sha256",
            "target_state_sha256",
            "target_plan",
            "target_state",
            "updated_at",
        },
        "plan change transaction",
    )
    if raw["schema_version"] != 1 or raw["status"] not in {
        "PREPARED",
        "PLAN_WRITTEN",
        "COMMITTED",
    }:
        raise PlanChangeConflictError("unsupported plan change transaction")
    target_plan_raw = raw["target_plan"]
    target_state_raw = raw["target_state"]
    if not isinstance(target_plan_raw, dict) or not isinstance(target_state_raw, dict):
        raise PlanChangeConflictError("plan change transaction payload is malformed")
    if _digest(target_plan_raw) != raw["target_plan_sha256"]:
        raise PlanChangeConflictError("plan change transaction plan digest mismatch")
    if _state_digest(target_state_raw) != raw["target_state_sha256"]:
        raise PlanChangeConflictError("plan change transaction state digest mismatch")
    target = validate_persisted_plan(
        target_plan_raw,
        profile,
        state_dir=state_dir,
    )
    if target.graph_version != raw["target_graph_version"]:
        raise PlanChangeConflictError("plan change transaction target version mismatch")
    known = RunState.__dataclass_fields__
    target_state = RunState(
        **{key: value for key, value in target_state_raw.items() if key in known}
    )
    if target_state.graph_version != target.graph_version:
        raise PlanChangeConflictError("plan change transaction state version mismatch")
    validate_task_states(target, target_state.task_states)
    if raw["status"] == "COMMITTED":
        return False

    current_plan_raw = json.loads((state_dir / "plan.json").read_text(encoding="utf-8"))
    current_digest = _digest(current_plan_raw)
    if current_digest not in {raw["base_plan_sha256"], raw["target_plan_sha256"]}:
        raise PlanChangeConflictError("plan changed outside the interrupted transaction")
    current_state = StateStore(state_dir).load()
    if current_state.graph_version not in {
        raw["base_graph_version"],
        raw["target_graph_version"],
    }:
        raise PlanChangeConflictError("run state changed outside the interrupted transaction")
    current_state_digest = _state_digest(asdict(current_state))
    if current_state_digest not in {
        raw["base_state_sha256"],
        raw["target_state_sha256"],
    }:
        raise PlanChangeConflictError(
            "run state changed outside the interrupted transaction"
        )
    save_plan(state_dir, target)
    StateStore(state_dir).save(target_state)
    raw["status"] = "COMMITTED"
    raw["updated_at"] = utc_now()
    atomic_json(path, raw)
    return True


def reconcile_running_work(
    plan: Plan,
    state: RunState,
    authoritative_states: Mapping[str, str],
    *,
    now_epoch: int,
    retry_delay_seconds: int,
    at: str | None = None,
) -> RuntimeReconciliation:
    """Reconcile crash survivors without treating silence as completion.

    Mapping keys are resource ownership/reservation tokens. Missing or unknown
    observations retain the task and lock. Authoritative terminal/absence means
    the trusted completion was missed, so the attempt is retired to RETRY_WAIT;
    it never advances verification or dependencies.
    """

    for token, value in authoritative_states.items():
        if not isinstance(token, str) or not token or value not in AUTHORITATIVE_WORKER_STATES:
            raise ValueError("authoritative worker states contain an invalid entry")
    pending = {
        str(item.get("resource_ownership_token") or item.get("reservation_token") or ""): item
        for item in state.worker_sessions
        if item.get("status") in {
            "RESERVED",
            "CREATE_REQUESTED",
            "RELAYING",
            "CREATED",
            "PREPARING",
            "PREPARED",
            "SEND_RELAYING",
            "ACTIVE",
            "AMBIGUOUS",
        }
    }
    retried: list[str] = []
    retained: list[str] = []
    unresolved: list[str] = []
    released: list[str] = []
    timestamp = at or utc_now()
    lock_ids = {
        str((item.get("owner") or {}).get("ownership_token")): str(item.get("lock_id"))
        for item in state.resource_locks
        if isinstance(item.get("owner"), dict)
    }
    for token, session in pending.items():
        task_id = str(session["task_id"])
        observed = authoritative_states.get(token, "unknown")
        if observed == "active":
            retained.append(task_id)
            continue
        if observed == "unknown":
            unresolved.append(task_id)
            continue
        # The on-call engineer's session is deliberately created WITHOUT
        # moving the task into an active state: the engineer repairs the
        # incident, it does not execute the task. So its hung turn is
        # cleared on its own and does not touch the task's state - that has
        # a life of its own.
        #
        # There was no such distinction before, and reconciliation demanded
        # an active state of ANY pending session. On 16 Sep 2026 the run
        # stopped dead: the engineer's turn completed, the session stayed
        # hanging, M8 was READY - and every resume answered «pending session
        # for M8 is not in an active task state». Nothing could resume the
        # run.
        if str(session.get("kind") or "") == "pipeline_engineer":
            session["status"] = "RETRY_WAIT"
            session["failure_reason"] = (
                f"crash reconciliation observed pipeline engineer {observed}"
            )
            if release_resources_in_state(
                state,
                token,
                reason=f"authoritative crash reconciliation: {observed}",
                now=timestamp,
            ):
                released.append(lock_ids.get(token, token))
            continue
        raw_state = TaskState(state.task_states[task_id])
        # Already in RETRY_WAIT means reconciliation ran for this task
        # before: the next line sets exactly that state. Repeating it is not
        # a conflict but a no-op.
        if raw_state is not TaskState.RETRY_WAIT and raw_state not in ACTIVE_TASK_STATES:
            raise PlanChangeConflictError(
                f"pending session for {task_id} is not in an active task state"
            )
        state.task_states[task_id] = TaskState.RETRY_WAIT.value
        state.active_task_ids = [item for item in state.active_task_ids if item != task_id]
        session["status"] = "RETRY_WAIT"
        session["failure_reason"] = f"crash reconciliation observed worker {observed}"
        retry_at = max(
            int(state.task_retry_at.get(task_id, 0)),
            now_epoch + retry_delay_seconds,
        )
        state.task_retry_at[task_id] = retry_at
        if release_resources_in_state(
            state,
            token,
            reason=f"authoritative crash reconciliation: {observed}",
            now=timestamp,
        ):
            released.append(lock_ids.get(token, token))
        retried.append(task_id)
        append_resilience_event(
            state,
            "worker_crash_reconciled",
            at=timestamp,
            task_id=task_id,
            detail={"worker_state": observed, "retry_at": retry_at, "token": token},
        )
    validate_task_states(plan, state.task_states)
    return RuntimeReconciliation(
        tuple(retried), tuple(retained), tuple(unresolved), tuple(released)
    )


def register_plan_change_request(
    state: RunState,
    request: PlanChangeRequest,
    *,
    requester_task_id: str,
    requester_session_token: str,
    at: str | None = None,
) -> dict[str, Any]:
    if state.active_plan_change_id is not None:
        raise PlanChangeConflictError("another plan change is already active")
    if request.target_task_id != requester_task_id:
        raise PlanChangeProtocolError(
            "a worker may request a plan change only for its own task"
        )
    state.plan_change_sequence += 1
    request_id = f"PC{state.plan_change_sequence}"
    record: dict[str, Any] = {
        "id": request_id,
        "status": "DRAINING",
        "requester_task_id": requester_task_id,
        "requester_session_token": requester_session_token,
        "base_graph_version": state.graph_version,
        "request": request.to_dict(),
        "created_at": at or utc_now(),
    }
    state.plan_changes.append(record)
    state.active_plan_change_id = request_id
    append_resilience_event(
        state,
        "plan_change_requested",
        at=record["created_at"],
        task_id=requester_task_id,
        plan_change_id=request_id,
        detail=request.to_dict(),
    )
    return record


def active_plan_change(
    state: RunState,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    selected = request_id or state.active_plan_change_id
    matches = [item for item in state.plan_changes if item.get("id") == selected]
    if len(matches) != 1:
        raise PlanChangeConflictError("active plan change is missing or non-unique")
    return matches[0]


def append_resilience_event(
    state: RunState,
    event: str,
    *,
    at: str | None = None,
    task_id: str | None = None,
    plan_change_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    state.resilience_journal_sequence += 1
    state.resilience_journal.append(
        {
            "sequence": state.resilience_journal_sequence,
            "event": _text(event, "resilience event", maximum=128),
            "at": at or utc_now(),
            "task_id": task_id,
            "plan_change_id": plan_change_id,
            "detail": dict(detail or {}),
        }
    )


def _single_final_protocol_object(message: str, prefix: str) -> dict[str, Any] | None:
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    matching = [line for line in lines if line.startswith(prefix)]
    if not matching:
        return None
    if len(matching) != 1 or lines[-1] != matching[0]:
        raise PlanChangeProtocolError(
            f"{prefix} must occur exactly once as the final non-empty line"
        )
    payload = matching[0][len(prefix) :].strip()
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PlanChangeProtocolError(f"{prefix} payload must be valid JSON") from exc
    if not isinstance(value, dict):
        raise PlanChangeProtocolError(f"{prefix} payload must be an object")
    return value


def _validate_change_payload(kind: str, change: dict[str, Any]) -> None:
    if kind == "prerequisite":
        allowed = {"description", "suggested_task_id"}
        _keys_subset(change, allowed, "prerequisite change")
        _text(change.get("description"), "prerequisite description", maximum=4_000)
        if "suggested_task_id" in change:
            _identifier(change["suggested_task_id"], "suggested_task_id")
    elif kind == "dependency":
        _exact_keys(change, {"dependency_task_id"}, "dependency change")
        _identifier(change["dependency_task_id"], "dependency_task_id")
    elif kind == "resource":
        _exact_keys(change, {"resources"}, "resource change")
        resources = change["resources"]
        if not isinstance(resources, list) or not resources or not all(
            isinstance(item, dict) for item in resources
        ):
            raise PlanChangeProtocolError("resource change resources must be non-empty objects")
    else:
        _exact_keys(change, {"verification"}, "verification change")
        if not isinstance(change["verification"], dict):
            raise PlanChangeProtocolError("verification change must contain an object")


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _state_digest(payload: Mapping[str, Any]) -> str:
    stable = dict(payload)
    # StateStore.touch() changes this advisory timestamp during each atomic
    # replacement. It is not part of the logical state transaction.
    stable.pop("updated_at", None)
    return _digest(stable)


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    keys = set(value)
    if keys != expected:
        raise PlanChangeProtocolError(
            f"{label} fields must be exactly {sorted(expected)}; got {sorted(keys)}"
        )


def _keys_subset(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise PlanChangeProtocolError(f"{label} has unknown fields: {sorted(unknown)}")


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label, maximum=64)
    if not _TASK_ID.fullmatch(text):
        raise PlanChangeProtocolError(f"{label} is not a valid task identifier")
    return text


def _text(value: Any, label: str, *, maximum: int = 8_000) -> str:
    if not isinstance(value, str):
        raise PlanChangeProtocolError(f"{label} must be a string")
    text = " ".join(value.split())
    if not text or len(text) > maximum:
        raise PlanChangeProtocolError(
            f"{label} must contain between 1 and {maximum} characters"
        )
    return text
