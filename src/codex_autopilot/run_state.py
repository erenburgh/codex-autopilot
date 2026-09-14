from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
import uuid

from .plan import (
    DEFAULT_COMPUTER_USE_SLOTS,
    DEFAULT_EXECUTION_STRATEGY,
    DEFAULT_MAX_PARALLEL_WORKERS,
    EXECUTION_STRATEGIES,
)
from .task_state import ACTIVE_TASK_STATES, TaskState, coerce_task_state


TERMINAL_STATUSES = {"DONE", "BLOCKED"}
RUN_STATE_SCHEMA_VERSION = 5
LEGACY_RUN_STATE_SCHEMA_VERSION = 4
WORKER_SESSION_KINDS = {
    "worker",
    "implementation",
    "verifier",
    "revision",
    "replanner",
    "pipeline_engineer",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class RunState:
    schema_version: int = RUN_STATE_SCHEMA_VERSION
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "IDLE"
    phase: str = "IDLE"
    milestone_index: int = 0
    worker_sequence: int = 0
    attempt: int = 0
    graph_version: int = 1
    execution_strategy: str = DEFAULT_EXECUTION_STRATEGY
    max_parallel_workers: int = DEFAULT_MAX_PARALLEL_WORKERS
    computer_use_slots: int = DEFAULT_COMPUTER_USE_SLOTS
    task_states: dict[str, str] = field(default_factory=dict)
    task_attempts: dict[str, int] = field(default_factory=dict)
    task_revisions: dict[str, int] = field(default_factory=dict)
    # Сколько раз задача была перенанята: воркер сменён, а способ
    # достижения поднят на ступень. План и DoD при этом не меняются.
    task_rehires: dict[str, int] = field(default_factory=dict)
    # Ступень усилия, назначенная перенаймом поверх того, что записано
    # в плане. Пусто, пока перенайма не было.
    task_effort: dict[str, str] = field(default_factory=dict)
    active_task_ids: list[str] = field(default_factory=list)
    scheduler_sequence: int = 0
    task_ready_since: dict[str, int] = field(default_factory=dict)
    resource_journal_sequence: int = 0
    resource_locks: list[dict[str, object]] = field(default_factory=list)
    resource_lock_journal: list[dict[str, object]] = field(default_factory=list)
    lifecycle_journal_sequence: int = 0
    lifecycle_journal: list[dict[str, object]] = field(default_factory=list)
    worker_sessions: list[dict[str, object]] = field(default_factory=list)
    task_retry_at: dict[str, int] = field(default_factory=dict)
    rate_limit_until: int | None = None
    # Последний снимок лимитов от App Server. Нужен не для реакции на
    # упор, а для планирования ёмкости: сколько воркеров имеет смысл
    # держать параллельно прямо сейчас.
    rate_limits: dict[str, Any] | None = None
    plan_change_sequence: int = 0
    active_plan_change_id: str | None = None
    plan_changes: list[dict[str, object]] = field(default_factory=list)
    resilience_journal_sequence: int = 0
    resilience_journal: list[dict[str, object]] = field(default_factory=list)
    migrated_from_schema: int | None = None
    current_thread_id: str | None = None
    current_turn_id: str | None = None
    previous_thread_ids: list[str] = field(default_factory=list)
    milestone_id: str | None = None
    planned_execution_mode: str | None = None
    execution_mode: str | None = None
    selected_model_key: str | None = None
    selected_model_id: str | None = None
    selected_model_display: str | None = None
    selected_reasoning: str | None = None
    model_selection_reason: str | None = None
    reasoning_adjustment: str | None = None
    last_worker_status: str | None = None
    worker_history: list[dict[str, object]] = field(default_factory=list)
    client_user_message_id: str | None = None
    prompt_sha256: str | None = None
    checkpoint_before: dict[str, str] | None = None
    memory_audit_before: int | None = None
    prompt_chars: int | None = None
    prompt_approx_tokens: int | None = None
    relevant_memory_count: int | None = None
    preflight_completed_at: str | None = None
    prep_app_server_exited_at: str | None = None
    retry_count: int = 0
    retry_at: int | None = None
    reset_at: int | None = None
    last_error: str | None = None
    last_final_message: str | None = None
    permission_profile: str | None = None
    project_id: str | None = None
    desktop_project_id: str | None = None
    dispatcher_pid: int | None = None
    initiator_thread_id: str | None = None
    initiator_turn_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    completed_at: str | None = None

    def touch(self) -> None:
        self.updated_at = utc_now()


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "run-state.json"
        self.lock_path = state_dir / "dispatcher.lock"
        self.pause_path = state_dir / "pause-requested"
        self.launch_path = state_dir / "launch-request.json"
        self._lock_handle = None

    def acquire(self) -> None:
        handle = self.lock_path.open("a", encoding="utf-8")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("Another Codex Autopilot dispatcher already holds this project lock")
        self._lock_handle = handle

    def release(self) -> None:
        if self._lock_handle:
            fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def load(self) -> RunState:
        if not self.path.exists():
            return RunState()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        schema = data.get("schema_version")
        if schema == LEGACY_RUN_STATE_SCHEMA_VERSION:
            data = _migrate_v08_payload(data)
        elif schema != RUN_STATE_SCHEMA_VERSION:
            raise ValueError(
                "Unsupported run-state schema. Start with v0.8 first for v0.7 state, "
                "then load it with v0.9."
            )
        known = RunState.__dataclass_fields__
        state = RunState(**{key: value for key, value in data.items() if key in known})
        _validate_state(state)
        return state

    def save(self, state: RunState) -> None:
        _validate_state(state)
        state.touch()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=".run-state-", dir=self.state_dir)
        temp = Path(raw)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def request_pause(self) -> None:
        self.pause_path.write_text(utc_now() + "\n", encoding="utf-8")

    def pause_requested(self) -> bool:
        return self.pause_path.exists()

    def clear_pause(self) -> None:
        self.pause_path.unlink(missing_ok=True)

    def arm(self, payload: dict[str, object]) -> None:
        from .plan import atomic_json
        atomic_json(self.launch_path, payload)

    def claim_launch(self) -> dict[str, object] | None:
        if not self.launch_path.is_file():
            return None
        claimed = self.state_dir / f".launch-claimed-{os.getpid()}.json"
        try:
            os.replace(self.launch_path, claimed)
        except FileNotFoundError:
            return None
        try:
            return json.loads(claimed.read_text(encoding="utf-8"))
        finally:
            claimed.unlink(missing_ok=True)


def _migrate_v08_payload(data: dict[str, object]) -> dict[str, object]:
    """Upgrade schema 4 with graph state without changing serial semantics."""

    migrated = dict(data)
    task_states: dict[str, str] = {}
    for raw in data.get("worker_history") or []:
        if not isinstance(raw, dict):
            continue
        task_id = raw.get("milestone_id")
        if isinstance(task_id, str) and task_id and raw.get("status") in {"ROTATE", "DONE"}:
            task_states[task_id] = TaskState.VERIFIED.value

    current_id = data.get("milestone_id")
    phase = str(data.get("phase") or "")
    status = str(data.get("status") or "")
    active: list[str] = []
    current_state: TaskState | None = None
    if isinstance(current_id, str) and current_id:
        if status == "DONE":
            current_state = TaskState.VERIFIED
        elif status == "BLOCKED":
            current_state = TaskState.BLOCKED
        elif phase == "WAITING_RATE_LIMIT":
            current_state = TaskState.RETRY_WAIT
        elif phase in {
            "CREATING_THREAD",
            "CLAIMING_PROJECT_SLOT",
            "PREPARING_PROJECT_SLOT",
            "THREAD_CREATED",
            "VERIFYING_MEMORY_MCP",
            "STARTING_TURN",
            "RUNNING_TURN",
        }:
            current_state = TaskState.RUNNING
            active.append(current_id)
        else:
            current_state = TaskState.READY
        if task_states.get(current_id) != TaskState.VERIFIED.value:
            task_states[current_id] = current_state.value

    attempt = data.get("attempt")
    task_attempts = (
        {current_id: attempt}
        if isinstance(current_id, str)
        and current_id
        and isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and attempt >= 0
        else {}
    )
    migrated.update(
        {
            "schema_version": RUN_STATE_SCHEMA_VERSION,
            "graph_version": 1,
            "execution_strategy": "serial",
            "max_parallel_workers": 1,
            "computer_use_slots": 1,
            "task_states": task_states,
            "task_attempts": task_attempts,
            "task_revisions": {},
            "task_rehires": {},
            "task_effort": {},
            "active_task_ids": active,
            "scheduler_sequence": 1 if current_state is TaskState.READY else 0,
            "task_ready_since": (
                {current_id: 1}
                if isinstance(current_id, str)
                and current_id
                and current_state is TaskState.READY
                else {}
            ),
            "resource_journal_sequence": 0,
            "resource_locks": [],
            "resource_lock_journal": [],
            "lifecycle_journal_sequence": 0,
            "lifecycle_journal": [],
            "worker_sessions": [],
            "task_retry_at": {},
            "rate_limit_until": None,
            "plan_change_sequence": 0,
            "active_plan_change_id": None,
            "plan_changes": [],
            "resilience_journal_sequence": 0,
            "resilience_journal": [],
            "migrated_from_schema": LEGACY_RUN_STATE_SCHEMA_VERSION,
        }
    )
    return migrated


def _validate_state(state: RunState) -> None:
    # Local import avoids a module cycle: the coordinator uses RunState while
    # RunState delegates validation of its nested resource journal records.
    from .resources import validate_persisted_resource_state

    if state.schema_version != RUN_STATE_SCHEMA_VERSION:
        raise ValueError(f"run-state schema_version must be {RUN_STATE_SCHEMA_VERSION}")
    if isinstance(state.graph_version, bool) or not isinstance(state.graph_version, int) or state.graph_version <= 0:
        raise ValueError("run-state graph_version must be a positive integer")
    if state.execution_strategy not in EXECUTION_STRATEGIES:
        raise ValueError(
            f"run-state execution_strategy must be one of {sorted(EXECUTION_STRATEGIES)}"
        )
    for name, value in (
        ("max_parallel_workers", state.max_parallel_workers),
        ("computer_use_slots", state.computer_use_slots),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"run-state {name} must be a positive integer")
    if not isinstance(state.task_states, dict) or not all(
        isinstance(task_id, str) and task_id and isinstance(value, str)
        for task_id, value in state.task_states.items()
    ):
        raise ValueError("run-state task_states must map non-empty task ids to states")
    for value in state.task_states.values():
        coerce_task_state(value)
    if not isinstance(state.active_task_ids, list) or not all(
        isinstance(task_id, str) and task_id for task_id in state.active_task_ids
    ):
        raise ValueError("run-state active_task_ids must be an array of non-empty task ids")
    if len(set(state.active_task_ids)) != len(state.active_task_ids):
        raise ValueError("run-state active_task_ids must not contain duplicates")
    for task_id in state.active_task_ids:
        if task_id not in state.task_states:
            raise ValueError(f"active task {task_id!r} has no task_states entry")
        if coerce_task_state(state.task_states[task_id]) not in ACTIVE_TASK_STATES:
            raise ValueError(f"active task {task_id!r} is not in an active state")
    if state.execution_strategy == "serial" and len(state.active_task_ids) > 1:
        raise ValueError("serial run-state cannot contain more than one active task")
    if len(state.active_task_ids) > state.max_parallel_workers:
        raise ValueError("active tasks exceed run-state max_parallel_workers")
    if (
        isinstance(state.scheduler_sequence, bool)
        or not isinstance(state.scheduler_sequence, int)
        or state.scheduler_sequence < 0
    ):
        raise ValueError("run-state scheduler_sequence must be a non-negative integer")
    if not isinstance(state.task_ready_since, dict):
        raise ValueError("run-state task_ready_since must be an object")
    ready_sequences: list[int] = []
    for task_id, value in state.task_ready_since.items():
        if task_id not in state.task_states:
            raise ValueError(f"run-state task_ready_since references unknown task {task_id!r}")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"run-state task_ready_since.{task_id} must be a positive integer"
            )
        ready_sequences.append(value)
    if len(set(ready_sequences)) != len(ready_sequences):
        raise ValueError("run-state task_ready_since sequences must be unique")
    if ready_sequences and max(ready_sequences) > state.scheduler_sequence:
        raise ValueError("run-state scheduler_sequence is behind task_ready_since")
    locks = validate_persisted_resource_state(
        state.resource_locks,
        state.resource_lock_journal,
        state.resource_journal_sequence,
    )
    if state.task_states:
        for lock in locks:
            if lock.owner.task_id not in state.task_states:
                raise ValueError(
                    f"resource lock references unknown task {lock.owner.task_id!r}"
                )
            if lock.owner.run_id != state.run_id:
                raise ValueError("resource lock owner run id does not match run-state run id")
    for name, values in (
        ("task_attempts", state.task_attempts),
        ("task_revisions", state.task_revisions),
        ("task_rehires", state.task_rehires),
    ):
        if not isinstance(values, dict):
            raise ValueError(f"run-state {name} must be an object")
        for task_id, value in values.items():
            if not isinstance(task_id, str) or not task_id:
                raise ValueError(f"run-state {name} has an invalid task id")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"run-state {name}.{task_id} must be a non-negative integer")
            if state.task_states and task_id not in state.task_states:
                raise ValueError(f"run-state {name} references unknown task {task_id!r}")
    if state.migrated_from_schema is not None and (
        isinstance(state.migrated_from_schema, bool)
        or not isinstance(state.migrated_from_schema, int)
        or state.migrated_from_schema <= 0
    ):
        raise ValueError("run-state migrated_from_schema must be a positive integer or null")
    if (
        isinstance(state.lifecycle_journal_sequence, bool)
        or not isinstance(state.lifecycle_journal_sequence, int)
        or state.lifecycle_journal_sequence < 0
    ):
        raise ValueError("lifecycle_journal_sequence must be a non-negative integer")
    if not isinstance(state.lifecycle_journal, list) or not all(
        isinstance(item, dict) for item in state.lifecycle_journal
    ):
        raise ValueError("lifecycle_journal must be an array of objects")
    journal_sequences = [item.get("sequence") for item in state.lifecycle_journal]
    if journal_sequences != list(range(1, state.lifecycle_journal_sequence + 1)):
        raise ValueError("lifecycle_journal must be contiguous and end at its sequence")
    for item in state.lifecycle_journal:
        for key in ("event", "operation_id", "task_id", "reservation_token", "at"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"lifecycle journal entries require non-empty {key}")
        attempt = item.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
            raise ValueError("lifecycle journal attempts must be positive integers")
        for key in (
            "thread_id",
            "relay_owner_thread_id",
            "turn_id",
            "client_user_message_id",
        ):
            if item.get(key) is not None and not isinstance(item.get(key), str):
                raise ValueError(f"lifecycle journal {key} must be a string or null")
    if not isinstance(state.worker_sessions, list) or not all(
        isinstance(item, dict) for item in state.worker_sessions
    ):
        raise ValueError("worker_sessions must be an array of objects")
    tokens: list[str] = []
    operations: list[str] = []
    pending_tasks: list[str] = []
    for item in state.worker_sessions:
        for key in (
            "reservation_token",
            "operation_id",
            "client_user_message_id",
            "task_id",
            "kind",
            "status",
            "created_at",
        ):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"worker sessions require non-empty {key}")
        if state.task_states and item["task_id"] not in state.task_states:
            raise ValueError("worker session references an unknown task")
        attempt = item.get("attempt")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt <= 0:
            raise ValueError("worker session attempts must be positive integers")
        kind = str(item["kind"])
        if kind not in WORKER_SESSION_KINDS:
            raise ValueError(f"unknown worker session kind {kind!r}")
        if kind == "verifier":
            round_number = item.get("verification_round")
            if (
                isinstance(round_number, bool)
                or not isinstance(round_number, int)
                or round_number <= 0
            ):
                raise ValueError("verifier sessions require a positive verification_round")
        if kind == "revision":
            revision_number = item.get("revision_number")
            if (
                isinstance(revision_number, bool)
                or not isinstance(revision_number, int)
                or revision_number <= 0
            ):
                raise ValueError("revision sessions require a positive revision_number")
            issues = item.get("verification_issues")
            if not isinstance(issues, list) or not issues or not all(
                isinstance(issue, dict) for issue in issues
            ):
                raise ValueError("revision sessions require structured verification_issues")
        relay_owner = item.get("relay_owner_thread_id")
        if relay_owner is not None and (
            not isinstance(relay_owner, str) or not relay_owner
        ):
            raise ValueError(
                "worker session relay_owner_thread_id must be a non-empty string or null"
            )
        identity_history = item.get("thread_identity_history")
        if identity_history is not None and (
            not isinstance(identity_history, list)
            or not all(isinstance(value, str) and value for value in identity_history)
            or len(identity_history) != len(set(identity_history))
        ):
            raise ValueError(
                "worker session thread_identity_history must contain unique non-empty strings"
            )
        if isinstance(identity_history, list) and item.get("thread_id") in identity_history:
            raise ValueError(
                "worker session current thread_id must not appear in thread_identity_history"
            )
        tokens.append(str(item["reservation_token"]))
        operations.append(str(item["operation_id"]))
        if item["status"] in {
            "RESERVED",
            "CREATE_REQUESTED",
            "RELAYING",
            "CREATED",
            "PREPARING",
            "PREPARED",
            "SEND_RELAYING",
            "ACTIVE",
            "AMBIGUOUS",
        }:
            if not isinstance(relay_owner, str) or not relay_owner:
                raise ValueError(
                    "pending worker sessions require a bound relay_owner_thread_id"
                )
            # Дежурный инженер чинит пайплайн, а не задачу: он назван её
            # идентификатором только ради контекста - каталога, роли в
            # заголовке и базовой линии области. Считать его вторым
            # исполнителем значило бы запретить чинить ровно ту задачу,
            # на которой пайплайн и сломался.
            if str(item.get("kind") or "") != "pipeline_engineer":
                pending_tasks.append(str(item["task_id"]))
    if len(tokens) != len(set(tokens)) or len(operations) != len(set(operations)):
        raise ValueError("worker session tokens and operation ids must be unique")
    if len(pending_tasks) != len(set(pending_tasks)):
        raise ValueError("a task must not have more than one pending worker session")
    if not isinstance(state.task_retry_at, dict):
        raise ValueError("task_retry_at must be an object")
    for task_id, value in state.task_retry_at.items():
        if state.task_states and task_id not in state.task_states:
            raise ValueError("task_retry_at references an unknown task")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("task_retry_at values must be non-negative integers")
    if state.rate_limit_until is not None and (
        isinstance(state.rate_limit_until, bool)
        or not isinstance(state.rate_limit_until, int)
        or state.rate_limit_until < 0
    ):
        raise ValueError("rate_limit_until must be a non-negative integer or null")
    if (
        isinstance(state.plan_change_sequence, bool)
        or not isinstance(state.plan_change_sequence, int)
        or state.plan_change_sequence < 0
    ):
        raise ValueError("plan_change_sequence must be a non-negative integer")
    if not isinstance(state.plan_changes, list) or not all(
        isinstance(item, dict) for item in state.plan_changes
    ):
        raise ValueError("plan_changes must be an array of objects")
    plan_change_ids: list[str] = []
    active_plan_changes: list[str] = []
    for item in state.plan_changes:
        for key in (
            "id",
            "status",
            "requester_task_id",
            "requester_session_token",
            "created_at",
        ):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f"plan changes require non-empty {key}")
        request_id = str(item["id"])
        if not request_id.startswith("PC") or not request_id[2:].isdigit():
            raise ValueError("plan change ids must match PC<number>")
        status = str(item["status"])
        if status not in {
            "DRAINING",
            "REPLANNER_RESERVED",
            "REPLANNING",
            "APPLIED",
            "REJECTED",
            "FAILED",
        }:
            raise ValueError(f"unknown plan change status {status!r}")
        if state.task_states and item["requester_task_id"] not in state.task_states:
            raise ValueError("plan change requester references an unknown task")
        base = item.get("base_graph_version")
        if isinstance(base, bool) or not isinstance(base, int) or base <= 0:
            raise ValueError("plan changes require a positive base_graph_version")
        if not isinstance(item.get("request"), dict):
            raise ValueError("plan changes require a structured request")
        plan_change_ids.append(request_id)
        if status in {"DRAINING", "REPLANNER_RESERVED", "REPLANNING"}:
            active_plan_changes.append(request_id)
    if len(plan_change_ids) != len(set(plan_change_ids)):
        raise ValueError("plan change ids must be unique")
    if plan_change_ids and max(int(item[2:]) for item in plan_change_ids) > state.plan_change_sequence:
        raise ValueError("plan_change_sequence is behind the plan change journal")
    if state.active_plan_change_id is not None and (
        not isinstance(state.active_plan_change_id, str)
        or state.active_plan_change_id not in plan_change_ids
    ):
        raise ValueError("active_plan_change_id must reference a plan change")
    if active_plan_changes != (
        [state.active_plan_change_id] if state.active_plan_change_id is not None else []
    ):
        raise ValueError("active_plan_change_id must identify the only active plan change")
    if (
        isinstance(state.resilience_journal_sequence, bool)
        or not isinstance(state.resilience_journal_sequence, int)
        or state.resilience_journal_sequence < 0
    ):
        raise ValueError("resilience_journal_sequence must be a non-negative integer")
    if not isinstance(state.resilience_journal, list) or not all(
        isinstance(item, dict) for item in state.resilience_journal
    ):
        raise ValueError("resilience_journal must be an array of objects")
    resilience_sequences = [item.get("sequence") for item in state.resilience_journal]
    if resilience_sequences != list(range(1, state.resilience_journal_sequence + 1)):
        raise ValueError("resilience_journal must be contiguous and end at its sequence")
    for item in state.resilience_journal:
        if not isinstance(item.get("event"), str) or not item["event"]:
            raise ValueError("resilience journal entries require non-empty event")
        if not isinstance(item.get("at"), str) or not item["at"]:
            raise ValueError("resilience journal entries require non-empty at")
        if item.get("task_id") is not None and not isinstance(item.get("task_id"), str):
            raise ValueError("resilience journal task_id must be a string or null")
        if item.get("plan_change_id") is not None and not isinstance(
            item.get("plan_change_id"), str
        ):
            raise ValueError("resilience journal plan_change_id must be a string or null")
        if not isinstance(item.get("detail"), dict):
            raise ValueError("resilience journal detail must be an object")
