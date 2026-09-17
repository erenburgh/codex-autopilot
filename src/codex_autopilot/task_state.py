from __future__ import annotations

from enum import Enum
from typing import Mapping

from .plan import Plan, Task


class TaskState(str, Enum):
    """Durable task lifecycle states.

    WAITING and READY describe scheduler eligibility. IMPLEMENTED is the
    implementer's result and is deliberately distinct from VERIFIED.
    """

    WAITING = "WAITING"
    READY = "READY"
    RUNNING = "RUNNING"
    IMPLEMENTED = "IMPLEMENTED"
    VERIFYING = "VERIFYING"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    REVISING = "REVISING"
    RETRY_WAIT = "RETRY_WAIT"
    VERIFIED = "VERIFIED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_TASK_STATES = {
    TaskState.RUNNING,
    TaskState.VERIFYING,
    TaskState.REVISING,
}
TERMINAL_TASK_STATES = {
    TaskState.VERIFIED,
    TaskState.CANCELLED,
}

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.WAITING: frozenset({TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED}),
    TaskState.READY: frozenset({TaskState.RUNNING, TaskState.BLOCKED, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.IMPLEMENTED,
            TaskState.RETRY_WAIT,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    # VERIFIED здесь намеренно нет: IMPLEMENTED -> VERIFIED отклоняется
    # всегда (R29), и таблица, разрешавшая это ребро, говорила неправду.
    TaskState.IMPLEMENTED: frozenset(
        {TaskState.VERIFYING, TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.VERIFYING: frozenset(
        {
            TaskState.VERIFIED,
            TaskState.REVISION_REQUIRED,
            # Верифаер, чей ответ не читается, ничего не проверил. Работа
            # остаётся сделанной и по-прежнему ждёт приёмки, поэтому
            # задача возвращается в IMPLEMENTED, а не переделывается.
            TaskState.IMPLEMENTED,
            TaskState.RETRY_WAIT,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.REVISION_REQUIRED: frozenset(
        {TaskState.REVISING, TaskState.BLOCKED, TaskState.CANCELLED}
    ),
    TaskState.REVISING: frozenset(
        {
            TaskState.IMPLEMENTED,
            TaskState.RETRY_WAIT,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.RETRY_WAIT: frozenset({TaskState.READY, TaskState.BLOCKED, TaskState.CANCELLED}),
    TaskState.BLOCKED: frozenset({TaskState.WAITING, TaskState.READY, TaskState.CANCELLED}),
    TaskState.FAILED: frozenset({TaskState.RETRY_WAIT, TaskState.CANCELLED}),
    TaskState.VERIFIED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


class IllegalTaskTransition(ValueError):
    pass


def coerce_task_state(value: TaskState | str) -> TaskState:
    try:
        return value if isinstance(value, TaskState) else TaskState(value)
    except ValueError as exc:
        raise ValueError(f"unknown task state {value!r}") from exc


def initial_task_states(plan: Plan) -> dict[str, str]:
    """Create a complete state map without making a scheduling decision."""

    return {
        task.id: (TaskState.READY.value if not task.depends_on else TaskState.WAITING.value)
        for task in plan.tasks
    }


def dependency_state_satisfies(task: Task, state: TaskState | str) -> bool:
    """Require independent acceptance before any dependency can unlock."""

    resolved = coerce_task_state(state)
    return resolved is TaskState.VERIFIED


def unmet_dependencies(
    plan: Plan,
    task_id: str,
    states: Mapping[str, TaskState | str],
) -> tuple[str, ...]:
    task = _task(plan, task_id)
    task_map = plan.task_map
    missing: list[str] = []
    for dependency_id in task.depends_on:
        if dependency_id not in states:
            missing.append(dependency_id)
            continue
        if not dependency_state_satisfies(task_map[dependency_id], states[dependency_id]):
            missing.append(dependency_id)
    return tuple(missing)


def dependencies_eligible(
    plan: Plan,
    task_id: str,
    states: Mapping[str, TaskState | str],
) -> bool:
    return not unmet_dependencies(plan, task_id, states)


def validate_transition(
    plan: Plan,
    task_id: str,
    current: TaskState | str,
    target: TaskState | str,
    states: Mapping[str, TaskState | str],
) -> TaskState:
    """Validate one state edge plus its dependency and verification guards."""

    task = _task(plan, task_id)
    source = coerce_task_state(current)
    destination = coerce_task_state(target)
    if task_id not in states or coerce_task_state(states[task_id]) is not source:
        raise IllegalTaskTransition(
            f"task {task_id} current state does not match the durable state map"
        )
    # R29 называется раньше общей таблицы: отказ обязан сказать, чего не
    # хватает - независимой верификации, - а не только «ребра нет».
    if source is TaskState.IMPLEMENTED and destination is TaskState.VERIFIED:
        raise IllegalTaskTransition(
            f"task {task_id} requires independent verification; "
            "IMPLEMENTED cannot transition directly to VERIFIED"
        )
    if destination not in TASK_TRANSITIONS[source]:
        raise IllegalTaskTransition(
            f"illegal task transition for {task_id}: {source.value} -> {destination.value}"
        )
    if destination in {TaskState.READY, TaskState.RUNNING}:
        unmet = unmet_dependencies(plan, task_id, states)
        if unmet:
            raise IllegalTaskTransition(
                f"task {task_id} is not dependency-eligible; unmet={list(unmet)}"
            )
    if source is TaskState.RUNNING and destination is TaskState.IMPLEMENTED:
        # This explicit edge is the only successful implementer-completion edge.
        # It prevents a worker result from being conflated with verification.
        return destination
    return destination


def transition_task(
    plan: Plan,
    states: Mapping[str, TaskState | str],
    task_id: str,
    target: TaskState | str,
) -> dict[str, str]:
    if task_id not in states:
        raise ValueError(f"task state map has no entry for {task_id!r}")
    destination = validate_transition(plan, task_id, states[task_id], target, states)
    updated = {key: coerce_task_state(value).value for key, value in states.items()}
    updated[task_id] = destination.value
    validate_task_states(plan, updated)
    return updated


def validate_task_states(
    plan: Plan,
    states: Mapping[str, TaskState | str],
    *,
    require_complete: bool = True,
) -> dict[str, str]:
    task_ids = set(plan.task_map)
    state_ids = set(states)
    unknown = state_ids - task_ids
    missing = task_ids - state_ids
    if unknown:
        raise ValueError(f"task state map references unknown tasks: {sorted(unknown)}")
    if require_complete and missing:
        raise ValueError(f"task state map is missing tasks: {sorted(missing)}")
    normalized = {task_id: coerce_task_state(value).value for task_id, value in states.items()}
    for task_id, raw_state in normalized.items():
        state = TaskState(raw_state)
        if state not in {TaskState.WAITING, TaskState.BLOCKED, TaskState.CANCELLED}:
            unmet = unmet_dependencies(plan, task_id, normalized)
            if unmet:
                raise ValueError(
                    f"task {task_id} is {state.value} with unmet dependencies {list(unmet)}"
                )
    return normalized


def migrate_v08_task_states(plan: Plan, legacy: Mapping[str, object]) -> dict[str, str]:
    """Translate one v0.8 serial cursor into complete v0.9 task states."""

    states = {task.id: TaskState.WAITING.value for task in plan.tasks}
    tasks = plan.tasks
    raw_index = legacy.get("milestone_index", 0)
    index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else 0
    index = max(0, min(index, len(tasks) - 1))

    completed_ids: set[str] = set()
    for entry in legacy.get("worker_history") or []:
        if not isinstance(entry, Mapping):
            continue
        task_id = entry.get("milestone_id")
        if isinstance(task_id, str) and entry.get("status") in {"ROTATE", "DONE"}:
            completed_ids.add(task_id)
    completed_ids.update(task.id for task in tasks[:index])
    if legacy.get("status") == "DONE":
        completed_ids.update(task.id for task in tasks)
    for task_id in completed_ids:
        if task_id in states:
            states[task_id] = TaskState.VERIFIED.value

    if legacy.get("status") != "DONE":
        current_id = legacy.get("milestone_id")
        if not isinstance(current_id, str) or current_id not in states:
            current_id = tasks[index].id
        phase = str(legacy.get("phase") or "")
        status = str(legacy.get("status") or "")
        if phase == "WAITING_RATE_LIMIT":
            current_state = TaskState.RETRY_WAIT
        elif status == "BLOCKED":
            current_state = TaskState.BLOCKED
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
        else:
            current_state = TaskState.READY
        if states[current_id] != TaskState.VERIFIED.value:
            states[current_id] = current_state.value

    # A legacy plan is a chain, so roots and the first task after a verified
    # prefix are READY. All later work remains WAITING.
    for task in tasks:
        if states[task.id] == TaskState.WAITING.value and dependencies_eligible(plan, task.id, states):
            states[task.id] = TaskState.READY.value
            if plan.legacy_serial:
                break
    return validate_task_states(plan, states)


def _task(plan: Plan, task_id: str) -> Task:
    try:
        return plan.task_map[task_id]
    except KeyError as exc:
        raise ValueError(f"unknown task id {task_id!r}") from exc
