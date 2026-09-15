from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence

from . import lifecycle as lifecycle_runtime
from .appserver import AppServerClient  # sentinel: DevOps recovery must never construct it
from .config import (
    DESKTOP_OWNED_SURFACE,
    STATE_DIR_NAME,
    load_config,
)
from .pipeline_engineer import (
    IncidentClass,
    IncidentPhase,
    IncidentSignal,
    PipelineIncidentError,
    PipelineIncidentStore,
    SideEffectOutcome,
)
from .launch_gate import (
    LaunchVerdict,
    await_launch,
    launch_confirmed,
    launch_verdict,
    render_launch_checklist,
    render_launch_timeline,
)
from .lifecycle import (
    pending_descriptors,
    LaunchDescriptor,
    DesktopLifecycleError,
    complete_desktop_worker,
    pause_desktop_run,
    record_policy_rejected_create_transport,
    record_desktop_interrupt,
    recover_desktop_frontier_from_predecessor_stop,
    relayable_descriptors,
    relay_session_status,
    reserve_ready_frontier,
    retired_session_for_thread,
)
from .launch_registry import LaunchRegistry
from .hook_trust import HookPreflightError
from .pipeline_engineer import HealthcheckResult, IncidentPhase, PipelineIncidentStore
from .plan import load_plan
from .resources import ResourceLockCoordinator
from .run_state import StateStore, utc_now
from .thread_titles import SEPARATOR as TITLE_SEPARATOR
from .task_state import TaskState, transition_task


def find_project_root(start: Path) -> Path | None:
    current = start.expanduser().resolve()
    for candidate in (current, *current.parents):
        if (candidate / STATE_DIR_NAME / "config.toml").is_file():
            return candidate
    return None


def _revive_dead_relay_session(session: dict[str, Any]) -> bool:
    """Вернуть к запуску релей, умерший до создания ветки.

    Ветки нет - значит дублировать нечего. Прежде такая сессия запирала
    прогон навсегда: запуск отвечал `cannot spawn from 'RELAYING'`, а
    разобрать её не мог никто, потому что наблюдать со стороны App Server
    тоже нечего. Это не догадка о побочном эффекте, а утверждение о его
    отсутствии, проверенное по состоянию: есть thread_id - не трогаем.
    """

    if session.get("status") != "RELAYING" or str(session.get("thread_id") or ""):
        return False
    session["status"] = "CREATE_REQUESTED"
    session["automatic_dispatch_state"] = None
    session["automatic_dispatch_pid"] = None
    session["automatic_dispatch_connection_pid"] = None
    return True


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _runtime_environment() -> dict[str, str]:
    """Make detached Python module launches independent of the caller wrapper."""

    env = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1])
    inherited = str(env.get("PYTHONPATH") or "").strip()
    entries = [item for item in inherited.split(os.pathsep) if item]
    if source_root not in entries:
        entries.insert(0, source_root)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def arm(root: Path) -> None:
    cfg = load_config(root)
    store = StateStore(cfg.state_dir)
    state = store.load()
    if state.status == "RUNNING" and pid_alive(state.dispatcher_pid):
        raise RuntimeError(f"dispatcher is already running with pid {state.dispatcher_pid}")
    if state.status == "DONE":
        raise RuntimeError("the migrated or initialized roadmap is already DONE")
    payload = {
        "project_root": str(cfg.root),
        "armed_at": utc_now(),
        "run_id": state.run_id,
    }
    request_id = LaunchRegistry().add(payload)
    payload["request_id"] = request_id
    store.arm(payload)
    state.status = "READY"
    state.phase = "ARMED"
    store.save(state)


def spawn_automatic_app_server_relay(
    root: Path,
    *,
    reservation_token: str,
    initiator_thread_id: str,
    initiator_turn_id: str,
) -> int:
    """Start one hook-owned App Server turn without any chat relay."""

    cfg = load_config(root)
    if cfg.runtime.worker_surface != DESKTOP_OWNED_SURFACE:
        raise RuntimeError("automatic relay requires desktop_owned lifecycle state")
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        matches = [
            item
            for item in state.worker_sessions
            if item.get("reservation_token") == reservation_token
        ]
        if len(matches) != 1:
            raise RuntimeError("automatic relay reservation is missing or non-unique")
        session = matches[0]
        if session.get("relay_owner_thread_id") != initiator_thread_id:
            raise RuntimeError("automatic relay initiator is not the causal owner")
        if session.get("status") not in {"CREATE_REQUESTED", "PREPARED"}:
            existing_pid = session.get("automatic_dispatch_pid")
            if isinstance(existing_pid, int) and pid_alive(existing_pid):
                return existing_pid
            if not _revive_dead_relay_session(session):
                raise RuntimeError(
                    f"automatic relay cannot spawn from {session.get('status')!r}"
                )
        existing_state = session.get("automatic_dispatch_state")
        existing_pid = session.get("automatic_dispatch_pid")
        if existing_state in {"SCHEDULED", "RUNNING"}:
            if isinstance(existing_pid, int) and pid_alive(existing_pid):
                return existing_pid
            if existing_state == "SCHEDULED" and existing_pid is None:
                return 0
        session["automatic_dispatch_state"] = "SCHEDULED"
        session["automatic_dispatch_pid"] = None
        session["automatic_dispatch_scheduled_at"] = utc_now()
        store.save(state)

    log_dir = cfg.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / f"automatic-relay-{reservation_token}.log").open(
        "a", encoding="utf-8"
    )
    command = [
        sys.executable,
        "-m",
        "codex_autopilot.cli",
        "_relay_dispatch",
        "--project",
        str(cfg.root),
        "--token",
        reservation_token,
        "--initiator-thread",
        initiator_thread_id,
        "--initiator-turn",
        initiator_turn_id,
    ]
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=_runtime_environment(),
        )
    except Exception:
        log.close()
        with coordinator.transaction():
            state = store.load()
            session = next(
                item
                for item in state.worker_sessions
                if item.get("reservation_token") == reservation_token
            )
            session["automatic_dispatch_state"] = "SPAWN_FAILED"
            session["automatic_dispatch_pid"] = None
            store.save(state)
        raise
    log.close()
    with coordinator.transaction():
        state = store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item.get("reservation_token") == reservation_token
        )
        session["automatic_dispatch_state"] = "RUNNING"
        session["automatic_dispatch_pid"] = proc.pid
        store.save(state)
    return proc.pid


def _turn_is_completed(state: Any, thread_id: str, turn_id: str) -> bool:
    """Ход владельца завершён - по записи в журнале, а не по статусу сессии.

    Проверка статуса "COMPLETED" отсекала законного предшественника:
    задача, вернувшая PLAN_CHANGE_REQUEST, завершила свой ход и записала
    turn_completed, но её сессия остаётся в PLAN_CHANGE_REQUESTED. Из-за
    этого зарезервированный планировщик некому было поднять, и прогон
    вставал с ошибкой про отсутствующего причинного предшественника.
    """

    if any(
        str(item.get("event") or "") == "turn_completed"
        and str(item.get("thread_id") or "") == thread_id
        and str(item.get("turn_id") or "") == turn_id
        for item in state.lifecycle_journal
    ):
        return True
    # Журнальная запись - не единственное доказательство. Прогоны,
    # созданные до того, как дежурный инженер начал её писать, имеют
    # завершённый ход и не имеют события: цепочка вставала на
    # "automatic relay has no completed causal predecessor", а починить
    # это можно было только правкой журнала руками - то есть подделкой
    # записи о том, чего система не наблюдала. Закрытая сессия с тем же
    # ходом является таким же наблюдением, сделанным в своё время.
    if any(
        str(item.get("thread_id") or "") == thread_id
        and str(item.get("turn_id") or "") == turn_id
        and item.get("status") == "COMPLETED"
        for item in state.worker_sessions
    ):
        return True
    # Прерванный ход тоже кончился. Успехом он не кончился, и
    # turn_completed по нему не будет никогда - значит ждать его значит
    # ждать вечно. Замерено: реплэннер попросил разрешение, ход остался
    # прерванным, и преемника было некому поднять.
    return any(
        str(item.get("event") or "") == "interrupt_observed"
        and str(item.get("thread_id") or "") == thread_id
        and str(item.get("turn_id") or "") == turn_id
        for item in state.lifecycle_journal
    )


def _spawn_automatic_descriptors(
    cfg: Any,
    descriptors: tuple[LaunchDescriptor, ...],
    *,
    triggering_thread_id: str,
    triggering_turn_id: str,
) -> tuple[int, ...]:
    state = StateStore(cfg.state_dir).load()
    pids: list[int] = []
    for descriptor in descriptors:
        session = next(
            item
            for item in state.worker_sessions
            if item.get("reservation_token") == descriptor.reservation_token
        )
        owner = str(session.get("relay_owner_thread_id") or "")
        if owner == triggering_thread_id:
            owner_turn = triggering_turn_id
        else:
            predecessor = next(
                (
                    item
                    for item in reversed(state.worker_sessions)
                    if item.get("thread_id") == owner
                    and item.get("turn_id")
                    and _turn_is_completed(state, owner, str(item["turn_id"]))
                ),
                None,
            )
            if predecessor is None:
                raise RuntimeError("automatic relay has no completed causal predecessor")
            owner_turn = str(predecessor["turn_id"])
        pids.append(
            spawn_automatic_app_server_relay(
                cfg.root,
                reservation_token=descriptor.reservation_token,
                initiator_thread_id=owner,
                initiator_turn_id=owner_turn,
            )
        )
    return tuple(pids)


def reactivate_desktop_relay_owner(root: Path, *, incident_id: str | None = None) -> dict[str, Any]:
    """Re-arm the exact causal owner's automatic dispatcher after repair."""

    cfg = load_config(root)
    if cfg.runtime.worker_surface != DESKTOP_OWNED_SURFACE:
        raise RuntimeError("relay-owner reactivation requires desktop_owned mode")
    incident_store = PipelineIncidentStore(cfg.state_dir)
    incidents = [
        item
        for item in incident_store.load()["incidents"]
        if item.get("code") in {
            "transport_policy_rejected",
            "app_server_thread_start_failed",
        }
        and item.get("operation") == "create_thread"
        and (
            incident_id is None
            or item.get("incident_id") == incident_id
        )
        and item.get("phase")
        in {IncidentPhase.PIPELINE_ENGINEER.value, IncidentPhase.RESOLVED.value}
    ]
    if len(incidents) != 1:
        raise RuntimeError(
            "relay-owner reactivation requires one exact known-failed create incident"
        )
    incident = incidents[0]
    incident_id = str(incident["incident_id"])
    system_state = dict(incident.get("system_state") or {})
    state = StateStore(cfg.state_dir).load()
    failed_token = str(system_state.get("reservation_token") or "")
    failed_sessions = [
        item
        for item in state.worker_sessions
        if item.get("reservation_token") == failed_token
    ]
    if len(failed_sessions) != 1:
        raise RuntimeError("incident failed reservation is not unique")
    failed_session = failed_sessions[0]
    candidates = [
        item
        for item in state.worker_sessions
        if item.get("reservation_token") == failed_token
        and item.get("status") == "RETRY_WAIT"
        and item.get("relay_owner_thread_id")
        and state.task_states.get(str(item.get("task_id") or "")) == "RETRY_WAIT"
    ]
    relayable = [
        item
        for item in state.worker_sessions
        if item.get("task_id") == system_state.get("task_id")
        and item.get("relay_owner_thread_id")
        == system_state.get("relay_owner_thread_id")
        and item.get("status") == "CREATE_REQUESTED"
    ]
    reopened_exact_rearm = (
        incident.get("phase") == IncidentPhase.PIPELINE_ENGINEER.value
        and len(relayable) == 1
        and failed_session.get("status") == "RETRY_WAIT"
        and state.task_states.get(str(system_state.get("task_id") or "")) == "RUNNING"
        and state.active_task_ids == [system_state.get("task_id")]
    )
    if not candidates and not (
        (
            incident.get("phase") == IncidentPhase.RESOLVED.value
            and len(relayable) == 1
        )
        or reopened_exact_rearm
    ):
        raise RuntimeError("incident has no unique retry-wait relay to repair")
    if len(candidates) > 1 or len(relayable) > 1:
        raise RuntimeError("incident relay state is not unique")
    session = candidates[0] if candidates else relayable[0]
    owner_thread_id = str(session["relay_owner_thread_id"])
    task_id = str(session["task_id"])
    if (
        failed_session.get("task_id") != system_state.get("task_id")
        or failed_session.get("relay_owner_thread_id")
        != system_state.get("relay_owner_thread_id")
        or failed_session.get("thread_id")
        or incident.get("side_effect_outcome") != "KNOWN_FAILED"
        or failed_session.get("operation_id") != system_state.get("operation_id")
    ):
        raise RuntimeError("incident does not match the durable failed relay")
    failed_descriptor = LaunchDescriptor.from_dict(dict(failed_session["descriptor"]))
    if incident.get("code") == "app_server_thread_start_failed":
        failed_contract = failed_session.get("app_server_creation_contract")
        if not isinstance(failed_contract, dict):
            raise RuntimeError(
                "failed App Server reservation has no durable creation contract"
            )
        failed_contract = dict(failed_contract)
    else:
        failed_contract = failed_descriptor.create_thread_payload()
    failed_payload = json.dumps(
        failed_contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if hashlib.sha256(failed_payload.encode("utf-8")).hexdigest() != system_state.get(
        "payload_sha256"
    ):
        raise RuntimeError("incident payload hash does not match the failed relay")
    repaired_contract = lifecycle_runtime.app_server_creation_contract(
        cfg,
        failed_descriptor,
    )
    repaired_params = dict(repaired_contract.get("params") or {})
    repaired_root_precondition = repaired_contract.get("project_root_precondition")
    expected_root_precondition = (
        {
            "method": "project/update-if-missing",
            "params": {
                "projectId": cfg.desktop.project_id,
                "root": str(cfg.root),
            },
        }
        if cfg.desktop.project_id
        else None
    )
    if (
        repaired_contract.get("method") != "thread/start"
        or Path(str(repaired_params.get("cwd") or "")).resolve() != cfg.root
        or repaired_params.get("projectId") != cfg.desktop.project_id
        or repaired_root_precondition != expected_root_precondition
        or state.project_id != cfg.desktop.project_id
        or repaired_params.get("runtimeWorkspaceRoots") != [str(cfg.root)]
    ):
        raise RuntimeError(
            "repaired App Server creation contract does not match canonical project metadata"
        )
    active_or_pending = [
        item
        for item in state.worker_sessions
        if item.get("task_id") == task_id
        and item is not session
        and item.get("status")
        in {
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
    ]
    expected_active = [] if candidates else [task_id]
    if active_or_pending or state.active_task_ids != expected_active:
        raise RuntimeError("another production or relay session is active")
    retry_at = int(state.task_retry_at.get(task_id) or 0)
    now_epoch = int(time.time())
    if candidates and retry_at > now_epoch:
        raise RuntimeError(f"relay retry backoff is active until epoch {retry_at}")
    predecessor = next(
        (
            item
            for item in reversed(state.worker_sessions)
            if item.get("thread_id") == owner_thread_id
            and item.get("status") == "COMPLETED"
            and item.get("final_status") in {"ROTATE", "DONE"}
        ),
        None,
    )
    if predecessor is None:
        raise RuntimeError("durable relay owner is not a completed predecessor task")
    plan = load_plan(cfg.state_dir, cfg.profile)
    task = plan.task_map.get(task_id)
    if task is None:
        raise RuntimeError("incident destination task is absent from the current plan")
    role = plan.role_map.get(task.role)
    if role is None or role.name.casefold() == "legacy serial worker":
        raise RuntimeError("destination task requires a concrete role-based title")
    healthcheck = HealthcheckResult(
        name="known_failed_create_relay_repair_verified",
        passed=True,
        checks=(
            "incident_matches_failed_reservation",
            "create_side_effect_known_failed_and_unbound",
            (
                "failed_app_server_creation_contract_matches"
                if incident.get("code") == "app_server_thread_start_failed"
                else "rejected_legacy_desktop_create_contract_matches"
            ),
            "repaired_app_server_thread_start_contract_matches_canonical_project_state",
            "completed_causal_owner_matches",
            "no_active_or_duplicate_destination_session",
            "retry_backoff_elapsed",
            "concrete_role_title_available",
            "existing_run_and_exact_causal_reservation_verified",
            "paused_run_released_only_for_exact_predecessor_rearm",
        ),
        observed_at=utc_now(),
    )
    was_paused = StateStore(cfg.state_dir).pause_requested()
    try:
        if incident.get("phase") == IncidentPhase.PIPELINE_ENGINEER.value:
            incident_store.complete_pipeline_engineer(
                incident_id,
                success=True,
                at=healthcheck.observed_at,
                healthcheck=healthcheck,
                reason="Known-failed create was reconciled and the causal retry is safe to re-arm.",
            )
        if was_paused:
            StateStore(cfg.state_dir).clear_pause()
        if candidates:
            descriptors = reserve_ready_frontier(
                cfg,
                now_epoch=now_epoch,
                hook_gate=lambda _cfg: None,
                relay_owner_thread_id=owner_thread_id,
            )
            matching = [item for item in descriptors if item.task_id == task_id]
            if len(matching) != 1:
                raise RuntimeError("repair did not reserve exactly one causal retry")
            descriptor = matching[0]
        else:
            descriptor = next(
                item
                for item in relayable_descriptors(
                    cfg,
                    relay_owner_thread_id=owner_thread_id,
                )
                if item.task_id == task_id
            )
    except Exception as exc:
        if was_paused:
            StateStore(cfg.state_dir).request_pause()
        if incident_store.incident_package(incident_id)["incident"]["phase"] == IncidentPhase.RESOLVED.value:
            incident_store.invalidate_pipeline_engineer_resolution(
                incident_id,
                at=utc_now(),
                reason=f"relay re-arm precondition failed: {exc}",
            )
        raise
    if not descriptor.title.startswith(f"{role.name}{TITLE_SEPARATOR}"):
        raise RuntimeError("repaired relay descriptor is not role-based")
    predecessor_turn_id = str(predecessor.get("turn_id") or "")
    if not predecessor_turn_id:
        raise RuntimeError("completed causal owner has no authoritative turn id")
    pid = spawn_automatic_app_server_relay(
        cfg.root,
        reservation_token=descriptor.reservation_token,
        initiator_thread_id=owner_thread_id,
        initiator_turn_id=predecessor_turn_id,
    )
    # Починка девопса заканчивается тем же гейтом, что и обычный запуск.
    # Иначе "REARMED" означало бы только "процесс релея порождён" - ровно
    # то заявление вместо наблюдения, ради которого гейт и написан.
    checks = await_launch(cfg, task_ids=[task_id], timeout=15.0)
    confirmed = launch_confirmed(checks)
    if not confirmed:
        # Решение инженера не подтвердилось наблюдением: инцидент не
        # считается закрытым, иначе починка сертифицирует сама себя.
        package = incident_store.incident_package(incident_id)["incident"]
        if package["phase"] == IncidentPhase.RESOLVED.value:
            incident_store.invalidate_pipeline_engineer_resolution(
                incident_id,
                at=utc_now(),
                reason="re-armed relay did not pass the launch checklist",
            )
    return {
        "incident_id": incident_id,
        "owner_thread_id": owner_thread_id,
        "destination_task_id": task_id,
        "reservation_token": descriptor.reservation_token,
        "destination_title": descriptor.title,
        "status": "REARMED" if confirmed else "LAUNCH_NOT_CONFIRMED",
        "launch_confirmed": confirmed,
        "launch_checklist": render_launch_checklist(checks),
        "automatic_dispatch_pid": pid,
    }


def recreate_archived_desktop_retry(
    root: Path,
    *,
    reservation_token: str,
    archived_thread_id: str,
    predecessor_thread_id: str,
) -> dict[str, Any]:
    """Retire one explicitly archived attempt and restart it from its predecessor."""

    cfg = load_config(root)
    if cfg.runtime.worker_surface != DESKTOP_OWNED_SURFACE:
        raise RuntimeError("archived retry recreation requires desktop_owned mode")
    store = StateStore(cfg.state_dir)
    plan = load_plan(cfg.state_dir, cfg.profile)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        if state.status != "PAUSED" or not store.pause_requested():
            raise RuntimeError("archived retry recreation requires a paused run")
        matches = [
            item
            for item in state.worker_sessions
            if item.get("reservation_token") == reservation_token
        ]
        if len(matches) != 1:
            raise RuntimeError("archived retry reservation is missing or non-unique")
        session = matches[0]
        task_id = str(session.get("task_id") or "")
        if (
            session.get("status") != "RETRY_WAIT"
            or session.get("thread_id") != archived_thread_id
            or session.get("relay_owner_thread_id") != predecessor_thread_id
            or state.task_states.get(task_id) != TaskState.RETRY_WAIT.value
        ):
            raise RuntimeError(
                "archived retry does not match the exact failed task and causal owner"
            )
        dispatcher_pid = session.get("automatic_dispatch_pid")
        if isinstance(dispatcher_pid, int) and pid_alive(dispatcher_pid):
            raise RuntimeError("archived retry dispatcher is still running")
        if state.active_task_ids:
            raise RuntimeError("another production task is active")
        predecessor = next(
            (
                item
                for item in reversed(state.worker_sessions)
                if item.get("thread_id") == predecessor_thread_id
                and item.get("status") == "COMPLETED"
                and item.get("final_status") in {"ROTATE", "DONE"}
                and item.get("turn_id")
            ),
            None,
        )
        if predecessor is None:
            raise RuntimeError("archived retry has no completed causal predecessor")
        timestamp = utc_now()
        session["status"] = "RETIRED_USER_ARCHIVED_RECREATE"
        session["completed_at"] = timestamp
        session["automatic_dispatch_state"] = "RETIRED"
        session["automatic_dispatch_pid"] = None
        session["failure_reason"] = (
            "User explicitly archived this incorrect task and requested a fresh "
            "dispatcher-created attempt from the recorded predecessor."
        )
        state.task_states = transition_task(
            plan,
            state.task_states,
            task_id,
            TaskState.READY,
        )
        state.task_retry_at.pop(task_id, None)
        state.current_thread_id = None
        state.current_turn_id = None
        state.client_user_message_id = None
        state.status = "READY"
        state.phase = "PREPARING"
        state.last_error = None
        state.lifecycle_journal_sequence += 1
        state.lifecycle_journal.append(
            {
                "sequence": state.lifecycle_journal_sequence,
                "event": "user_archived_retry_retired",
                "operation_id": str(session.get("operation_id") or ""),
                "task_id": task_id,
                "attempt": int(session.get("attempt") or 0),
                "reservation_token": reservation_token,
                "thread_id": archived_thread_id,
                "relay_owner_thread_id": predecessor_thread_id,
                "turn_id": session.get("turn_id"),
                "client_user_message_id": str(
                    session.get("client_user_message_id") or ""
                ),
                "at": timestamp,
            }
        )
        predecessor_turn_id = str(predecessor["turn_id"])
        store.save(state)

    store.clear_pause()
    try:
        descriptors = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id=predecessor_thread_id,
            hook_gate=lambda _cfg: None,
        )
        matching = [item for item in descriptors if item.task_id == task_id]
        if len(matching) != 1:
            raise RuntimeError("recreation did not reserve exactly one fresh attempt")
        descriptor = matching[0]
        pid = spawn_automatic_app_server_relay(
            cfg.root,
            reservation_token=descriptor.reservation_token,
            initiator_thread_id=predecessor_thread_id,
            initiator_turn_id=predecessor_turn_id,
        )
    except Exception:
        store.request_pause()
        raise
    return {
        "retired_thread_id": archived_thread_id,
        "predecessor_thread_id": predecessor_thread_id,
        "destination_task_id": task_id,
        "reservation_token": descriptor.reservation_token,
        "destination_title": descriptor.title,
        "status": "RECREATED",
        "automatic_dispatch_pid": pid,
    }


POLICY_REJECTION_PREFIX = "This action was rejected due to unacceptable risk."


def _policy_rejection_detail(value: Any) -> str:
    texts: list[str] = []
    explicitly_failed = False
    if isinstance(value, str):
        texts.append(value)
        explicitly_failed = True
    elif isinstance(value, dict):
        explicitly_failed = bool(
            value.get("isError") is True
            or value.get("is_error") is True
            or value.get("error")
            or value.get("status") in {"error", "failed", "rejected"}
        )
        for key in ("error", "message", "detail"):
            if isinstance(value.get(key), str):
                texts.append(str(value[key]))
        content = value.get("content")
        if isinstance(content, list):
            texts.extend(
                str(item.get("text"))
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
    if not explicitly_failed:
        return ""
    detail = "\n".join(item.strip() for item in texts if item.strip())
    return detail[:2_000] if detail.startswith(POLICY_REJECTION_PREFIX) else ""


def handle_post_tool_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """Capture only the exact structured Codex App create policy rejection."""

    if payload.get("tool_name") != "mcp__codex_app__create_thread":
        return {}
    detail = _policy_rejection_detail(payload.get("tool_response"))
    if not detail:
        return {}
    root = find_project_root(Path(str(payload.get("cwd") or ".")))
    if not root:
        return {}
    cfg = load_config(root)
    package = record_policy_rejected_create_transport(
        cfg,
        relay_owner_thread_id=str(payload.get("session_id") or ""),
        turn_id=str(payload.get("turn_id") or ""),
        tool_input=dict(payload.get("tool_input") or {}),
        rejection_detail=detail,
    )
    incident_id = str(package["incident"]["incident_id"])
    return {
        "decision": "block",
        "reason": (
            f"Codex Autopilot recorded {incident_id}; the create side effect is "
            "known failed. Do not retry or create the destination task."
        ),
    }


def _launch_report(
    cfg: Config,
    task_ids: Sequence[str],
    *,
    started: str,
    timeout: float,
) -> dict[str, Any]:
    """Ответ хука о запуске: наблюдение вместо заявления.

    Прежние сообщения сообщали только pid порождённого процесса. Ход
    завершался, и если задача при этом не поднималась, об этом никто не
    узнавал. Теперь ход заканчивается чек-листом, а неподтверждённый
    запуск явно называется отказом.
    """

    checks = await_launch(cfg, task_ids=task_ids, timeout=timeout)
    verdict = launch_verdict(checks)
    headline = {
        LaunchVerdict.CONFIRMED: "ЗАПУСК ПОДТВЕРЖДЁН",
        LaunchVerdict.IN_PROGRESS: "ЗАПУСК ИДЁТ — отказов нет, часть шагов впереди",
        LaunchVerdict.FAILED: "ЗАПУСК ОТКАЗАЛ",
    }[verdict]
    # Лента шагов вместо снимка: по снимку нельзя понять, понадобилась ли
    # починка по дороге. Итог отдельной строкой сверху, чтобы вывод читался
    # с первой секунды.
    timeline = render_launch_timeline(StateStore(cfg.state_dir).load(), task_ids)
    report = f"{started}\n{headline}\n{timeline}"
    if verdict is not LaunchVerdict.FAILED:
        # Идущий запуск - не отказ. Создание ветки через App Server занимает
        # десятки секунд, а хук живёт тридцать: объявлять отказ по нехватке
        # времени значит плодить ложные тикеты.
        #
        # Ответ обязан быть "continue". Прежде здесь стоял блокирующий - ради
        # видимости отчёта, с обоснованием, что диспетчер поднят отдельным
        # процессом и от завершения этого хода не зависит. Это неверно:
        # диспетчер ждёт ровно устойчивого "completed" на инициирующем ходе,
        # а ход, чей Stop-хук ответил block, остаётся "interrupted" навсегда.
        #
        # Замерено на обоих прогонах. 0.7 отвечает continue, и её ход виден
        # сначала interrupted, затем completed - воркер стартует. 0.8 с
        # блокирующим ответом: владелец 01a097d7, ход 01a097e3 остался
        # interrupted, диспетчер ждал до таймаута, ветка не создалась ни
        # разу. Показ лестницы и запуск исключали друг друга.
        #
        # Отчёт при continue пользователю не виден - это цена, которую
        # платила и 0.7. Видимым остаётся тот случай, ради которого отчёт и
        # нужен: отказ. Ему блокировать уже нечего.
        return {"continue": True, "systemMessage": report}
    ticket = _open_launch_incident(cfg, task_ids, checks)
    return {
        "decision": "block",
        "reason": (
            report
            + "\n\n"
            + ticket
            + "\nНе чини запуск в этом ходе: починка пайплайна идёт по тикету, "
            "а не правками из этой сессии."
        ),
    }


def _open_launch_incident(
    cfg: Config,
    task_ids: Sequence[str],
    checks: Sequence[Any],
) -> str:
    """Завести тикет на неподтверждённый запуск и отдать его девопсу.

    Подпись инцидента нормализованная, поэтому повтор того же отказа
    опознаётся как повтор, а не как новая загадка.
    """

    store = PipelineIncidentStore(cfg.state_dir)
    failed = [item.id for item in checks if item.passed is not True]
    now = utc_now()
    signal = IncidentSignal(
        signal_id=f"launch-not-confirmed:{','.join(task_ids)}:{':'.join(sorted(set(failed)))}",
        code="launch_not_confirmed",
        surface=IncidentClass.PIPELINE,
        summary=(
            "Запуск не подтверждён чек-листом: "
            + ", ".join(sorted(set(failed)))
        ),
        affected_task_ids=tuple(task_ids),
        operation="create_thread",
        side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
        system_state={"failed_checks": sorted(set(failed))},
    )
    try:
        incident = store.open_incident(signal, at=now)
        incident_id = str(incident["incident_id"])
        phase = store.route_incident(incident_id, at=now)
        if phase is IncidentPhase.DEGRADED:
            # Уровень 1: способ уже выучен, модель не поднимается.
            phase = store.attempt_known_recovery(
                incident_id, at=now, owner_id="launch-gate"
            )
        if phase is IncidentPhase.AUTO_RECOVERY_FAILED:
            store.ensure_pipeline_engineer(incident_id, at=now)
            phase = IncidentPhase.PIPELINE_ENGINEER
    except PipelineIncidentError as error:
        return f"Тикет завести не удалось: {error}"
    # Владельца не выдумываем: автоматического исполнителя у тикета нет,
    # пока его не поднимет живой Pipeline Engineer.
    return (
        f"Тикет {incident_id} открыт (фаза {phase.value}). "
        "Автоматический исполнитель не поднят — тикет ждёт разбора."
    )


def _orphaned_pending_descriptors(cfg: Config) -> tuple[Any, ...]:
    """Резервации, под которые ветку так и не создали.

    Сессия остаётся в ожидании создания, а диспетчера у неё нет: процесс
    вышел, не подхватив преемника. Планировщик новых дескрипторов при этом
    не выдаёт - слот уже занят этой самой резервацией, - и прогон встаёт
    молча. Такие резервации надо поднимать заново, а не ждать.
    """

    state = StateStore(cfg.state_dir).load()
    live = {
        str(item.get("reservation_token"))
        for item in state.worker_sessions
        if pid_alive(item.get("automatic_dispatch_pid"))
    }
    return tuple(
        item
        for item in pending_descriptors(cfg)
        if item.reservation_token not in live
    )


def handle_stop_hook(payload: dict[str, Any]) -> dict[str, Any]:
    registry = LaunchRegistry()
    root = find_project_root(Path(str(payload.get("cwd") or ".")))
    if root:
        cfg = load_config(root)
        if _is_workspace_handoff_stop(cfg, payload):
            return {}
        # A live automatic dispatcher owns the authoritative
        # turn/completed event and the complete-to-successor transition.
        # The worker Stop hook is only an observer in the v0.7 transport;
        # consuming the same result here races the dispatcher and can
        # launch a duplicate successor.
        state = StateStore(cfg.state_dir).load()
        thread_id = str(payload.get("session_id") or "")
        automatic_owner = next(
            (
                item
                for item in state.worker_sessions
                if item.get("thread_id") == thread_id
                and item.get("status") == "ACTIVE"
                and item.get("automatic_dispatch_state") == "RUNNING"
                and isinstance(item.get("automatic_dispatch_pid"), int)
                and pid_alive(item.get("automatic_dispatch_pid"))
            ),
            None,
        )
        if automatic_owner is not None:
            return {}
        try:
            outcome = complete_desktop_worker(
                cfg,
                thread_id=str(payload.get("session_id") or ""),
                turn_id=str(payload.get("turn_id") or ""),
                final_message=str(payload.get("last_assistant_message") or ""),
                source_thread_id=(
                    str(payload.get("source_thread_id") or "").strip() or None
                ),
            )
        except (DesktopLifecycleError, HookPreflightError) as exc:
            return {"decision": "block", "reason": str(exc)}
        if outcome.matched:
            if outcome.descriptors:
                pids = _spawn_automatic_descriptors(
                    cfg,
                    outcome.descriptors,
                    triggering_thread_id=str(payload.get("session_id") or ""),
                    triggering_turn_id=str(payload.get("turn_id") or ""),
                )
                return _launch_report(
                    cfg,
                    [item.task_id for item in outcome.descriptors],
                    started=(
                        "Codex Autopilot automatic dispatcher started: "
                        + ", ".join(str(pid) for pid in pids)
                    ),
                    timeout=15.0,
                )
            return {}
        continuation = _desktop_relay_continuation(
            cfg,
            relay_owner_thread_id=str(payload.get("session_id") or ""),
            relay_owner_turn_id=str(payload.get("turn_id") or ""),
        )
        if continuation:
            return continuation
        try:
            recovered = recover_desktop_frontier_from_predecessor_stop(
                cfg,
                predecessor_thread_id=str(payload.get("session_id") or ""),
                stop_turn_id=str(payload.get("turn_id") or ""),
            )
        except (DesktopLifecycleError, HookPreflightError) as exc:
            return {"decision": "block", "reason": str(exc)}
        if recovered:
            pids = _spawn_automatic_descriptors(
                cfg,
                recovered,
                triggering_thread_id=str(payload.get("session_id") or ""),
                triggering_turn_id=str(payload.get("turn_id") or ""),
            )
            return _launch_report(
                cfg,
                [item.task_id for item in recovered],
                started=(
                    "Codex Autopilot automatic retry dispatcher started: "
                    + ", ".join(str(pid) for pid in pids)
                ),
                timeout=15.0,
            )
    store = StateStore(root / STATE_DIR_NAME) if root else None
    request = store.claim_launch() if store else None
    if request:
        registry.remove(str(request.get("request_id") or ""))
    else:
        request = registry.claim_unique(project_hint=root)
        if not request:
            return {}
        root = Path(str(request.get("project_root") or "")).expanduser().resolve()
        if not (root / STATE_DIR_NAME / "config.toml").is_file():
            raise RuntimeError(f"armed target is no longer an initialized Autopilot project: {root}")
        store = StateStore(root / STATE_DIR_NAME)
        local = store.claim_launch()
        if local and local.get("request_id") != request.get("request_id"):
            store.arm(local)
            raise RuntimeError("target project launch request did not match the initiating Stop hook")
    assert root is not None and store is not None
    state = store.load()
    if request.get("run_id") != state.run_id:
        raise RuntimeError("armed launch request belongs to an older Autopilot run")
    cfg = load_config(root)
    if state.status != "READY" or state.phase != "ARMED":
        # A supported manual resume may consume this initialized run before
        # the initiating turn ends. Never turn its stale Stop hook into a
        # second dispatcher.
        #
        # Но молча выбрасывать уже изъятый запрос нельзя: состояние могло
        # уйти вперёд в этом же ходе - например, в PLAN_CHANGE_DRAINING, -
        # и тогда возобновление исчезало без следа. Если при этом есть
        # резервация без живого диспетчера, поднимаем именно её.
        stalled = _orphaned_pending_descriptors(cfg)
        if not stalled:
            return {}
        pids = _spawn_automatic_descriptors(
            cfg,
            stalled,
            triggering_thread_id=str(payload.get("session_id") or ""),
            triggering_turn_id=str(payload.get("turn_id") or ""),
        )
        return _launch_report(
            cfg,
            [item.task_id for item in stalled],
            started=(
                "Codex Autopilot поднял зависшую резервацию: "
                + ", ".join(str(pid) for pid in pids)
            ),
            timeout=15.0,
        )
    try:
        descriptors = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id=str(payload.get("session_id") or ""),
        )
    except Exception:
        store.arm(request)
        registry.add(request)
        raise
    if not descriptors:
        # Резерв уже сделан раньше, а ветку под него никто не создал:
        # прежний диспетчер умер, не подхватив преемника. Тихий возврат
        # здесь и оставлял прогон стоять без единой записи в журнале.
        descriptors = _orphaned_pending_descriptors(cfg)
        if not descriptors:
            return {}
    pids = _spawn_automatic_descriptors(
        cfg,
        descriptors,
        triggering_thread_id=str(payload.get("session_id") or ""),
        triggering_turn_id=str(payload.get("turn_id") or ""),
    )
    return _launch_report(
        cfg,
        [item.task_id for item in descriptors],
        started=(
            "Codex Autopilot automatic dispatcher started: "
            + ", ".join(str(pid) for pid in pids)
        ),
        timeout=15.0,
    )


# Название продукта, записанное так, как его реально произносят. Диктовка
# по-русски неизбежно даёт кириллицу: управляющая фраза не должна зависеть
# от того, переключил ли говорящий раскладку в середине предложения.
PRODUCT_ALIASES = ("codex autopilot", "кодекс автопайлот", "кодекс автопилот")

# Знаки, которые речь и диктовка добавляют, не меняя смысла команды.
_STRIPPED_PUNCTUATION = ",.!?;:"


# Вводные слова, которые речь добавляет в начало, не меняя команды.
# Список намеренно короткий: сопоставление остаётся точным, иначе хук
# начнёт перехватывать обычные просьбы пользователя.
_LEADING_FILLERS = frozenset({"просто", "давай", "давайте", "пожалуйста", "just", "please"})


def _normalized_prompt(value: str) -> str:
    text = value.strip().lower()
    for mark in _STRIPPED_PUNCTUATION:
        text = text.replace(mark, " ")
    words = text.split()
    while words and words[0] in _LEADING_FILLERS:
        words.pop(0)
    return " ".join(words)


def _phrases(*templates: str) -> set[str]:
    """Развернуть шаблоны по всем написаниям названия продукта."""

    return {
        template.format(product=product)
        for template in templates
        for product in PRODUCT_ALIASES
    }


PAUSE_PROMPTS = _phrases(
    "pause {product}",
    "stop {product}",
    "приостанови {product}",
    "останови {product}",
)
RESUME_PROMPTS = _phrases(
    "resume {product}",
    "continue {product}",
    "возобнови {product}",
    "продолжи {product}",
    "продолжить {product}",
)
DETAILED_STATUS_PROMPTS = _phrases(
    "{product} status detail",
    "подробный статус {product}",
) | {
    "подробный статус",
    "статус подробно",
    "detailed status",
    "status detail",
}
STATUS_PROMPTS = _phrases(
    "{product} status",
    "what is {product} doing right now",
    "что сейчас делает {product}",
    "статус {product}",
) | DETAILED_STATUS_PROMPTS | {
    # Скилл обещает пользователю ровно одно слово: "спроси `статус`".
    # Развёрнутых форм хук знал четыре, а этой - ни одной, и обещанный
    # видимый путь не работал как написано. Совпадение идёт по всему
    # вводу целиком, поэтому одинокое слово - это намерение, а не
    # случайное попадание внутрь фразы.
    "статус",
    "status",
    "статус автопилота",
}
UNINSTALL_PROMPTS = _phrases(
    "uninstall {product}",
    "remove {product}",
    "удали {product}",
)


def _retired_task_fence(payload: dict[str, Any]) -> dict[str, Any]:
    """Заслон до побочного эффекта: в отставленную задачу писать нечего.

    M11-PRE-SIDE-EFFECT-FENCE. Отставленная сессия уже падала закрыто -
    но на завершении хода, то есть после того, как модель отработала
    воркером по резервации, которой нет. Замерено на прогоне M11: у
    исходников менялись mtime, пока рядом шла замена той же задачи.

    Здесь отказ наступает на UserPromptSubmit, до единого вызова модели
    или инструмента. Это единственный ответ хука, который пользователю
    видно, поэтому он же и объясняет, куда идти.

    Провал чтения состояния - не отказ. Заслон знает про конкретную
    отставленную ветку; если состояние нечитаемо, знания нет, и глушить
    из-за этого всю переписку в проекте было бы хуже болезни.
    """

    thread_id = str(payload.get("session_id") or "")
    if not thread_id:
        return {}
    root = find_project_root(Path(str(payload.get("cwd") or ".")))
    if not root:
        return {}
    try:
        state = StateStore(root / STATE_DIR_NAME).load()
        retired = retired_session_for_thread(state, thread_id)
    except Exception:
        return {}
    if retired is None:
        return {}
    task_id = str(retired.get("task_id") or "?")
    reason = str(
        retired.get("retired_reason") or retired.get("failure_reason") or ""
    ).strip()
    detail = f" Причина отставки: {reason}" if reason else ""
    return {
        "decision": "block",
        "reason": (
            f"Эта задача отставлена ({retired.get('status')}) и больше не "
            f"ведёт работу по {task_id}. Продолжать в ней нельзя: её "
            f"резервации у пайплайна уже нет, и всё сделанное здесь пойдёт "
            f"мимо прогона.{detail} Скажи «статус», чтобы увидеть, какая "
            "задача сейчас действующая."
        ),
    }


def _reconcile_before_resume(cfg) -> tuple[str, ...]:
    """Вернуть в работу задачи, чьи воркеры уже не живут.

    reconcile_desktop_runtime написан ровно для этого и вызывался
    только из тестов - наблюдений, без которых он ничего не делает,
    в продакшене не производил никто. Поэтому сессия, чей ход
    закончился без разбираемого ответа, оставалась ACTIVE навсегда, а
    задача - в VERIFYING, и возобновление её не подхватывало.

    Наблюдение спрашивается у сервера. Молчание и обрыв связи дают
    "unknown", и такая сессия удерживается: "не знаю" не читается как
    "закончилось".
    """

    from .lifecycle import observe_worker_states, reconcile_desktop_runtime

    observations = observe_worker_states(cfg)
    if not observations:
        return ()
    result = reconcile_desktop_runtime(cfg, authoritative_states=observations)
    return tuple(result.retried_task_ids)


def _answer_escalation(cfg, state) -> tuple[str, ...]:
    """Возобновление - это и есть ответ пользователя на эскалацию.

    R13 разрешает обращение к пользователю как исключение, но обращение
    без обратного пути - тупик, а не исключение. Инженер объявлял
    ESCALATE_TO_USER, прогон уходил в BLOCKED, и возобновление
    отказывало ровно потому, что прогон в BLOCKED. Человеку, который
    уже всё починил, сказать об этом было нечем.

    Закрываются только эскалированные тикеты. BLOCKED по любой другой
    причине остаётся отказом: "продолжи" не должно быть кнопкой,
    стирающей неразобранную поломку.
    """


    # Фаза прогона авторитетом здесь не является. Её выставляет только
    # завершение инженера; инцидент, эскалированный маршрутизацией - как
    # любой AMBIGUOUS_SIDE_EFFECT, - оставлял прогон в его прежней фазе, и
    # возобновление молча ничего не закрывало. Тикет ждал человека,
    # человек отвечал, и ответ пропадал. Авторитет - само хранилище
    # инцидентов: закрываются ровно те тикеты, что ждут пользователя.
    store = PipelineIncidentStore(cfg.state_dir)
    closed: list[str] = []
    for incident_id in store.incident_ids_awaiting_the_user():
        store.resolve_escalation_by_user(
            incident_id,
            at=utc_now(),
            note="пользователь возобновил прогон, ответив на эскалацию",
        )
        closed.append(incident_id)
    return tuple(closed)


def handle_prompt_hook(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = _normalized_prompt(str(payload.get("prompt") or ""))
    if prompt not in PAUSE_PROMPTS | RESUME_PROMPTS | STATUS_PROMPTS | UNINSTALL_PROMPTS:
        # Управляющие фразы проходят и из отставленной ветки: они про
        # прогон, а не про задачу, и до модели не доходят вовсе.
        return _retired_task_fence(payload)
    root = find_project_root(Path(str(payload.get("cwd") or ".")))
    if prompt in UNINSTALL_PROMPTS:
        command = [sys.executable, "-m", "codex_autopilot.cli", "uninstall", "--yes"]
        if root:
            command.extend(["--project", str(root)])
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        return {"decision": "block", "reason": "Codex Autopilot uninstall started. Project source and project state are preserved."}
    if not root:
        return {"decision": "block", "reason": "No initialized Codex Autopilot project was found from this working directory."}
    store = StateStore(root / STATE_DIR_NAME)
    state = store.load()
    if prompt in PAUSE_PROMPTS:
        pause_desktop_run(load_config(root))
        return {"decision": "block", "reason": "Pause requested. No new Desktop task will launch. Active tasks use deterministic drain semantics and retain their locks until authoritative Stop or Interrupt."}
    if prompt in RESUME_PROMPTS:
        if state.status == "DONE":
            return {"decision": "block", "reason": "Codex Autopilot is already DONE."}
        cfg = load_config(root)
        answered_ids: tuple[str, ...] = ()
        if state.status == "BLOCKED":
            answered_ids = _answer_escalation(cfg, state)
            if not answered_ids:
                return {
                    "decision": "block",
                    "reason": (
                        f"Codex Autopilot is BLOCKED: "
                        f"{state.last_error or 'review BLOCKED.json'}"
                    ),
                }
            state.last_error = None
        store.clear_pause()
        recovered = _reconcile_before_resume(cfg)
        state = store.load()
        request = {
            "project_root": str(cfg.root),
            "armed_at": utc_now(),
            "run_id": state.run_id,
        }
        request_id = LaunchRegistry().add(request)
        request["request_id"] = request_id
        store.arm(request)
        state.status = "READY"
        state.phase = "ARMED"
        store.save(state)
        # Do not block this user-authorized turn. Its exact Stop event binds
        # the causal owner and launches the automatic dispatcher.
        note = "Codex Autopilot resume is armed for this turn's Stop hook."
        if answered_ids:
            note += f" Escalation closed by the user: {', '.join(answered_ids)}."
        if recovered:
            note += f" Returned to retry after a dead worker: {', '.join(recovered)}."
        return {"systemMessage": note}
    # Короткий ответ по умолчанию: текст хука приходит пользователю одним
    # куском, и полный отчёт в переписке читается как стена.
    return {
        "decision": "block",
        "reason": status_text(root, detailed=prompt in DETAILED_STATUS_PROMPTS),
    }


def handle_interrupt_hook(payload: dict[str, Any]) -> dict[str, Any]:
    root = find_project_root(Path(str(payload.get("cwd") or ".")))
    if not root:
        return {}
    cfg = load_config(root)
    record_desktop_interrupt(
        cfg,
        thread_id=str(payload.get("session_id") or ""),
        turn_id=str(payload.get("turn_id") or ""),
    )
    return {}


def _is_workspace_handoff_stop(cfg: Any, payload: dict[str, Any]) -> bool:
    thread_id = str(payload.get("session_id") or "")
    turn_id = str(payload.get("turn_id") or "")
    if not thread_id or not turn_id:
        return False
    state = StateStore(cfg.state_dir).load()
    return any(
        item.get("status") == "PREPARING"
        and item.get("thread_id") == thread_id
        and item.get("prep_turn_id") == turn_id
        for item in state.worker_sessions
    )


def _desktop_relay_continuation(
    cfg: Any,
    *,
    relay_owner_thread_id: str,
    relay_owner_turn_id: str,
) -> str:
    """Поднять уже зарезервированный релей и отчитаться лентой."""

    if not relay_owner_thread_id or not relay_owner_turn_id:
        return {}
    descriptors = relayable_descriptors(
        cfg,
        relay_owner_thread_id=relay_owner_thread_id,
    )
    launchable = tuple(
        item
        for item in descriptors
        if relay_session_status(cfg, item.reservation_token)["status"]
        in {"CREATE_REQUESTED", "PREPARED"}
    )
    if not launchable:
        return {}
    pids = _spawn_automatic_descriptors(
        cfg,
        launchable,
        triggering_thread_id=relay_owner_thread_id,
        triggering_turn_id=relay_owner_turn_id,
    )
    # Отчёт лентой, как на всех остальных путях запуска: голое "диспетчер
    # запущен, pid такой-то" - это заявление, а не наблюдение.
    return _launch_report(
        cfg,
        [item.task_id for item in launchable],
        started=(
            "Codex Autopilot automatic dispatcher started: "
            + ", ".join(str(pid) for pid in pids)
        ),
        timeout=15.0,
    )


def status_text(root: Path, *, detailed: bool = True) -> str:
    cfg = load_config(root)
    state = StateStore(cfg.state_dir).load()
    from .status import render_project_status, render_short_status
    plan = load_plan(cfg.state_dir, cfg.profile)
    running = pid_alive(state.dispatcher_pid)
    if not detailed:
        return render_short_status(cfg, state, plan, dispatcher_running=running)
    summary = render_project_status(
        cfg,
        state,
        plan,
        dispatcher_running=running,
    )
    # Лента шагов прямо в чате: хук не умеет дописывать строки по ходу
    # дела, но по запросу может показать, где сейчас находится задача и
    # что уже чинилось. Иначе за этим пришлось бы идти в терминал.
    active = list(state.active_task_ids or ())
    if not active:
        return summary
    timeline = render_launch_timeline(state, active)
    return f"{summary}\n\nШаги активных задач:\n{timeline}"
