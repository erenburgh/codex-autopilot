"""The reservations a plan change makes: its replanner and its plan verifier.

Moved out of lifecycle_reservations (at the 1500-line limit) when their
refusals became stops.

Both used to raise inside the frontier when the plan-change record did not
fit the run: a proposal with no graph, a digest that moved, no verification
mode, a requester that was not READY. The raise rolled back the completion
that called the frontier - a worker's, the on-call's own - and while the
record stayed as it was, the same raise stood in front of the engineer's
reservation (``finish`` in ``_reserve_in_state`` comes after this call), so
the one who could look never came. The same inconsistent-state character
as the scheduler race and the pending producer (sweep items 25 and 27),
which already went through ``stop_on_inconsistent_state``; these were left
raising, and the independent check named them.

Now each is a stop: the requester is held by an ``inconsistent_state``
ticket, nothing is reserved from the record, and the on-call is reserved in
the same pass. The ticket is reused while open, so a pass that meets the
same record again files nothing new; two closures that did not repair it
send the third to the owner (``stop_repeats``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config
from .department_runtime import settled_task_ids
from .engineer_reservation import stop_on_inconsistent_state
from .lifecycle_base import (
    PENDING_SESSION_STATUSES,
    LaunchDescriptor,
    _append_event,
    _stable_id,
    task_checkpoint,
)
from .memory import ProjectMemory
from .plan import Plan, validate_plan_change
from .plan_verification import FULL_PLAN_REVALIDATION, PLAN_PATCH_VERIFICATION, plan_sha256
from .resilience import active_plan_change, append_resilience_event
from .resources import LockOwner, acquire_resources_in_state
from .run_state import RunState
from .scope import scope_baseline
from .task_state import TaskState, transition_task


def _unverifiable_proposal(cfg: Config, plan: Plan, state: RunState, record: dict[str, Any]) -> str | None:
    """Why this plan change cannot be put before a verifier - or None."""

    raw_candidate = record.get("proposed_plan")
    if not isinstance(raw_candidate, dict):
        return "plan verification requires a complete proposed replacement graph"
    try:
        candidate = validate_plan_change(
            plan,
            raw_candidate,
            cfg.profile,
            promotion_evidence_store=ProjectMemory(cfg.root),
            settled=settled_task_ids(state.task_states),
        )
    except ValueError as exc:
        return f"the proposed replacement graph no longer validates: {exc}"
    if record.get("proposed_plan_sha256") != plan_sha256(candidate):
        return "proposed plan digest changed before independent verification"
    if str(record.get("verification_mode") or "") not in {
        PLAN_PATCH_VERIFICATION,
        FULL_PLAN_REVALIDATION,
    }:
        return "plan change has no supported verification mode"
    return None


def _prompt_over_budget(cfg: Config, plan: Plan, state: RunState, record: dict[str, Any], task_id: str) -> bool:
    """The verifier's prompt does not fit: a context-planning stop for the on-call.

    R17: rules plus a minimal specification that do not fit are not
    launched, and that is reported as a context-planning defect. Building
    the prompt used to raise PlanVerificationError out of the reservation,
    unguarded - the completion that reserved was rolled back with it.
    """

    from .plan_verification import (
        PlanVerificationError,
        build_plan_verification_prompt,
        load_active_memory_constraints,
    )

    try:
        candidate = validate_plan_change(
            plan, record["proposed_plan"], cfg.profile, promotion_evidence_store=ProjectMemory(cfg.root),
            settled=settled_task_ids(state.task_states),
        )
        build_plan_verification_prompt(
            candidate,
            load_active_memory_constraints(cfg.root),
            mode=str(record.get("verification_mode") or ""),
            state_dir=cfg.state_dir,
        )
    except PlanVerificationError as exc:
        _stop_for_context_budget(cfg, state, record, task_id, "plan verifier", exc)
        return True
    return False


def _replanner_prompt_over_budget(
    cfg: Config, plan: Plan, state: RunState, record: dict[str, Any], task_id: str
) -> bool:
    """The replanner's prompt does not fit: the same stop as the plan verifier's.

    The replanner's prompt carries every refused attempt, the inherited ones
    too, and it was built only inside the descriptor, after the attempt was
    counted and the locks taken. Its refusal raised out of the reservation:
    reached from a replanner's own refused completion, it rolled that
    completion back - the refusal went unrecorded, no stop, no ticket - and
    took down the dispatcher, which catches only protocol errors. Measured
    by the independent check with 500 inherited issues.

    Built here first, before anything is reserved. The token is not known
    yet; a placeholder of the same length (a uuid, ``_stable_id``) gives the
    same prompt length.
    """

    from .lifecycle_prompts import ReplannerPromptOverBudget, _replanner_prompt

    try:
        _replanner_prompt(cfg, plan, state, record, "0" * 36)
    except ReplannerPromptOverBudget as exc:
        _stop_for_context_budget(cfg, state, record, task_id, "replanner", exc)
        return True
    return False


def _stop_for_context_budget(
    cfg: Config, state: RunState, record: dict[str, Any], task_id: str, who: str, exc: Exception
) -> None:
    """R17: rules plus a minimal specification that do not fit are not launched,
    and that is reported as a context-planning defect - a stop for the on-call.

    The change is closed here, as an exhausted budget closes it. It used to
    stay DRAINING with ``active_plan_change_id`` set, and while a change is
    active the frontier reserves only its replanner or plan verifier (and the
    on-call): measured by the independent check with 500 inherited issues and
    three workers allowed, B stayed READY through three passes - the whole
    run stood behind one requester's ticket. Now the requester alone is held
    by the ticket (it is already BLOCKED: a request and a proposed graph both
    park it there) and its neighbours go on. The on-call returns
    it to its worker (``return_stopped_task``); a new round would inherit the
    same refusals and not fit again, so this stop is not re-planned by it.
    """

    from .blocked_runs import stop_run
    from .run_state import utc_now

    at = utc_now()
    record["status"] = "REJECTED"
    record["completed_at"] = at
    record["closed_for"] = "context_budget"
    state.active_plan_change_id = None
    stop_run(
        cfg,
        state,
        stop_kind="context_budget",
        phase="CONTEXT_BUDGET_EXCEEDED",
        reason=f"{task_id}: the {who} cannot be launched: {exc}",
        summary=(
            f"The {who}'s prompt for {record.get('id')} does not fit the context "
            "budget with the rules block, which is never cut (R17)."
        ),
        at=at,
        task_ids=(task_id,),
        plan_change_id=str(record.get("id") or ""),
    )


def _reserve_plan_verifier_in_state(
    cfg: Config,
    plan: Plan,
    state: RunState,
    *,
    memory_audit_before: int,
    relay_owner_thread_id: str,
) -> tuple[LaunchDescriptor, ...]:
    """Reserve a fresh semantic judge without committing the proposed graph."""

    from .lifecycle_reservations import _build_descriptor

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
    task_id = str(record["requester_task_id"])
    refusal = _unverifiable_proposal(cfg, plan, state, record)
    if refusal is not None:
        stop_on_inconsistent_state(cfg, state, task_id, refusal)
        return ()
    if _prompt_over_budget(cfg, plan, state, record, task_id):
        return ()
    mode = str(record.get("verification_mode") or "")
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
        stop_on_inconsistent_state(
            cfg, state, task_id, f"plan verification anchor is {raw_state.value}, not READY"
        )
        return ()

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

    from .lifecycle_reservations import _build_descriptor, _descriptor_state_dir

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
    if _replanner_prompt_over_budget(cfg, plan, state, record, task_id):
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
        stop_on_inconsistent_state(
            cfg, state, task_id, f"plan change requester is {raw_state.value}, not READY"
        )
        return ()

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
        # Nothing is draining (that returned above), so a lock in the way
        # belongs either to a live session or to nobody. Waiting on nobody
        # was forever: no wake-up covers it and no door was opened.
        live = {
            str(item.get("resource_ownership_token"))
            for item in state.worker_sessions
            if item.get("status") in PENDING_SESSION_STATUSES
        }
        if not any(
            str((lock.get("owner") or {}).get("ownership_token")) in live
            for lock in state.resource_locks
        ):
            stop_on_inconsistent_state(
                cfg,
                state,
                task_id,
                f"the plan change waits on locks no live session holds: {acquired.reason}",
            )
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
