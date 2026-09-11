from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass
import hashlib
import html
import json
import os
from pathlib import Path
import re
import time
import uuid
from typing import Any, Callable

from .ai_studio import AIStudioRuntime
from .appserver import (
    AppServerClient,
    AppServerError,
    AppServerRpcError,
    PauseRequested,
    final_agent_message,
    is_rate_limit_error,
)
from .bootstrap import mark_roadmap, select_milestone
from .config import Config, DESKTOP_OWNED_SURFACE
from .hook_trust import require_trusted_stop_hook_for_config
from .language import is_russian
from .lifecycle_prompts import (
    _evidence_selectors,
    _replanner_prompt,
    _revision_prompt,
    _verification_contract,
    _verifier_prompt,
    _worker_prompt,
)
from .memory import ProjectMemory
from .models import MODEL_IDS, ModelRoutingError, logical_model
from .pipeline_engineer import (
    IncidentClass,
    IncidentSignal,
    PipelineIncidentStore,
    SideEffectOutcome,
)
from .preflight import installed_plugin_root
from .plan import Plan, Task, VerificationCheck, atomic_json, load_plan, plan_to_dict
from .resilience import (
    PLAN_CHANGE_RESULT_PREFIX,
    PlanChangeConflictError,
    PlanChangeProtocolError,
    RuntimeReconciliation,
    active_plan_change,
    append_resilience_event,
    commit_plan_change,
    parse_plan_change_request,
    parse_plan_change_result,
    reconcile_plan_change_state,
    reconcile_running_work,
    recover_plan_change_transaction,
    register_plan_change_request,
    validate_replanner_result,
)
from .resources import (
    DurableResourceLock,
    LockOwner,
    ResourceLockCoordinator,
    acquire_resources_in_state,
    build_scheduler_availability,
    release_resources_in_state,
)
from .run_state import RunState, StateStore, utc_now
from .scheduler import schedule
from .task_state import (
    TaskState,
    migrate_v08_task_states,
    transition_task,
    validate_task_states,
)
from .thread_titles import replanner_thread_title, task_phase_thread_title
from .verification import (
    DeterministicCheckResult,
    VerificationIssue,
    VerificationProtocolError,
    VerificationVerdict,
    VERIFICATION_PREFIX,
    deterministic_issues,
    parse_verifier_result,
    run_deterministic_checks,
    verifier_route,
)


DESCRIPTOR_SCHEMA_VERSION = 2
DESKTOP_SLOT_READY = "AUTOPILOT_SLOT_READY"
WORKSPACE_HANDOFF_OK = "AUTOPILOT_WORKSPACE_READY"
WORKSPACE_HANDOFF_PROMPT = (
    "Codex Autopilot workspace handoff. Do not inspect or modify files and do not call tools. "
    f"Reply exactly: {WORKSPACE_HANDOFF_OK}"
)
PENDING_SESSION_STATUSES = frozenset(
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
RELAYABLE_SESSION_STATUSES = frozenset(
    {"CREATE_REQUESTED", "CREATED", "PREPARING", "PREPARED"}
)
SUCCESS_STATUSES = frozenset({"ROTATE", "DONE"})
ALLOWED_STATUSES = frozenset({"ROTATE", "DONE", "BLOCKED", "ESCALATE"})
IMPLEMENTATION_SESSION_KINDS = frozenset({"worker", "implementation"})
SESSION_KINDS = IMPLEMENTATION_SESSION_KINDS | frozenset(
    {"verifier", "revision", "replanner"}
)




class DesktopLifecycleError(RuntimeError):
    pass

class DesktopSlotHistoryError(DesktopLifecycleError):
    """The Desktop task is not the pristine no-op slot claimed by the relay."""

@dataclass(frozen=True, slots=True)
class LaunchDescriptor:
    schema_version: int
    surface: str
    run_id: str
    graph_version: int
    task_id: str
    task_title: str
    kind: str
    attempt: int
    worker_sequence: int
    reservation_token: str
    operation_id: str
    client_user_message_id: str
    desktop_project_id: str
    cwd: str
    title: str
    prompt: str
    model: str | None
    thinking: str | None
    execution_mode: str
    created_at: str
    prep_app_server_exited_at: str
    descriptor_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LaunchDescriptor":
        return cls(**{name: raw[name] for name in cls.__dataclass_fields__})

    def create_thread_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": self.slot_prompt(),
            "title": self.title,
            "target": {
                "type": "project",
                "projectId": self.desktop_project_id,
                "environment": {"type": "local"},
            },
        }
        if self.model:
            payload["model"] = self.model
        if self.thinking:
            payload["thinking"] = self.thinking
        return payload

    def slot_prompt(self) -> str:
        return (
            "Codex Autopilot Desktop slot reservation. During this no-op turn, do not "
            "inspect or modify files and do not call tools. Reply exactly: "
            f"{DESKTOP_SLOT_READY}. After that exact reply, a trusted Codex Autopilot "
            "Stop hook may record the result and durable causal provenance. The user's "
            "authorization for the complete Autopilot run covers the fixed scheduler-selected "
            "task chain. Pipeline Engineer may repair a failed transport and re-arm the causal "
            "predecessor, but never creates, starts, forks, or messages the next task itself."
        )

    def send_message_payload(self, *, thread_id: str, host_id: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "threadId": thread_id,
            "prompt": self.prompt,
        }
        if host_id:
            payload["hostId"] = host_id
        if self.model:
            payload["model"] = self.model
        if self.thinking:
            payload["thinking"] = self.thinking
        return payload

@dataclass(frozen=True, slots=True)
class CompletionOutcome:
    matched: bool
    worker_status: str | None
    descriptors: tuple[LaunchDescriptor, ...]
    run_done: bool

def parse_desktop_worker_status(message: str) -> str:
    pattern = re.compile(
        r"(?m)^AUTOPILOT_STATUS:\s*(ROTATE|DONE|BLOCKED|ESCALATE)\s*$"
    )
    matches = pattern.findall(message)
    last = next((line.strip() for line in reversed(message.splitlines()) if line.strip()), "")
    if len(matches) != 1 or last != f"AUTOPILOT_STATUS: {matches[0]}":
        raise DesktopLifecycleError(
            "Desktop worker final response must end with exactly one allowed "
            "AUTOPILOT_STATUS line"
        )
    return matches[0]

def confirm_prep_app_server_exit(cfg: Config, *, at: str | None = None) -> None:
    """Persist the one-way boundary after a bounded preflight client exits."""

    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        if _pid_alive(state.dispatcher_pid):
            raise DesktopLifecycleError(
                "the external App Server dispatcher is still active; Desktop launch is forbidden"
            )
        state.dispatcher_pid = None
        state.prep_app_server_exited_at = at or utc_now()
        _append_event(
            state,
            "prep_app_server_exited",
            _synthetic_session(state),
            state.prep_app_server_exited_at,
            detail="bounded metadata/preflight client fully exited",
        )
        store.save(state)

def pause_desktop_run(cfg: Config, *, at: str | None = None) -> None:
    """Persist a drain pause before returning control to the user.

    Existing Desktop turns continue and retain their locks. Their Stop hooks may
    record completion, but no new reservation is admitted while the pause marker
    exists. An explicit Interrupt remains authoritative and moves only that task
    to RETRY_WAIT.
    """

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    store.request_pause()
    coordinator = ResourceLockCoordinator(store, cfg.root)
    timestamp = at or utc_now()
    with coordinator.transaction():
        state = store.load()
        append_resilience_event(
            state,
            "pause_requested",
            at=timestamp,
            detail={"semantics": "drain", "active_task_ids": list(state.active_task_ids)},
        )
        if state.status != "DONE":
            state.status = "PAUSED"
            state.phase = "PAUSED_DRAINING" if state.active_task_ids else "PAUSED"
        store.save(state)

def relay_session_status(cfg: Config, reservation_token: str) -> dict[str, Any]:
    """Read the durable phase that determines the relay's next fixed action."""

    _require_desktop_owned(cfg)
    state = StateStore(cfg.state_dir).load()
    session = _session_by_token(state, reservation_token)
    status = str(session["status"])
    prep_failure = str(session.get("prep_failure_reason") or "")
    release_attempts = int(session.get("writer_release_attempts") or 0)
    created_action = "await_trusted_hook_cwd_prep"
    if "already has an active writer" in prep_failure:
        created_action = (
            "release_active_writer"
            if release_attempts == 0
            else "active_writer_release_exhausted"
        )
    action = {
        "CREATE_REQUESTED": "create_with_app_server",
        "CREATED": created_action,
        "PREPARING": "await_trusted_hook_cwd_prep",
        "PREPARED": "claim_production_send",
        "ACTIVE": "done",
        "COMPLETED": "done",
        "RELAYING": "reconcile_create_do_not_repeat",
        "SEND_RELAYING": "reconcile_send_do_not_repeat",
        "AMBIGUOUS": "reconcile_do_not_repeat",
    }.get(status, "stop")
    result = {
        "reservation_token": reservation_token,
        "task_id": session["task_id"],
        "status": status,
        "next_action": action,
        "thread_id": session.get("thread_id"),
        "actual_cwd": session.get("actual_cwd"),
        "prep_app_server_exited_at": session.get("prep_app_server_exited_at"),
        "writer_release_attempts": release_attempts,
        "relay_owner_thread_id": session.get("relay_owner_thread_id"),
    }
    if status == "ACTIVE":
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        result["visible_report"] = str(
            session.get("visible_launch_report")
            or relay_success_report(
                descriptor,
                thread_id=str(session.get("thread_id") or ""),
                status="ACTIVE",
                language=cfg.language,
            )
        )
    return result

def _dispatcher_owns_reservation(
    session: dict[str, Any],
    *,
    dispatcher_pid: int | None,
) -> bool:
    """Prove this process is the dispatcher already spawned for this reservation."""

    return bool(
        dispatcher_pid
        and dispatcher_pid == os.getpid()
        and session.get("automatic_dispatch_state") == "RUNNING"
        and session.get("automatic_dispatch_pid") == dispatcher_pid
    )

def _wait_for_dispatcher_ownership(
    cfg: Config,
    reservation_token: str,
    *,
    owner_thread_id: str,
    timeout: float = 5.0,
) -> bool:
    """Wait for the spawning parent to durably bind this child PID."""

    deadline = time.monotonic() + timeout
    while True:
        state = StateStore(cfg.state_dir).load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, owner_thread_id)
        if _dispatcher_owns_reservation(session, dispatcher_pid=os.getpid()):
            return True
        if (
            session.get("automatic_dispatch_state") != "SCHEDULED"
            or session.get("automatic_dispatch_pid") is not None
            or time.monotonic() >= deadline
        ):
            return False
        time.sleep(0.02)

def acknowledge_desktop_send(
    cfg: Config,
    reservation_token: str,
    *,
    thread_id: str,
    at: str | None = None,
    relay_executor_thread_id: str | None = None,
) -> LaunchDescriptor:
    """Acknowledge production turn/start and register Desktop Stop waiting."""

    _require_desktop_owned(cfg)
    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session.get("thread_id") != thread_id:
            raise DesktopLifecycleError("production send acknowledged an unexpected Desktop task")
        if session["status"] == "ACTIVE":
            return LaunchDescriptor.from_dict(dict(session["descriptor"]))
        if session["status"] != "SEND_RELAYING":
            raise DesktopLifecycleError("reservation is not awaiting production-send acknowledgement")
        session["status"] = "ACTIVE"
        session["start_acknowledged_at"] = timestamp
        for event in ("start_acknowledged", "wait_registered"):
            _append_event(state, event, session, timestamp)
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        session["visible_launch_report"] = relay_success_report(
            descriptor,
            thread_id=thread_id,
            status="ACTIVE",
            language=cfg.language,
        )
        _append_event(state, "visible_launch_report_ready", session, timestamp)
        if _session_kind(session) == "replanner":
            change = active_plan_change(
                state,
                request_id=str(session.get("plan_change_id") or ""),
            )
            change["status"] = "REPLANNING"
            append_resilience_event(
                state,
                "replanner_active",
                at=timestamp,
                task_id=str(session["task_id"]),
                plan_change_id=str(change["id"]),
                detail={"thread_id": thread_id},
            )
        state.current_thread_id = thread_id
        state.phase = "DESKTOP_WORKERS_ACTIVE"
        state.status = "RUNNING"
        store.save(state)
        return descriptor

def relay_success_report(
    descriptor: LaunchDescriptor,
    *,
    thread_id: str,
    status: str,
    language: str,
) -> str:
    """Render the durable user-visible result of a successful production relay."""

    current_thread = str(thread_id or "").strip()
    if not current_thread:
        raise DesktopLifecycleError("visible relay report requires a Desktop thread ID")
    if status != "ACTIVE":
        raise DesktopLifecycleError("visible relay report requires ACTIVE launch status")
    if is_russian(language):
        return "\n".join(
            (
                "Codex Autopilot: следующая задача запущена.",
                f"Следующая задача: {descriptor.task_id}",
                f"Название: {descriptor.title}",
                f"Thread ID: {current_thread}",
                f"Статус запуска: {status}",
            )
        )
    return "\n".join(
        (
            "Codex Autopilot: next task launched.",
            f"Next task: {descriptor.task_id}",
            f"Title: {descriptor.title}",
            f"Thread ID: {current_thread}",
            f"Launch status: {status}",
        )
    )

def _record_deterministic_evidence(
    memory: ProjectMemory,
    task_id: str,
    checks: tuple[VerificationCheck, ...],
    results: tuple[DeterministicCheckResult, ...],
    *,
    provider_thread_id: str,
) -> None:
    by_id = {item.id: item for item in checks}
    for result in results:
        check = by_id[result.check_id]
        outcome = "PASS" if result.passed else "REVISE"
        common = {
            "summary": (
                f"Deterministic verification check {result.check_id} {outcome}: "
                f"{result.description}"
            ),
            "created_by": "codex-autopilot deterministic verifier",
            "milestone_id": task_id,
            "role": result.check_id,
            "provider": "deterministic-runtime",
            "provider_thread_id": provider_thread_id,
        }
        if result.kind == "command":
            memory.record_evidence(
                kind="test",
                command=json.dumps(list(check.argv), ensure_ascii=False),
                result=json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True),
                exit_code=result.exit_code if result.exit_code is not None else -1,
                **common,
            )
        elif result.kind == "artifact" and result.passed and result.path:
            memory.record_evidence(kind="artifact", path=result.path, **common)
        elif result.kind == "artifact":
            memory.record_evidence(
                kind="environment_probe",
                environment_probe=json.dumps(
                    result.to_dict(), ensure_ascii=False, sort_keys=True
                ),
                **common,
            )
        elif result.kind == "evidence" and not result.passed:
            memory.record_evidence(
                kind="environment_probe",
                environment_probe=json.dumps(
                    result.to_dict(), ensure_ascii=False, sort_keys=True
                ),
                **common,
            )

def _record_deterministic_verification_results(
    memory: ProjectMemory,
    task_id: str,
    results: tuple[DeterministicCheckResult, ...],
    evidence: list[dict[str, Any]],
    *,
    provider_thread_id: str,
    provider_turn_id: str,
) -> tuple[str, ...]:
    """Persist each deterministic check as an evidence-linked audit outcome."""

    verification_ids: list[str] = []
    for result in results:
        evidence_ids = [
            str(item["id"])
            for item in evidence
            if item.get("role") == result.check_id
        ]
        if not evidence_ids:
            raise DesktopLifecycleError(
                f"deterministic check {result.check_id} has no evidence with its exact check ID role"
            )
        verification = memory.record_verification_result(
            task_id=task_id,
            check_id=result.check_id,
            policy="deterministic",
            verdict="PASS" if result.passed else "REVISE",
            summary=(
                f"Deterministic verification check {result.check_id} "
                f"{'PASS' if result.passed else 'REVISE'}: {result.description}"
            ),
            evidence_ids=evidence_ids,
            created_by="codex-autopilot deterministic verifier",
            provider="deterministic-runtime",
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            details=result.to_dict(),
        )
        verification_ids.append(str(verification["id"]))
    return tuple(verification_ids)

def _latest_completion_context(
    memory: ProjectMemory,
    state: RunState,
    task_id: str,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    completed = next(
        (
            item
            for item in reversed(state.worker_sessions)
            if item.get("task_id") == task_id
            and _session_kind(item) in IMPLEMENTATION_SESSION_KINDS | {"revision"}
            and item.get("status") == "COMPLETED"
        ),
        None,
    )
    if completed is None:
        raise DesktopLifecycleError(
            f"task {task_id} has no completed implementation for verification"
        )
    evidence_ids = {
        str(item) for item in completed.get("completion_evidence_ids", []) if item
    }
    records = tuple(
        item
        for item in memory.milestone_evidence(task_id, limit=100)
        if str(item.get("id")) in evidence_ids
    )
    checks = completed.get("deterministic_results", [])
    if not isinstance(checks, list) or not all(isinstance(item, dict) for item in checks):
        raise DesktopLifecycleError("persisted deterministic results are malformed")
    return records, tuple(dict(item) for item in checks)

def _latest_verification_issues(
    state: RunState,
    task_id: str,
) -> tuple[VerificationIssue, ...]:
    raw = next(
        (
            item.get("verification_issues")
            for item in reversed(state.worker_sessions)
            if item.get("task_id") == task_id and item.get("verification_issues")
        ),
        None,
    )
    if not isinstance(raw, list):
        return ()
    try:
        return tuple(VerificationIssue.from_dict(item) for item in raw)
    except VerificationProtocolError as exc:
        raise DesktopLifecycleError("persisted verification issues are malformed") from exc

def _latest_task_session(state: RunState, task_id: str) -> dict[str, Any]:
    session = next(
        (item for item in reversed(state.worker_sessions) if item.get("task_id") == task_id),
        None,
    )
    if session is None:
        raise DesktopLifecycleError(f"task {task_id} has no lifecycle session")
    return session

def _block_if_revision_limit_reached(
    plan: Plan,
    state: RunState,
    task_id: str,
    session: dict[str, Any],
    at: str,
) -> bool:
    task = plan.task_map[task_id]
    used = int(state.task_revisions.get(task_id, 0))
    maximum = task.verification.max_revision_attempts
    if used < maximum:
        return False
    if state.task_states[task_id] == TaskState.REVISION_REQUIRED.value:
        state.task_states = transition_task(
            plan, state.task_states, task_id, TaskState.BLOCKED
        )
    state.last_error = (
        f"{task_id} exhausted {maximum} verification revision attempt(s)"
    )
    _append_event(
        state,
        "revision_limit_reached",
        session,
        at,
        detail=json.dumps(
            {"revision_attempts": used, "maximum": maximum}, sort_keys=True
        ),
    )
    return True

def _session_kind(session: dict[str, Any]) -> str:
    kind = str(session.get("kind") or "worker")
    if kind not in SESSION_KINDS:
        raise DesktopLifecycleError(f"unknown Desktop session kind: {kind}")
    return kind

def _append_event(
    state: RunState,
    event: str,
    session: dict[str, Any],
    at: str,
    *,
    detail: str | None = None,
    turn_id: str | None = None,
) -> None:
    state.lifecycle_journal_sequence += 1
    state.lifecycle_journal.append(
        {
            "sequence": state.lifecycle_journal_sequence,
            "event": event,
            "operation_id": str(session["operation_id"]),
            "task_id": str(session["task_id"]),
            "attempt": int(session["attempt"]),
            "reservation_token": str(session["reservation_token"]),
            "thread_id": session.get("thread_id"),
            "relay_owner_thread_id": session.get("relay_owner_thread_id"),
            "turn_id": turn_id if turn_id is not None else session.get("turn_id"),
            "client_user_message_id": session.get("client_user_message_id"),
            "at": at,
            "detail": detail,
        }
    )

def _synthetic_session(state: RunState) -> dict[str, Any]:
    task_id = state.milestone_id or next(iter(state.task_states), "PREP")
    attempt = max(1, int(state.task_attempts.get(task_id, 0) or 1))
    token = _stable_id(state, f"prep:{state.lifecycle_journal_sequence + 1}")
    return {
        "operation_id": _stable_id(state, f"prep-operation:{token}"),
        "task_id": task_id,
        "attempt": attempt,
        "reservation_token": token,
        "thread_id": None,
        "turn_id": None,
        "client_user_message_id": None,
    }

def _session_by_token(state: RunState, token: str) -> dict[str, Any]:
    matches = [item for item in state.worker_sessions if item.get("reservation_token") == token]
    if len(matches) != 1:
        raise DesktopLifecycleError("unknown or non-unique reservation token")
    return matches[0]

def _require_relay_executor(
    session: dict[str, Any],
    relay_executor_thread_id: str | None,
) -> None:
    """Bind every relay mutation to the causal predecessor task.

    Direct in-process callers may omit the identity at the deterministic test
    boundary. Production CLI commands always supply ``CODEX_THREAD_ID`` and may
    mutate only the reservation owned by that exact Desktop task.
    """

    if relay_executor_thread_id is None:
        return
    owner = str(session.get("relay_owner_thread_id") or "")
    executor = str(relay_executor_thread_id or "")
    if not owner:
        raise DesktopLifecycleError(
            "relay reservation has no bound owner thread; reconcile fail-closed"
        )
    if not executor:
        raise DesktopLifecycleError(
            "relay mutation requires the current Codex thread identity"
        )
    if executor != owner:
        raise DesktopLifecycleError(
            "relay executor thread does not match the reservation owner"
        )

# M10-REV-006: статус для сессии, вытесненной заменой той же задачи.
RETIRED_SUPERSEDED = "RETIRED_SUPERSEDED"

# Статусы, после которых сессия уже не может ничего изменить.
_TERMINAL_SESSION_STATUSES = {"COMPLETED", "BLOCKED", RETIRED_SUPERSEDED}


def fence_superseded_sessions(
    state: RunState,
    task_id: str,
    *,
    at: str,
    reason: str,
) -> list[dict[str, Any]]:
    """Оградить прежние сессии задачи перед тем, как замена возьмёт ресурсы.

    Наблюдалось на самом аудите M10: исходная резервация оставалась
    в RETRY_WAIT, новая становилась ACTIVE, а прерванная Desktop-задача
    продолжала менять то же рабочее дерево - у исходников и тестов
    менялись mtime во время аудита.
    """
    fenced: list[dict[str, Any]] = []
    for session in state.worker_sessions:
        if session.get("task_id") != task_id:
            continue
        if session.get("status") in _TERMINAL_SESSION_STATUSES:
            continue
        # Ограждать нужно то, что реально может продолжить производство:
        # адресуемую Desktop-задачу. У сессии без привязанного треда
        # создание не состоялось, продолжать нечему, и она остаётся
        # доступной для штатного ремонта и повторного запуска DevOps.
        if not str(session.get("thread_id") or "").strip():
            continue
        session["status"] = RETIRED_SUPERSEDED
        session["retired_at"] = at
        session["retired_reason"] = reason
        session["automatic_dispatch_state"] = RETIRED_SUPERSEDED
        session["automatic_dispatch_pid"] = None
        session["automatic_dispatch_connection_pid"] = None
        fenced.append(session)
        _append_event(state, "session_retired_superseded", session, at, detail=reason)
    return fenced

def session_is_fenced(session: dict[str, Any]) -> bool:
    return session.get("status") == RETIRED_SUPERSEDED

def _active_session_by_thread(state: RunState, thread_id: str) -> dict[str, Any] | None:
    matches = [
        item
        for item in state.worker_sessions
        if item.get("thread_id") == thread_id and item.get("status") == "ACTIVE"
    ]
    if len(matches) > 1:
        raise DesktopLifecycleError("Desktop thread has multiple active reservations")
    if matches:
        return matches[0]
    # M10-REV-006: вытесненная сессия не молчит, а падает закрыто.
    # Её Desktop-задача остаётся адресуемой, и без этого она продолжала
    # бы производство рядом с активной попыткой той же задачи.
    superseded = [
        item
        for item in state.worker_sessions
        if item.get("thread_id") == thread_id and session_is_fenced(item)
    ]
    if superseded:
        retired = superseded[-1]
        raise DesktopLifecycleError(
            f"Desktop task {thread_id} was superseded by a replacement for "
            f"{retired.get('task_id')} and must not continue production: "
            f"{retired.get('retired_reason') or 'retired'}"
        )
    return None

def _latest_implementation_thread_id(state: RunState, task_id: str) -> str:
    candidates = [
        item
        for item in state.worker_sessions
        if item.get("task_id") == task_id
        and _session_kind(item) in IMPLEMENTATION_SESSION_KINDS | {"revision"}
        and item.get("status") == "COMPLETED"
        and item.get("final_status") in SUCCESS_STATUSES
        and item.get("thread_id")
    ]
    if not candidates:
        raise DesktopLifecycleError(
            f"task {task_id} has no completed implementation predecessor"
        )
    latest = max(candidates, key=lambda item: int(item.get("worker_sequence") or 0))
    return str(latest["thread_id"])

def _bind_resource_identity(
    state: RunState,
    ownership_token: str,
    *,
    thread_id: str,
    turn_id: str | None = None,
) -> None:
    updated: list[dict[str, Any]] = []
    for raw in state.resource_locks:
        lock = DurableResourceLock.from_dict(raw)
        if lock.owner.ownership_token == ownership_token:
            owner = LockOwner.create(
                run_id=lock.owner.run_id,
                task_id=lock.owner.task_id,
                attempt=lock.owner.attempt,
                worker_id=lock.owner.worker_id,
                thread_id=thread_id,
                turn_id=turn_id or lock.owner.turn_id,
                ownership_token=lock.owner.ownership_token,
            )
            lock = DurableResourceLock(
                lock_id=lock.lock_id,
                owner=owner,
                claims=lock.claims,
                acquired_at=lock.acquired_at,
                heartbeat_at=lock.heartbeat_at,
                computer_use_slot=lock.computer_use_slot,
            )
        updated.append(lock.to_dict())
    state.resource_locks = updated

def _finish_global_state(
    plan: Plan,
    state: RunState,
    descriptors: tuple[LaunchDescriptor, ...],
    *,
    paused: bool = False,
) -> None:
    if all(value == TaskState.VERIFIED.value for value in state.task_states.values()):
        state.status = "DONE"
        state.phase = "DONE"
        state.completed_at = utc_now()
        return
    if paused:
        state.status = "PAUSED"
        state.phase = "PAUSED_DRAINING" if state.active_task_ids else "PAUSED"
        return
    if state.active_plan_change_id is not None:
        if descriptors:
            state.status = "RUNNING"
            state.phase = "AWAITING_DESKTOP_CREATE"
        elif state.active_task_ids:
            replanning = any(
                item.get("kind") == "replanner"
                and item.get("task_id") in state.active_task_ids
                and item.get("status") in PENDING_SESSION_STATUSES
                for item in state.worker_sessions
            )
            state.status = "RUNNING"
            state.phase = "PLAN_CHANGE_REPLANNING" if replanning else "PLAN_CHANGE_DRAINING"
        elif state.rate_limit_until is not None:
            state.status = "WAITING"
            state.phase = "WAITING_RATE_LIMIT"
        else:
            state.status = "WAITING"
            state.phase = "PLAN_CHANGE_WAITING_LOCKS"
        return
    if descriptors:
        state.status = "RUNNING"
        state.phase = "AWAITING_DESKTOP_CREATE"
    elif state.active_task_ids:
        state.status = "RUNNING"
        state.phase = "DESKTOP_WORKERS_ACTIVE"
    elif state.rate_limit_until is not None or any(
        value == TaskState.RETRY_WAIT.value for value in state.task_states.values()
    ):
        state.status = "WAITING"
        state.phase = "WAITING_RATE_LIMIT"
    elif any(value == TaskState.BLOCKED.value for value in state.task_states.values()):
        state.status = "BLOCKED"
        state.phase = "BLOCKED"
    else:
        state.status = "WAITING"
        state.phase = "WAITING_DEPENDENCIES"

def _sync_legacy_cursor(plan: Plan, state: RunState) -> None:
    for index, task in enumerate(plan.tasks):
        if state.task_states[task.id] != TaskState.VERIFIED.value:
            state.milestone_index = index
            state.milestone_id = task.id
            return
    state.milestone_index = len(plan.tasks) - 1
    state.milestone_id = plan.tasks[-1].id

def _verified_prefix(plan: Plan, state: RunState) -> int:
    count = 0
    for task in plan.tasks:
        if state.task_states[task.id] != TaskState.VERIFIED.value:
            break
        count += 1
    return count

def _checkpoint(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()

def task_checkpoint_path(state_dir: Path, task_id: str) -> Path:
    """Задачный файл передачи работы (M10-REV-005).

    Раньше гейтом завершения был общий .codex-autopilot/HANDOFF.md:
    все параллельно зарезервированные задачи получали ОДИН хэш этого
    файла, и первый же воркер, который его записал, закрывал гейт всем
    остальным. Плюс параллельная запись в один файл теряла правки,
    хотя ресурсы задач не пересекались.

    Теперь у каждой задачи свой файл, и гейт проверяет именно его.
    HANDOFF.md остаётся общей запиской для человека и гейтом не является.
    """
    return state_dir / "handoff" / f"{_checkpoint_slug(task_id)}.md"

def _checkpoint_slug(task_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(task_id)).strip("-")
    if not slug:
        raise DesktopLifecycleError(f"task id has no usable checkpoint name: {task_id!r}")
    return slug

def task_checkpoint(state_dir: Path, task_id: str) -> str:
    return _checkpoint(task_checkpoint_path(state_dir, task_id))

def _thread_cwd(thread: dict[str, Any]) -> Path | None:
    raw = thread.get("cwd")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).expanduser().resolve()

def _contains_exact_text(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return _text_matches_expected(value, expected)
    if isinstance(value, dict):
        return any(_contains_exact_text(item, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_exact_text(item, expected) for item in value)
    return False

def _text_matches_expected(value: str, expected: str) -> bool:
    actual = value.strip()
    wanted = expected.strip()
    if actual == wanted:
        return True
    redacted = re.fullmatch(
        r"<redacted chars=(\d+) sha256=([0-9a-fA-F]{64})>", actual
    )
    if redacted:
        return int(redacted.group(1)) == len(wanted) and redacted.group(2).lower() == (
            hashlib.sha256(wanted.encode("utf-8")).hexdigest()
        )
    for encoded in re.findall(r"<input>(.*?)</input>", actual, flags=re.DOTALL):
        if html.unescape(encoded).strip() == wanted:
            return True
    return False

def _client_process_exited(client: Any) -> bool:
    marker = getattr(client, "process_exited", None)
    if callable(marker):
        return bool(marker())
    if marker is not None:
        return bool(marker)
    proc = getattr(client, "proc", None)
    return proc is not None and proc.poll() is not None

def _client_process_pid(client: Any) -> int | None:
    proc = getattr(client, "proc", None)
    pid = getattr(proc, "pid", None)
    return pid if isinstance(pid, int) and pid > 0 else None

def _process_id_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True

def _stable_text_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def _stable_id(state: RunState, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-autopilot:{state.run_id}:{value}"))

def _materialize(descriptors: tuple[LaunchDescriptor, ...]) -> None:
    for descriptor in descriptors:
        payload = descriptor.to_dict()
        payload["create_thread_payload"] = descriptor.create_thread_payload()
        atomic_json(Path(descriptor.descriptor_path), payload)

def _require_desktop_owned(cfg: Config) -> None:
    if cfg.runtime.worker_surface != DESKTOP_OWNED_SURFACE:
        raise DesktopLifecycleError(
            "Desktop lifecycle is disabled; this run is explicitly headless_app_server"
        )
    if not cfg.desktop.desktop_project_id:
        raise DesktopLifecycleError("desktop_owned requires desktop.desktop_project_id")

def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def reconcile_desktop_runtime(
    cfg: Config,
    *,
    authoritative_states: dict[str, str] | None = None,
    now_epoch: int | None = None,
    at: str | None = None,
) -> RuntimeReconciliation:
    """Reconcile local plan commits, worker attempts, and resource ownership.

    Unknown or omitted external worker states remain locked. Only an explicit
    terminal/absent observation retires an attempt, and it retries rather than
    advancing the graph because no trusted completion protocol was observed.
    """
    # поздний импорт: развязка обратной зависимости модулей
    from .lifecycle_reservations import _prepare_state

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    epoch = int(time.time()) if now_epoch is None else now_epoch
    with coordinator.transaction():
        recover_plan_change_transaction(cfg.state_dir, cfg.profile)
        plan = load_plan(cfg.state_dir, cfg.profile)
        state = store.load()
        if state.graph_version != plan.graph_version:
            raise DesktopLifecycleError("resume reconciliation found a graph version mismatch")
        for session in state.worker_sessions:
            if (
                session.get("status") == "RETRY_WAIT"
                and session.get("automatic_dispatch_state") == "RUNNING"
            ):
                dispatcher_pid = session.get("automatic_dispatch_pid")
                if _process_id_alive(dispatcher_pid):
                    raise DesktopLifecycleError(
                        "retry-wait task still has a live automatic dispatcher"
                    )
                session["automatic_dispatch_state"] = "RETRY_WAIT"
                session["automatic_dispatch_pid"] = None
                session["automatic_dispatch_connection_pid"] = None
                _append_event(
                    state,
                    "stale_automatic_dispatcher_reconciled",
                    session,
                    at or utc_now(),
                    detail="dead dispatcher identity cleared during resume",
                )
        result = reconcile_running_work(
            plan,
            state,
            authoritative_states or {},
            now_epoch=epoch,
            retry_delay_seconds=cfg.retry.initial_seconds,
            at=at,
        )
        _prepare_state(plan, state, now_epoch=epoch)
        _finish_global_state(
            plan,
            state,
            (),
            paused=store.pause_requested(),
        )
        store.save(state)
    return result


def pending_descriptors(cfg: Config) -> tuple[LaunchDescriptor, ...]:
    state = StateStore(cfg.state_dir).load()
    return tuple(
        LaunchDescriptor.from_dict(dict(item["descriptor"]))
        for item in state.worker_sessions
        if item.get("status") in PENDING_SESSION_STATUSES
        and isinstance(item.get("descriptor"), dict)
    )
