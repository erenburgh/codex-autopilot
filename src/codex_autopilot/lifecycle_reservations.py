from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from typing import Any, Callable

from .ai_studio import AIStudioRuntime
from .artifact_staging import ArtifactStagingStore, task_requires_staging
from .config import Config, DESKTOP_OWNED_SURFACE, STATE_DIR_NAME
from .department_acceptance import task_department_binding
from .hook_trust import require_trusted_stop_hook_for_config
from .lifecycle_prompts import (
    _replanner_prompt,
    _worker_prompt,
)
from .memory import ProjectMemory
from .models import MODEL_IDS, ModelRoutingError, logical_model
from .plan import Plan, load_plan, validate_plan_change
from .plan_verification import (
    FULL_PLAN_REVALIDATION,
    PLAN_PATCH_VERIFICATION,
    build_plan_verification_prompt,
    load_active_memory_constraints,
    plan_sha256,
    require_plan_verified,
)
from .resilience import (
    active_plan_change,
    append_resilience_event,
    recover_plan_change_transaction,
)
from .resources import (
    LockOwner,
    ResourceLockCoordinator,
    acquire_resources_in_state,
    build_scheduler_availability,
)
from .scope import scope_baseline
from .run_state import RunState, StateStore, utc_now
from .scheduler import schedule
from .task_state import (
    TaskState,
    migrate_v08_task_states,
    transition_task,
    validate_task_states,
)
from .thread_titles import (
    plan_verifier_thread_title,
    pipeline_engineer_thread_title,
    replanner_thread_title,
    task_phase_thread_title,
)
from .verification import (
    VerificationIssue,
    verifier_route,
)

from .lifecycle_base import (
    DESCRIPTOR_SCHEMA_VERSION,
    PENDING_SESSION_STATUSES,
    RELAYABLE_SESSION_STATUSES,
    SESSION_KINDS,
    SUCCESS_STATUSES,
    DesktopLifecycleError,
    LaunchDescriptor,
    _append_event,
    _rehire_or_block_on_revision_limit,
    task_effort,
    _latest_completion_context,
    _latest_task_session,
    _latest_verification_issues,
    _materialize,
    _pid_alive,
    _require_desktop_owned,
    _session_kind,
    _stable_id,
    fence_superseded_sessions,
    task_checkpoint,
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
    # This frontier also owns revision, verifier, replanner, and incident
    # sessions before the normal scheduler call below.  Gate the shared entry
    # point so none of those branches can reserve work from an unverified
    # canonical graph.  Persisted pre-v1 plans remain explicitly exempt in
    # require_plan_verified.
    require_plan_verified(plan, state)
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
    if state.rate_limit_until is not None and epoch < state.rate_limit_until:
        state.status = "WAITING"
        state.phase = "WAITING_RATE_LIMIT"
        return ()
    # _prepare_state lifts an expired rate-limit barrier and raises tasks
    # whose retry time has passed. It used to be called only inside the
    # barrier branch: a run with no barrier at all never revisited its
    # retry times.
    #
    # Measured: the on-call engineer closed the incident and exited, M0's
    # retry time had expired twelve minutes earlier, the task stayed in
    # RETRY_WAIT, the plan-change reservation saw RETRY_WAIT and parked the
    # run in WAITING_RATE_LIMIT - with no rate limit in sight. The
    # dispatcher exited, nobody was left to wake it, and a 24-task run
    # stood forever with zero done.
    _prepare_state(plan, state, now_epoch=epoch)
    # A broken pipeline outranks any work: while an incident is routed to
    # the on-call engineer, no new tasks are taken - the engineer is
    # reserved instead. This phase used to be only a label in JSON, and the
    # run stood silently.
    engineer = _reserve_pipeline_engineer_in_state(
        cfg,
        plan,
        state,
        memory_audit_before=memory_audit_before,
        relay_owner_thread_id=relay_owner_thread_id,
    )
    if engineer:
        return engineer
    # An open incident no longer stops the whole run. It holds only its own
    # tasks; everything else that is ready proceeds as usual. The on-call
    # engineer closes the ticket in its own turn.
    paused = tasks_paused_by_incidents(cfg, plan)
    if state.active_plan_change_id is not None:
        change = active_plan_change(state)
        if change.get("status") in {
            "PLAN_VERIFICATION_REQUIRED",
            "PLAN_VERIFYING",
        }:
            return _reserve_plan_verifier_in_state(
                cfg,
                plan,
                state,
                memory_audit_before=memory_audit_before,
                relay_owner_thread_id=relay_owner_thread_id,
            )
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
            paused_task_ids=paused,
        )
    )
    decision = schedule(
        plan,
        state,
        build_scheduler_availability(plan, state, cfg.root),
    )
    for task_id in decision.selected_task_ids:
        if task_id in paused:
            # This task waits for its incident. The others do not.
            continue
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
            "checkpoint_before": task_checkpoint(
                _descriptor_state_dir(cfg, descriptor), task_id
            ),
            "scope_baseline": scope_baseline(Path(descriptor.cwd)),
            "descriptor": descriptor.to_dict(),
        }
        fence_superseded_sessions(
            state,
            task_id,
            at=descriptor.created_at,
            reason=f"replacement reservation {descriptor.reservation_token} owns this task",
        )
        state.worker_sessions.append(session)
        for event in ("reservation_created", "create_requested"):
            _append_event(state, event, session, descriptor.created_at)
        descriptors.append(descriptor)
    if descriptors:
        state.status = "RUNNING"
        state.phase = "AWAITING_DESKTOP_CREATE"
        state.milestone_id = descriptors[0].task_id
    return tuple(descriptors)

def tasks_paused_by_incidents(cfg: Config, plan: Plan) -> set[str]:
    """The tasks named by open incidents - and only those.

    Any open incident used to stop the WHOLE run: while the on-call
    engineer dealt with M0, nothing moved, not even tasks unrelated to the
    incident. A ticket about a failed transport on one thread held
    twenty-three others.

    An incident names its own tasks - `affected_task_ids`. The pause covers
    exactly those.
    """

    from .pipeline_engineer import PipelineIncidentStore

    paused: set[str] = set()
    for item in PipelineIncidentStore(cfg.state_dir).load().get("incidents", []):
        if item.get("resolved_at"):
            continue
        for task_id in item.get("affected_task_ids") or ():
            if str(task_id) in plan.task_map:
                paused.add(str(task_id))
    return paused


def open_pipeline_engineer_incident(cfg: Config) -> dict[str, Any] | None:
    """An open incident routed to the on-call engineer."""

    from .pipeline_engineer import IncidentPhase, PipelineIncidentStore

    store = PipelineIncidentStore(cfg.state_dir)
    for item in store.load().get("incidents", []):
        if item.get("resolved_at"):
            continue
        if str(item.get("phase")) == IncidentPhase.PIPELINE_ENGINEER.value:
            return item
    return None


def pipeline_engineer_package(cfg: Config, state: RunState) -> dict[str, Any]:
    """The bounded incident package - the engineer's only entry into context."""

    from .pipeline_engineer import PipelineIncidentStore

    incident = open_pipeline_engineer_incident(cfg)
    if incident is None:
        raise DesktopLifecycleError(
            "the on-call engineer is requested without an incident in phase PIPELINE_ENGINEER"
        )
    return PipelineIncidentStore(cfg.state_dir).incident_package(
        str(incident["incident_id"])
    )


def _reserve_pipeline_engineer_in_state(
    cfg: Config,
    plan: Plan,
    state: RunState,
    *,
    memory_audit_before: int,
    relay_owner_thread_id: str,
) -> tuple[LaunchDescriptor, ...]:
    """Reserve exactly one on-call engineer for an open incident.

    The engineer repairs the pipeline, not the task. So it deliberately does
    NOT take the affected task's resources: the failed session may hold
    them, and waiting on the lock would mean the repairer arrives blocked
    itself. For the same reason the task state is not changed and the task
    is not added to active_task_ids - the engineer takes no work slot.

    The incident's task is needed only as context: its directory, the role
    in the title and the scope baseline.
    """

    incident = open_pipeline_engineer_incident(cfg)
    if incident is None:
        return ()
    incident_id = str(incident["incident_id"])
    if any(
        item.get("kind") == "pipeline_engineer"
        and item.get("incident_id") == incident_id
        and item.get("status") in PENDING_SESSION_STATUSES
        for item in state.worker_sessions
    ):
        return ()
    affected = [
        str(item)
        for item in incident.get("affected_task_ids") or ()
        if str(item) in plan.task_map
    ]
    if not affected:
        raise DesktopLifecycleError(
            f"incident {incident_id} names no task of the current plan; "
            "there is nothing to bind the on-call engineer to"
        )
    task_id = affected[0]

    worker_sequence = state.worker_sequence + 1
    token = _stable_id(
        state,
        f"reservation:{task_id}:pipeline_engineer:{incident_id}:{worker_sequence}",
    )
    state.worker_sequence = worker_sequence
    operation_id = _stable_id(state, f"operation:{token}")
    client_id = f"autopilot-{_stable_id(state, f'client:{token}')[:24]}"
    descriptor = _build_descriptor(
        cfg,
        plan,
        state,
        task_id=task_id,
        kind="pipeline_engineer",
        attempt=int(state.task_attempts.get(task_id, 0)) or 1,
        token=token,
        operation_id=operation_id,
        client_id=client_id,
    )
    session: dict[str, Any] = {
        "reservation_token": token,
        "resource_ownership_token": None,
        "operation_id": operation_id,
        "client_user_message_id": client_id,
        "task_id": task_id,
        "kind": "pipeline_engineer",
        "attempt": int(state.task_attempts.get(task_id, 0)) or 1,
        "worker_sequence": worker_sequence,
        "incident_id": incident_id,
        "status": "CREATE_REQUESTED",
        "thread_id": None,
        "turn_id": None,
        "host_id": None,
        "relay_owner_thread_id": relay_owner_thread_id,
        "created_at": descriptor.created_at,
        "memory_audit_before": memory_audit_before,
        "checkpoint_before": task_checkpoint(
            _descriptor_state_dir(cfg, descriptor), task_id
        ),
        "scope_baseline": scope_baseline(Path(descriptor.cwd)),
        "descriptor": descriptor.to_dict(),
    }
    state.worker_sessions.append(session)
    state.status = "RUNNING"
    state.phase = "PIPELINE_ENGINEER_ACTIVE"
    _append_event(
        state,
        "pipeline_engineer_reserved",
        session,
        utc_now(),
        detail=f"{incident_id} -> {task_id}",
    )
    return (descriptor,)


def _reserve_plan_verifier_in_state(
    cfg: Config,
    plan: Plan,
    state: RunState,
    *,
    memory_audit_before: int,
    relay_owner_thread_id: str,
) -> tuple[LaunchDescriptor, ...]:
    """Reserve a fresh semantic judge without committing the proposed graph."""

    record = active_plan_change(state)
    if state.active_task_ids:
        state.status = "RUNNING"
        state.phase = "PLAN_VERIFICATION_DRAINING"
        return ()
    if any(
        item.get("kind") == "plan_verifier"
        and item.get("plan_change_id") == record["id"]
        and item.get("status") in PENDING_SESSION_STATUSES
        for item in state.worker_sessions
    ):
        return ()
    raw_candidate = record.get("proposed_plan")
    if not isinstance(raw_candidate, dict):
        raise DesktopLifecycleError(
            "plan verification requires a complete proposed replacement graph"
        )
    candidate = validate_plan_change(
        plan,
        raw_candidate,
        cfg.profile,
        promotion_evidence_store=ProjectMemory(cfg.root),
    )
    if record.get("proposed_plan_sha256") != plan_sha256(candidate):
        raise DesktopLifecycleError(
            "proposed plan digest changed before independent verification"
        )
    mode = str(record.get("verification_mode") or "")
    if mode not in {PLAN_PATCH_VERIFICATION, FULL_PLAN_REVALIDATION}:
        raise DesktopLifecycleError("plan change has no supported verification mode")

    task_id = str(record["requester_task_id"])
    raw_state = TaskState(state.task_states[task_id])
    if raw_state is TaskState.RETRY_WAIT:
        state.status = "WAITING"
        state.phase = "WAITING_RATE_LIMIT"
        return ()
    if raw_state is TaskState.BLOCKED:
        state.task_states = transition_task(
            plan, state.task_states, task_id, TaskState.READY
        )
        raw_state = TaskState.READY
    if raw_state is not TaskState.READY:
        raise DesktopLifecycleError(
            f"plan verification anchor {task_id} is not ready"
        )

    previous_attempt = int(state.task_attempts.get(task_id, 0))
    attempt = previous_attempt + 1
    state.task_attempts[task_id] = attempt
    state.worker_sequence += 1
    token = _stable_id(
        state,
        f"reservation:{task_id}:plan-verifier:{record['id']}:{attempt}:{state.worker_sequence}",
    )
    operation_id = _stable_id(state, f"operation:{token}")
    client_id = f"autopilot-{_stable_id(state, f'client:{token}')[:24]}"
    descriptor = _build_descriptor(
        cfg,
        plan,
        state,
        task_id=task_id,
        kind="plan_verifier",
        attempt=attempt,
        token=token,
        operation_id=operation_id,
        client_id=client_id,
    )
    state.task_states = transition_task(
        plan, state.task_states, task_id, TaskState.RUNNING
    )
    state.active_task_ids.append(task_id)
    session: dict[str, Any] = {
        "reservation_token": token,
        "resource_ownership_token": token,
        "operation_id": operation_id,
        "client_user_message_id": client_id,
        "task_id": task_id,
        "kind": "plan_verifier",
        "attempt": attempt,
        "worker_sequence": state.worker_sequence,
        "plan_change_id": record["id"],
        "verification_mode": mode,
        "proposed_plan_sha256": record["proposed_plan_sha256"],
        "status": "CREATE_REQUESTED",
        "thread_id": None,
        "turn_id": None,
        "host_id": None,
        "relay_owner_thread_id": relay_owner_thread_id,
        "created_at": descriptor.created_at,
        "memory_audit_before": memory_audit_before,
        "descriptor": descriptor.to_dict(),
    }
    state.worker_sessions.append(session)
    record["status"] = "PLAN_VERIFYING"
    record["plan_verifier_session_token"] = token
    record["plan_verification_attempts"] = int(
        record.get("plan_verification_attempts") or 0
    ) + 1
    for event in ("reservation_created", "plan_verifier_started", "create_requested"):
        _append_event(state, event, session, descriptor.created_at)
    append_resilience_event(
        state,
        "plan_verifier_reserved",
        at=descriptor.created_at,
        task_id=task_id,
        plan_change_id=str(record["id"]),
        detail={
            "reservation_token": token,
            "mode": mode,
            "proposed_plan_sha256": record["proposed_plan_sha256"],
        },
    )
    state.status = "RUNNING"
    state.phase = "PLAN_VERIFYING"
    state.milestone_id = task_id
    return (descriptor,)


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
        "checkpoint_before": task_checkpoint(
            _descriptor_state_dir(cfg, descriptor), task_id
        ),
        "scope_baseline": scope_baseline(Path(descriptor.cwd)),
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
    paused_task_ids: set[str] | None = None,
) -> tuple[LaunchDescriptor, ...]:
    """Reserve verifier/revision work before admitting unrelated READY work."""

    paused_task_ids = paused_task_ids or set()

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
        if task.id in paused_task_ids:
            continue
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
            if _rehire_or_block_on_revision_limit(
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
            "checkpoint_before": task_checkpoint(
                _descriptor_state_dir(cfg, descriptor), task.id
            ),
            "scope_baseline": scope_baseline(Path(descriptor.cwd)),
            "descriptor": descriptor.to_dict(),
        }
        fence_superseded_sessions(
            state,
            task.id,
            at=descriptor.created_at,
            reason=f"replacement reservation {descriptor.reservation_token} owns this task",
        )
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
    elif kind in {"replanner", "plan_verifier", "pipeline_engineer"}:
        execution_mode = "code"
        if plan.model_strategy == "host-settings":
            model = None
            thinking = None
        else:
            key = logical_model(plan.model_strategy, execution_mode)
            model = MODEL_IDS[key]
            # Plan verification is a fresh semantic judgment over the whole
            # bounded graph.  Its effort must not inherit the requester task's
            # local planning choice merely because that task anchors the
            # lifecycle reservation.
            thinking = "high" if kind == "plan_verifier" else task.reasoning or "medium"
    else:
        execution_mode = task.execution_mode
        if plan.model_strategy == "host-settings":
            model = None
            thinking = None
        else:
            key = logical_model(plan.model_strategy, execution_mode)
            model = MODEL_IDS[key]
            # A re-hire raises the effort step above the one recorded in the plan.
            thinking = task_effort(plan, state, task_id)
    if kind == "pipeline_engineer":
        package = pipeline_engineer_package(cfg, state)
        incident = package["incident"]
        title = pipeline_engineer_thread_title(
            str(incident["incident_id"]),
            str(incident.get("summary") or incident.get("code") or "incident"),
        )
        prompt = AIStudioRuntime(
            plan,
            cfg.root,
            language=cfg.language,
            skill_path=cfg.skill_path,
        ).build_pipeline_engineer_prompt(package, reservation_token=token)
    elif kind == "replanner":
        change = active_plan_change(state)
        title = replanner_thread_title(
            str(change["id"]),
            str(change["request"]["summary"]),
        )
        prompt = _replanner_prompt(cfg, plan, state, change, token)
    elif kind == "plan_verifier":
        change = active_plan_change(state)
        raw_candidate = change.get("proposed_plan")
        if not isinstance(raw_candidate, dict):
            raise DesktopLifecycleError(
                "plan verifier descriptor has no proposed plan"
            )
        candidate = validate_plan_change(
            plan,
            raw_candidate,
            cfg.profile,
            promotion_evidence_store=ProjectMemory(cfg.root),
        )
        mode = str(change.get("verification_mode") or "")
        title = plan_verifier_thread_title(candidate.graph_version, mode)
        prompt = build_plan_verification_prompt(
            candidate,
            load_active_memory_constraints(cfg.root),
            mode=mode,
        )
    else:
        role_id = route.role_id if kind == "verifier" else task.role
        title = task_phase_thread_title(
            task_id=task.id,
            task_title=task.title,
            kind=kind,
            role_name=plan.role_map[role_id].name,
            revision_number=revision_number,
            departmental_verifier=(
                kind == "verifier" and task_department_binding(task) is not None
            ),
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
    workspace = cfg.root
    if kind in {"implementation", "worker", "revision", "verifier"} and task_requires_staging(
        task, legacy_serial=plan.legacy_serial
    ):
        staging = ArtifactStagingStore(cfg.root, cfg.state_dir)
        if kind in {"revision", "verifier"}:
            # A follow-up may inspect only the exact proposal produced by its
            # implementation predecessor.  Creating a fresh snapshot here
            # would silently bless canonical side effects from an older path.
            staging.load(task.id)
        staged = staging.prepare(
            run_id=state.run_id,
            task_id=task.id,
            reservation_token=token,
        )
        workspace = staged.workspace
        prompt = _staged_workspace_prompt(
            prompt,
            canonical_root=cfg.root,
            workspace=workspace,
            task_id=task.id,
            verifier=(kind == "verifier"),
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
        cwd=str(workspace),
        title=title,
        prompt=prompt,
        model=model,
        thinking=thinking,
        execution_mode=execution_mode,
        created_at=utc_now(),
        prep_app_server_exited_at=str(state.prep_app_server_exited_at),
        descriptor_path=str(path),
    )


def _descriptor_state_dir(cfg: Config, descriptor: LaunchDescriptor) -> Path:
    workspace = Path(descriptor.cwd).expanduser().resolve(strict=False)
    return cfg.state_dir if workspace == cfg.root else workspace / STATE_DIR_NAME


def _staged_workspace_prompt(
    prompt: str,
    *,
    canonical_root: Path,
    workspace: Path,
    task_id: str,
    verifier: bool,
) -> str:
    """Make the isolation boundary explicit in the worker-visible contract."""

    phase = "verify" if verifier else "edit and test"
    return (
        prompt
        + "\n\nSTAGED_ARTIFACT_GATE: The App Server cwd is the isolated workspace "
        + f"{workspace}. {phase.capitalize()} only this workspace for task {task_id}. "
        + f"The canonical project {canonical_root} must remain unchanged until an independent "
        + "PASS; the runtime alone promotes the verifier-bound manifest. REVISE keeps the "
        + "workspace isolated. Write the required handoff under this cwd's "
        + f"{STATE_DIR_NAME}/handoff/{task_id}.md.\n"
    )
