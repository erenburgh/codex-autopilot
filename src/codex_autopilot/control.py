from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Sequence

from .appserver import AppServerClient  # sentinel: DevOps recovery must never construct it
from .config import (
    Config,
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
    LaunchCheck,
    LaunchVerdict,
    await_launch,
    launch_verdict,
    render_launch_checklist,
    render_launch_timeline,
)
from .lifecycle import (
    app_server_creation_contract,
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
from .hook_trust import HookPreflightError, require_trusted_stop_hook_for_config
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
    """Bring back to launch a relay that died before the thread was created.

    No thread means nothing to duplicate. Such a session used to lock the run
    forever: the launch answered `cannot spawn from 'RELAYING'`, and nobody
    could sort it out, because there was nothing to observe on the App Server
    side either. This is not a guess about a side effect but a statement of
    its absence, checked against the state: if there is a thread_id, we do
    not touch it.
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



def _register_for_wake(root: Path) -> None:
    from .wake import register_project

    try:
        register_project(root)
    except Exception:  # noqa: BLE001 - the sweep registry may not fail the launch
        return


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
    # The project joins the wake agent's sweep: from now on a due retry is
    # raised even after a reboot.
    _register_for_wake(cfg.root)
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
    """The owner's turn is complete - by the journal record, not the session status.

    Checking for the status "COMPLETED" cut off a legitimate predecessor: a
    task that returned PLAN_CHANGE_REQUEST finished its turn and recorded
    turn_completed, but its session stays in PLAN_CHANGE_REQUESTED. Because
    of that, nobody could raise the reserved planner, and the run stopped with
    an error about a missing causal predecessor.
    """

    if any(
        str(item.get("event") or "") == "turn_completed"
        and str(item.get("thread_id") or "") == thread_id
        and str(item.get("turn_id") or "") == turn_id
        for item in state.lifecycle_journal
    ):
        return True
    # The journal record is not the only proof. Runs created before the
    # on-call engineer started writing it have a completed turn and no event:
    # the chain stopped on "automatic relay has no completed causal
    # predecessor", and the only fix was editing the journal by hand - that
    # is, forging a record of something the system never observed. A closed
    # session with the same turn is an equal observation, made in its time.
    if any(
        str(item.get("thread_id") or "") == thread_id
        and str(item.get("turn_id") or "") == turn_id
        and item.get("status") == "COMPLETED"
        for item in state.worker_sessions
    ):
        return True
    # An interrupted turn has ended too. It did not end in success, and no
    # turn_completed will ever come for it - so waiting for one means waiting
    # forever. Measured: the replanner asked for a permission, the turn stayed
    # interrupted, and nobody was left to raise the successor.
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
    # Hook trust is never bypassed. This is a fresh CLI process that arms a
    # NEW dispatcher, not a step inside an already-gated operation, and it
    # reached reserve_ready_frontier with the gate switched off - so the one
    # boundary the owner says is absolute was open on the repair path. It
    # is also the honest answer to the engineer: a re-armed relay is
    # executed by the Stop hook, so arming one while the hook is not
    # trusted would promise a launch that can never happen.
    require_trusted_stop_hook_for_config(cfg)
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
    repaired_contract = app_server_creation_contract(cfg, failed_descriptor)
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
                # The repair names itself. A resolution with no named action is
                # refused (engineer_authority.REPAIR_ACTIONS), and this call
                # arrived from a line that never had to pass that gate: the
                # command closed the ticket wordlessly, so it raised in the one
                # phase it exists for - the ticket still held by the engineer.
                actions=("rearm_relay_owner",),
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
    # A DevOps repair ends with the same gate as an ordinary launch.
    # Otherwise "REARMED" would mean only "the relay process was spawned" -
    # exactly the claim-instead-of-observation the gate was written against.
    checks = await_launch(cfg, task_ids=[task_id], timeout=15.0)
    settled = _settle_rearmed_launch(incident_store, incident_id, checks, at=utc_now())
    return {
        "incident_id": incident_id,
        "owner_thread_id": owner_thread_id,
        "destination_task_id": task_id,
        "reservation_token": descriptor.reservation_token,
        "destination_title": descriptor.title,
        **settled,
        "launch_checklist": render_launch_checklist(checks),
        "automatic_dispatch_pid": pid,
    }


def _settle_rearmed_launch(
    incident_store: PipelineIncidentStore,
    incident_id: str,
    checks: Sequence[LaunchCheck],
    *,
    at: str,
) -> dict[str, Any]:
    """What the re-arm says about the launch - in three words, not two.

    The tail used to read the boolean ``launch_confirmed`` ("every item
    True") and on False invalidated the engineer's resolution. But a re-arm
    arms the run so that the predecessor executes the turn on its NEXT
    Stop: by construction there can be no thread inside the waiting window.
    Measured on a constructed "just re-armed" state: the three-valued
    verdict answers IN_PROGRESS, the boolean answers False. A gate that
    cannot pass inside its own window cancelled every repair.

    One gate had two consumers with different semantics: the Stop hook
    (:802) told three verdicts apart, the re-arm did not. Now the decision
    is one:

    - FAILED - a deciding item is broken; the engineer's resolution was not
      confirmed by observation, the incident opens again, otherwise the
      repair would certify itself;
    - IN_PROGRESS - no refusals, some steps still ahead; neither invalidate
      nor declare confirmed. R26: exactly what is not yet observable is
      named in ``pending_checks``, not swallowed;
    - CONFIRMED - confirmed.
    """

    verdict = launch_verdict(checks)
    if verdict is LaunchVerdict.FAILED:
        package = incident_store.incident_package(incident_id)["incident"]
        if package["phase"] == IncidentPhase.RESOLVED.value:
            incident_store.invalidate_pipeline_engineer_resolution(
                incident_id,
                at=at,
                reason="re-armed relay failed the launch checklist",
            )
    status = {
        LaunchVerdict.CONFIRMED: "REARMED",
        LaunchVerdict.IN_PROGRESS: "LAUNCH_IN_PROGRESS",
        LaunchVerdict.FAILED: "LAUNCH_NOT_CONFIRMED",
    }[verdict]
    return {
        "status": status,
        "launch_verdict": verdict.value,
        "launch_confirmed": verdict is LaunchVerdict.CONFIRMED,
        "pending_checks": [item.id for item in checks if item.passed is not True],
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
    # The same boundary as the re-arm above: a fresh CLI process that
    # reserves production work and spawns a relay of its own.
    require_trusted_stop_hook_for_config(cfg)
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
    """The hook's launch reply: an observation instead of a claim.

    The old messages reported only the pid of the spawned process. The turn
    ended, and if the task did not come up, nobody learned about it. Now the
    turn ends with a checklist, and an unconfirmed launch is called a failure
    explicitly.
    """

    checks = await_launch(cfg, task_ids=task_ids, timeout=timeout)
    verdict = launch_verdict(checks)
    headline = {
        LaunchVerdict.CONFIRMED: "LAUNCH CONFIRMED",
        LaunchVerdict.IN_PROGRESS: "LAUNCH IN PROGRESS — no failures, some steps ahead",
        LaunchVerdict.FAILED: "LAUNCH FAILED",
    }[verdict]
    # A timeline of steps instead of a snapshot: a snapshot cannot show
    # whether a repair was needed along the way. The verdict on its own line
    # at the top, so the output reads from the first second.
    timeline = render_launch_timeline(StateStore(cfg.state_dir).load(), task_ids)
    report = f"{started}\n{headline}\n{timeline}"
    if verdict is not LaunchVerdict.FAILED:
        # A launch in progress is not a failure. Creating a thread through App
        # Server takes tens of seconds, and the hook lives for thirty: declaring
        # a failure for lack of time would breed false tickets.
        #
        # The answer must be "continue". A blocking one stood here before - for
        # the sake of a visible report, justified by the dispatcher being a
        # separate process that does not depend on this turn ending. That is
        # wrong: the dispatcher waits precisely for a stable "completed" on the
        # initiating turn, and a turn whose Stop hook answered block stays
        # "interrupted" forever.
        #
        # Measured on both runs. 0.7 answers continue, and its turn is seen
        # first interrupted, then completed - the worker starts. 0.8 with the
        # blocking answer: owner 01a097d7, turn 01a097e3 stayed interrupted,
        # the dispatcher waited until the timeout, the thread was never created.
        # Showing the ladder and launching excluded each other.
        #
        # The report on continue is invisible to the user - the price 0.7 paid
        # too. What stays visible is the case the report exists for: a failure.
        # It has nothing left to block.
        return {"continue": True, "systemMessage": report}
    ticket = _open_launch_incident(cfg, task_ids, checks)
    return {
        "decision": "block",
        "reason": (
            report
            + "\n\n"
            + ticket
            + "\nDo not repair the launch in this turn: the pipeline is repaired through "
            "its ticket, not by edits from this session."
        ),
    }


def _open_launch_incident(
    cfg: Config,
    task_ids: Sequence[str],
    checks: Sequence[Any],
) -> str:
    """Open a ticket for an unconfirmed launch and hand it to DevOps.

    The incident signature is normalized, so a repeat of the same failure is
    recognized as a repeat, not as a new mystery.
    """

    store = PipelineIncidentStore(cfg.state_dir)
    failed = [item.id for item in checks if item.passed is not True]
    now = utc_now()
    signal = IncidentSignal(
        signal_id=f"launch-not-confirmed:{','.join(task_ids)}:{':'.join(sorted(set(failed)))}",
        code="launch_not_confirmed",
        surface=IncidentClass.PIPELINE,
        summary=(
            "Launch not confirmed by the checklist: "
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
            # Level 1: the repair is already learned, no model is raised.
            phase = store.attempt_known_recovery(
                incident_id, at=now, owner_id="launch-gate"
            )
        if phase is IncidentPhase.AUTO_RECOVERY_FAILED:
            store.ensure_pipeline_engineer(incident_id, at=now)
            phase = IncidentPhase.PIPELINE_ENGINEER
    except PipelineIncidentError as error:
        return f"The ticket could not be opened: {error}"
    # No owner is invented: no automatic executor until a live engineer raises one.
    return (
        f"Ticket {incident_id} opened (phase {phase.value}). "
        "No automatic executor was raised — the ticket awaits triage."
    )


def _reservations_without_a_live_dispatcher(cfg: Config) -> tuple[Any, ...]:
    """Reservations for which the thread was never created.

    The session stays waiting for creation, and it has no dispatcher: the
    process exited without picking up the successor. The scheduler issues no
    new descriptors meanwhile - the slot is taken by this very reservation -
    and the run stops silently. Such reservations must be raised again, not
    waited for.
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



def _ensure_wake_from_hook(cfg: Any, *, owner: str, owner_turn: str) -> None:
    """A wake-up from the Stop hook: a courtesy, not a contract; it never fails the hook."""

    from .wake import ensure_wake

    if not owner or not owner_turn:
        return
    try:
        ensure_wake(cfg, owner=owner, owner_turn=owner_turn)
    except Exception as exc:  # noqa: BLE001 - the hook must answer Codex no matter what
        # Silence is not an option: a wake-up that did not arm is a run that
        # waits for a human again. The hook is not failed, but a trace is left.
        _note_wake_failure(cfg, exc)
        return


def _note_wake_failure(cfg: Any, exc: BaseException) -> None:
    try:
        log_dir = cfg.state_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "wake-errors.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_now()} wake scheduling failed: {exc!r}\n")
    except Exception:  # noqa: BLE001 - writing the trace may not fail the hook itself
        return


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
            # The turn ended, there are no successors. If something waits for
            # a due retry, nobody will be left to raise it - the hook is the
            # last live process. It leaves a wake-up behind.
            _ensure_wake_from_hook(
                cfg,
                owner=str(payload.get("session_id") or ""),
                owner_turn=str(payload.get("turn_id") or ""),
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
        # But an already claimed request must not be dropped silently: the
        # state may have moved on within this turn - into PLAN_CHANGE_DRAINING,
        # say - and the resume vanished without a trace. If a reservation
        # without a live dispatcher exists, that is the one we raise.
        stalled = _reservations_without_a_live_dispatcher(cfg)
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
                "Codex Autopilot revived a stalled reservation: "
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
        # The reservation was made earlier and nobody created a thread for
        # it: the previous dispatcher died without picking up the successor.
        # A silent return here is what left the run standing with not one
        # journal record.
        descriptors = _reservations_without_a_live_dispatcher(cfg)
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


from .control_phrases import (  # noqa: F401  (re-exported: the hook and the tests import them from here)
    DETAILED_STATUS_PROMPTS,
    PAUSE_PROMPTS,
    PRODUCT_ALIASES,
    RESUME_PROMPTS,
    STATUS_PROMPTS,
    UNINSTALL_PROMPTS,
    _normalized_prompt,
)


def _retired_task_fence(payload: dict[str, Any]) -> dict[str, Any]:
    """A fence before the side effect: there is nothing to write into a retired task.

    M11-PRE-SIDE-EFFECT-FENCE. A retired session already failed closed - but
    at the end of the turn, that is after the model had worked as a worker on
    a reservation that no longer exists. Measured on the M11 run: source
    mtimes changed while a replacement of the same task ran next to it.

    Here the refusal happens on UserPromptSubmit, before a single model or
    tool call. It is the only hook reply the user can see, so it is also the
    one that explains where to go.

    A failure to read the state is not a refusal. The fence knows about a
    specific retired thread; if the state is unreadable there is no such
    knowledge, and silencing every conversation in the project because of it
    would be worse than the disease.
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
    detail = f" Reason for retirement: {reason}" if reason else ""
    return {
        "decision": "block",
        "reason": (
            f"This task has been retired ({retired.get('status')}) and no longer "
            f"carries the work on {task_id}. Continuing here is not possible: the "
            f"pipeline no longer holds its reservation, and anything done here "
            f"would bypass the run.{detail} Say «status» to see which task is "
            "the active one."
        ),
    }


def _reconcile_before_resume(cfg) -> tuple[str, ...]:
    """Return to work the tasks whose workers are no longer alive.

    reconcile_desktop_runtime was written for exactly this and was called
    only from tests - the observations without which it does nothing were
    produced by nobody in production. So a session whose turn ended without a
    parseable reply stayed ACTIVE forever, the task stayed in VERIFYING, and
    resuming did not pick it up.

    The observation is asked of the server. Silence and a dropped connection
    yield "unknown", and such a session is retained: "I do not know" is not
    read as "it ended".
    """

    from .lifecycle import observe_worker_states, reconcile_desktop_runtime

    observations = observe_worker_states(cfg)
    if not observations:
        return ()
    result = reconcile_desktop_runtime(cfg, authoritative_states=observations)
    return tuple(result.retried_task_ids)


def _answer_escalation(cfg, state) -> tuple[str, ...]:
    """Resuming is the user's answer to an escalation.

    R13 allows turning to the user as an exception, but an appeal with no
    way back is a dead end, not an exception. The engineer declared
    ESCALATE_TO_USER, the run went to BLOCKED, and resuming refused precisely
    because the run was BLOCKED. A person who had already fixed everything
    had no way to say so.

    The tickets closed are those waiting for a human: the ones that declared
    ESCALATE_TO_USER and the ones stuck in PIPELINE_ENGINEER - the second had
    nobody left to close it, neither the engineer nor the human, and it is
    exactly those that unblocked the run on 16 Sep (the authority is
    incident_ids_awaiting_the_user). BLOCKED for any other reason remains a
    refusal: "resume" must not be a button that erases an unexamined fault.
    """


    # The run phase is not the authority here. Only the engineer's completion
    # sets it; an incident escalated by routing - like any
    # AMBIGUOUS_SIDE_EFFECT - left the run in its previous phase, and
    # resuming silently closed nothing. The ticket waited for a human, the
    # human answered, and the answer was lost. The authority is the incident
    # store itself: exactly the tickets awaiting the user are closed.
    store = PipelineIncidentStore(cfg.state_dir)
    closed: list[str] = []
    for incident_id in store.incident_ids_awaiting_the_user():
        store.resolve_escalation_by_user(
            incident_id,
            at=utc_now(),
            note="the user resumed the run, answering the escalation",
        )
        closed.append(incident_id)
    return tuple(closed)


def handle_prompt_hook(payload: dict[str, Any]) -> dict[str, Any]:
    prompt = _normalized_prompt(str(payload.get("prompt") or ""))
    if prompt not in PAUSE_PROMPTS | RESUME_PROMPTS | STATUS_PROMPTS | UNINSTALL_PROMPTS:
        # Control phrases pass even from a retired thread: they are about the
        # run, not the task, and never reach the model at all.
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
        store.clear_pause()
        recovered = _reconcile_before_resume(cfg)
        state = store.load()
        if answered_ids:
            # The reason is cleared AFTER re-reading. It used to be cleared
            # on the object above, and then the state was re-read from disk
            # for the tasks reconciliation returned - bringing the old
            # last_error back. Measured: the run went to READY/ARMED while the
            # detailed status printed a stop reason that no longer existed.
            state.last_error = None
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
    # Short by default: the hook's text arrives in one piece, and the full
    # report reads like a wall in a conversation.
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
) -> dict[str, Any]:
    """Raise an already reserved relay and report with the timeline."""

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
    # The ordinary continuation path: a Stop arrives for a thread with no
    # active worker session while a reservation it owns waits to be created.
    # complete_desktop_worker returns before its own gate in exactly that
    # branch, so production relays were spawned here with hook trust checked
    # by nobody in any process on the path. The cost is the one the normal
    # completion path already pays, and only when there is something to
    # launch.
    require_trusted_stop_hook_for_config(cfg)
    pids = _spawn_automatic_descriptors(
        cfg,
        launchable,
        triggering_thread_id=relay_owner_thread_id,
        triggering_turn_id=relay_owner_turn_id,
    )
    # A timeline report, as on every other launch path: a bare "dispatcher
    # started, pid such-and-such" is a claim, not an observation.
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
    # The step timeline right in the chat: the hook cannot append lines as
    # things happen, but on request it shows where the task is and what was
    # repaired - otherwise that would mean the terminal.
    active = list(state.active_task_ids or ())
    if not active:
        return summary
    timeline = render_launch_timeline(state, active)
    return f"{summary}\n\nSteps of the active tasks:\n{timeline}"
