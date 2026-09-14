from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Mapping

from .plan import Plan, Task
from .run_state import RunState
from .usage import worker_budget
from .task_state import (
    ACTIVE_TASK_STATES,
    TaskState,
    coerce_task_state,
    dependencies_eligible,
    unmet_dependencies,
    validate_task_states,
)


COMPUTER_USE_CAPABILITY = "computer_use"


@dataclass(frozen=True, slots=True)
class SchedulerAvailability:
    """One deterministic snapshot supplied by the runtime/resource layers.

    Named capabilities omitted from ``capability_limits`` are unbounded. When
    ``available_capabilities`` is ``None``, all named task capabilities are
    available. ``resource_available`` is keyed by task ID and is the hand-off
    point for M3's resource coordinator; an omitted task is available.
    """

    available_capabilities: frozenset[str] | None = None
    capability_limits: Mapping[str, int] = field(default_factory=dict)
    resource_available: Mapping[str, bool] = field(default_factory=dict)
    resource_conflicts: Mapping[str, frozenset[str]] = field(default_factory=dict)
    blocked_reasons: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PriorityScore:
    task_id: str
    resource_available: bool
    explicit_priority: int
    critical_path_length: int
    transitive_fan_out: int
    ready_sequence: int
    declaration_index: int


@dataclass(frozen=True, slots=True)
class DeferredTask:
    task_id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SchedulerDecision:
    """A launch recommendation; it never starts a model or worker itself."""

    strategy: str
    worker_limit: int
    open_worker_slots: int
    ready_task_ids: tuple[str, ...]
    selected_task_ids: tuple[str, ...]
    priorities: tuple[PriorityScore, ...]
    deferred: tuple[DeferredTask, ...]

    def reasons_for(self, task_id: str) -> tuple[str, ...]:
        item = next((item for item in self.deferred if item.task_id == task_id), None)
        return item.reasons if item else ()


def compute_ready_task_ids(
    plan: Plan,
    states: Mapping[str, TaskState | str],
) -> tuple[str, ...]:
    """Return the dependency-eligible WAITING/READY set in declaration order.

    This is the pure readiness calculation. It deliberately delegates the
    IMPLEMENTED-versus-VERIFIED rule to ``task_state.dependencies_eligible``.
    """

    normalized = validate_task_states(plan, states)
    candidates: list[str] = []
    for task in plan.tasks:
        state = TaskState(normalized[task.id])
        if state not in {TaskState.WAITING, TaskState.READY}:
            continue
        if dependencies_eligible(plan, task.id, normalized):
            candidates.append(task.id)
    return tuple(candidates)


def reconcile_ready_tasks(plan: Plan, state: RunState) -> tuple[str, ...]:
    """Persist newly eligible READY states and deterministic logical age.

    The function mutates only the supplied in-memory ``RunState``. Its caller
    remains responsible for the existing atomic ``StateStore.save`` boundary.
    Logical sequences, rather than wall-clock timestamps, make age tie-breaking
    repeatable in tests and after process recovery.
    """

    _validate_scheduler_state(plan, state)
    normalized = validate_task_states(plan, state.task_states)
    eligible = compute_ready_task_ids(plan, normalized)
    for task_id in eligible:
        if normalized[task_id] == TaskState.WAITING.value:
            normalized[task_id] = TaskState.READY.value

    ready_ids = tuple(
        task.id for task in plan.tasks if normalized[task.id] == TaskState.READY.value
    )
    ready_set = set(ready_ids)
    ready_since = {
        task_id: sequence
        for task_id, sequence in state.task_ready_since.items()
        if task_id in ready_set
    }
    sequence = state.scheduler_sequence
    for task_id in ready_ids:
        if task_id not in ready_since:
            sequence += 1
            ready_since[task_id] = sequence

    state.task_states = normalized
    state.task_ready_since = ready_since
    state.scheduler_sequence = sequence
    return ready_ids


def schedule(
    plan: Plan,
    state: RunState,
    availability: SchedulerAvailability | None = None,
) -> SchedulerDecision:
    """Compute one bounded deterministic launch decision.

    Scheduling is a greedy pass over a fully stable priority order. A decision
    updates READY states and their age in memory but does not transition a task
    to RUNNING or perform any external action.
    """

    snapshot = availability or SchedulerAvailability()
    _validate_availability(plan, snapshot)
    _validate_scheduler_state(plan, state)
    reconcile_ready_tasks(plan, state)

    strategy = _effective_strategy(plan, state)
    # Заявленное число - потолок и решение пользователя. Адаптация может
    # только понижать его, и только когда лимит действительно рядом:
    # человеку с автосписанием урезать нечего, он платит по факту.
    declared = min(plan.max_parallel_workers, state.max_parallel_workers)
    budget = worker_budget(declared, getattr(state, "rate_limits", None))
    worker_limit = budget.workers
    if strategy == "serial":
        worker_limit = 1
        if len(state.active_task_ids) > 1:
            raise ValueError("serial scheduler cannot contain more than one active task")
    open_slots = max(0, worker_limit - len(state.active_task_ids))

    critical_paths, fan_out = _graph_relevance(plan)
    resource_available = {
        task.id: snapshot.resource_available.get(task.id, True) for task in plan.tasks
    }
    scores = tuple(
        PriorityScore(
            task_id=task.id,
            resource_available=resource_available[task.id],
            explicit_priority=task.priority,
            critical_path_length=critical_paths[task.id],
            transitive_fan_out=fan_out[task.id],
            ready_sequence=state.task_ready_since[task.id],
            declaration_index=index,
        )
        for index, task in enumerate(plan.tasks)
        if state.task_states[task.id] == TaskState.READY.value
    )
    ordered = tuple(sorted(scores, key=_priority_key))

    capability_limits = _effective_capability_limits(plan, state, snapshot)
    usage = _active_capability_usage(plan, state)
    selected: list[str] = []
    deferred: list[DeferredTask] = []
    for score in ordered:
        task = plan.task_map[score.task_id]
        reasons = _availability_reasons(
            task,
            score,
            snapshot,
            capability_limits,
            usage,
            plan,
        )
        reasons.extend(
            f"resource_conflict:{selected_task_id}"
            for selected_task_id in selected
            if selected_task_id in snapshot.resource_conflicts.get(task.id, frozenset())
        )
        if len(selected) >= open_slots:
            reasons.append("worker_capacity")
        if reasons:
            deferred.append(DeferredTask(task.id, tuple(reasons)))
            continue
        selected.append(task.id)
        usage.update(_task_capabilities(task, plan))

    return SchedulerDecision(
        strategy=strategy,
        worker_limit=worker_limit,
        open_worker_slots=open_slots,
        ready_task_ids=tuple(item.task_id for item in ordered),
        selected_task_ids=tuple(selected),
        priorities=ordered,
        deferred=tuple(deferred),
    )


# Readable alias for call sites that prefer an action-oriented name.
schedule_tasks = schedule


def _priority_key(score: PriorityScore) -> tuple[int, int, int, int, int, int, str]:
    # Resource-admissible tasks form the first tier. Within a tier: explicit
    # priority, remaining critical-path length, transitive fan-out, oldest READY
    # sequence, declaration order, and task ID. Every element is deterministic.
    return (
        0 if score.resource_available else 1,
        -score.explicit_priority,
        -score.critical_path_length,
        -score.transitive_fan_out,
        score.ready_sequence,
        score.declaration_index,
        score.task_id,
    )


def _graph_relevance(plan: Plan) -> tuple[dict[str, int], dict[str, int]]:
    dependents: dict[str, list[str]] = {task.id: [] for task in plan.tasks}
    for task in plan.tasks:
        for dependency in task.depends_on:
            dependents[dependency].append(task.id)

    critical_paths: dict[str, int] = {}
    descendants: dict[str, frozenset[str]] = {}

    def visit(task_id: str) -> None:
        if task_id in critical_paths:
            return
        for child in dependents[task_id]:
            visit(child)
        critical_paths[task_id] = 1 + max(
            (critical_paths[child] for child in dependents[task_id]),
            default=0,
        )
        reached: set[str] = set(dependents[task_id])
        for child in dependents[task_id]:
            reached.update(descendants[child])
        descendants[task_id] = frozenset(reached)

    for task in reversed(plan.tasks):
        visit(task.id)
    return critical_paths, {task_id: len(items) for task_id, items in descendants.items()}


def _effective_strategy(plan: Plan, state: RunState) -> str:
    # Plan and durable/runtime state may each tighten execution. The least
    # permissive value wins so a stale/migrated state can never enable parallel
    # work that the plan did not authorize.
    if plan.legacy_serial or "serial" in {plan.execution_strategy, state.execution_strategy}:
        return "serial"
    if "auto" in {plan.execution_strategy, state.execution_strategy}:
        return "auto"
    return "parallel"


def _effective_capability_limits(
    plan: Plan,
    state: RunState,
    snapshot: SchedulerAvailability,
) -> dict[str, int]:
    limits = dict(snapshot.capability_limits)
    computer_use_limit = min(plan.computer_use_slots, state.computer_use_slots)
    if COMPUTER_USE_CAPABILITY in limits:
        computer_use_limit = min(computer_use_limit, limits[COMPUTER_USE_CAPABILITY])
    limits[COMPUTER_USE_CAPABILITY] = computer_use_limit
    return limits


def _task_capabilities(task: Task, plan: Plan | None = None) -> tuple[str, ...]:
    capabilities = list(task.required_capabilities)
    needs_surface = task.execution_mode == "computer_use"
    if plan is not None and not needs_surface:
        # Две Астры одновременно недопустимы: они делят одну поверхность
        # Computer Use, перехватывают управление друг у друга и жгут
        # лимиты. При стратегии auto Астра выбирается ровно для
        # computer_use, и слот держал это сам. При astra-only на Астру
        # уходят ВСЕ задачи, включая code, - и слот их не удерживал.
        from .models import logical_model

        try:
            needs_surface = logical_model(plan.model_strategy, task.execution_mode) == "astra"
        except Exception:
            needs_surface = False
    if needs_surface and COMPUTER_USE_CAPABILITY not in capabilities:
        capabilities.append(COMPUTER_USE_CAPABILITY)
    return tuple(capabilities)


def _active_capability_usage(plan: Plan, state: RunState) -> Counter[str]:
    usage: Counter[str] = Counter()
    for task_id in state.active_task_ids:
        usage.update(_task_capabilities(plan.task_map[task_id], plan))
    return usage


def _availability_reasons(
    task: Task,
    score: PriorityScore,
    snapshot: SchedulerAvailability,
    limits: Mapping[str, int],
    usage: Mapping[str, int],
    plan: Plan | None = None,
) -> list[str]:
    reasons: list[str] = []
    reasons.extend(snapshot.blocked_reasons.get(task.id, ()))
    if not score.resource_available:
        reasons.append("resource_unavailable")
    named_capabilities = set(task.required_capabilities)
    if snapshot.available_capabilities is not None:
        available = set(snapshot.available_capabilities)
        reasons.extend(
            f"capability_unavailable:{capability}"
            for capability in sorted(named_capabilities - available)
        )
    for capability in sorted(_task_capabilities(task, plan)):
        limit = limits.get(capability)
        if limit is not None and usage.get(capability, 0) >= limit:
            reasons.append(f"capability_capacity:{capability}")
    return reasons


def _validate_scheduler_state(plan: Plan, state: RunState) -> None:
    if state.graph_version != plan.graph_version:
        raise ValueError(
            "scheduler graph version mismatch: "
            f"plan={plan.graph_version}, state={state.graph_version}"
        )
    normalized = validate_task_states(plan, state.task_states)
    active_from_states = {
        task_id
        for task_id, raw_state in normalized.items()
        if coerce_task_state(raw_state) in ACTIVE_TASK_STATES
    }
    if active_from_states != set(state.active_task_ids):
        raise ValueError(
            "scheduler active_task_ids must exactly match tasks in active states"
        )
    if any(task_id not in normalized for task_id in state.task_ready_since):
        raise ValueError("scheduler READY age references an unknown task")


def _validate_availability(plan: Plan, snapshot: SchedulerAvailability) -> None:
    if snapshot.available_capabilities is not None:
        if not all(isinstance(item, str) and item for item in snapshot.available_capabilities):
            raise ValueError("available capabilities must be non-empty strings")
    for capability, limit in snapshot.capability_limits.items():
        if not isinstance(capability, str) or not capability:
            raise ValueError("capability limit names must be non-empty strings")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"capability limit for {capability!r} must be a non-negative integer")
    unknown = set(snapshot.resource_available) - set(plan.task_map)
    if unknown:
        raise ValueError(f"resource availability references unknown tasks: {sorted(unknown)}")
    if not all(isinstance(value, bool) for value in snapshot.resource_available.values()):
        raise ValueError("resource availability values must be booleans")
    unknown_conflict_tasks = set(snapshot.resource_conflicts) - set(plan.task_map)
    if unknown_conflict_tasks:
        raise ValueError(
            f"resource conflicts reference unknown tasks: {sorted(unknown_conflict_tasks)}"
        )
    for task_id, conflicts in snapshot.resource_conflicts.items():
        if not isinstance(conflicts, frozenset):
            raise ValueError("resource conflict values must be frozensets")
        unknown = set(conflicts) - set(plan.task_map)
        if unknown:
            raise ValueError(
                f"resource conflicts for {task_id!r} reference unknown tasks: {sorted(unknown)}"
            )
        if task_id in conflicts:
            raise ValueError("a task cannot resource-conflict with itself")
        for other_id in conflicts:
            if task_id not in snapshot.resource_conflicts.get(other_id, frozenset()):
                raise ValueError("resource conflict snapshots must be symmetric")
    unknown_blocked = set(snapshot.blocked_reasons) - set(plan.task_map)
    if unknown_blocked:
        raise ValueError(
            f"blocked reasons reference unknown tasks: {sorted(unknown_blocked)}"
        )
    for reasons in snapshot.blocked_reasons.values():
        if not isinstance(reasons, tuple) or not all(
            isinstance(reason, str) and reason for reason in reasons
        ):
            raise ValueError("blocked reasons must be tuples of non-empty strings")
