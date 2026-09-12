from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config
from .plan import Plan
from .pipeline_engineer import PipelineIncidentStore, render_pipeline_status
from .run_state import RunState
from .task_state import TaskState, unmet_dependencies
from .thread_titles import task_phase_thread_title


_ACTIVE_SESSION_STATUSES = frozenset(
    {
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
)


def project_status_snapshot(cfg: Config, state: RunState, plan: Plan) -> dict[str, Any]:
    sessions = _active_sessions(state)
    pipeline = PipelineIncidentStore(cfg.state_dir).status_snapshot()
    incident_paused = set(pipeline["paused_task_ids"])
    running: list[dict[str, str]] = []
    verifying: list[dict[str, str]] = []
    ready: list[dict[str, str]] = []
    waiting: list[dict[str, str]] = []

    for task in plan.tasks:
        task_state = TaskState(state.task_states[task.id])
        session = sessions.get(task.id)
        item = {"id": task.id, "title": task.title, "state": task_state.value}
        if task_state in {TaskState.RUNNING, TaskState.REVISING}:
            item["active_title"] = _active_title(plan, task.id, task_state, session)
            running.append(item)
        elif task_state is TaskState.VERIFYING:
            item["active_title"] = _active_title(plan, task.id, task_state, session)
            verifying.append(item)
        elif task_state is TaskState.READY and task.id in incident_paused:
            incident = next(
                item
                for item in pipeline["incidents"]
                if task.id in item["affected_task_ids"]
                and item["phase"] not in {"RECOVERED", "RESOLVED"}
            )
            item["reason"] = (
                f"paused by {incident['incident_id']} "
                f"({incident['classification']} / {incident['phase']})"
            )
            waiting.append(item)
        elif task_state is TaskState.READY:
            ready.append(item)
        elif task_state is not TaskState.VERIFIED:
            item["reason"] = _waiting_reason(plan, state, task.id, task_state, cfg.root)
            waiting.append(item)

    verified = sum(
        value == TaskState.VERIFIED.value for value in state.task_states.values()
    )
    worker_limit = min(plan.max_parallel_workers, state.max_parallel_workers)
    if plan.legacy_serial or "serial" in {
        plan.execution_strategy,
        state.execution_strategy,
    }:
        worker_limit = 1
    worker_used = len(state.active_task_ids)
    computer_use_limit = min(plan.computer_use_slots, state.computer_use_slots)
    computer_use_used = _computer_use_used(state, sessions)

    if state.status == "DONE" or verified == len(plan.tasks):
        semantic = "Done"
    elif state.status == "PAUSED":
        semantic = "Paused"
    elif state.status == "BLOCKED":
        semantic = "Blocked"
    elif state.active_plan_change_id:
        semantic = "Replanning" if running else "Waiting"
    elif running:
        semantic = "Running"
    elif verifying:
        semantic = "Verifying"
    elif ready:
        semantic = "Ready"
    else:
        semantic = "Waiting"

    association = _association_status(cfg, sessions)
    return {
        "status": semantic,
        "progress": {"verified": verified, "total": len(plan.tasks)},
        "worker_slots": {
            "used": worker_used,
            "total": worker_limit,
            "available": max(0, worker_limit - worker_used),
        },
        "computer_use_slots": {
            "used": computer_use_used,
            "total": computer_use_limit,
            "available": max(0, computer_use_limit - computer_use_used),
        },
        "running": running,
        "verifying": verifying,
        "waiting": waiting,
        "ready": ready,
        "placement": {
            "canonical_cwd": str(cfg.root),
            "desktop_project_id": cfg.desktop.desktop_project_id,
            "app_server_project_id": cfg.desktop.project_id,
            "association": association,
        },
        "pause": {
            "requested": state.status == "PAUSED",
            "semantics": "drain",
            "phase": state.phase,
        },
        "plan_change": _plan_change_status(state),
        "rate_limit_until": state.rate_limit_until,
        "pipeline_engineer": pipeline,
    }


def render_project_status(
    cfg: Config,
    state: RunState,
    plan: Plan,
    *,
    dispatcher_running: bool,
) -> str:
    snapshot = project_status_snapshot(cfg, state, plan)
    progress = snapshot["progress"]
    workers = snapshot["worker_slots"]
    computer = snapshot["computer_use_slots"]
    lines = [
        f"Codex Autopilot — {snapshot['status']}",
        f"Verified progress: {progress['verified']}/{progress['total']}",
        f"Worker slots: {workers['used']}/{workers['total']} used, {workers['available']} available",
        f"Computer Use slots: {computer['used']}/{computer['total']} used, {computer['available']} available",
        (
            f"Pause: drain ({snapshot['pause']['phase']})"
            if snapshot["pause"]["requested"]
            else "Pause: not requested"
        ),
        (
            f"Plan change: {snapshot['plan_change']['id']} / {snapshot['plan_change']['status']}"
            if snapshot["plan_change"]
            else "Plan change: none"
        ),
        (
            f"Rate-limit barrier: epoch {snapshot['rate_limit_until']}"
            if snapshot["rate_limit_until"] is not None
            else "Rate-limit barrier: none"
        ),
        render_pipeline_status(snapshot["pipeline_engineer"]),
    ]
    for heading, key in (
        ("Running", "running"),
        ("Verifying", "verifying"),
        ("Waiting", "waiting"),
        ("Ready", "ready"),
    ):
        lines.append(f"{heading}:")
        items = snapshot[key]
        if not items:
            lines.append("- none")
            continue
        for item in items:
            suffix = ""
            if item.get("active_title"):
                suffix = f" — active title: {item['active_title']}"
            elif item.get("reason"):
                suffix = f" — {item['reason']}"
            lines.append(f"- {item['id']}: {item['title']}{suffix}")
    placement = snapshot["placement"]
    lines.extend(
        [
            f"Canonical cwd: {placement['canonical_cwd']}",
            f"Project association: {placement['association']}",
            (
                f"Runtime: model={state.selected_model_display or 'Host default'}, "
                f"reasoning={state.selected_reasoning or 'Host default'}, "
                f"execution_mode={state.execution_mode or plan.tasks[state.milestone_index].execution_mode}, "
                f"strategy={plan.model_strategy}, surface={cfg.runtime.worker_surface}, "
                f"dispatcher={'running' if dispatcher_running else 'not running'}, "
                f"phase={state.phase}, last_error={state.last_error or 'none'}"
            ),
        ]
    )
    return "\n".join(lines)


def _active_sessions(state: RunState) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    active = set(state.active_task_ids)
    for session in state.worker_sessions:
        task_id = str(session.get("task_id") or "")
        if task_id in active and session.get("status") in _ACTIVE_SESSION_STATUSES:
            result[task_id] = session
    return result


def _plan_change_status(state: RunState) -> dict[str, Any] | None:
    if state.active_plan_change_id is None:
        return None
    record = next(
        (
            item
            for item in state.plan_changes
            if item.get("id") == state.active_plan_change_id
        ),
        None,
    )
    if record is None:
        return {"id": state.active_plan_change_id, "status": "UNKNOWN"}
    return {
        "id": str(record["id"]),
        "status": str(record["status"]),
        "requester_task_id": str(record["requester_task_id"]),
        "summary": str((record.get("request") or {}).get("summary") or ""),
    }


def _active_title(
    plan: Plan,
    task_id: str,
    task_state: TaskState,
    session: dict[str, Any] | None,
) -> str:
    if session:
        descriptor = session.get("descriptor")
        if isinstance(descriptor, dict) and isinstance(descriptor.get("title"), str):
            return str(descriptor["title"])
    kind = "verifier" if task_state is TaskState.VERIFYING else "revision" if task_state is TaskState.REVISING else "implementation"
    task = plan.task_map[task_id]
    role_id = (
        task.verification.verifier_role or task.role
        if kind == "verifier"
        else task.role
    )
    revision_number = int((session or {}).get("revision_number") or 1)
    return task_phase_thread_title(
        task_id=task_id,
        task_title=task.title,
        kind=kind,
        role_name=plan.role_map[role_id].name,
        revision_number=revision_number,
    )


def _waiting_reason(
    plan: Plan,
    state: RunState,
    task_id: str,
    task_state: TaskState,
    project_root: Path,
) -> str:
    if task_state is TaskState.WAITING:
        dependencies = unmet_dependencies(plan, task_id, state.task_states)
        if dependencies:
            return f"waiting for verified dependencies: {', '.join(dependencies)}"
        # Раздел 33 спецификации требует называть причину ожидания:
        # "T18 · resource locked by T14". Без этого задача, у которой
        # зависимости выполнены, стоит без объяснения.
        return _resource_reason(plan, state, task_id, project_root) or (
            "waiting for scheduler eligibility"
        )
    if task_state is TaskState.RETRY_WAIT:
        retry_at = state.task_retry_at.get(task_id)
        return f"retry scheduled at epoch {retry_at}" if retry_at is not None else "retry is pending"
    if task_state is TaskState.IMPLEMENTED:
        return "implementation complete; verification not yet started"
    if task_state is TaskState.REVISION_REQUIRED:
        return "verification requires a revision"
    if task_state is TaskState.BLOCKED:
        return f"blocked: {state.last_error or 'no reason recorded'}"
    if task_state is TaskState.FAILED:
        return f"failed: {state.last_error or 'no reason recorded'}"
    if task_state is TaskState.CANCELLED:
        return "cancelled"
    return f"state={task_state.value}"


def _resource_reason(
    plan: Plan, state: RunState, task_id: str, project_root: Path
) -> str | None:
    """Почему задача стоит из-за ресурса, если стоит.

    Нечитаемая запись блокировки не выдаётся за отсутствие владельца:
    "не удалось прочитать" и "никто не держит" - разные вещи, и вторая
    успокаивает там, где успокаивать нечем.
    """

    task = plan.task_map.get(task_id)
    if task is None or not task.resources:
        return None
    from .resources import DurableResourceLock, claims_conflict, normalize_task_claims

    try:
        wanted = normalize_task_claims(task, project_root)
    except (ValueError, TypeError) as error:
        return f"resource claims are unreadable: {error}"
    unreadable = 0
    for raw in state.resource_locks:
        try:
            lock = DurableResourceLock.from_dict(raw)
        except (ValueError, KeyError, TypeError):
            unreadable += 1
            continue
        if lock.owner.task_id == task_id:
            continue
        if any(claims_conflict(left, right) for left in wanted for right in lock.claims):
            return f"resource locked by {lock.owner.task_id}"
    if unreadable:
        return f"resource lock state is unreadable ({unreadable} of {len(state.resource_locks)})"
    return None


def _computer_use_used(
    state: RunState,
    sessions: dict[str, dict[str, Any]],
) -> int:
    slots = {
        lock.get("computer_use_slot")
        for lock in state.resource_locks
        if lock.get("computer_use_slot") is not None
    }
    session_count = 0
    for session in sessions.values():
        descriptor = session.get("descriptor")
        if isinstance(descriptor, dict) and descriptor.get("execution_mode") == "computer_use":
            session_count += 1
    return max(len(slots), session_count)


def _association_status(
    cfg: Config,
    sessions: dict[str, dict[str, Any]],
) -> str:
    details = [
        str(session.get("project_association_verification"))
        for session in sessions.values()
        if session.get("project_association_verification")
    ]
    if details:
        return "; ".join(dict.fromkeys(details))
    if cfg.desktop.project_id:
        return f"App Server saved project {cfg.desktop.project_id} configured; no active metadata snapshot"
    if cfg.desktop.desktop_project_id:
        return (
            f"Desktop project {cfg.desktop.desktop_project_id} is Codex App-authoritative; "
            "App Server projectId is a separate namespace and is unavailable"
        )
    return "no saved-project association; task appears in Tasks/Recents"
