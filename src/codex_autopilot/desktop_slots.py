"""Выведенный из эксплуатации Desktop-slot relay.

НЕ НА ПРОДАКШЕН-ПУТИ. Живой путь создания задачи -
reserve_ready_frontier -> create_desktop_thread_via_app_server ->
run_automatic_app_server_turn -> complete_desktop_worker, и он в lifecycle.py.

Здесь лежит прежняя схема, где задача сначала создавалась как пустой
Desktop-слот, потом готовился её cwd, потом отправлялся production-промпт.
Она появилась из попытки переиспользовать созданные в Desktop треды
и дала отказы "already has an active writer". CLI ничего отсюда
не импортирует; модуль держится только тестами.

Зависимость односторонняя: этот модуль импортирует lifecycle,
lifecycle его не импортирует. Удаление модуля целиком - отдельный шаг,
он снимает около семидесяти тестовых ссылок.
"""

from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable

from .appserver import AppServerClient, final_agent_message
from .config import Config
from .lifecycle import (
    DESKTOP_SLOT_READY,
    PENDING_SESSION_STATUSES,
    DesktopLifecycleError,
    DesktopSlotHistoryError,
    LaunchDescriptor,
    _append_event,
    _bind_resource_identity,
    _client_process_exited,
    _client_process_pid,
    _contains_exact_text,
    _finish_global_state,
    _materialize,
    _pid_alive,
    _prepare_state,
    _process_id_alive,
    _require_desktop_owned,
    _require_relay_executor,
    _session_by_token,
    _session_kind,
    _stable_text_id,
    _text_matches_expected,
    _thread_cwd,
    relay_success_report,
    reserve_ready_frontier,
)
from .orchestrator import WORKSPACE_HANDOFF_OK, WORKSPACE_HANDOFF_PROMPT
from .plan import load_plan
from .resilience import (
    append_resilience_event,
    reconcile_running_work,
    recover_plan_change_transaction,
)
from .resources import ResourceLockCoordinator, release_resources_in_state
from .run_state import StateStore, utc_now
from .task_state import TaskState, transition_task


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


def pending_descriptors(cfg: Config) -> tuple[LaunchDescriptor, ...]:
    state = StateStore(cfg.state_dir).load()
    return tuple(
        LaunchDescriptor.from_dict(dict(item["descriptor"]))
        for item in state.worker_sessions
        if item.get("status") in PENDING_SESSION_STATUSES
        and isinstance(item.get("descriptor"), dict)
    )


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
