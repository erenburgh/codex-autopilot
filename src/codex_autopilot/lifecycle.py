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
from .memory import ProjectMemory
from .models import MODEL_IDS, ModelRoutingError, logical_model
from .pipeline_engineer import (
    AuthorityKind,
    IncidentClass,
    IncidentSignal,
    PipelineIncidentStore,
    SideEffectOutcome,
    TransportClaim,
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


def resume_desktop_run(
    cfg: Config,
    *,
    relay_owner_thread_id: str,
    authoritative_states: dict[str, str] | None = None,
    now_epoch: int | None = None,
    hook_gate: Callable[[Config], Any] | None = None,
) -> tuple[LaunchDescriptor, ...]:
    """Reconcile first, clear the durable pause, then admit fresh work."""

    _require_desktop_owned(cfg)
    reconcile_desktop_runtime(
        cfg,
        authoritative_states=authoritative_states,
        now_epoch=now_epoch,
    )
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        if state.status == "DONE":
            store.clear_pause()
            return ()
        append_resilience_event(
            state,
            "resume_requested",
            detail={"active_task_ids": list(state.active_task_ids)},
        )
        state.status = "RUNNING" if state.active_task_ids else "READY"
        state.phase = "RESUMING"
        store.save(state)
    store.clear_pause()
    return reserve_ready_frontier(
        cfg,
        now_epoch=now_epoch,
        hook_gate=hook_gate,
        relay_owner_thread_id=relay_owner_thread_id,
    )


def reserve_ready_frontier(
    cfg: Config,
    *,
    now_epoch: int | None = None,
    hook_gate: Callable[[Config], Any] | None = None,
    relay_owner_thread_id: str | None = None,
    recovery_stop_turn_id: str | None = None,
) -> tuple[LaunchDescriptor, ...]:
    """Atomically reserve only the bounded READY frontier for Desktop creation."""

    _require_desktop_owned(cfg)
    relay_owner_thread_id = str(
        relay_owner_thread_id or os.environ.get("CODEX_THREAD_ID") or ""
    ).strip()
    if not relay_owner_thread_id:
        raise DesktopLifecycleError(
            "Desktop reservation requires a bound relay owner thread"
        )
    (hook_gate or require_trusted_stop_hook_for_config)(cfg)
    store = StateStore(cfg.state_dir)
    memory_audit_before = ProjectMemory(cfg.root).audit_highwater()
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        recover_plan_change_transaction(cfg.state_dir, cfg.profile)
        plan = load_plan(cfg.state_dir, cfg.profile)
        state = store.load()
        epoch = int(time.time()) if now_epoch is None else now_epoch
        if _legacy_retry_requires_bound_owner(
            state,
            plan,
            now_epoch=epoch,
        ):
            recovery = _legacy_retry_recovery_context(
                state,
                plan,
                now_epoch=epoch,
            )
            if recovery is None:
                raise DesktopLifecycleError(
                    "due legacy retry has no unique authoritative predecessor owner"
                )
            task_id, retry_session, required_owner = recovery
            if relay_owner_thread_id != required_owner:
                raise DesktopLifecycleError(
                    f"due retry {task_id} belongs to owner thread {required_owner}"
                )
            if recovery_stop_turn_id:
                retry_session["relay_owner_thread_id"] = required_owner
                _append_event(
                    state,
                    "retry_owner_recovered",
                    retry_session,
                    utc_now(),
                    detail=json.dumps(
                        {
                            "predecessor_thread_id": required_owner,
                            "stop_turn_id": recovery_stop_turn_id,
                        },
                        sort_keys=True,
                    ),
                )
        _prepare_state(plan, state, now_epoch=now_epoch)
        descriptors = _reserve_in_state(
            cfg,
            plan,
            state,
            memory_audit_before=memory_audit_before,
            relay_owner_thread_id=relay_owner_thread_id,
            now_epoch=now_epoch,
        )
        store.save(state)
    _materialize(descriptors)
    return descriptors


def recover_desktop_frontier_from_predecessor_stop(
    cfg: Config,
    *,
    predecessor_thread_id: str,
    stop_turn_id: str,
    now_epoch: int | None = None,
    hook_gate: Callable[[Config], Any] | None = None,
) -> tuple[LaunchDescriptor, ...]:
    """Recover a due legacy retry only from its causal predecessor Stop.

    A delegated message is not trusted user input, but its resulting Stop event
    still carries the authoritative Desktop thread identity.  This recovery path
    lets that exact completed predecessor resume an already-authorized run after
    a create-side failure or process interruption.  It never accepts a caller-
    supplied substitute for the Stop hook's thread id.
    """

    _require_desktop_owned(cfg)
    owner = str(predecessor_thread_id or "").strip()
    turn_id = str(stop_turn_id or "").strip()
    if not owner or not turn_id:
        return ()
    epoch = int(time.time()) if now_epoch is None else now_epoch
    store = StateStore(cfg.state_dir)
    plan = load_plan(cfg.state_dir, cfg.profile)
    recovery = _legacy_retry_recovery_context(
        store.load(),
        plan,
        now_epoch=epoch,
    )
    if (
        store.pause_requested()
        or recovery is None
        or recovery[2] != owner
    ):
        return ()
    return reserve_ready_frontier(
        cfg,
        now_epoch=epoch,
        hook_gate=hook_gate,
        relay_owner_thread_id=owner,
        recovery_stop_turn_id=turn_id,
    )


def pending_descriptors(cfg: Config) -> tuple[LaunchDescriptor, ...]:
    state = StateStore(cfg.state_dir).load()
    return tuple(
        LaunchDescriptor.from_dict(dict(item["descriptor"]))
        for item in state.worker_sessions
        if item.get("status") in PENDING_SESSION_STATUSES
        and isinstance(item.get("descriptor"), dict)
    )


def relayable_descriptors(
    cfg: Config,
    *,
    relay_owner_thread_id: str | None = None,
) -> tuple[LaunchDescriptor, ...]:
    """Return only pending phases whose next action cannot duplicate a side effect."""

    state = StateStore(cfg.state_dir).load()
    return tuple(
        LaunchDescriptor.from_dict(dict(item["descriptor"]))
        for item in state.worker_sessions
        if item.get("status") in RELAYABLE_SESSION_STATUSES
        and (
            relay_owner_thread_id is None
            or item.get("relay_owner_thread_id") == relay_owner_thread_id
        )
        and isinstance(item.get("descriptor"), dict)
    )


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


def app_server_creation_contract(
    cfg: Config,
    descriptor: LaunchDescriptor,
) -> dict[str, Any]:
    """Return the exact project-scoped v0.7-style create contract."""

    params: dict[str, Any] = {
        "cwd": str(cfg.root),
        "permissions": cfg.desktop.permission_profile,
        "ephemeral": False,
        "runtimeWorkspaceRoots": [str(cfg.root)],
        "threadSource": "agent_created_thread",
    }
    if cfg.desktop.project_id:
        params["projectId"] = cfg.desktop.project_id
    if descriptor.model:
        params["model"] = descriptor.model
    contract: dict[str, Any] = {
        "method": "thread/start",
        "params": params,
        "name": descriptor.title,
    }
    if cfg.desktop.project_id:
        contract["project_root_precondition"] = {
            "method": "project/update-if-missing",
            "params": {
                "projectId": cfg.desktop.project_id,
                "root": str(cfg.root),
            },
        }
    return contract


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


def create_desktop_thread_via_app_server(
    cfg: Config,
    reservation_token: str,
    *,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    at: str | None = None,
    now_epoch: int | None = None,
    relay_executor_thread_id: str | None = None,
    dispatcher_pid: int | None = None,
    connected_client: AppServerClient | None = None,
) -> dict[str, Any]:
    """Create a persistent canonical-cwd task through the local dispatcher.

    One bounded App Server connection owns ``thread/start``, ``turn/start``,
    and the authoritative completion wait for this task. The outer v0.7-style
    dispatcher closes that process before it advances to a successor. No model
    continuation or Codex App task API participates.
    """

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if not _dispatcher_owns_reservation(
            session,
            dispatcher_pid=dispatcher_pid,
        ):
            require_trusted_stop_hook_for_config(cfg)
        if session.get("status") != "CREATE_REQUESTED":
            raise DesktopLifecycleError(
                "App Server create was already claimed or the reservation is not launchable"
            )
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        session["status"] = "RELAYING"
        session["creation_transport"] = "app_server_thread_start"
        session["app_server_creation_contract"] = app_server_creation_contract(
            cfg, descriptor
        )
        _append_event(state, "app_server_create_claimed", session, utc_now())
        store.save(state)

    client: Any = None
    thread_id = ""
    create_invoked = False
    actual_cwd: Path | None = None
    actual_name: str | None = None
    actual_project_id: str | None = None
    log_path = cfg.state_dir / "logs" / f"app-server-create-{reservation_token}.jsonl"
    owns_client = connected_client is None
    client_context = (
        client_factory(cfg.desktop.binary, log_path)
        if owns_client
        else nullcontext(connected_client)
    )
    try:
        with client_context as connected:
            client = connected
            profiles = client.list_permission_profiles(cfg.root)
            allowed = {
                str(item.get("id"))
                for item in profiles
                if item.get("allowed") is not False and item.get("id")
            }
            if cfg.desktop.permission_profile not in allowed:
                raise DesktopLifecycleError(
                    "configured permission profile is unavailable to App Server create"
                )
            if cfg.desktop.project_id:
                project = client.ensure_project_root(
                    cfg.desktop.project_id,
                    cfg.root,
                )
                if str(project.get("id") or "") != cfg.desktop.project_id:
                    raise DesktopLifecycleError(
                        "configured App Server project could not be verified"
                    )
            create_invoked = True
            started = client.start_thread(
                cwd=cfg.root,
                permission_profile=cfg.desktop.permission_profile,
                # v0.7 invariant: create the task in the saved project, with a
                # cwd that is already one of that project's durable roots.
                project_id=cfg.desktop.project_id,
                model=descriptor.model,
                plugin_root=installed_plugin_root(cfg.skill_path),
                ephemeral=False,
                thread_source="agent_created_thread",
            )
            thread = started.get("thread") or {}
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                raise DesktopLifecycleError("App Server thread/start returned no thread id")
            active_profile = started.get("activePermissionProfile") or {}
            if active_profile and active_profile.get("id") != cfg.desktop.permission_profile:
                raise DesktopLifecycleError(
                    "App Server thread/start applied an unexpected permission profile"
                )
            client.name_thread(thread_id, descriptor.title)
            metadata = client.read_thread(thread_id)
            if str(metadata.get("id") or "") != thread_id:
                raise DesktopLifecycleError(
                    "App Server thread/read returned an unexpected created thread"
                )
            actual_cwd = _thread_cwd(metadata) or _thread_cwd(thread)
            if actual_cwd != cfg.root:
                raise DesktopLifecycleError(
                    "App Server-created task does not use the canonical project cwd"
                )
            actual_name = metadata.get("name")
            if actual_name != descriptor.title:
                raise DesktopLifecycleError(
                    "App Server did not preserve the deterministic task title"
                )
            raw_project_id = metadata.get("projectId")
            actual_project_id = (
                str(raw_project_id) if raw_project_id is not None else None
            )
            if cfg.desktop.project_id and actual_project_id != cfg.desktop.project_id:
                raise DesktopLifecycleError(
                    "App Server did not preserve the configured project association"
                )
        if owns_client and (client is None or not _client_process_exited(client)):
            raise DesktopLifecycleError(
                "App Server create process did not fully exit before Desktop handoff"
            )
    except Exception as exc:
        if thread_id:
            _record_created_app_server_ambiguity(
                cfg,
                reservation_token,
                thread_id=thread_id,
                reason=str(exc),
                at=at,
            )
        else:
            definitive = (not create_invoked) or (
                isinstance(exc, AppServerRpcError) and exc.method == "thread/start"
            )
            _record_app_server_create_failure(
                cfg,
                reservation_token,
                reason=str(exc),
                definitive=definitive,
                at=at,
                now_epoch=now_epoch,
            )
        raise DesktopLifecycleError(str(exc)) from exc

    timestamp = at or utc_now()
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session.get("status") != "RELAYING" or session.get("thread_id"):
            raise DesktopLifecycleError(
                "App Server create reservation changed before acknowledgement"
            )
        session["thread_id"] = thread_id
        session["status"] = "PREPARED"
        session["actual_cwd"] = str(actual_cwd)
        session["actual_thread_name"] = actual_name
        session["title_verification"] = "verified by App Server thread/read"
        session["actual_project_id"] = actual_project_id
        session["project_association_verification"] = (
            "verified project-scoped thread/start App Server projectId and canonical cwd by thread/read; "
            "Desktop rootPaths/sidebar placement require separate verification"
            if cfg.desktop.project_id
            else "App Server projectId not configured"
        )
        session["create_acknowledged_at"] = timestamp
        if owns_client:
            session["prep_app_server_exited_at"] = timestamp
            session["app_server_create_exited_at"] = timestamp
            state.prep_app_server_exited_at = timestamp
        else:
            session["automatic_dispatch_connection_pid"] = dispatcher_pid
            session["app_server_create_exited_at"] = None
        state.current_thread_id = thread_id
        state.phase = "AWAITING_DESKTOP_SEND"
        state.last_error = None
        _bind_resource_identity(state, reservation_token, thread_id=thread_id)
        lifecycle_events = [
            ("app_server_thread_created", thread_id),
            ("create_acknowledged", "thread/start"),
            ("prep_completed", str(actual_cwd)),
        ]
        if cfg.desktop.project_id:
            lifecycle_events.insert(
                2,
                ("app_server_project_scoped_create", str(actual_project_id)),
            )
        lifecycle_events.insert(
            2,
            (
                "app_server_create_process_exited"
                if owns_client
                else "app_server_dispatcher_connection_retained",
                "full process exit" if owns_client else f"pid={dispatcher_pid}",
            ),
        )
        for event, detail in lifecycle_events:
            _append_event(state, event, session, timestamp, detail=detail)
        store.save(state)
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
    _materialize((descriptor,))
    return {
        "reservation_token": reservation_token,
        "thread_id": thread_id,
        "status": "PREPARED",
        "cwd": str(actual_cwd),
        "app_server_project_id": actual_project_id,
        "app_server_process_exited_at": timestamp if owns_client else None,
    }


def run_automatic_app_server_turn(
    cfg: Config,
    reservation_token: str,
    *,
    initiator_thread_id: str,
    initiator_turn_id: str,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    now_epoch: int | None = None,
    connected_client: AppServerClient | None = None,
) -> CompletionOutcome:
    """Run one reserved worker without a model-mediated relay.

    The trusted Stop hook starts this function in a detached local process. It
    waits until the causal predecessor turn is durably complete, creates the
    persistent App Server task, starts the production turn itself, waits for the
    authoritative completion event, and returns the structured result to the
    same dispatcher loop. Codex App ``create_thread`` and
    ``send_message_to_thread`` are not part of this transport.
    """

    _require_desktop_owned(cfg)
    owner = str(initiator_thread_id or "").strip()
    owner_turn = str(initiator_turn_id or "").strip()
    if not owner or not owner_turn:
        raise DesktopLifecycleError(
            "automatic App Server relay requires the causal predecessor thread and turn"
        )
    initial = StateStore(cfg.state_dir).load()
    session = _session_by_token(initial, reservation_token)
    _require_relay_executor(session, owner)
    dispatcher_authorized = _wait_for_dispatcher_ownership(
        cfg,
        reservation_token,
        owner_thread_id=owner,
    )
    if not dispatcher_authorized:
        require_trusted_stop_hook_for_config(cfg)
    if session.get("status") not in {"CREATE_REQUESTED", "PREPARED"}:
        if session.get("status") == "COMPLETED":
            return CompletionOutcome(False, None, (), initial.status == "DONE")
        raise DesktopLifecycleError(
            f"automatic relay cannot start from {session.get('status')!r}"
        )

    wait_log = cfg.state_dir / "logs" / f"app-server-wait-{reservation_token}.jsonl"
    deadline = time.monotonic() + cfg.desktop.reconcile_timeout_seconds
    wait_context = (
        client_factory(cfg.desktop.binary, wait_log)
        if connected_client is None
        else nullcontext(connected_client)
    )
    with wait_context as wait_client:
        while True:
            predecessor = wait_client.read_thread(owner)
            turn = next(
                (
                    item
                    for item in predecessor.get("turns") or []
                    if item.get("id") == owner_turn
                ),
                None,
            )
            if turn and turn.get("status") == "completed":
                break
            if time.monotonic() >= deadline:
                raise DesktopLifecycleError(
                    "causal predecessor did not reach durable completed state"
                )
            time.sleep(0.25)

    session = _session_by_token(StateStore(cfg.state_dir).load(), reservation_token)
    if session.get("status") == "CREATE_REQUESTED":
        create_desktop_thread_via_app_server(
            cfg,
            reservation_token,
            client_factory=client_factory,
            now_epoch=now_epoch,
            relay_executor_thread_id=owner,
            dispatcher_pid=(os.getpid() if dispatcher_authorized else None),
            connected_client=connected_client,
        )

    descriptor = claim_automatic_app_server_turn(
        cfg,
        reservation_token,
        relay_executor_thread_id=owner,
        dispatcher_pid=(os.getpid() if dispatcher_authorized else None),
    )
    state_after_create = StateStore(cfg.state_dir).load()
    session = _session_by_token(state_after_create, reservation_token)
    thread_id = str(session["thread_id"])
    turn_id = ""
    completed_turn: dict[str, Any] | None = None
    production_log = (
        cfg.state_dir / "logs" / f"app-server-production-{reservation_token}.jsonl"
    )
    client: Any = None
    production_context = (
        client_factory(cfg.desktop.binary, production_log)
        if connected_client is None
        else nullcontext(connected_client)
    )
    try:
        with production_context as production_client:
            client = production_client
            if connected_client is None:
                resumed = production_client.resume_thread(thread_id)
                thread = resumed.get("thread") or {}
            else:
                thread = production_client.read_thread(thread_id)
            if _thread_cwd(thread) != cfg.root:
                raise DesktopLifecycleError(
                    "App Server production task is not bound to the canonical cwd"
                )
            if cfg.desktop.project_id and thread.get("projectId") != cfg.desktop.project_id:
                raise DesktopLifecycleError(
                    "App Server production task lost its configured project association"
                )
            started = production_client.start_turn(
                thread_id=thread_id,
                prompt=descriptor.prompt,
                effort=(
                    descriptor.thinking
                    if descriptor.thinking
                    else None
                ),
                client_user_message_id=str(session["client_user_message_id"]),
                skill_name=cfg.skill_name,
                skill_path=cfg.skill_path,
                cwd=cfg.root,
                permission_profile=cfg.desktop.permission_profile,
                model=(
                    descriptor.model
                    if descriptor.model
                    else None
                ),
            )
            turn_id = str((started.get("turn") or {}).get("id") or "")
            if not turn_id:
                raise DesktopLifecycleError("App Server turn/start returned no turn id")
            acknowledge_desktop_send(
                cfg,
                reservation_token,
                thread_id=thread_id,
                relay_executor_thread_id=owner,
            )
            result = production_client.wait_for_turn(
                thread_id,
                turn_id,
                timeout=cfg.desktop.turn_timeout_seconds,
                pause_requested=StateStore(cfg.state_dir).pause_requested,
            )
            completed_turn = dict(result.turn)
            if not completed_turn.get("error") and result.errors:
                completed_turn["error"] = (
                    result.errors[-1].get("error") or result.errors[-1]
                )
        if connected_client is None and (
            client is None or not _client_process_exited(client)
        ):
            raise DesktopLifecycleError(
                "automatic App Server production process did not fully exit"
            )
    except PauseRequested:
        record_desktop_failure(
            cfg,
            reservation_token,
            reason="automatic App Server worker paused",
            definitive=True,
            thread_id=thread_id,
            turn_id=turn_id or None,
            now_epoch=now_epoch,
            reserve_other_ready=False,
            relay_executor_thread_id=owner,
        )
        raise
    except Exception as exc:
        state = StateStore(cfg.state_dir).load()
        current = _session_by_token(state, reservation_token)
        if current.get("status") in {"SEND_RELAYING", "ACTIVE"}:
            rpc_method = exc.method if isinstance(exc, AppServerRpcError) else None
            record_desktop_failure(
                cfg,
                reservation_token,
                reason=str(exc),
                definitive=(rpc_method == "turn/start" or bool(turn_id)),
                thread_id=thread_id,
                turn_id=turn_id or None,
                rate_limited=is_rate_limit_error(getattr(exc, "error", None)),
                now_epoch=now_epoch,
                reserve_other_ready=False,
                relay_executor_thread_id=owner,
            )
        raise DesktopLifecycleError(str(exc)) from exc

    assert completed_turn is not None
    if completed_turn.get("status") != "completed":
        reason = json.dumps(
            completed_turn.get("error") or completed_turn,
            ensure_ascii=False,
            sort_keys=True,
        )
        record_desktop_failure(
            cfg,
            reservation_token,
            reason=f"App Server production turn ended non-completed: {reason}",
            definitive=True,
            thread_id=thread_id,
            turn_id=turn_id,
            rate_limited=is_rate_limit_error(completed_turn.get("error")),
            now_epoch=now_epoch,
            reserve_other_ready=False,
            relay_executor_thread_id=owner,
        )
        raise DesktopLifecycleError(reason)

    # The local dispatcher is authoritative. The worker Stop hook observes an
    # owned automatic turn but never consumes it or starts its successor.
    return complete_desktop_worker(
        cfg,
        thread_id=thread_id,
        turn_id=turn_id,
        final_message=final_agent_message(completed_turn),
        now_epoch=now_epoch,
        dispatcher_reservation_token=reservation_token,
        dispatcher_pid=(os.getpid() if dispatcher_authorized else None),
    )


def record_automatic_app_server_exit(
    cfg: Config,
    reservation_token: str,
    *,
    dispatcher_pid: int,
    at: str | None = None,
) -> None:
    """Journal the per-task App Server full-exit barrier.

    The dispatcher process may continue with a successor, but each task gets a
    fresh App Server subprocess. Recording the barrier before successor
    adoption proves the completed task has no surviving transport writer.
    """

    _require_desktop_owned(cfg)
    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        if session.get("automatic_dispatch_pid") != dispatcher_pid:
            raise DesktopLifecycleError(
                "App Server exit acknowledgement does not match dispatcher ownership"
            )
        if not session.get("completed_at"):
            raise DesktopLifecycleError(
                "App Server exit acknowledgement requires an authoritative completed turn"
            )
        if session.get("app_server_worker_exited_at"):
            return
        session["app_server_worker_exited_at"] = timestamp
        session["automatic_dispatch_connection_pid"] = None
        if session.get("automatic_dispatch_state") == "COMPLETED":
            session["automatic_dispatch_pid"] = None
        _append_event(
            state,
            "app_server_worker_process_exited",
            session,
            timestamp,
            detail="full process exit before successor adoption",
        )
        store.save(state)


def adopt_automatic_dispatcher_successor(
    cfg: Config,
    *,
    completed_reservation_token: str,
    successor_reservation_token: str,
) -> tuple[str, str]:
    """Move the v0.7-style dispatcher loop to its exact reserved successor."""

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        completed = _session_by_token(state, completed_reservation_token)
        successor = _session_by_token(state, successor_reservation_token)
        if (
            completed.get("automatic_dispatch_state") != "ADVANCING"
            or completed.get("automatic_dispatch_pid") != os.getpid()
            or successor_reservation_token
            not in set(completed.get("automatic_successor_tokens") or [])
        ):
            raise DesktopLifecycleError(
                "current dispatcher does not own the completed-to-successor transition"
            )
        if successor.get("status") not in {"CREATE_REQUESTED", "PREPARED"}:
            raise DesktopLifecycleError("automatic successor is not launchable")
        owner = str(successor.get("relay_owner_thread_id") or "")
        if not owner:
            raise DesktopLifecycleError("automatic successor has no causal owner")
        predecessor = next(
            (
                item
                for item in reversed(state.worker_sessions)
                if item.get("thread_id") == owner
                and item.get("status") == "COMPLETED"
                and item.get("turn_id")
            ),
            None,
        )
        if predecessor is None:
            raise DesktopLifecycleError(
                "automatic successor has no completed causal predecessor turn"
            )
        completed["automatic_dispatch_state"] = "COMPLETED"
        completed["automatic_dispatch_pid"] = None
        successor["automatic_dispatch_state"] = "RUNNING"
        successor["automatic_dispatch_pid"] = os.getpid()
        successor["automatic_dispatch_adopted_at"] = utc_now()
        store.save(state)
        return owner, str(predecessor["turn_id"])


def acknowledge_desktop_create(
    cfg: Config,
    reservation_token: str,
    *,
    thread_id: str,
    host_id: str | None = None,
    slot_turn_id: str | None = None,
    slot_final_message: str,
    at: str | None = None,
    relay_executor_thread_id: str | None = None,
) -> LaunchDescriptor:
    """Bind a completed no-op Codex App create turn to its reservation."""

    _require_desktop_owned(cfg)
    if slot_final_message.strip() != DESKTOP_SLOT_READY:
        raise DesktopLifecycleError(
            f"Desktop slot turn must complete with exactly {DESKTOP_SLOT_READY}"
        )
    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if (
            session["status"] == "AMBIGUOUS"
            and session.get("ambiguous_phase") not in {None, "CREATE_REQUESTED", "RELAYING"}
        ):
            raise DesktopLifecycleError(
                "ambiguous reservation did not fail during Desktop task creation"
            )
        if session["status"] in {
            "CREATED",
            "PREPARING",
            "PREPARED",
            "SEND_RELAYING",
            "ACTIVE",
        }:
            if session.get("thread_id") != thread_id:
                raise DesktopLifecycleError(
                    "reservation token is already bound to a different Desktop task"
                )
            return LaunchDescriptor.from_dict(dict(session["descriptor"]))
        if session["status"] not in {"RELAYING", "AMBIGUOUS"}:
            raise DesktopLifecycleError("reservation is no longer launchable")
        existing = next(
            (
                item
                for item in state.worker_sessions
                if item is not session and item.get("thread_id") == thread_id
            ),
            None,
        )
        if existing:
            raise DesktopLifecycleError("Desktop task is already bound to another reservation")
        session["thread_id"] = thread_id
        session["host_id"] = host_id
        session["slot_turn_id"] = slot_turn_id
        session["status"] = "CREATED"
        session["create_acknowledged_at"] = timestamp
        _bind_resource_identity(state, reservation_token, thread_id=thread_id)
        _append_event(
            state,
            "slot_ready",
            session,
            timestamp,
            turn_id=slot_turn_id,
            detail=DESKTOP_SLOT_READY,
        )
        _append_event(state, "create_acknowledged", session, timestamp)
        _append_event(state, "prep_requested", session, timestamp)
        state.current_thread_id = thread_id
        state.phase = "AWAITING_DESKTOP_CWD_PREP"
        state.status = "RUNNING"
        store.save(state)
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
    _materialize((descriptor,))
    return descriptor


def acknowledge_active_writer_release(
    cfg: Config,
    reservation_token: str,
    *,
    thread_id: str,
    at: str | None = None,
    relay_executor_thread_id: str | None = None,
) -> LaunchDescriptor:
    """Record the one allowed Codex App archive/unarchive compatibility release."""

    _require_desktop_owned(cfg)
    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session.get("status") != "CREATED":
            raise DesktopLifecycleError(
                "active-writer release requires a created Desktop task"
            )
        if session.get("thread_id") != thread_id:
            raise DesktopLifecycleError(
                "active-writer release acknowledged an unexpected Desktop task"
            )
        attempts = int(session.get("writer_release_attempts") or 0)
        if attempts != 0:
            raise DesktopLifecycleError(
                "active-writer compatibility release was already acknowledged"
            )
        if "already has an active writer" not in str(
            session.get("prep_failure_reason") or ""
        ):
            raise DesktopLifecycleError(
                "active-writer release requires the exact preparation failure"
            )
        session["writer_release_attempts"] = 1
        session["writer_release_acknowledged_at"] = timestamp
        session["prep_failure_reason"] = None
        state.last_error = None
        state.phase = "AWAITING_TRUSTED_HOOK_CWD_PREP"
        _append_event(
            state,
            "active_writer_release_acknowledged",
            session,
            timestamp,
            detail=thread_id,
        )
        store.save(state)
        return LaunchDescriptor.from_dict(dict(session["descriptor"]))


def acknowledge_desktop_launch(
    cfg: Config,
    reservation_token: str,
    *,
    thread_id: str,
    host_id: str | None = None,
    slot_turn_id: str | None = None,
    slot_final_message: str = "",
    at: str | None = None,
    relay_executor_thread_id: str | None = None,
) -> LaunchDescriptor:
    """Compatibility name for the create acknowledgement phase."""

    return acknowledge_desktop_create(
        cfg,
        reservation_token,
        thread_id=thread_id,
        host_id=host_id,
        slot_turn_id=slot_turn_id,
        slot_final_message=slot_final_message,
        at=at,
        relay_executor_thread_id=relay_executor_thread_id,
    )


def prepare_desktop_thread(
    cfg: Config,
    reservation_token: str,
    *,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    at: str | None = None,
    relay_executor_thread_id: str | None = None,
) -> LaunchDescriptor:
    """Adopt canonical cwd in one harmless App Server turn, then fully exit.

    The external process is scoped only to workspace preparation. Production
    cannot be claimed until the context manager has closed that process and the
    verified cwd plus exit barrier have been persisted.
    """

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    prep_pid = os.getpid()
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session["status"] == "PREPARED":
            return LaunchDescriptor.from_dict(dict(session["descriptor"]))
        if session["status"] == "PREPARING":
            existing_pid = session.get("prep_process_pid")
            app_server_pid = session.get("prep_app_server_pid")
            if (
                isinstance(existing_pid, int)
                and _pid_alive(existing_pid)
            ) or (
                isinstance(app_server_pid, int)
                and _pid_alive(app_server_pid)
            ):
                raise DesktopLifecycleError("Desktop cwd preparation is already active")
            _append_event(
                state,
                "prep_crash_recovered",
                session,
                utc_now(),
                detail="previous preparation process is no longer alive",
            )
            session["status"] = "CREATED"
            session["prep_process_pid"] = None
            session["prep_app_server_pid"] = None
        if session["status"] != "CREATED":
            raise DesktopLifecycleError("Desktop task is not ready for cwd preparation")
        if not session.get("thread_id"):
            raise DesktopLifecycleError("Desktop cwd preparation requires a bound thread")
        session["status"] = "PREPARING"
        session["prep_process_pid"] = prep_pid
        session["prep_started_at"] = utc_now()
        _append_event(state, "prep_started", session, str(session["prep_started_at"]))
        state.phase = "PREPARING_DESKTOP_CWD"
        store.save(state)
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        thread_id = str(session["thread_id"])

    client: AppServerClient | None = None
    actual_cwd: Path | None = None
    actual_thread_name: str | None = None
    actual_project_id: str | None = None
    title_verification = "unavailable: App Server thread/read did not expose name"
    project_association_verification = (
        "unavailable: App Server thread/read did not expose projectId; "
        "Desktop project placement remains Codex App-authoritative"
    )
    prep_turn_id: str | None = None
    slot_history_sha256: str | None = None
    try:
        log_path = cfg.state_dir / "logs" / f"desktop-prep-{reservation_token}.jsonl"
        with client_factory(cfg.desktop.binary, log_path) as connected:
            client = connected
            app_server_pid = _client_process_pid(client)
            if app_server_pid is not None:
                with coordinator.transaction():
                    state = store.load()
                    session = _session_by_token(state, reservation_token)
                    if (
                        session.get("status") != "PREPARING"
                        or session.get("prep_process_pid") != prep_pid
                    ):
                        raise DesktopLifecycleError(
                            "Desktop cwd preparation identity changed before App Server start"
                        )
                    session["prep_app_server_pid"] = app_server_pid
                    _append_event(
                        state,
                        "prep_app_server_started",
                        session,
                        utc_now(),
                        detail=str(app_server_pid),
                    )
                    store.save(state)
            resumed = client.resume_thread(thread_id)
            thread = resumed.get("thread") or {}
            if str(thread.get("id") or "") != thread_id:
                raise DesktopLifecycleError(
                    "Desktop cwd preparation resolved an unexpected thread"
                )
            snapshot = client.read_thread(thread_id)
            if str(snapshot.get("id") or "") != thread_id:
                raise DesktopSlotHistoryError(
                    "Desktop slot history resolved an unexpected thread"
                )
            slot_history_sha256 = _validate_pristine_desktop_slot(
                snapshot,
                descriptor=descriptor,
                slot_turn_id=session.get("slot_turn_id"),
            )
            actual_cwd = _thread_cwd(snapshot) or _thread_cwd(thread)
            if actual_cwd != cfg.root:
                prep_client_id = f"autopilot-prep-{_stable_text_id(reservation_token)[:24]}"
                started = client.start_plain_turn(
                    thread_id=thread_id,
                    prompt=WORKSPACE_HANDOFF_PROMPT,
                    effort=descriptor.thinking,
                    client_user_message_id=prep_client_id,
                    cwd=cfg.root,
                    permission_profile=cfg.desktop.permission_profile,
                    model=descriptor.model,
                )
                prep_turn_id = str((started.get("turn") or {}).get("id") or "")
                if not prep_turn_id:
                    raise DesktopLifecycleError(
                        "Desktop cwd preparation did not return a turn id"
                    )
                # The handoff turn can finish quickly. Persist its identity before
                # waiting so its own Stop hook can recognize this bounded,
                # non-production turn and avoid recursively advancing the relay.
                with coordinator.transaction():
                    state = store.load()
                    session = _session_by_token(state, reservation_token)
                    if (
                        session.get("status") != "PREPARING"
                        or session.get("prep_process_pid") != prep_pid
                    ):
                        raise DesktopLifecycleError(
                            "Desktop cwd preparation identity changed before handoff wait"
                        )
                    session["prep_turn_id"] = prep_turn_id
                    store.save(state)
                completed = client.wait_for_turn(
                    thread_id,
                    prep_turn_id,
                    timeout=cfg.desktop.reconcile_timeout_seconds,
                    pause_requested=store.pause_requested,
                )
                if (
                    completed.turn.get("status") != "completed"
                    or final_agent_message(completed.turn).strip() != WORKSPACE_HANDOFF_OK
                ):
                    raise DesktopLifecycleError(
                        "Desktop workspace handoff did not complete exactly"
                    )
                thread = client.read_thread(thread_id)
                actual_cwd = _thread_cwd(thread)
            if actual_cwd != cfg.root:
                raise DesktopLifecycleError(
                    "Desktop task did not adopt the canonical project cwd"
                )
            client.name_thread(thread_id, descriptor.title)
            metadata = client.read_thread(thread_id)
            if str(metadata.get("id") or "") != thread_id:
                raise DesktopLifecycleError(
                    "Desktop title verification resolved an unexpected thread"
                )
            actual_cwd = _thread_cwd(metadata)
            if actual_cwd != cfg.root:
                raise DesktopLifecycleError(
                    "Desktop task lost the canonical project cwd during title verification"
                )
            if "name" in metadata:
                raw_name = metadata.get("name")
                if not isinstance(raw_name, str) or raw_name != descriptor.title:
                    raise DesktopLifecycleError(
                        "App Server did not preserve the deterministic Desktop task title"
                    )
                actual_thread_name = raw_name
                title_verification = "verified by App Server thread/read"
            raw_project_id = metadata.get("projectId")
            if raw_project_id is not None:
                actual_project_id = str(raw_project_id)
                project_association_verification = (
                    f"observed App Server projectId {actual_project_id}"
                )
            if cfg.desktop.project_id:
                if actual_project_id != cfg.desktop.project_id:
                    raise DesktopLifecycleError(
                        "App Server did not preserve the configured saved-project association"
                    )
                project_association_verification = (
                    "verified configured App Server projectId by thread/read"
                )
        if client is None or not _client_process_exited(client):
            raise DesktopLifecycleError(
                "workspace preparation App Server did not fully exit"
            )
    except Exception as exc:
        with coordinator.transaction():
            state = store.load()
            session = _session_by_token(state, reservation_token)
            if session.get("status") == "PREPARING" and session.get("prep_process_pid") == prep_pid:
                session["prep_process_pid"] = None
                session["prep_app_server_pid"] = None
                session["prep_failure_reason"] = str(exc)
                if isinstance(exc, DesktopSlotHistoryError):
                    session["status"] = "AMBIGUOUS"
                    session["ambiguous_phase"] = "PREPARING"
                    session["failure_reason"] = str(exc)
                    _append_event(
                        state,
                        "slot_history_rejected",
                        session,
                        utc_now(),
                        detail=str(exc),
                    )
                    state.phase = "RECONCILE_AMBIGUOUS"
                else:
                    session["status"] = "CREATED"
                    _append_event(state, "prep_failed", session, utc_now(), detail=str(exc))
                    state.phase = "AWAITING_DESKTOP_CWD_PREP"
                state.last_error = str(exc)
                store.save(state)
        raise

    timestamp = at or utc_now()
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        if session.get("status") != "PREPARING" or session.get("prep_process_pid") != prep_pid:
            raise DesktopLifecycleError("Desktop cwd preparation identity changed")
        session["status"] = "PREPARED"
        session["prep_process_pid"] = None
        session["prep_app_server_pid"] = None
        session["prep_turn_id"] = prep_turn_id
        session["actual_cwd"] = str(actual_cwd)
        session["actual_thread_name"] = actual_thread_name
        session["title_verification"] = title_verification
        session["actual_project_id"] = actual_project_id
        session["project_association_verification"] = project_association_verification
        session["prep_app_server_exited_at"] = timestamp
        session["slot_history_verified_at"] = timestamp
        session["slot_history_sha256"] = slot_history_sha256
        state.prep_app_server_exited_at = timestamp
        state.last_error = None
        _append_event(
            state,
            "prep_completed",
            session,
            timestamp,
            turn_id=prep_turn_id,
            detail=str(actual_cwd),
        )
        _append_event(
            state,
            "prep_app_server_exited",
            session,
            timestamp,
            detail="workspace preparation process fully exited",
        )
        state.phase = "AWAITING_DESKTOP_SEND"
        store.save(state)
        return LaunchDescriptor.from_dict(dict(session["descriptor"]))


def production_send_payload(
    cfg: Config,
    reservation_token: str,
    *,
    relay_executor_thread_id: str | None = None,
) -> dict[str, Any]:
    """Legacy-state helper retained only for migration/reconciliation tests.

    No installed hook or CLI command calls this model-mediated transport.
    """

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session["status"] != "PREPARED":
            raise DesktopLifecycleError(
                "production send was already claimed or cwd preparation is incomplete"
            )
        if _thread_cwd({"cwd": session.get("actual_cwd")}) != cfg.root:
            raise DesktopLifecycleError("production send requires the canonical task cwd")
        if not session.get("prep_app_server_exited_at"):
            raise DesktopLifecycleError("production send requires full App Server process exit")
        if (
            session.get("creation_transport") != "app_server_thread_start"
            and (
                not session.get("slot_history_verified_at")
                or not session.get("slot_history_sha256")
            )
        ):
            raise DesktopLifecycleError(
                "production send requires verified pristine Desktop slot history"
            )
        session["status"] = "SEND_RELAYING"
        timestamp = utc_now()
        _append_event(state, "send_relay_claimed", session, timestamp)
        _append_event(state, "start_requested", session, timestamp)
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        thread_id = str(session["thread_id"])
        payload = descriptor.send_message_payload(
            thread_id=thread_id,
            host_id=str(session["host_id"]) if session.get("host_id") else None,
        )
        state.phase = "AWAITING_DESKTOP_SEND_ACK"
        store.save(state)
        return {
            "reservation_token": descriptor.reservation_token,
            "send_message_to_thread_payload": payload,
        }


def claim_automatic_app_server_turn(
    cfg: Config,
    reservation_token: str,
    *,
    relay_executor_thread_id: str,
    dispatcher_pid: int | None = None,
) -> LaunchDescriptor:
    """Claim the one production ``turn/start`` for the local dispatcher."""

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session.get("status") != "PREPARED":
            raise DesktopLifecycleError(
                "automatic production turn was already claimed or creation is incomplete"
            )
        if session.get("creation_transport") != "app_server_thread_start":
            raise DesktopLifecycleError(
                "automatic production requires an App Server-created task"
            )
        if _thread_cwd({"cwd": session.get("actual_cwd")}) != cfg.root:
            raise DesktopLifecycleError(
                "automatic production requires the canonical task cwd"
            )
        retained_connection = bool(
            _dispatcher_owns_reservation(session, dispatcher_pid=dispatcher_pid)
            and session.get("automatic_dispatch_connection_pid") == dispatcher_pid
        )
        if not session.get("app_server_create_exited_at") and not retained_connection:
            raise DesktopLifecycleError(
                "automatic production requires either the v0.7 dispatcher connection "
                "or the legacy creator process exit barrier"
            )
        session["status"] = "SEND_RELAYING"
        timestamp = utc_now()
        _append_event(state, "automatic_turn_claimed", session, timestamp)
        _append_event(state, "start_requested", session, timestamp)
        state.phase = "AWAITING_APP_SERVER_TURN_ACK"
        store.save(state)
        return LaunchDescriptor.from_dict(dict(session["descriptor"]))


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


def complete_desktop_worker(
    cfg: Config,
    *,
    thread_id: str,
    turn_id: str,
    final_message: str,
    source_thread_id: str | None = None,
    at: str | None = None,
    now_epoch: int | None = None,
    hook_gate: Callable[[Config], Any] | None = None,
    dispatcher_reservation_token: str | None = None,
    dispatcher_pid: int | None = None,
) -> CompletionOutcome:
    """Consume an authoritative Desktop Stop event and schedule the next frontier."""

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    initial = store.load()
    session = _active_session_by_thread(initial, thread_id)
    gate = hook_gate or require_trusted_stop_hook_for_config
    gate_checked = False
    dispatcher_authorized = bool(
        session
        and dispatcher_reservation_token == session.get("reservation_token")
        and _dispatcher_owns_reservation(session, dispatcher_pid=dispatcher_pid)
    )
    source_identity = str(source_thread_id or "").strip()
    if session is None and source_identity and source_identity != thread_id:
        source_session = _active_session_by_thread(initial, source_identity)
        if source_session is not None:
            gate(cfg)
            gate_checked = True
            reconcile_desktop_thread_identity(
                cfg,
                str(source_session["reservation_token"]),
                previous_thread_id=source_identity,
                current_thread_id=thread_id,
                expected_task_id=str(source_session["task_id"]),
                at=at,
                hook_gate=lambda _cfg: None,
            )
            initial = store.load()
            session = _active_session_by_thread(initial, thread_id)
    if session is None:
        return CompletionOutcome(False, None, (), initial.status == "DONE")
    if not gate_checked and not dispatcher_authorized:
        gate(cfg)
    kind = _session_kind(session)
    verdict: VerificationVerdict | None = None
    plan_change_request = None
    if kind == "replanner":
        try:
            replanner_result = parse_plan_change_result(final_message)
        except PlanChangeProtocolError as exc:
            raise DesktopLifecycleError(str(exc)) from exc
        return _complete_replanner(
            cfg,
            session=session,
            thread_id=thread_id,
            turn_id=turn_id,
            result=replanner_result,
            at=at,
            now_epoch=now_epoch,
        )
    try:
        plan_change_request = parse_plan_change_request(final_message)
    except PlanChangeProtocolError as exc:
        raise DesktopLifecycleError(str(exc)) from exc
    if plan_change_request is not None:
        worker_status = "PLAN_CHANGE_REQUEST"
    elif kind == "verifier":
        try:
            verdict = parse_verifier_result(final_message)
        except VerificationProtocolError as exc:
            raise DesktopLifecycleError(str(exc)) from exc
        worker_status = verdict.verdict
    else:
        worker_status = parse_desktop_worker_status(final_message)
    task_id = str(session["task_id"])
    checkpoint_before = str(session.get("checkpoint_before") or "")
    checkpoint_path = task_checkpoint_path(cfg.state_dir, task_id)
    if task_checkpoint(cfg.state_dir, task_id) == checkpoint_before:
        raise DesktopLifecycleError(
            "worker did not update its own checkpoint file: "
            f"{checkpoint_path.relative_to(cfg.state_dir.parent)}"
        )
    memory = ProjectMemory(cfg.root)
    evidence = memory.milestone_evidence(
        task_id,
        after_audit_id=int(session.get("memory_audit_before") or 0),
        limit=100,
    )
    needs_evidence = kind == "verifier" or worker_status in SUCCESS_STATUSES
    if needs_evidence and not evidence:
        raise DesktopLifecycleError(
            f"{task_id} returned completion without new Project Memory evidence"
        )
    plan = load_plan(cfg.state_dir, cfg.profile)
    task = plan.task_map[task_id]
    if verdict is not None:
        invalid_refs = sorted(
            {
                ref
                for issue in verdict.issues
                for ref in issue.dod_refs
                if ref > len(task.definition_of_done)
            }
        )
        if invalid_refs:
            raise DesktopLifecycleError(
                f"verifier issues reference unknown Definition of Done items: {invalid_refs}"
            )
    deterministic_results: tuple[DeterministicCheckResult, ...] = ()
    if (
        kind in IMPLEMENTATION_SESSION_KINDS | {"revision"}
        and worker_status in SUCCESS_STATUSES
        and task.verification.policy in {"deterministic", "auto"}
        and task.verification.deterministic_checks
    ):
        deterministic_results = run_deterministic_checks(
            cfg.root,
            task.verification.deterministic_checks,
            evidence,
        )
        _record_deterministic_evidence(
            memory,
            task_id,
            task.verification.deterministic_checks,
            deterministic_results,
            provider_thread_id=thread_id,
        )
        evidence = memory.milestone_evidence(
            task_id,
            after_audit_id=int(session.get("memory_audit_before") or 0),
            limit=100,
        )
    memory_verification_ids: list[str] = []
    if deterministic_results:
        memory_verification_ids.extend(
            _record_deterministic_verification_results(
                memory,
                task_id,
                deterministic_results,
                evidence,
                provider_thread_id=thread_id,
                provider_turn_id=turn_id,
            )
        )
    if verdict is not None:
        verifier_role = plan.role_map[verifier_route(plan, task).role_id].name
        verification = memory.record_verification_result(
            task_id=task_id,
            check_id="independent-acceptance",
            policy="independent",
            verdict=verdict.verdict,
            summary=(
                "Fresh independent verifier accepted every Definition of Done item."
                if verdict.verdict == "PASS"
                else f"Fresh independent verifier requested revision with {len(verdict.issues)} issue(s)."
            ),
            evidence_ids=[str(item["id"]) for item in evidence],
            created_by=verifier_role,
            provider="codex-desktop",
            provider_thread_id=thread_id,
            provider_turn_id=turn_id,
            details={
                "verification_round": int(session.get("verification_round") or 0),
                "issues": [item.to_dict() for item in verdict.issues],
            },
        )
        memory_verification_ids.append(str(verification["id"]))
    timestamp = at or utc_now()
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        current = _active_session_by_thread(state, thread_id)
        if current is None:
            return CompletionOutcome(False, None, (), state.status == "DONE")
        if current["reservation_token"] != session["reservation_token"]:
            raise DesktopLifecycleError("Desktop completion identity changed during reconciliation")
        current["turn_id"] = turn_id
        current["final_status"] = worker_status
        current["completed_at"] = timestamp
        current["status"] = (
            "COMPLETED"
            if kind == "verifier" or worker_status in SUCCESS_STATUSES
            else worker_status
        )
        current["completion_evidence_ids"] = [str(item["id"]) for item in evidence]
        if deterministic_results:
            current["deterministic_results"] = [item.to_dict() for item in deterministic_results]
        if verdict is not None:
            current["verification_result"] = verdict.to_dict()
        if memory_verification_ids:
            current["memory_verification_ids"] = memory_verification_ids
        _bind_resource_identity(
            state,
            str(current["reservation_token"]),
            thread_id=thread_id,
            turn_id=turn_id,
        )
        _append_event(state, "turn_identity_bound", current, timestamp)
        _append_event(state, "turn_completed", current, timestamp, detail=worker_status)

        release_resources_in_state(
            state,
            str(current["resource_ownership_token"]),
            reason="authoritative Desktop turn completion",
            now=timestamp,
        )
        state.active_task_ids = [item for item in state.active_task_ids if item != task_id]

        if plan_change_request is not None:
            source = TaskState(state.task_states[task_id])
            if source not in {
                TaskState.RUNNING,
                TaskState.VERIFYING,
                TaskState.REVISING,
            }:
                raise DesktopLifecycleError(
                    "plan change request requires an active implementation phase"
                )
            state.task_states = transition_task(
                plan,
                state.task_states,
                task_id,
                TaskState.BLOCKED,
            )
            current["status"] = "PLAN_CHANGE_REQUESTED"
            current["final_status"] = "PLAN_CHANGE_REQUEST"
            record = register_plan_change_request(
                state,
                plan_change_request,
                requester_task_id=task_id,
                requester_session_token=str(current["reservation_token"]),
                at=timestamp,
            )
            current["plan_change_id"] = record["id"]
            descriptors = _reserve_in_state(
                cfg,
                plan,
                state,
                memory_audit_before=memory.audit_highwater(),
                relay_owner_thread_id=thread_id,
                now_epoch=now_epoch,
            )
            if dispatcher_authorized:
                current["automatic_successor_tokens"] = [
                    item.reservation_token for item in descriptors
                ]
                current["automatic_dispatch_state"] = (
                    "ADVANCING" if descriptors else "COMPLETED"
                )
            _finish_global_state(
                plan,
                state,
                descriptors,
                paused=store.pause_requested(),
            )
            store.save(state)
            _materialize(descriptors)
            return CompletionOutcome(True, worker_status, descriptors, False)

        if kind == "verifier":
            if state.task_states[task_id] != TaskState.VERIFYING.value:
                raise DesktopLifecycleError("verifier completion requires VERIFYING state")
            assert verdict is not None
            if verdict.verdict == "PASS":
                state.task_states = transition_task(
                    plan, state.task_states, task_id, TaskState.VERIFIED
                )
                accepted_owner = _latest_implementation_thread_id(state, task_id)
                current["accepted_implementation_thread_id"] = accepted_owner
                _append_event(
                    state,
                    "verification_passed",
                    current,
                    timestamp,
                    detail=json.dumps(
                        {
                            "verdict": "PASS",
                            "accepted_implementation_thread_id": accepted_owner,
                        },
                        sort_keys=True,
                    ),
                )
            else:
                state.task_states = transition_task(
                    plan, state.task_states, task_id, TaskState.REVISION_REQUIRED
                )
                current["verification_issues"] = [item.to_dict() for item in verdict.issues]
                _append_event(
                    state,
                    "verification_revise",
                    current,
                    timestamp,
                    detail=json.dumps(verdict.to_dict(), ensure_ascii=False, sort_keys=True),
                )
                _block_if_revision_limit_reached(plan, state, task_id, current, timestamp)
        elif worker_status in SUCCESS_STATUSES:
            expected = (
                TaskState.REVISING.value
                if kind == "revision"
                else TaskState.RUNNING.value
            )
            if state.task_states[task_id] != expected:
                raise DesktopLifecycleError(
                    f"{kind} completion requires {expected} state"
                )
            state.task_states = transition_task(
                plan, state.task_states, task_id, TaskState.IMPLEMENTED
            )
            _append_event(
                state,
                "revision_completed" if kind == "revision" else "implementation_completed",
                current,
                timestamp,
                detail=TaskState.IMPLEMENTED.value,
            )
            if deterministic_results:
                passed = all(item.passed for item in deterministic_results)
                _append_event(
                    state,
                    "deterministic_verification_completed",
                    current,
                    timestamp,
                    detail=json.dumps(
                        {
                            "verdict": "PASS" if passed else "REVISE",
                            "checks": [item.to_dict() for item in deterministic_results],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                if not passed:
                    state.task_states = transition_task(
                        plan, state.task_states, task_id, TaskState.VERIFYING
                    )
                    issues = deterministic_issues(deterministic_results)
                    current["verification_issues"] = [item.to_dict() for item in issues]
                    state.task_states = transition_task(
                        plan, state.task_states, task_id, TaskState.REVISION_REQUIRED
                    )
                    _block_if_revision_limit_reached(
                        plan, state, task_id, current, timestamp
                    )
        else:
            source = TaskState.REVISING if kind == "revision" else TaskState.RUNNING
            if state.task_states[task_id] != source.value:
                raise DesktopLifecycleError(
                    f"{kind} failure requires {source.value} state"
                )
            state.task_states = transition_task(
                plan, state.task_states, task_id, TaskState.BLOCKED
            )
            state.last_error = f"{task_id} {kind} returned {worker_status}"

        if state.task_states[task_id] == TaskState.VERIFIED.value:
            memory.mark_milestone_complete(
                milestone_id=task_id,
                run_id=state.run_id,
                worker_sequence=int(current["worker_sequence"]),
                source=(
                    "independent_verifier"
                    if kind == "verifier"
                    else "deterministic_verification"
                    if deterministic_results
                    else "desktop_stop"
                ),
            )
        state.task_retry_at.pop(task_id, None)
        _sync_legacy_cursor(plan, state)
        next_relay_owner = thread_id
        if kind == "verifier" and verdict is not None and verdict.verdict == "PASS":
            next_relay_owner = str(
                current.get("accepted_implementation_thread_id") or ""
            )
            if not next_relay_owner:
                raise DesktopLifecycleError(
                    "verified task has no exact causal implementation predecessor"
                )
        descriptors = _reserve_in_state(
            cfg,
            plan,
            state,
            memory_audit_before=memory.audit_highwater(),
            relay_owner_thread_id=next_relay_owner,
            now_epoch=now_epoch,
        )
        if dispatcher_authorized:
            current["automatic_successor_tokens"] = [
                item.reservation_token for item in descriptors
            ]
            current["automatic_dispatch_state"] = (
                "ADVANCING" if descriptors else "COMPLETED"
            )
        _finish_global_state(
            plan,
            state,
            descriptors,
            paused=store.pause_requested(),
        )
        store.save(state)
        done = state.status == "DONE"
        completed = _verified_prefix(plan, state)
        next_index = state.milestone_index
    mark_roadmap(cfg.root, plan, completed, language=cfg.language)
    if plan.legacy_serial and not done:
        select_milestone(cfg.state_dir, plan, next_index, language=cfg.language)
    _materialize(descriptors)
    return CompletionOutcome(True, worker_status, descriptors, done)


def reconcile_desktop_thread_identity(
    cfg: Config,
    reservation_token: str,
    *,
    previous_thread_id: str,
    current_thread_id: str,
    expected_task_id: str | None = None,
    at: str | None = None,
    hook_gate: Callable[[Config], Any] | None = None,
) -> LaunchDescriptor:
    """Atomically rebind one ACTIVE worker after an authoritative platform handoff.

    Both identities and the exact reservation are mandatory. The runtime never
    guesses a successor from the active frontier or from conversation prose.
    """

    _require_desktop_owned(cfg)
    previous = str(previous_thread_id or "").strip()
    current = str(current_thread_id or "").strip()
    if not previous or not current:
        raise DesktopLifecycleError(
            "thread identity reconciliation requires non-empty previous and current IDs"
        )
    if previous == current:
        raise DesktopLifecycleError(
            "thread identity reconciliation requires two distinct IDs"
        )
    (hook_gate or require_trusted_stop_hook_for_config)(cfg)
    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        if expected_task_id is not None and session.get("task_id") != expected_task_id:
            raise DesktopLifecycleError(
                "thread identity reconciliation reservation belongs to another task"
            )
        if session.get("status") != "ACTIVE":
            raise DesktopLifecycleError(
                "thread identity reconciliation requires an ACTIVE worker"
            )
        history = session.get("thread_identity_history") or []
        if not isinstance(history, list) or not all(
            isinstance(item, str) and item for item in history
        ):
            raise DesktopLifecycleError("worker thread identity history is malformed")
        if session.get("thread_id") == current and previous in history:
            return LaunchDescriptor.from_dict(dict(session["descriptor"]))
        if session.get("thread_id") != previous:
            raise DesktopLifecycleError(
                "thread identity reconciliation source does not match the active reservation"
            )
        collision = next(
            (
                item
                for item in state.worker_sessions
                if item is not session
                and (
                    item.get("thread_id") == current
                    or current in (item.get("thread_identity_history") or [])
                )
            ),
            None,
        )
        if collision is not None:
            raise DesktopLifecycleError(
                "thread identity reconciliation target is already bound to another reservation"
            )
        session["thread_identity_history"] = [*history, previous]
        session["thread_id"] = current
        session["identity_reconciled_at"] = timestamp
        _bind_resource_identity(
            state,
            str(session["resource_ownership_token"]),
            thread_id=current,
        )
        if state.current_thread_id == previous:
            state.current_thread_id = current
        _append_event(
            state,
            "thread_identity_reconciled",
            session,
            timestamp,
            detail=json.dumps(
                {
                    "previous_thread_id": previous,
                    "current_thread_id": current,
                    "authority": "platform_handoff",
                },
                sort_keys=True,
            ),
        )
        store.save(state)
        return LaunchDescriptor.from_dict(dict(session["descriptor"]))


def _complete_replanner(
    cfg: Config,
    *,
    session: dict[str, Any],
    thread_id: str,
    turn_id: str,
    result: Any,
    at: str | None,
    now_epoch: int | None,
) -> CompletionOutcome:
    current_plan = load_plan(cfg.state_dir, cfg.profile)
    request_id = str(session.get("plan_change_id") or "")
    try:
        candidate = validate_replanner_result(
            current_plan,
            result,
            request_id=request_id,
            profile=cfg.profile,
        )
    except (PlanChangeProtocolError, PlanChangeConflictError, ValueError) as exc:
        raise DesktopLifecycleError(str(exc)) from exc

    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        current = _active_session_by_thread(state, thread_id)
        if current is None or current.get("reservation_token") != session.get(
            "reservation_token"
        ):
            return CompletionOutcome(False, None, (), state.status == "DONE")
        if _session_kind(current) != "replanner":
            raise DesktopLifecycleError("plan change result came from a non-replanner task")
        change = active_plan_change(state, request_id=request_id)
        if int(change["base_graph_version"]) != current_plan.graph_version:
            raise DesktopLifecycleError("active plan change base version changed")
        current["turn_id"] = turn_id
        current["final_status"] = "PLAN_CHANGE_APPLIED"
        current["completed_at"] = timestamp
        current["status"] = "COMPLETED"
        current["plan_change_result"] = {
            "request_id": result.request_id,
            "base_graph_version": result.base_graph_version,
            "target_graph_version": candidate.graph_version,
        }
        _bind_resource_identity(
            state,
            str(current["reservation_token"]),
            thread_id=thread_id,
            turn_id=turn_id,
        )
        _append_event(state, "turn_identity_bound", current, timestamp)
        _append_event(
            state,
            "turn_completed",
            current,
            timestamp,
            detail="PLAN_CHANGE_APPLIED",
        )
        release_resources_in_state(
            state,
            str(current["resource_ownership_token"]),
            reason="authoritative replanner completion",
            now=timestamp,
        )
        state.active_task_ids = [
            task_id for task_id in state.active_task_ids if task_id != current["task_id"]
        ]
        change["status"] = "REPLANNING"
        reconcile_plan_change_state(
            current_plan,
            candidate,
            state,
            request_id=request_id,
            requester_task_id=str(change["requester_task_id"]),
            at=timestamp,
        )
        _sync_legacy_cursor(candidate, state)
        descriptors = _reserve_in_state(
            cfg,
            candidate,
            state,
            memory_audit_before=ProjectMemory(cfg.root).audit_highwater(),
            relay_owner_thread_id=thread_id,
            now_epoch=now_epoch,
        )
        _finish_global_state(
            candidate,
            state,
            descriptors,
            paused=store.pause_requested(),
        )
        commit_plan_change(
            cfg.state_dir,
            profile=cfg.profile,
            current=current_plan,
            candidate=candidate,
            state=state,
            request_id=request_id,
        )
        done = state.status == "DONE"
        completed = _verified_prefix(candidate, state)
        next_index = state.milestone_index
    mark_roadmap(cfg.root, candidate, completed, language=cfg.language)
    if candidate.legacy_serial and not done:
        select_milestone(cfg.state_dir, candidate, next_index, language=cfg.language)
    _materialize(descriptors)
    return CompletionOutcome(True, "PLAN_CHANGE_APPLIED", descriptors, done)


def retire_incompatible_legacy_desktop_session(
    cfg: Config,
    reservation_token: str,
    *,
    thread_id: str,
    observed_status: str,
    observed_cwd: str,
    observer_thread_id: str,
    at: str | None = None,
) -> str:
    """Retire one proven terminal, wrong-cwd session from the removed relay.

    This is deliberately narrower than ordinary retry reconciliation. It may be
    used only while the run is paused, only for a pre-App-Server-creation
    session, and only with an authoritative Desktop observation that the known
    task is idle in a non-canonical cwd. The task is preserved in history; no
    destination is created or messaged here.
    """

    _require_desktop_owned(cfg)
    known_thread = str(thread_id or "").strip()
    observer = str(observer_thread_id or "").strip()
    if not known_thread or not observer:
        raise DesktopLifecycleError(
            "legacy transport retirement requires exact task and observer thread IDs"
        )
    if str(observed_status or "").strip().casefold() != "idle":
        raise DesktopLifecycleError(
            "legacy transport retirement requires authoritative idle Desktop status"
        )
    try:
        actual_cwd = Path(observed_cwd).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DesktopLifecycleError(
            "legacy transport retirement observed cwd is invalid"
        ) from exc
    if actual_cwd == cfg.root:
        raise DesktopLifecycleError(
            "canonical Desktop task cannot be retired as a wrong-cwd legacy session"
        )
    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    plan = load_plan(cfg.state_dir, cfg.profile)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        if state.status != "PAUSED" or not store.pause_requested():
            raise DesktopLifecycleError(
                "legacy transport retirement requires a paused run and pause marker"
            )
        session = _session_by_token(state, reservation_token)
        if session.get("status") not in {"CREATED", "PREPARING"}:
            raise DesktopLifecycleError(
                "legacy transport retirement requires a pre-production created session"
            )
        if session.get("creation_transport") == "app_server_thread_start":
            raise DesktopLifecycleError(
                "the repaired App Server creation path is not a legacy session"
            )
        if session.get("thread_id") != known_thread:
            raise DesktopLifecycleError(
                "authoritative Desktop observation does not match the bound task"
            )
        if session.get("start_acknowledged_at"):
            raise DesktopLifecycleError(
                "an acknowledged production session cannot be retired by transport migration"
            )
        task_id = str(session["task_id"])
        if state.task_states.get(task_id) != TaskState.RUNNING.value:
            raise DesktopLifecycleError(
                "legacy transport retirement requires a RUNNING destination"
            )

        session["status"] = "RETIRED_INCOMPATIBLE_TRANSPORT"
        session["completed_at"] = timestamp
        session["failure_reason"] = (
            "Authoritative Desktop read proved the removed relay produced an idle "
            f"task in non-canonical cwd {actual_cwd}; workspace changes are preserved."
        )
        session["retirement_observation"] = {
            "source": "codex_app_read_thread",
            "observer_thread_id": observer,
            "thread_id": known_thread,
            "status": "idle",
            "cwd": str(actual_cwd),
            "observed_at": timestamp,
        }
        _append_event(
            state,
            "legacy_desktop_transport_retired",
            session,
            timestamp,
            detail=json.dumps(session["retirement_observation"], sort_keys=True),
        )
        state.task_states = transition_task(
            plan,
            state.task_states,
            task_id,
            TaskState.RETRY_WAIT,
        )
        state.active_task_ids = [
            active for active in state.active_task_ids if active != task_id
        ]
        release_resources_in_state(
            state,
            str(session["resource_ownership_token"]),
            reason="incompatible legacy Desktop transport retired",
            now=timestamp,
        )
        state.task_retry_at[task_id] = 0
        if known_thread not in state.previous_thread_ids:
            state.previous_thread_ids.append(known_thread)
        if state.current_thread_id == known_thread:
            state.current_thread_id = None
            state.current_turn_id = None
            state.client_user_message_id = None
        state.status = "PAUSED"
        state.phase = "PAUSED_DRAINING"
        state.last_error = session["failure_reason"]
        append_resilience_event(
            state,
            "legacy_desktop_transport_retired",
            at=timestamp,
            task_id=task_id,
            detail=dict(session["retirement_observation"]),
        )
        store.save(state)
        return task_id


def record_desktop_failure(
    cfg: Config,
    reservation_token: str,
    *,
    reason: str,
    definitive: bool,
    rate_limited: bool = False,
    reset_at: int | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
    now_epoch: int | None = None,
    at: str | None = None,
    reserve_other_ready: bool = True,
    hook_gate: Callable[[Config], Any] | None = None,
    relay_executor_thread_id: str | None = None,
) -> tuple[LaunchDescriptor, ...]:
    """Fail one reservation without corrupting or stopping independent work."""

    _require_desktop_owned(cfg)
    if reserve_other_ready:
        (hook_gate or require_trusted_stop_hook_for_config)(cfg)
    timestamp = at or utc_now()
    epoch = int(time.time()) if now_epoch is None else now_epoch
    store = StateStore(cfg.state_dir)
    plan = load_plan(cfg.state_dir, cfg.profile)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session.get("status") not in PENDING_SESSION_STATUSES:
            raise DesktopLifecycleError("reservation failure was already reconciled")
        if thread_id:
            session["thread_id"] = thread_id
        if turn_id:
            session["turn_id"] = turn_id
        prior_status = str(session["status"])
        failure_phase = (
            str(session.get("ambiguous_phase"))
            if prior_status == "AMBIGUOUS" and session.get("ambiguous_phase")
            else prior_status
        )
        if turn_id:
            event = "interrupt_observed"
        elif failure_phase in {"RESERVED", "CREATE_REQUESTED", "RELAYING"}:
            event = "create_failed"
        elif failure_phase in {"CREATED", "PREPARING"}:
            event = "prep_failed"
        else:
            event = "start_failed"
        _append_event(state, event, session, timestamp, detail=reason)
        if not definitive:
            session["status"] = "AMBIGUOUS"
            if session.get("automatic_dispatch_state") is not None:
                session["automatic_dispatch_state"] = "AMBIGUOUS"
                session["automatic_dispatch_pid"] = None
                session["automatic_dispatch_connection_pid"] = None
            session["ambiguous_phase"] = failure_phase
            session["failure_reason"] = reason
            store.save(state)
            return ()

        # A known Desktop task is never replaced merely because its harmless
        # cwd preparation or a definitely rejected production send failed.
        # Both phases can retry the same task without risking duplicate work.
        if failure_phase in {"CREATED", "PREPARING"}:
            session["status"] = "CREATED"
            if session.get("automatic_dispatch_state") is not None:
                session["automatic_dispatch_state"] = "FAILED"
                session["automatic_dispatch_pid"] = None
                session["automatic_dispatch_connection_pid"] = None
            session["prep_process_pid"] = None
            session["prep_app_server_pid"] = None
            session["failure_reason"] = reason
            state.phase = "AWAITING_DESKTOP_CWD_PREP"
            state.last_error = reason
            store.save(state)
            return ()
        if failure_phase in {"PREPARED", "SEND_RELAYING"}:
            session["status"] = "PREPARED"
            if session.get("automatic_dispatch_state") is not None:
                session["automatic_dispatch_state"] = "FAILED"
                session["automatic_dispatch_pid"] = None
                session["automatic_dispatch_connection_pid"] = None
            session["failure_reason"] = reason
            state.phase = "AWAITING_DESKTOP_SEND"
            state.last_error = reason
            store.save(state)
            return ()

        task_id = str(session["task_id"])
        session["status"] = "RETRY_WAIT"
        if session.get("automatic_dispatch_state") is not None:
            session["automatic_dispatch_state"] = "RETRY_WAIT"
            session["automatic_dispatch_pid"] = None
            session["automatic_dispatch_connection_pid"] = None
        session["failure_reason"] = reason
        state.task_states = transition_task(
            plan, state.task_states, task_id, TaskState.RETRY_WAIT
        )
        state.active_task_ids = [item for item in state.active_task_ids if item != task_id]
        release_resources_in_state(
            state,
            str(session["resource_ownership_token"]),
            reason="definitive Desktop worker failure",
            now=timestamp,
        )
        delay = min(
            cfg.retry.maximum_seconds,
            cfg.retry.initial_seconds * (2 ** max(0, int(session["attempt"]) - 1)),
        )
        retry_at = epoch + delay
        if reset_at is not None:
            if isinstance(reset_at, bool) or not isinstance(reset_at, int) or reset_at < 0:
                raise DesktopLifecycleError("rate-limit reset_at must be a non-negative epoch")
            retry_at = max(retry_at, reset_at + 5)
        state.task_retry_at[task_id] = retry_at
        if rate_limited:
            state.rate_limit_until = max(int(state.rate_limit_until or 0), retry_at)
            append_resilience_event(
                state,
                "rate_limit_coordinated",
                at=timestamp,
                task_id=task_id,
                detail={"retry_at": retry_at, "reset_at": reset_at},
            )
        _append_event(
            state,
            "retry_scheduled",
            session,
            timestamp,
            detail=json.dumps(
                {"retry_at": retry_at, "rate_limited": rate_limited},
                sort_keys=True,
            ),
        )
        descriptors = (
            _reserve_in_state(
                cfg,
                plan,
                state,
                memory_audit_before=ProjectMemory(cfg.root).audit_highwater(),
                relay_owner_thread_id=session.get("relay_owner_thread_id"),
                now_epoch=now_epoch,
            )
            if reserve_other_ready
            else ()
        )
        _finish_global_state(
            plan,
            state,
            descriptors,
            paused=store.pause_requested(),
        )
        store.save(state)
    _materialize(descriptors)
    return descriptors


def record_policy_rejected_create_transport(
    cfg: Config,
    *,
    relay_owner_thread_id: str,
    turn_id: str,
    tool_input: dict[str, Any],
    rejection_detail: str,
    now_epoch: int | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """Reconcile one structured Codex App create rejection fail-closed.

    The hook supplies the actual Desktop session identity and exact tool input;
    neither a task id nor reservation token is caller-selected.  Matching the
    already claimed descriptor makes repeat delivery idempotent and prevents a
    foreign or stale tool result from opening a second recovery lane.
    """

    _require_desktop_owned(cfg)
    owner = str(relay_owner_thread_id or "").strip()
    observed_turn = str(turn_id or "").strip()
    detail = str(rejection_detail or "").strip()
    if not owner or not observed_turn:
        raise DesktopLifecycleError(
            "policy-rejected create reconciliation requires hook session and turn identity"
        )
    if not isinstance(tool_input, dict) or not detail:
        raise DesktopLifecycleError(
            "policy-rejected create reconciliation requires structured input and detail"
        )
    timestamp = at or utc_now()
    state = StateStore(cfg.state_dir).load()
    plan = load_plan(cfg.state_dir, cfg.profile)
    expected_payload = json.dumps(
        tool_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    matches: list[dict[str, Any]] = []
    for session in state.worker_sessions:
        if (
            session.get("relay_owner_thread_id") != owner
            or session.get("status") not in {"RELAYING", "RETRY_WAIT"}
            or session.get("thread_id")
            or not isinstance(session.get("descriptor"), dict)
        ):
            continue
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        candidate = json.dumps(
            descriptor.create_thread_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if candidate == expected_payload:
            matches.append(session)
    if len(matches) != 1:
        raise DesktopLifecycleError(
            "policy-rejected create result does not match one owned relay reservation"
        )
    session = matches[0]
    token = str(session["reservation_token"])
    task_id = str(session["task_id"])
    reservation_events = [
        item
        for item in state.lifecycle_journal
        if item.get("event") == "reservation_created"
        and item.get("reservation_token") == token
        and item.get("relay_owner_thread_id") == owner
    ]
    owner_completions = [
        item
        for item in state.lifecycle_journal
        if item.get("event") == "turn_completed"
        and item.get("thread_id") == owner
        and int(item.get("sequence") or 0)
        < int(reservation_events[0].get("sequence") or 0)
    ] if len(reservation_events) == 1 else []
    completed_tokens = {
        str(item.get("reservation_token") or "")
        for item in owner_completions
    }
    completed_owners = [
        item
        for item in state.worker_sessions
        if item.get("reservation_token") in completed_tokens
        and item.get("thread_id") == owner
        and item.get("status") == "COMPLETED"
        and item.get("final_status") in SUCCESS_STATUSES
        and state.task_states.get(str(item.get("task_id") or ""))
        == TaskState.VERIFIED.value
    ]
    if len(reservation_events) != 1 or not completed_owners:
        raise DesktopLifecycleError(
            "policy-rejected create lacks authoritative completed causal owner evidence"
        )
    if task_id not in plan.task_map:
        raise DesktopLifecycleError("policy-rejected create references an unknown task")

    payload_sha256 = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
    incident_store = PipelineIncidentStore(cfg.state_dir)
    incident = incident_store.open_incident(
        IncidentSignal(
            signal_id=(
                f"{state.run_id}:{token}:create_thread:transport_policy_rejected"
            ),
            code="transport_policy_rejected",
            surface=IncidentClass.PIPELINE,
            summary=(
                f"Codex App definitively rejected the owned create relay for {task_id}."
            ),
            affected_task_ids=(task_id,),
            operation="create_thread",
            side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
            system_state={
                "run_id": state.run_id,
                "reservation_token": token,
                "operation_id": str(session.get("operation_id") or ""),
                "task_id": task_id,
                "relay_owner_thread_id": owner,
                "observed_turn_id": observed_turn,
                "relay_status": str(session.get("status") or ""),
                "payload_sha256": payload_sha256,
            },
            recent_events=tuple(
                dict(item)
                for item in state.lifecycle_journal
                if item.get("reservation_token") == token
                or item.get("thread_id") == owner
            )[-20:],
        ),
        at=timestamp,
    )
    package = incident_store.ensure_pipeline_engineer(
        str(incident["incident_id"]),
        at=timestamp,
    )
    if session.get("status") == "RELAYING":
        record_desktop_failure(
            cfg,
            token,
            reason=detail,
            definitive=True,
            now_epoch=now_epoch,
            at=timestamp,
            reserve_other_ready=False,
            relay_executor_thread_id=owner,
        )
    return package


def record_desktop_interrupt(
    cfg: Config,
    *,
    thread_id: str,
    turn_id: str,
    reason: str = "Desktop turn interrupted",
    now_epoch: int | None = None,
    at: str | None = None,
) -> bool:
    """Journal an authoritative Interrupt hook without creating work in the hook."""

    state = StateStore(cfg.state_dir).load()
    session = _active_session_by_thread(state, thread_id)
    if session is None:
        return False
    record_desktop_failure(
        cfg,
        str(session["reservation_token"]),
        reason=reason,
        definitive=True,
        thread_id=thread_id,
        turn_id=turn_id,
        now_epoch=now_epoch,
        at=at,
        reserve_other_ready=False,
    )
    return True


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


def create_descriptor_payload(
    cfg: Config,
    reservation_token: str,
    *,
    relay_executor_thread_id: str | None = None,
) -> dict[str, Any]:
    """Atomically claim the single Codex App create side effect."""

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session["status"] != "CREATE_REQUESTED":
            raise DesktopLifecycleError(
                "relay payload was already claimed or the reservation is not launchable"
            )
        session["status"] = "RELAYING"
        _append_event(state, "create_relay_claimed", session, utc_now())
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        store.save(state)
        return {
            "reservation_token": descriptor.reservation_token,
            "create_thread_payload": descriptor.create_thread_payload(),
        }


def descriptor_payload(
    cfg: Config,
    reservation_token: str,
    *,
    relay_executor_thread_id: str | None = None,
) -> dict[str, Any]:
    """Compatibility name for the create-side payload claim."""

    return create_descriptor_payload(
        cfg,
        reservation_token,
        relay_executor_thread_id=relay_executor_thread_id,
    )


def _reserve_in_state(
    cfg: Config,
    plan: Plan,
    state: RunState,
    *,
    memory_audit_before: int,
    relay_owner_thread_id: str | None = None,
    now_epoch: int | None = None,
) -> tuple[LaunchDescriptor, ...]:
    relay_owner_thread_id = str(relay_owner_thread_id or "").strip()
    if not relay_owner_thread_id:
        raise DesktopLifecycleError(
            "Desktop reservation requires a bound relay owner thread"
        )
    if state.status == "DONE" or StateStore(cfg.state_dir).pause_requested():
        return ()
    if not state.prep_app_server_exited_at:
        raise DesktopLifecycleError(
            "bounded App Server preparation has not recorded a full process exit"
        )
    if _pid_alive(state.dispatcher_pid):
        raise DesktopLifecycleError(
            "external App Server dispatcher is active; Desktop-owned production is forbidden"
        )
    state.dispatcher_pid = None
    epoch = int(time.time()) if now_epoch is None else now_epoch
    if state.rate_limit_until is not None:
        if epoch < state.rate_limit_until:
            state.status = "WAITING"
            state.phase = "WAITING_RATE_LIMIT"
            return ()
        append_resilience_event(
            state,
            "rate_limit_cleared",
            detail={"rate_limit_until": state.rate_limit_until},
        )
        state.rate_limit_until = None
        _prepare_state(plan, state, now_epoch=epoch)
    if state.active_plan_change_id is not None:
        return _reserve_replanner_in_state(
            cfg,
            plan,
            state,
            memory_audit_before=memory_audit_before,
            relay_owner_thread_id=relay_owner_thread_id,
        )
    descriptors: list[LaunchDescriptor] = list(
        _reserve_followup_sessions_in_state(
            cfg,
            plan,
            state,
            memory_audit_before=memory_audit_before,
            relay_owner_thread_id=relay_owner_thread_id,
        )
    )
    decision = schedule(
        plan,
        state,
        build_scheduler_availability(plan, state, cfg.root),
    )
    for task_id in decision.selected_task_ids:
        if any(
            item.get("task_id") == task_id
            and item.get("status") in PENDING_SESSION_STATUSES
            for item in state.worker_sessions
        ):
            raise DesktopLifecycleError(f"task {task_id} already has a pending reservation")
        task = plan.task_map[task_id]
        attempt = state.task_attempts.get(task_id, 0) + 1
        state.task_attempts[task_id] = attempt
        state.worker_sequence += 1
        token = _stable_id(
            state,
            f"reservation:{task_id}:{attempt}:{state.worker_sequence}",
        )
        operation_id = _stable_id(state, f"operation:{token}")
        client_id = f"autopilot-{_stable_id(state, f'client:{token}')[:24]}"
        descriptor = _build_descriptor(
            cfg,
            plan,
            state,
            task_id=task_id,
            kind="implementation",
            attempt=attempt,
            token=token,
            operation_id=operation_id,
            client_id=client_id,
        )
        state.task_states = transition_task(
            plan, state.task_states, task_id, TaskState.RUNNING
        )
        if task_id not in state.active_task_ids:
            state.active_task_ids.append(task_id)
        owner = LockOwner.create(
            run_id=state.run_id,
            task_id=task_id,
            attempt=attempt,
            worker_id=f"desktop-worker-{state.worker_sequence}",
            ownership_token=token,
        )
        acquired = acquire_resources_in_state(
            plan, state, cfg.root, task_id, owner
        )
        if not acquired.acquired:
            raise DesktopLifecycleError(
                f"scheduler/resource race for {task_id}: {acquired.reason}"
            )
        session: dict[str, Any] = {
            "reservation_token": token,
            "resource_ownership_token": token,
            "operation_id": operation_id,
            "client_user_message_id": client_id,
            "task_id": task_id,
            "kind": "implementation",
            "attempt": attempt,
            "worker_sequence": state.worker_sequence,
            "status": "CREATE_REQUESTED",
            "thread_id": None,
            "turn_id": None,
            "host_id": None,
            "relay_owner_thread_id": relay_owner_thread_id,
            "created_at": descriptor.created_at,
            "memory_audit_before": memory_audit_before,
            "checkpoint_before": task_checkpoint(cfg.state_dir, task_id),
            "descriptor": descriptor.to_dict(),
        }
        state.worker_sessions.append(session)
        for event in ("reservation_created", "create_requested"):
            _append_event(state, event, session, descriptor.created_at)
        descriptors.append(descriptor)
    if descriptors:
        state.status = "RUNNING"
        state.phase = "AWAITING_DESKTOP_CREATE"
        state.milestone_id = descriptors[0].task_id
    return tuple(descriptors)


def _reserve_replanner_in_state(
    cfg: Config,
    plan: Plan,
    state: RunState,
    *,
    memory_audit_before: int,
    relay_owner_thread_id: str,
) -> tuple[LaunchDescriptor, ...]:
    """Reserve exactly one fresh replanner after all production workers drain."""

    record = active_plan_change(state)
    if state.active_task_ids:
        state.status = "RUNNING"
        state.phase = "PLAN_CHANGE_DRAINING"
        return ()
    if any(
        item.get("kind") == "replanner"
        and item.get("plan_change_id") == record["id"]
        and item.get("status") in PENDING_SESSION_STATUSES
        for item in state.worker_sessions
    ):
        return ()
    task_id = str(record["requester_task_id"])
    raw_state = TaskState(state.task_states[task_id])
    if raw_state is TaskState.RETRY_WAIT:
        state.status = "WAITING"
        state.phase = "WAITING_RATE_LIMIT"
        return ()
    if raw_state is TaskState.BLOCKED:
        state.task_states = transition_task(
            plan,
            state.task_states,
            task_id,
            TaskState.READY,
        )
        raw_state = TaskState.READY
    if raw_state is not TaskState.READY:
        raise DesktopLifecycleError(
            f"plan change requester {task_id} is not ready for replanning"
        )

    previous_attempt = int(state.task_attempts.get(task_id, 0))
    attempt = previous_attempt + 1
    worker_sequence = state.worker_sequence + 1
    token = _stable_id(
        state,
        f"reservation:{task_id}:replanner:{record['id']}:{attempt}:{worker_sequence}",
    )
    owner = LockOwner.create(
        run_id=state.run_id,
        task_id=task_id,
        attempt=attempt,
        worker_id=f"desktop-replanner-{worker_sequence}",
        ownership_token=token,
    )
    state.task_attempts[task_id] = attempt
    acquired = acquire_resources_in_state(
        plan,
        state,
        cfg.root,
        task_id,
        owner,
        execution_mode="code",
    )
    if not acquired.acquired:
        state.task_attempts[task_id] = previous_attempt
        state.status = "WAITING"
        state.phase = "PLAN_CHANGE_WAITING_LOCKS"
        return ()
    state.worker_sequence = worker_sequence
    operation_id = _stable_id(state, f"operation:{token}")
    client_id = f"autopilot-{_stable_id(state, f'client:{token}')[:24]}"
    descriptor = _build_descriptor(
        cfg,
        plan,
        state,
        task_id=task_id,
        kind="replanner",
        attempt=attempt,
        token=token,
        operation_id=operation_id,
        client_id=client_id,
    )
    state.task_states = transition_task(
        plan,
        state.task_states,
        task_id,
        TaskState.RUNNING,
    )
    state.active_task_ids.append(task_id)
    session: dict[str, Any] = {
        "reservation_token": token,
        "resource_ownership_token": token,
        "operation_id": operation_id,
        "client_user_message_id": client_id,
        "task_id": task_id,
        "kind": "replanner",
        "attempt": attempt,
        "worker_sequence": worker_sequence,
        "plan_change_id": record["id"],
        "status": "CREATE_REQUESTED",
        "thread_id": None,
        "turn_id": None,
        "host_id": None,
        "relay_owner_thread_id": relay_owner_thread_id,
        "created_at": descriptor.created_at,
        "memory_audit_before": memory_audit_before,
        "checkpoint_before": task_checkpoint(cfg.state_dir, task_id),
        "descriptor": descriptor.to_dict(),
    }
    state.worker_sessions.append(session)
    record["status"] = "REPLANNER_RESERVED"
    record["replanner_session_token"] = token
    for event in ("reservation_created", "replanner_started", "create_requested"):
        _append_event(state, event, session, descriptor.created_at)
    append_resilience_event(
        state,
        "replanner_reserved",
        at=descriptor.created_at,
        task_id=task_id,
        plan_change_id=str(record["id"]),
        detail={"reservation_token": token, "attempt": attempt},
    )
    state.status = "RUNNING"
    state.phase = "AWAITING_DESKTOP_CREATE"
    state.milestone_id = task_id
    return (descriptor,)


def _reserve_followup_sessions_in_state(
    cfg: Config,
    plan: Plan,
    state: RunState,
    *,
    memory_audit_before: int,
    relay_owner_thread_id: str | None = None,
) -> tuple[LaunchDescriptor, ...]:
    """Reserve verifier/revision work before admitting unrelated READY work."""

    worker_limit = min(plan.max_parallel_workers, state.max_parallel_workers)
    if plan.legacy_serial or "serial" in {
        plan.execution_strategy,
        state.execution_strategy,
    }:
        worker_limit = 1
    descriptors: list[LaunchDescriptor] = []
    memory = ProjectMemory(cfg.root)
    for task in plan.tasks:
        if len(state.active_task_ids) >= worker_limit:
            break
        raw_state = state.task_states[task.id]
        if raw_state == TaskState.IMPLEMENTED.value:
            kind = "verifier"
            destination = TaskState.VERIFYING
            revision_number = int(state.task_revisions.get(task.id, 0))
            verification_round = 1 + max(
                (
                    int(item.get("verification_round", 0))
                    for item in state.worker_sessions
                    if item.get("task_id") == task.id
                    and _session_kind(item) == "verifier"
                ),
                default=0,
            )
            issues: tuple[VerificationIssue, ...] = ()
            evidence, check_results = _latest_completion_context(memory, state, task.id)
            try:
                execution_mode = verifier_route(plan, task).execution_mode
            except ModelRoutingError as exc:
                state.task_states = transition_task(
                    plan, state.task_states, task.id, TaskState.BLOCKED
                )
                state.last_error = str(exc)
                _append_event(
                    state,
                    "verifier_routing_blocked",
                    _latest_task_session(state, task.id),
                    utc_now(),
                    detail=str(exc),
                )
                continue
        elif raw_state == TaskState.REVISION_REQUIRED.value:
            if _block_if_revision_limit_reached(
                plan,
                state,
                task.id,
                _latest_task_session(state, task.id),
                utc_now(),
            ):
                continue
            kind = "revision"
            destination = TaskState.REVISING
            revision_number = int(state.task_revisions.get(task.id, 0)) + 1
            verification_round = 0
            issues = _latest_verification_issues(state, task.id)
            if not issues:
                raise DesktopLifecycleError(
                    f"task {task.id} requires revision without structured issues"
                )
            evidence = ()
            check_results = ()
            execution_mode = task.execution_mode
        else:
            continue

        if any(
            item.get("task_id") == task.id
            and item.get("status") in PENDING_SESSION_STATUSES
            for item in state.worker_sessions
        ):
            raise DesktopLifecycleError(
                f"task {task.id} already has a pending follow-up reservation"
            )
        previous_attempt = int(state.task_attempts.get(task.id, 0))
        attempt = previous_attempt + 1
        worker_sequence = state.worker_sequence + 1
        token = _stable_id(
            state,
            f"reservation:{task.id}:{kind}:{attempt}:{worker_sequence}",
        )
        operation_id = _stable_id(state, f"operation:{token}")
        client_id = f"autopilot-{_stable_id(state, f'client:{token}')[:24]}"
        state.task_attempts[task.id] = attempt
        owner = LockOwner.create(
            run_id=state.run_id,
            task_id=task.id,
            attempt=attempt,
            worker_id=f"desktop-worker-{worker_sequence}",
            ownership_token=token,
        )
        acquired = acquire_resources_in_state(
            plan,
            state,
            cfg.root,
            task.id,
            owner,
            execution_mode=execution_mode,
        )
        if not acquired.acquired:
            state.task_attempts[task.id] = previous_attempt
            continue

        state.worker_sequence = worker_sequence
        if kind == "revision":
            state.task_revisions[task.id] = revision_number
        descriptor = _build_descriptor(
            cfg,
            plan,
            state,
            task_id=task.id,
            kind=kind,
            attempt=attempt,
            token=token,
            operation_id=operation_id,
            client_id=client_id,
            verification_round=verification_round,
            revision_number=revision_number,
            verification_issues=issues,
            verification_evidence=evidence,
            deterministic_results=check_results,
        )
        state.task_states = transition_task(
            plan, state.task_states, task.id, destination
        )
        if task.id not in state.active_task_ids:
            state.active_task_ids.append(task.id)
        session: dict[str, Any] = {
            "reservation_token": token,
            "resource_ownership_token": token,
            "operation_id": operation_id,
            "client_user_message_id": client_id,
            "task_id": task.id,
            "kind": kind,
            "attempt": attempt,
            "worker_sequence": worker_sequence,
            "verification_round": verification_round,
            "revision_number": revision_number,
            "verification_issues": [item.to_dict() for item in issues],
            "status": "CREATE_REQUESTED",
            "thread_id": None,
            "turn_id": None,
            "host_id": None,
            "relay_owner_thread_id": relay_owner_thread_id,
            "created_at": descriptor.created_at,
            "memory_audit_before": memory_audit_before,
            "checkpoint_before": task_checkpoint(cfg.state_dir, task.id),
            "descriptor": descriptor.to_dict(),
        }
        state.worker_sessions.append(session)
        for event in (
            "reservation_created",
            "verification_started" if kind == "verifier" else "revision_started",
            "create_requested",
        ):
            _append_event(state, event, session, descriptor.created_at)
        descriptors.append(descriptor)
    return tuple(descriptors)


def _prepare_state(plan: Plan, state: RunState, *, now_epoch: int | None) -> None:
    if set(state.task_states) != set(plan.task_map):
        if state.worker_sessions or not plan.legacy_serial:
            raise DesktopLifecycleError("durable task state does not match the active graph")
        state.task_states = migrate_v08_task_states(plan, asdict(state))
        state.active_task_ids = [
            task_id
            for task_id, value in state.task_states.items()
            if value in {TaskState.RUNNING.value, TaskState.VERIFYING.value, TaskState.REVISING.value}
        ]
        state.task_attempts = {
            task.id: int(state.task_attempts.get(task.id, 0)) for task in plan.tasks
        }
        state.task_revisions = {
            task.id: int(state.task_revisions.get(task.id, 0)) for task in plan.tasks
        }
    else:
        state.task_states = validate_task_states(plan, state.task_states)
    epoch = int(time.time()) if now_epoch is None else now_epoch
    if state.rate_limit_until is not None:
        if epoch < state.rate_limit_until:
            return
        append_resilience_event(
            state,
            "rate_limit_cleared",
            detail={"rate_limit_until": state.rate_limit_until},
        )
        state.rate_limit_until = None
    for task_id, retry_at in list(state.task_retry_at.items()):
        if retry_at <= epoch and state.task_states.get(task_id) == TaskState.RETRY_WAIT.value:
            state.task_states = transition_task(
                plan, state.task_states, task_id, TaskState.READY
            )
            del state.task_retry_at[task_id]


def _legacy_retry_requires_bound_owner(
    state: RunState,
    plan: Plan,
    *,
    now_epoch: int,
) -> bool:
    if (
        not plan.legacy_serial
        or state.status != "WAITING"
        or state.phase != "WAITING_RATE_LIMIT"
        or state.active_task_ids
        or any(
            item.get("status") in PENDING_SESSION_STATUSES
            for item in state.worker_sessions
        )
    ):
        return False
    task_id = str(state.milestone_id or "")
    task = plan.task_map.get(task_id)
    if task is None:
        return False
    current_attempt = int(state.task_attempts.get(task_id, 0))
    current_sessions = [
        item
        for item in state.worker_sessions
        if item.get("task_id") == task_id
        and item.get("status") == "RETRY_WAIT"
        and int(item.get("attempt") or 0) == current_attempt
    ]
    has_required_owner = bool(
        len(current_sessions) == 1
        and (
            current_sessions[0].get("relay_owner_thread_id")
            or task.depends_on
        )
    )
    raw_state = state.task_states.get(task_id)
    if raw_state == TaskState.RETRY_WAIT.value:
        retry_at = state.task_retry_at.get(task_id)
        return has_required_owner and (
            not isinstance(retry_at, int) or retry_at <= now_epoch
        )
    if raw_state != TaskState.READY.value or task_id in state.task_retry_at:
        return False
    return has_required_owner


def _legacy_retry_recovery_context(
    state: RunState,
    plan: Plan,
    *,
    now_epoch: int,
) -> tuple[str, dict[str, Any], str] | None:
    """Resolve a due retry to its exact authoritative predecessor thread."""

    task_id = str(state.milestone_id or "")
    if (
        not _legacy_retry_requires_bound_owner(
            state,
            plan,
            now_epoch=now_epoch,
        )
        or state.status != "WAITING"
        or state.phase != "WAITING_RATE_LIMIT"
    ):
        return None
    task = plan.task_map[task_id]
    current_attempt = int(state.task_attempts.get(task_id, 0))
    retry_sessions = [
        item
        for item in state.worker_sessions
        if item.get("task_id") == task_id
        and item.get("status") == "RETRY_WAIT"
        and int(item.get("attempt") or 0) == current_attempt
    ]
    if len(retry_sessions) != 1:
        return None
    retry_session = retry_sessions[0]
    retry_token = str(retry_session.get("reservation_token") or "")
    retry_events = [
        item
        for item in state.lifecycle_journal
        if item.get("event") == "retry_scheduled"
        and item.get("reservation_token") == retry_token
    ]
    if not retry_token or len(retry_events) != 1:
        return None
    try:
        retry_detail = json.loads(str(retry_events[0].get("detail") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    retry_at = retry_detail.get("retry_at")
    if not isinstance(retry_at, int) or retry_at > now_epoch:
        return None
    raw_state = state.task_states.get(task_id)
    if raw_state == TaskState.RETRY_WAIT.value:
        if state.task_retry_at.get(task_id) != retry_at:
            return None
    elif raw_state == TaskState.READY.value:
        if task_id in state.task_retry_at:
            return None
    else:
        return None

    bound_owner = str(retry_session.get("relay_owner_thread_id") or "")
    if not bound_owner and not task.depends_on:
        return None

    completion_sequence: dict[str, int] = {}
    for event in state.lifecycle_journal:
        if event.get("event") != "turn_completed":
            continue
        token = str(event.get("reservation_token") or "")
        sequence = int(event.get("sequence") or 0)
        if token and sequence > completion_sequence.get(token, 0):
            completion_sequence[token] = sequence
    reservation_events = [
        item
        for item in state.lifecycle_journal
        if item.get("event") == "reservation_created"
        and item.get("reservation_token") == retry_token
    ]
    if len(reservation_events) != 1:
        return None
    reservation = reservation_events[0]
    reservation_sequence = int(reservation.get("sequence") or 0)
    allowed_source_tasks = {task_id, *task.depends_on}
    sources = [
        item
        for item in state.worker_sessions
        if item.get("task_id") in allowed_source_tasks
        and item.get("status") == "COMPLETED"
        and item.get("thread_id")
        and 0
        < completion_sequence.get(str(item.get("reservation_token") or ""), 0)
        < reservation_sequence
    ]
    if bound_owner:
        if reservation.get("relay_owner_thread_id") != bound_owner:
            return None
        owned_sources = [
            item
            for item in sources
            if str(item.get("thread_id") or "") == bound_owner
        ]
        if task.depends_on and not owned_sources:
            return None
        if not owned_sources:
            return task_id, retry_session, bound_owner
        source = max(
            owned_sources,
            key=lambda item: completion_sequence[
                str(item.get("reservation_token") or "")
            ],
        )
    else:
        if not sources:
            return None
        prior_events = [
            item
            for item in state.lifecycle_journal
            if int(item.get("sequence") or 0) == reservation_sequence - 1
            and item.get("event")
            in {
                "implementation_completed",
                "revision_completed",
                "verification_passed",
                "deterministic_verification_completed",
                "verification_revise",
            }
        ]
        if len(prior_events) != 1:
            return None
        prior = prior_events[0]
        accepted_owner = ""
        if prior.get("event") == "verification_passed":
            try:
                accepted_owner = str(
                    json.loads(str(prior.get("detail") or "")).get(
                        "accepted_implementation_thread_id"
                    )
                    or ""
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
        source = next(
            (
                item
                for item in sources
                if (
                    str(item.get("thread_id") or "") == accepted_owner
                    if accepted_owner
                    else item.get("reservation_token")
                    == prior.get("reservation_token")
                )
            ),
            None,
        )
        if source is None:
            return None
        if not accepted_owner and (
            prior.get("thread_id") != source.get("thread_id")
            or prior.get("turn_id") != source.get("turn_id")
        ):
            return None

    source_token = str(source.get("reservation_token") or "")
    completion_events = [
        item
        for item in state.lifecycle_journal
        if item.get("event") == "turn_completed"
        and item.get("reservation_token") == source_token
    ]
    if len(completion_events) != 1:
        return None
    completion = completion_events[0]
    if (
        completion.get("thread_id") != source.get("thread_id")
        or completion.get("turn_id") != source.get("turn_id")
    ):
        return None
    if source.get("task_id") in task.depends_on and (
        state.task_states.get(str(source.get("task_id")))
        != TaskState.VERIFIED.value
        or source.get("final_status") not in SUCCESS_STATUSES
    ):
        return None
    required_owner = bound_owner or str(source.get("thread_id") or "")
    return task_id, retry_session, required_owner


def _build_descriptor(
    cfg: Config,
    plan: Plan,
    state: RunState,
    *,
    task_id: str,
    kind: str,
    attempt: int,
    token: str,
    operation_id: str,
    client_id: str,
    verification_round: int = 0,
    revision_number: int = 0,
    verification_issues: tuple[VerificationIssue, ...] = (),
    verification_evidence: tuple[dict[str, Any], ...] = (),
    deterministic_results: tuple[dict[str, Any], ...] = (),
) -> LaunchDescriptor:
    task = plan.task_map[task_id]
    if kind not in SESSION_KINDS:
        raise DesktopLifecycleError(f"unsupported Desktop session kind: {kind}")
    if kind == "verifier":
        route = verifier_route(plan, task)
        model = route.model_id
        thinking = route.reasoning
        execution_mode = route.execution_mode
    elif kind == "replanner":
        execution_mode = "code"
        if plan.model_strategy == "host-settings":
            model = None
            thinking = None
        else:
            key = logical_model(plan.model_strategy, execution_mode)
            model = MODEL_IDS[key]
            thinking = task.reasoning or "medium"
    else:
        execution_mode = task.execution_mode
        if plan.model_strategy == "host-settings":
            model = None
            thinking = None
        else:
            key = logical_model(plan.model_strategy, execution_mode)
            model = MODEL_IDS[key]
            thinking = task.reasoning or "medium"
    if kind == "replanner":
        change = active_plan_change(state)
        title = replanner_thread_title(
            str(change["id"]),
            str(change["request"]["summary"]),
        )
        prompt = _replanner_prompt(cfg, plan, state, change, token)
    else:
        role_id = route.role_id if kind == "verifier" else task.role
        title = task_phase_thread_title(
            task_id=task.id,
            task_title=task.title,
            kind=kind,
            role_name=plan.role_map[role_id].name,
            revision_number=revision_number,
        )
        prompt = _worker_prompt(
            cfg,
            plan,
            state,
            task_id,
            token,
            kind=kind,
            verification_round=verification_round,
            revision_number=revision_number,
            verification_issues=verification_issues,
            verification_evidence=verification_evidence,
            deterministic_results=deterministic_results,
        )
    path = cfg.state_dir / "launches" / f"{token}.json"
    return LaunchDescriptor(
        schema_version=DESCRIPTOR_SCHEMA_VERSION,
        surface=DESKTOP_OWNED_SURFACE,
        run_id=state.run_id,
        graph_version=state.graph_version,
        task_id=task.id,
        task_title=task.title,
        kind=kind,
        attempt=attempt,
        worker_sequence=state.worker_sequence,
        reservation_token=token,
        operation_id=operation_id,
        client_user_message_id=client_id,
        desktop_project_id=str(cfg.desktop.desktop_project_id),
        cwd=str(cfg.root),
        title=title,
        prompt=prompt,
        model=model,
        thinking=thinking,
        execution_mode=execution_mode,
        created_at=utc_now(),
        prep_app_server_exited_at=str(state.prep_app_server_exited_at),
        descriptor_path=str(path),
    )


def _replanner_prompt(
    cfg: Config,
    plan: Plan,
    state: RunState,
    change: dict[str, Any],
    token: str,
) -> str:
    """Build one bounded, transcript-free graph-replacement request."""

    memory = ProjectMemory(cfg.root)
    request = dict(change["request"])
    evidence_ids = list(request.get("evidence_ids") or [])
    for evidence_id in evidence_ids:
        memory.get_evidence(str(evidence_id))
    verified_state = []
    for task in plan.tasks:
        if state.task_states.get(task.id) != TaskState.VERIFIED.value:
            continue
        verified_state.append(
            {
                "task_id": task.id,
                "state": TaskState.VERIFIED.value,
                "evidence_ids": [
                    str(item["id"])
                    for item in memory.milestone_evidence(task.id, limit=20)[:8]
                ],
            }
        )
    envelope = {
        "phase": "replanning",
        "request_id": change["id"],
        "base_graph_version": plan.graph_version,
        "request": request,
        "current_plan": plan_to_dict(plan),
        "verified_state": verified_state,
        "constraints": {
            "goal_immutable": True,
            "user_request_immutable": True,
            "model_strategy_immutable": True,
            "verified_task_contracts_immutable": True,
            "existing_task_ids_must_remain": True,
            "next_graph_version": plan.graph_version + 1,
        },
    }
    payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    finish = (
        f'{PLAN_CHANGE_RESULT_PREFIX} '
        '{"request_id":"'
        + str(change["id"])
        + '","base_graph_version":'
        + str(plan.graph_version)
        + ',"plan":{...complete schema-3 plan...}}'
    )
    if is_russian(cfg.language):
        prompt = f"""Codex Autopilot AI Studio Runtime — свежий replanner.

Выполни только короткую перепланировку {change['id']} для канонического каталога {cfg.root}. Ниже расположен полный разрешённый контекст: текущий валидный граф, структурированный запрос и селекторы подтверждённого состояния. Не запрашивай транскрипты, HANDOFF prose или параллельные разговоры.

AUTOPILOT_CONTEXT: {payload}

Сначала полностью прочитай {cfg.skill_path}. При необходимости получи только перечисленные evidence ID через Project Memory. Не изменяй файлы, не запускай production и не становись manager: верни один полный schema-3 replacement graph. Дословно сохрани user_request, а также goal, model_strategy, контракты VERIFIED задач, структурированные RoleProfile и все существующие task ID; установи graph_version={plan.graph_version + 1}. Runtime заново проверит все ссылки, состояния и циклы и выполнит crash-safe commit. Reservation token: {token}.

Последняя непустая строка должна быть единственной protocol line в точном формате:
{finish}"""
    else:
        prompt = f"""Codex Autopilot AI Studio Runtime — fresh replanner.

Perform only the short {change['id']} replan for canonical directory {cfg.root}. The bounded context below is complete: the current validated graph, typed request, and verified-state selectors. Do not request transcripts, HANDOFF prose, or concurrent conversations.

AUTOPILOT_CONTEXT: {payload}

Read {cfg.skill_path} completely first. Retrieve only listed evidence IDs from Project Memory if needed. Do not modify files, start production, or become a manager: return one complete schema-3 replacement graph. Preserve user_request verbatim, plus the goal, model_strategy, VERIFIED task contracts, structured RoleProfiles, and every existing task ID; set graph_version={plan.graph_version + 1}. The runtime will revalidate every reference, state, and cycle and perform the crash-safe commit. Reservation token: {token}.

The final non-empty line must be the only protocol line in this exact format:
{finish}"""
    if len(prompt) > 64_000:
        raise DesktopLifecycleError("replanner prompt exceeds 64000 characters")
    return prompt


def _worker_prompt(
    cfg: Config,
    plan: Plan,
    state: RunState,
    task_id: str,
    token: str,
    *,
    kind: str = "implementation",
    verification_round: int = 0,
    revision_number: int = 0,
    verification_issues: tuple[VerificationIssue, ...] = (),
    verification_evidence: tuple[dict[str, Any], ...] = (),
    deterministic_results: tuple[dict[str, Any], ...] = (),
) -> str:
    phase = {
        "implementation": "implementation",
        "worker": "implementation",
        "verifier": "verification",
        "revision": "revision",
    }.get(kind)
    if phase is None:
        raise DesktopLifecycleError(f"unsupported worker prompt kind: {kind}")
    runtime = AIStudioRuntime(
        plan,
        cfg.root,
        language=cfg.language,
        skill_path=cfg.skill_path,
    )
    return runtime.build_prompt(
        task_id,
        phase=phase,
        task_states=state.task_states,
        reservation_token=token,
        verification_round=verification_round,
        revision_number=revision_number,
        issues=verification_issues,
        evidence=verification_evidence,
        deterministic_results=deterministic_results,
    )


def _verifier_prompt(
    cfg: Config,
    plan: Plan,
    task_id: str,
    token: str,
    *,
    verification_round: int,
    evidence: tuple[dict[str, Any], ...],
    deterministic_results: tuple[dict[str, Any], ...],
) -> str:
    task = plan.task_map[task_id]
    route = verifier_route(plan, task)
    role = plan.role_map[route.role_id]
    dod = "\n".join(
        f"{index}. {item}" for index, item in enumerate(task.definition_of_done, 1)
    )
    evidence_json = json.dumps(
        _evidence_selectors(evidence), ensure_ascii=False, separators=(",", ":")
    )
    checks_json = json.dumps(
        list(deterministic_results), ensure_ascii=False, separators=(",", ":")
    )
    role_json = json.dumps(
        {
            "id": role.id,
            "name": role.name,
            "responsibilities": list(role.responsibilities),
            "verification_expectations": list(role.verification_expectations),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    revise_example = (
        f'{VERIFICATION_PREFIX}{{"verdict":"REVISE","issues":['
        '{"code":"ISSUE-1","summary":"short issue","details":"specific evidence and required correction","dod_refs":[1]}]}'
    )
    pass_example = f'{VERIFICATION_PREFIX}{{"verdict":"PASS","issues":[]}}'
    if is_russian(cfg.language):
        return f"""Codex Autopilot Desktop-owned independent verifier.

Независимо проверь {task.id}: {task.title} в каноническом каталоге {cfg.root}. Это свежий verifier V{verification_round}; не изменяй реализацию и не выполняй работу implementer/revision worker.
Исходное пользовательское ТЗ: {plan.user_request}
Цель run: {plan.goal}
Цель задачи: {task.objective}
Роль verifier: {role_json}
Требуемая capability: {route.execution_mode}. Причина: {route.execution_mode_reason}

Критерии готовности:
{dod}

Селективные идентификаторы evidence текущей реализации: {evidence_json}
Результаты deterministic checks, если policy передала их: {checks_json}

Acceptance gate: независимо сопоставь фактический результат с исходным пользовательским ТЗ, целью run, структурированным контрактом задачи и каждым критерием готовности. Тесты implementer являются только evidence и не определяют критерии приёмки.

В prompt намеренно нет ответа implementer, его самооценки, transcript history, HANDOFF prose или параллельных разговоров. Не запрашивай их и не считай утверждения другого worker доказательством. Полностью прочитай {cfg.skill_path}, получи перечисленные evidence через Project Memory, самостоятельно проверь файлы/команды/артефакты и зафиксируй новое evidence для {task.id} с ролью independent_verification. Обнови свой задачный файл передачи .codex-autopilot/handoff/{task.id}.md — обязательный чекпойнт завершения, принадлежащий этой задаче. Не создавай commit, tag, push, publish, reset или clean. Не запускай production через App Server. Reservation token: {token}.

Верни PASS только если каждый критерий подтверждён. Иначе верни REVISE с уникальными структурированными issues. Свободный краткий отчёт разрешён перед protocol line. Последняя непустая строка должна быть ровно одним JSON-результатом одного из форматов:
{pass_example}
{revise_example}"""
    return f"""Codex Autopilot Desktop-owned independent verifier.

Independently verify {task.id}: {task.title} in canonical directory {cfg.root}. This is fresh verifier V{verification_round}; do not modify the implementation or perform implementer/revision work.
Original user request: {plan.user_request}
Run goal: {plan.goal}
Task objective: {task.objective}
Verifier role: {role_json}
Required capability: {route.execution_mode}. Reason: {route.execution_mode_reason}

Definition of Done:
{dod}

Selective evidence identifiers for the current implementation: {evidence_json}
Deterministic check results, when supplied by policy: {checks_json}

Acceptance gate: independently compare the actual result with the original user request, run goal, structured task contract, and every Definition of Done item. Implementer-authored tests are evidence only and do not define the acceptance criteria.

The prompt deliberately contains no implementer response, self-assessment, transcript history, HANDOFF prose, or concurrent conversation. Do not request them or treat another worker's claims as evidence. Read {cfg.skill_path} completely, retrieve the listed evidence through Project Memory, independently inspect the files/commands/artifacts, and record new evidence for {task.id} with role independent_verification. Update your own task handoff file .codex-autopilot/handoff/{task.id}.md - the required completion checkpoint owned by this task. Do not commit, tag, push, publish, reset, or clean. Never start production through App Server. Reservation token: {token}.

Return PASS only when every criterion is evidenced. Otherwise return REVISE with unique structured issues. A concise free-form report may precede the protocol line. The final non-empty line must be exactly one JSON result in one of these forms:
{pass_example}
{revise_example}"""


def _revision_prompt(
    cfg: Config,
    plan: Plan,
    task_id: str,
    token: str,
    *,
    revision_number: int,
    issues: tuple[VerificationIssue, ...],
) -> str:
    task = plan.task_map[task_id]
    role = plan.role_map[task.role]
    dod = "\n".join(
        f"{index}. {item}" for index, item in enumerate(task.definition_of_done, 1)
    )
    issues_json = json.dumps(
        [item.to_dict() for item in issues],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    role_json = json.dumps(
        {
            "id": role.id,
            "name": role.name,
            "responsibilities": list(role.responsibilities),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    verification_contract = json.dumps(
        _verification_contract(task), ensure_ascii=False, separators=(",", ":")
    )
    if is_russian(cfg.language):
        return f"""Codex Autopilot Desktop-owned revision worker.

Выполни только revision R{revision_number} для {task.id}: {task.title} в каноническом каталоге {cfg.root}.
Цель задачи: {task.objective}
Роль: {role_json}

Критерии готовности:
{dod}
Verification policy: {verification_contract}

Структурированные issues verifier/deterministic policy:
{issues_json}

Это свежий worker: prompt содержит только контракт задачи и issues, без verifier transcript, implementer transcript или параллельных разговоров. Полностью прочитай {cfg.skill_path}, исправь перечисленные issues, заново проверь затронутые критерии и зафиксируй новое evidence для {task.id}. Обнови свой задачный файл передачи .codex-autopilot/handoff/{task.id}.md — обязательный чекпойнт завершения, принадлежащий этой задаче. Сохраняй чужие изменения; не создавай commit, tag, push, publish, reset или clean. Не запускай production через App Server. Reservation token: {token}.

Заверши кратким проверенным итогом и ровно одной последней строкой:
AUTOPILOT_STATUS: ROTATE
Используй BLOCKED только при реальном блокере, ESCALATE — только после исчерпания текущего уровня рассуждения."""
    return f"""Codex Autopilot Desktop-owned revision worker.

Perform only revision R{revision_number} for {task.id}: {task.title} in canonical directory {cfg.root}.
Task objective: {task.objective}
Role: {role_json}

Definition of Done:
{dod}
Verification policy: {verification_contract}

Structured verifier/deterministic-policy issues:
{issues_json}

This is a fresh worker: the prompt contains only the task contract and issues, with no verifier transcript, implementer transcript, or concurrent conversation. Read {cfg.skill_path} completely, correct the listed issues, re-check the affected criteria, and record new evidence for {task.id}. Update your own task handoff file .codex-autopilot/handoff/{task.id}.md - the required completion checkpoint owned by this task. Preserve unrelated changes; do not commit, tag, push, publish, reset, or clean. Never start production through App Server. Reservation token: {token}.

Finish with a concise verified result and exactly one final line:
AUTOPILOT_STATUS: ROTATE
Use BLOCKED only for a real blocker and ESCALATE only after exhausting the current reasoning level."""


def _evidence_selectors(evidence: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    selectors: list[dict[str, Any]] = []
    for item in evidence[:20]:
        selector = {
            key: item[key]
            for key in ("id", "kind", "role", "path", "artifact_path")
            if item.get(key) is not None
        }
        if selector.get("id"):
            selectors.append(selector)
    return selectors


def _verification_contract(task: Task) -> dict[str, Any]:
    policy = task.verification
    return {
        "policy": policy.policy,
        "required": policy.required,
        "deterministic_checks": [
            {
                "id": check.id,
                "kind": check.kind,
                "description": check.description,
                **({"argv": list(check.argv)} if check.argv else {}),
                **({"path": check.path} if check.path else {}),
                "timeout_seconds": check.timeout_seconds,
                "expected_exit_code": check.expected_exit_code,
            }
            for check in policy.deterministic_checks
        ],
        "max_revision_attempts": policy.max_revision_attempts,
    }


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
        # Passing evidence checks point at the existing item whose role is the
        # exact check ID. A failed lookup records the deterministic absence
        # probe above so every outcome still has first-class evidence.


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


def reserve_authorized_transport_request(
    cfg: Config,
    reservation_token: str,
    *,
    operation: str,
    at: str | None = None,
) -> dict[str, Any]:
    """Project one durable lifecycle reservation into the authority plane.

    This copies the exact predecessor ``turn_completed`` provenance and payload
    digest, but deliberately does not grant authority or perform transport.
    """

    _require_desktop_owned(cfg)
    state = StateStore(cfg.state_dir).load()
    session = _session_by_token(state, reservation_token)
    descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
    if operation == "create_thread":
        if session.get("status") != "CREATE_REQUESTED":
            raise DesktopLifecycleError(
                "create transport requires a CREATE_REQUESTED lifecycle reservation"
            )
        payload = descriptor.create_thread_payload()
    elif operation == "send_message_to_thread":
        if session.get("status") != "PREPARED":
            raise DesktopLifecycleError(
                "send transport requires a PREPARED lifecycle reservation"
            )
        thread_id = str(session.get("thread_id") or "")
        if not thread_id:
            raise DesktopLifecycleError(
                "send transport cannot be reserved before create acknowledgement"
            )
        payload = descriptor.send_message_payload(
            thread_id=thread_id,
            host_id=str(session.get("host_id") or "") or None,
        )
    else:
        raise DesktopLifecycleError("transport request operation is not allowlisted")
    owner = str(session.get("relay_owner_thread_id") or "")
    reservation_sequences = [
        int(event["sequence"])
        for event in state.lifecycle_journal
        if event.get("reservation_token") == reservation_token
        and event.get("event") == "reservation_created"
    ]
    if len(reservation_sequences) != 1:
        raise DesktopLifecycleError("lifecycle reservation provenance is missing or non-unique")
    created_sequence = reservation_sequences[0]
    causal_events = [
        event
        for event in state.lifecycle_journal
        if event.get("event") == "turn_completed"
        and event.get("thread_id") == owner
        and int(event.get("sequence") or 0) < created_sequence
    ]
    if not causal_events:
        raise DesktopLifecycleError(
            "persistent transport has no authoritative predecessor completion"
        )
    causal_event = max(causal_events, key=lambda item: int(item["sequence"]))
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return PipelineIncidentStore(cfg.state_dir).reserve_transport_from_lifecycle(
        state,
        causal_event_sequence=int(causal_event["sequence"]),
        operation=operation,
        payload_sha256=digest,
        destination_task_id=str(session["task_id"]),
        at=at or utc_now(),
    )


def require_authorized_transport(
    cfg: Config,
    reservation_token: str,
    *,
    executor_thread_id: str,
    operation: str,
) -> None:
    """Compatibility gate: require the exact causal predecessor task."""

    _require_desktop_owned(cfg)
    if operation not in {"create_thread", "send_message_to_thread"}:
        raise DesktopLifecycleError("transport request operation is not allowlisted")
    session = _session_by_token(StateStore(cfg.state_dir).load(), reservation_token)
    _require_relay_executor(session, executor_thread_id)


def bind_authorized_transport(
    cfg: Config,
    reservation_token: str,
    claim: TransportClaim,
    *,
    at: str | None = None,
) -> None:
    """Bind an out-of-band authority claim to one exact create/send payload.

    There is intentionally no CLI wrapper. A trusted host adapter must first
    obtain ``TransportClaim`` from a real user-authorized task or an official
    platform capability through ``PipelineIncidentStore``.
    """

    _require_desktop_owned(cfg)
    PipelineIncidentStore(cfg.state_dir).verify_transport_claim(claim)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        if claim.destination_task_id != session.get("task_id"):
            raise DesktopLifecycleError("transport claim targets another task")
        causal_owner = str(session.get("relay_owner_thread_id") or "")
        if (
            claim.actor_thread_id == causal_owner
            and claim.authority_kind is not AuthorityKind.AUTOPILOT_RUN
        ):
            raise DesktopLifecycleError(
                "causal task requires the durable Autopilot run authorization"
            )
        if (
            claim.actor_thread_id != causal_owner
            and claim.authority_kind is AuthorityKind.AUTOPILOT_RUN
        ):
            raise DesktopLifecycleError(
                "Autopilot run authorization is bound to the causal predecessor task"
            )
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        if claim.operation == "create_thread":
            payload = descriptor.create_thread_payload()
        elif claim.operation == "send_message_to_thread":
            thread_id = str(session.get("thread_id") or "")
            if not thread_id:
                raise DesktopLifecycleError(
                    "send transport cannot bind before create acknowledgement"
                )
            payload = descriptor.send_message_payload(
                thread_id=thread_id,
                host_id=str(session.get("host_id") or "") or None,
            )
        else:
            raise DesktopLifecycleError("transport claim operation is not allowlisted")
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if digest != claim.payload_sha256:
            raise DesktopLifecycleError("transport claim payload digest does not match")
        claims = session.setdefault("authorized_transport_claims", {})
        expected = {
            "reservation_id": claim.reservation_id,
            "actor_thread_id": claim.actor_thread_id,
            "authority_kind": claim.authority_kind.value,
            "authority_evidence_id": claim.authority_evidence_id,
            "payload_sha256": claim.payload_sha256,
        }
        existing = claims.get(claim.operation)
        if existing is not None and existing != expected:
            raise DesktopLifecycleError("a different transport authority is already bound")
        claims[claim.operation] = expected
        _append_event(
            state,
            "transport_authority_bound",
            session,
            at or utc_now(),
            detail=json.dumps(expected, sort_keys=True),
        )
        store.save(state)


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


def _active_session_by_thread(state: RunState, thread_id: str) -> dict[str, Any] | None:
    matches = [
        item
        for item in state.worker_sessions
        if item.get("thread_id") == thread_id and item.get("status") == "ACTIVE"
    ]
    if len(matches) > 1:
        raise DesktopLifecycleError("Desktop thread has multiple active reservations")
    return matches[0] if matches else None


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


def _record_app_server_create_failure(
    cfg: Config,
    reservation_token: str,
    *,
    reason: str,
    definitive: bool,
    at: str | None,
    now_epoch: int | None,
) -> dict[str, Any]:
    timestamp = at or utc_now()
    before = StateStore(cfg.state_dir).load()
    session = _session_by_token(before, reservation_token)
    descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
    stored_contract = session.get("app_server_creation_contract")
    contract = (
        dict(stored_contract)
        if isinstance(stored_contract, dict)
        else app_server_creation_contract(cfg, descriptor)
    )
    record_desktop_failure(
        cfg,
        reservation_token,
        reason=reason,
        definitive=definitive,
        now_epoch=now_epoch,
        at=timestamp,
        reserve_other_ready=False,
        relay_executor_thread_id=str(session.get("relay_owner_thread_id") or ""),
    )
    state = StateStore(cfg.state_dir).load()
    outcome = (
        SideEffectOutcome.KNOWN_FAILED if definitive else SideEffectOutcome.UNKNOWN
    )
    incident_store = PipelineIncidentStore(cfg.state_dir)
    incident = incident_store.open_incident(
        IncidentSignal(
            signal_id=(
                f"{state.run_id}:{reservation_token}:app_server_thread_start:"
                f"{outcome.value}"
            ),
            code=(
                "app_server_thread_start_failed"
                if definitive
                else "app_server_thread_start_ambiguous"
            ),
            surface=IncidentClass.PIPELINE,
            summary=(
                f"App Server thread/start failed for {session['task_id']}: {reason}"
            ),
            affected_task_ids=(str(session["task_id"]),),
            operation="create_thread",
            side_effect_outcome=outcome,
            system_state={
                "run_id": state.run_id,
                "reservation_token": reservation_token,
                "operation_id": str(session.get("operation_id") or ""),
                "task_id": str(session["task_id"]),
                "relay_owner_thread_id": str(
                    session.get("relay_owner_thread_id") or ""
                ),
                "relay_status": str(
                    _session_by_token(state, reservation_token).get("status") or ""
                ),
                "payload_sha256": hashlib.sha256(
                    json.dumps(
                        contract,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            },
            recent_events=tuple(
                dict(item)
                for item in state.lifecycle_journal
                if item.get("reservation_token") == reservation_token
            )[-20:],
        ),
        at=timestamp,
    )
    if definitive:
        return incident_store.ensure_pipeline_engineer(
            str(incident["incident_id"]), at=timestamp
        )
    incident_store.route_incident(str(incident["incident_id"]), at=timestamp)
    return incident_store.incident_package(str(incident["incident_id"]))


def _record_created_app_server_ambiguity(
    cfg: Config,
    reservation_token: str,
    *,
    thread_id: str,
    reason: str,
    at: str | None,
) -> None:
    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        if session.get("status") != "RELAYING":
            raise DesktopLifecycleError(
                "created App Server thread ambiguity no longer matches its reservation"
            )
        session["thread_id"] = thread_id
        session["status"] = "AMBIGUOUS"
        session["ambiguous_phase"] = "APP_SERVER_CREATED"
        session["failure_reason"] = reason
        _bind_resource_identity(state, reservation_token, thread_id=thread_id)
        _append_event(
            state,
            "app_server_created_thread_ambiguous",
            session,
            timestamp,
            detail=reason,
        )
        state.current_thread_id = thread_id
        state.status = "RUNNING"
        state.phase = "RECONCILE_AMBIGUOUS"
        state.last_error = reason
        store.save(state)


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


def _validate_pristine_desktop_slot(
    thread: dict[str, Any],
    *,
    descriptor: LaunchDescriptor,
    slot_turn_id: Any,
) -> str:
    """Attest the actual App Server history before any production side effect."""

    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise DesktopSlotHistoryError("Desktop slot history is unavailable")
    if _contains_exact_text(thread, descriptor.prompt):
        raise DesktopSlotHistoryError(
            "Desktop task contains the production prompt before cwd preparation"
        )
    if len(turns) != 1:
        raise DesktopSlotHistoryError(
            "Desktop task is not a pristine slot: expected exactly one turn before preparation"
        )
    if not isinstance(slot_turn_id, str) or not slot_turn_id:
        raise DesktopSlotHistoryError("Desktop slot acknowledgement has no turn identity")
    slot_turn = turns[0]
    if not isinstance(slot_turn, dict) or str(slot_turn.get("id") or "") != slot_turn_id:
        raise DesktopSlotHistoryError(
            "Desktop slot turn identity does not match the actual task history"
        )
    if slot_turn.get("status") != "completed":
        raise DesktopSlotHistoryError("Desktop slot turn is not completed")
    if not _text_matches_expected(
        final_agent_message(slot_turn), DESKTOP_SLOT_READY
    ):
        raise DesktopSlotHistoryError(
            f"Desktop slot history must end with exactly {DESKTOP_SLOT_READY}"
        )
    if not _contains_exact_text(slot_turn, descriptor.slot_prompt()):
        raise DesktopSlotHistoryError(
            "Desktop task was not created with the exact no-op slot prompt"
        )
    canonical = json.dumps(thread, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
