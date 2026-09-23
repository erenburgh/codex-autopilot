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
from .revision_budget import basis_for
from .plan import Plan, load_plan, validate_plan_change
from .plan_verification import (
    FULL_PLAN_REVALIDATION,
    PLAN_PATCH_VERIFICATION,
    PlanVerificationError,
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
from .lifecycle_screening import screening_gate
from .blocked_runs import stop_run
from .engineer_reservation import (  # noqa: F401 - re-exported for existing importers
    _reserve_pipeline_engineer_in_state,
    open_pipeline_engineer_incident,
    pipeline_engineer_package,
    route_waiting_tickets,
    stop_on_inconsistent_state,
    stop_on_mismatched_task_states,
    stop_on_unverified_plan,
    tasks_paused_by_incidents,
)
from .plan_change_reservation import (  # noqa: F401 - re-exported for existing importers
    _reserve_plan_verifier_in_state,
    _reserve_replanner_in_state,
)
from .scope import scope_baseline
from .run_state import RunState, StateStore, utc_now
from .scheduler import effective_worker_limit, schedule
from .task_state import (
    TaskState,
    migrate_v08_task_states,
    transition_task,
    validate_task_states,
)
from .thread_titles import (
    plan_verifier_thread_title,
    pipeline_engineer_thread_title,
    screening_thread_title,
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
        try:
            _prepare_state(plan, state, now_epoch=now_epoch)
        except (DesktopLifecycleError, ValueError):
            pass  # a stop, not a raise: _reserve_in_state files it past its guards
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
    # require_plan_verified. The gate used to raise before the on-call's
    # reservation too, so the engineer for an open ticket could never come
    # while it failed; the failure is now held until the ownership and
    # external-dispatcher guards below have passed, and then only the
    # engineer may pass it - see the invariant at `unverified`.
    try:
        require_plan_verified(plan, state)
        unverified: PlanVerificationError | None = None
    except PlanVerificationError as exc:
        unverified = exc
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
    # retry times - measured: M0's retry expired twelve minutes before the
    # engineer exited, and a 24-task run stood forever with zero done.
    try:
        _prepare_state(plan, state, now_epoch=epoch)
        mismatch: Exception | None = None
    except (DesktopLifecycleError, ValueError) as exc:
        mismatch = exc  # a stop below, never a raise: see the plan gate
    # Every ticket that needs the on-call reaches its lane, and a stopped
    # task nobody holds gets a ticket (engineer_reservation).
    route_waiting_tickets(cfg, plan, state)

    def finish(work: tuple[LaunchDescriptor, ...]) -> tuple[LaunchDescriptor, ...]:
        # The on-call comes last in the pass - a ticket filed by this very
        # pass (the ladder, a routing failure) gets its engineer now - and
        # NEXT TO the work, never instead of it: the early return here is
        # what froze the neighbours of a stopped task.
        found = _reserve_pipeline_engineer_in_state(
            cfg,
            plan,
            state,
            memory_audit_before=memory_audit_before,
            relay_owner_thread_id=relay_owner_thread_id,
        ) + tuple(work)
        if found and not work:
            state.status = "RUNNING"
            state.phase = "AWAITING_DESKTOP_CREATE"
        return found

    if unverified is not None or mismatch is not None:
        # Invariant: nothing is built from an unverified graph or from task
        # states that do not fit it - only the engineer, whose descriptor
        # reads no routing from a refused graph (_graph_for_the_engineer).
        # Both refusals used to raise here, rolling back the completion that
        # called us (the on-call's own included); now each is a stop.
        if unverified is not None:
            stop_on_unverified_plan(cfg, plan, state, unverified)
        if mismatch is not None:
            stop_on_mismatched_task_states(cfg, plan, state, mismatch)
        return finish(())
    paused = tasks_paused_by_incidents(cfg, plan)
    if state.active_plan_change_id is not None:
        change = active_plan_change(state)
        verifying = change.get("status") in {"PLAN_VERIFICATION_REQUIRED", "PLAN_VERIFYING"}
        # A plan change drains the run; it is a graph, not a stop, and the
        # on-call is reserved during it like at any other time.
        return finish(
            (_reserve_plan_verifier_in_state if verifying else _reserve_replanner_in_state)(
                cfg,
                plan,
                state,
                memory_audit_before=memory_audit_before,
                relay_owner_thread_id=relay_owner_thread_id,
            )
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
        # Hiring runs one reservation before the worker exists: the prompt,
        # and with it the skill stack, is frozen into the descriptor below,
        # inside this lock-held transaction that asks no model anything.
        gate = screening_gate(
            cfg,
            plan,
            state,
            task_id=task_id,
            memory_audit_before=memory_audit_before,
            relay_owner_thread_id=relay_owner_thread_id,
            build_descriptor=_build_descriptor,
        )
        if gate.action == "wait":
            continue
        if gate.action == "reserve" and gate.descriptor is not None:
            descriptors.append(gate.descriptor)
            continue
        if _pending_producer(state, task_id):
            stop_on_inconsistent_state(cfg, state, task_id, "already has a pending reservation")
            continue
        attempt = state.task_attempts.get(task_id, 0) + 1
        state.task_attempts[task_id] = attempt
        state.worker_sequence += 1
        token = _stable_id(
            state,
            f"reservation:{task_id}:{attempt}:{state.worker_sequence}",
        )
        operation_id = _stable_id(state, f"operation:{token}")
        client_id = f"autopilot-{_stable_id(state, f'client:{token}')[:24]}"
        owner = LockOwner.create(
            run_id=state.run_id,
            task_id=task_id,
            attempt=attempt,
            worker_id=f"desktop-worker-{state.worker_sequence}",
            ownership_token=token,
        )
        # Locks first, then the descriptor and the RUNNING edge: a refused
        # lock used to raise after the task was already marked RUNNING, and
        # the raise rolled back the whole completion that called us.
        acquired = acquire_resources_in_state(
            plan, state, cfg.root, task_id, owner
        )
        if not acquired.acquired:
            state.task_attempts[task_id] = attempt - 1
            stop_on_inconsistent_state(
                cfg, state, task_id, f"scheduler/resource race: {acquired.reason}"
            )
            continue
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
    return finish(tuple(descriptors))


def _pending_producer(state: RunState, task_id: str) -> bool:
    """A pending session that produces this task - the on-call only anchors to it."""

    return any(
        item.get("task_id") == task_id
        and item.get("status") in PENDING_SESSION_STATUSES
        and item.get("kind") != "pipeline_engineer"
        for item in state.worker_sessions
    )



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

    # The same calculation as the scheduler's: the followups gate runs
    # BEFORE schedule(), the ceiling in the state is still the declared one,
    # and on an unlimited account min(plan, state) refused a verifier to a
    # completed implementation.
    worker_limit = effective_worker_limit(plan, state)
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
                _append_event(
                    state,
                    "verifier_routing_blocked",
                    _latest_task_session(state, task.id),
                    utc_now(),
                    detail=str(exc),
                )
                # It used to stop with only last_error, then (0.13) BLOCKED
                # and a ticket. R3: routing is infrastructure - the task stays
                # IMPLEMENTED, held by the ticket until the on-call looks.
                stop_run(
                    cfg,
                    state,
                    stop_kind="verifier_routing",
                    phase="VERIFIER_ROUTING_BLOCKED",
                    reason=str(exc),
                    summary=f"No verifier could be routed for {task.id}.",
                    at=utc_now(),
                    task_ids=(task.id,),
                )
                continue
        elif raw_state == TaskState.REVISION_REQUIRED.value:
            if _rehire_or_block_on_revision_limit(
                cfg,
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
                stop_on_inconsistent_state(
                    cfg, state, task.id, "requires revision without structured issues"
                )
                continue
            evidence = ()
            check_results = ()
            execution_mode = task.execution_mode
        else:
            continue

        if _pending_producer(state, task.id):
            stop_on_inconsistent_state(
                cfg, state, task.id, "already has a pending follow-up reservation"
            )
            continue
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
            # Remember WHAT the attempt was spent on. A replanner can insert
            # a prerequisite under a task, and the attempts made before it
            # existed were attempts at a different problem on a different
            # tree; without this the ladder charges the new work for them.
            state.task_revision_basis[task.id] = basis_for(
                state.graph_version, task.depends_on
            )
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
            raise DesktopLifecycleError(
                "durable task state does not match the active graph: "
                f"unknown {sorted(set(state.task_states) - set(plan.task_map))}, "
                f"missing {sorted(set(plan.task_map) - set(state.task_states))}"
            )
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
    elif kind in {"replanner", "plan_verifier", "pipeline_engineer", "screening"}:
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
    elif kind == "screening":
        title = screening_thread_title(task.id, task.title)
        prompt = AIStudioRuntime(
            plan,
            cfg.root,
            language=cfg.language,
            skill_path=cfg.skill_path,
        ).build_screening_prompt(task.id, reservation_token=token)
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
