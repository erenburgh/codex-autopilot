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


from .lifecycle_base import (
    DesktopLifecycleError,
    LaunchDescriptor,
    _active_session_by_thread,
    _append_event,
    _bind_resource_identity,
    _finish_global_state,
    _materialize,
    _require_desktop_owned,
    _require_relay_executor,
    _session_by_token,
)
from .lifecycle_reservations import (
    _reserve_in_state,
)


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

def _record_app_server_create_failure(
    cfg: Config,
    reservation_token: str,
    *,
    reason: str,
    definitive: bool,
    at: str | None,
    now_epoch: int | None,
) -> dict[str, Any]:
    # поздний импорт: развязка обратной зависимости модулей
    from .lifecycle_dispatch import app_server_creation_contract
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
