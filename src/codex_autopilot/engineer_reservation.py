"""The on-call works next to the run, not instead of it.

Moved out of lifecycle_reservations (at the 1500-line limit) when the
engineer's reservation changed its place in the frontier.

It used to outrank every task: while any ticket sat in the engineer's lane,
`_reserve_in_state` returned the engineer and nothing else. That early return
is why 0.13.0 opened some stops with ``route=False``: calling the on-call
would have halted neighbours that were not stuck. With that exception, the
ticket of a task at the top of its hiring ladder never reached anyone. The
freeze was never about slots - the engineer takes no work slot, it was
never in ``active_task_ids`` - it was the return.

So now:

- the engineer is reserved IN ADDITION to the work, at the end of the pass,
  so a ticket filed during this very pass (the ladder runs inside the
  follow-up reservation) gets its engineer at once;
- at most one engineer per run at a time - a second concurrent Codex thread
  is a real cost, and the other tickets wait their turn in the lane;
- it holds no resources and takes no slot, as before; at serial with one
  worker, one worker and one engineer run side by side;
- a ticket that names no task no longer breaks every reservation of the run:
  it anchors the engineer to a context task that it does not pause.

And tickets no longer wait for luck to reach the lane. Every reservation
first sweeps the incident journal: a ticket in DEGRADED that no runbook will
replay, or in AUTO_RECOVERY_FAILED, goes to the engineer; a learned repair
nobody replayed is replayed; a task in BLOCKED that no open ticket holds and
no plan change explains gets a ticket of its own (``orphan_block``). Without
that, a run could stand in WAITING_DEPENDENCIES with its tasks held by
tickets nobody would ever read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .lifecycle_base import (
    PENDING_SESSION_STATUSES,
    RELAYABLE_SESSION_STATUSES,
    DesktopLifecycleError,
    LaunchDescriptor,
    _append_event,
    _pid_alive,
    _stable_id,
    task_checkpoint,
)
from .run_state import utc_now
from .scope import scope_baseline
from .task_state import TaskState

# How many times the on-call may close a ticket about a blocked task and
# leave the task blocked before the next such ticket goes to the owner. The
# second closure without a return is not a repair; a third engineer would
# only burn the limits on the same finding.
MAX_ORPHAN_TICKETS_PER_TASK = 2
RESERVABLE_TASK_STATES = frozenset(
    {TaskState.READY.value, TaskState.IMPLEMENTED.value, TaskState.REVISION_REQUIRED.value}
)


def _incidents(cfg: Any) -> list[dict[str, Any]]:
    from .pipeline_engineer import PipelineIncidentStore

    loaded = PipelineIncidentStore(cfg.state_dir).load().get("incidents") or []
    return list(loaded.values()) if isinstance(loaded, dict) else list(loaded)


def _open(cfg: Any) -> list[dict[str, Any]]:
    return [item for item in _incidents(cfg) if not item.get("resolved_at")]


def tasks_paused_by_incidents(cfg: Any, plan: Any) -> set[str]:
    """The tasks named by open incidents - and only those.

    Any open incident used to stop the WHOLE run: while the on-call
    engineer dealt with M0, nothing moved, not even tasks unrelated to the
    incident. A ticket about a failed transport on one thread held
    twenty-three others.

    An incident names its own tasks - `affected_task_ids`. The pause covers
    exactly those. `context_task_id` pauses nothing. The one exception is
    the engineer's own finding that the whole run must wait (``blocks_run``,
    declared with ``scope: run``): then every task waits with it.
    """

    paused: set[str] = set()
    for item in _open(cfg):
        if item.get("blocks_run"):
            return set(plan.task_map)
        for task_id in item.get("affected_task_ids") or ():
            if str(task_id) in plan.task_map:
                paused.add(str(task_id))
    return paused


def open_pipeline_engineer_incident(cfg: Any) -> dict[str, Any] | None:
    """An open incident routed to the on-call engineer."""

    from .pipeline_engineer import IncidentPhase

    for item in _open(cfg):
        if str(item.get("phase")) == IncidentPhase.PIPELINE_ENGINEER.value:
            return item
    return None


def pipeline_engineer_package(
    cfg: Any, state: Any, incident_id: str | None = None
) -> dict[str, Any]:
    """The bounded incident package - the engineer's only entry into context.

    With `incident_id` the package is the one the session was reserved for.
    Without it, the first ticket in the lane - which is what a reservation
    binds. A dispatcher building the prompt later must pass the session's
    own ticket: by then another may be first in the lane.
    """

    from .pipeline_engineer import IncidentPhase, PipelineIncidentStore

    if incident_id:
        incident = next(
            (
                item
                for item in _open(cfg)
                if str(item.get("incident_id")) == incident_id
                and str(item.get("phase")) == IncidentPhase.PIPELINE_ENGINEER.value
            ),
            None,
        )
    else:
        incident = open_pipeline_engineer_incident(cfg)
    if incident is None:
        raise DesktopLifecycleError(
            "the on-call engineer is requested without an incident in phase PIPELINE_ENGINEER"
        )
    return PipelineIncidentStore(cfg.state_dir).incident_package(
        str(incident["incident_id"])
    )


def _infrastructure(item: dict[str, Any]) -> bool:
    from .engineer_authority import INFRASTRUCTURE_INCIDENT_CLASSES, IncidentClass

    try:
        return IncidentClass(str(item.get("classification"))) in INFRASTRUCTURE_INCIDENT_CLASSES
    except ValueError:
        return False


def _needs_lane(item: dict[str, Any]) -> bool:
    """A ticket that should be in the engineer's lane and is not there yet."""

    from .pipeline_engineer import STOP_CODE_PREFIX, IncidentPhase

    if not _infrastructure(item):
        return False
    phase = str(item.get("phase"))
    if phase == IncidentPhase.AUTO_RECOVERY_FAILED.value:
        return True
    return phase == IncidentPhase.DEGRADED.value and (
        item.get("runbook_id") is None
        or str(item.get("code") or "").startswith(STOP_CODE_PREFIX)
    )


def route_waiting_tickets(cfg: Any, plan: Any, state: Any) -> None:
    """The general sweep: every ticket that needs the on-call reaches it.

    Replaces ``route_pending_stops``, which took only ``run_stopped:`` tickets
    in DEGRADED, and only when the run had already gone BLOCKED. Housekeeping
    may never stop a run, so every failure here is contained per ticket.
    """

    from .pipeline_engineer import IncidentPhase, PipelineIncidentStore

    at = utc_now()
    try:
        store = PipelineIncidentStore(cfg.state_dir)
        waiting = _open(cfg)
    except Exception:  # noqa: BLE001 - an unreadable journal is itself a matter for doctor
        return
    for item in waiting:
        incident_id = str(item.get("incident_id"))
        try:
            if (
                str(item.get("phase")) == IncidentPhase.DEGRADED.value
                and not _needs_lane(item)
                and _infrastructure(item)
                and item.get("runbook_id") is not None
            ):
                # A learned repair that failed once and was never tried again
                # (budget left, nobody scheduled it). Level 1 first - no model.
                phase = store.attempt_known_recovery(
                    incident_id, at=at, owner_id="reservation-sweep"
                )
                if phase is IncidentPhase.AUTO_RECOVERY_FAILED:
                    store.ensure_pipeline_engineer(incident_id, at=at)
            elif _needs_lane(item):
                store.ensure_pipeline_engineer(incident_id, at=at)
        except Exception:  # noqa: BLE001 - one sick ticket does not block the rest
            continue
    _file_orphan_blocks(cfg, plan, state, at)


def orphan_blocked_tasks(cfg: Any, state: Any, task_ids: Any = None) -> list[str]:
    """Tasks in BLOCKED that no open ticket holds and no plan change explains.

    Such a task waits for nobody: the ticket that stopped it was closed
    without returning it (the on-call repaired and left, an old Resume
    closed the ticket and kept the stop), or no ticket was ever written.
    """

    held = {str(task) for item in _open(cfg) for task in item.get("affected_task_ids") or ()}
    requester = ""
    if getattr(state, "active_plan_change_id", None) is not None:
        from .resilience import active_plan_change

        try:
            requester = str(active_plan_change(state).get("requester_task_id") or "")
        except Exception:  # noqa: BLE001 - an unreadable change explains nothing
            requester = ""
    names = list(task_ids) if task_ids is not None else list(state.task_states)
    return [
        task_id
        for task_id in names
        if state.task_states.get(task_id) == TaskState.BLOCKED.value
        and task_id not in held
        and task_id != requester
    ]


def _file_orphan_blocks(cfg: Any, plan: Any, state: Any, at: str) -> None:
    from .blocked_runs import escalate_to_owner, stop_run

    try:
        orphans = orphan_blocked_tasks(cfg, state, [task.id for task in plan.tasks])
    except Exception:  # noqa: BLE001 - housekeeping may never stop a run
        return
    for task_id in orphans:
        earlier = sum(
            1
            for item in _incidents(cfg)
            if str((item.get("system_state") or {}).get("stop_kind")) == "orphan_block"
            and task_id in (item.get("affected_task_ids") or ())
        )
        incident_id = stop_run(
            cfg,
            state,
            stop_kind="orphan_block",
            phase="ORPHAN_BLOCK",
            reason=f"{task_id} is BLOCKED and no open ticket holds it",
            summary=(
                f"{task_id} is stopped, and the ticket that stopped it was closed "
                "without returning it to work."
            ),
            at=at,
            task_ids=(task_id,),
            system_state={"earlier_orphan_tickets": earlier},
        )
        if incident_id and earlier >= MAX_ORPHAN_TICKETS_PER_TASK:
            escalate_to_owner(
                cfg,
                incident_id,
                code="RECOVERY_EXHAUSTED",
                detail=(
                    f"The on-call closed {earlier} tickets about {task_id} and left it "
                    "stopped each time. Another engineer would find the same thing."
                ),
                at=at,
                escalation={
                    "diagnosis": f"{task_id} stays BLOCKED after {earlier} repairs",
                    "decision_needed": f"whether {task_id} goes back to work",
                    "recommendation": "look at the last ticket's diagnosis, then unblock or change the plan",
                    "scope": "task",
                },
            )


def engineer_session_pending(state: Any) -> bool:
    return any(
        item.get("kind") == "pipeline_engineer"
        and item.get("status") in PENDING_SESSION_STATUSES
        for item in state.worker_sessions
    )


def engineer_needed(cfg: Any, state: Any) -> bool:
    """Something waits for the on-call that no engineer session is handling."""

    if engineer_session_pending(state):
        return False
    from .pipeline_engineer import IncidentPhase

    return any(
        str(item.get("phase")) == IncidentPhase.PIPELINE_ENGINEER.value or _needs_lane(item)
        for item in _open(cfg)
    ) or bool(orphan_blocked_tasks(cfg, state))


def waiting_for_owner(cfg: Any, state: Any) -> bool:
    """Something - a stopped task, a ticket handed up - waits for her answer."""

    from .pipeline_engineer import IncidentPhase

    if any(value == TaskState.BLOCKED.value for value in state.task_states.values()):
        return True
    return any(
        str(item.get("phase")) == IncidentPhase.ESCALATE_TO_USER.value for item in _open(cfg)
    )


def reservable_work(cfg: Any, state: Any) -> bool:
    """A task the frontier could take right now, not held by any ticket."""

    import time

    barrier = getattr(state, "rate_limit_until", None)
    if isinstance(barrier, int) and barrier > time.time():
        return False  # the account's limit holds every task alike
    held: set[str] = set()
    for item in _open(cfg):
        if item.get("blocks_run"):
            return False
        held.update(str(task) for task in item.get("affected_task_ids") or ())
    return any(
        value in RESERVABLE_TASK_STATES and task_id not in held
        for task_id, value in state.task_states.items()
    )


def stalled_reservation(state: Any) -> bool:
    """A reservation that can be raised again and whose dispatcher is gone."""

    return any(
        (
            item.get("status") in RELAYABLE_SESSION_STATUSES
            or (item.get("status") == "RELAYING" and not str(item.get("thread_id") or ""))
        )
        and not _pid_alive(item.get("automatic_dispatch_pid"))
        for item in state.worker_sessions
    )


def stranded_reason(cfg: Any, state: Any) -> str | None:
    """Why nobody will move this run unless it is raised - or None.

    The caller has already checked that no dispatcher is alive.
    """

    if engineer_needed(cfg, state):
        return "on-call ticket without an engineer"
    pending = any(item.get("status") in PENDING_SESSION_STATUSES for item in state.worker_sessions)
    # Only a run that has started: an initialized or armed run waits for her
    # start phrase, and raising it would start it on her behalf.
    started = str(getattr(state, "status", "") or "") not in {"READY", "IDLE", ""}
    if started and not pending and reservable_work(cfg, state):
        return "reservable work without a session"
    if stalled_reservation(state):
        return "reservation without a live dispatcher"
    return None


def stop_on_inconsistent_state(cfg: Any, state: Any, task_id: str, reason: str) -> None:
    """A reservation found state that cannot be. Ticket the task, go on.

    These used to raise inside the frontier: the transaction rolled back,
    the completion that called it was lost, and the same raise then stood in
    front of the engineer's own reservation - nobody could come. Now the
    task is held by a ticket for the on-call and its neighbours proceed.
    """

    from .blocked_runs import stop_run

    stop_run(
        cfg,
        state,
        stop_kind="inconsistent_state",
        phase="INCONSISTENT_STATE",
        reason=f"{task_id}: {reason}",
        summary=f"The reservation of {task_id} found state that cannot be.",
        at=utc_now(),
        task_ids=(task_id,),
    )


def _anchor_task(plan: Any, state: Any, incident: dict[str, Any]) -> str:
    affected = [
        str(item) for item in incident.get("affected_task_ids") or () if str(item) in plan.task_map
    ]
    if affected:
        return affected[0]
    context = str(incident.get("context_task_id") or "")
    if context in plan.task_map:
        return context
    # A ticket with no task at all - a stop of the run itself, an old ticket,
    # a detached dispatch that did not know its task. It used to raise "names
    # no task of the current plan" on every reservation of the run. It is an
    # anchor for cwd and title, nothing more: the engineer's prompt is the
    # incident package, never this task's role, DoD or dependencies.
    return next(
        (
            task.id
            for task in plan.tasks
            if state.task_states.get(task.id) != TaskState.VERIFIED.value
        ),
        plan.tasks[0].id,
    )


def _reserve_pipeline_engineer_in_state(
    cfg: Any,
    plan: Any,
    state: Any,
    *,
    memory_audit_before: int,
    relay_owner_thread_id: str,
) -> tuple[LaunchDescriptor, ...]:
    """Reserve at most one on-call engineer for the first ticket in the lane.

    The engineer repairs the pipeline, not the task. So it deliberately does
    NOT take the affected task's resources: the failed session may hold
    them, and waiting on the lock would mean the repairer arrives blocked
    itself. For the same reason the task state is not changed and the task
    is not added to active_task_ids - the engineer takes no work slot.

    Nor does it set the run's status or phase any more: RUNNING with an
    engineer at work is derived in ``run_status`` from its pending session.
    """

    from .lifecycle_reservations import _build_descriptor, _descriptor_state_dir

    if engineer_session_pending(state):
        return ()
    incident = open_pipeline_engineer_incident(cfg)
    if incident is None:
        return ()
    incident_id = str(incident["incident_id"])
    task_id = _anchor_task(plan, state, incident)

    worker_sequence = state.worker_sequence + 1
    token = _stable_id(
        state,
        f"reservation:{task_id}:pipeline_engineer:{incident_id}:{worker_sequence}",
    )
    state.worker_sequence = worker_sequence
    operation_id = _stable_id(state, f"operation:{token}")
    client_id = f"autopilot-{_stable_id(state, f'client:{token}')[:24]}"
    attempt = int(state.task_attempts.get(task_id, 0)) or 1
    descriptor = _build_descriptor(
        cfg,
        plan,
        state,
        task_id=task_id,
        kind="pipeline_engineer",
        attempt=attempt,
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
        "attempt": attempt,
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
    _append_event(
        state,
        "pipeline_engineer_reserved",
        session,
        utc_now(),
        detail=f"{incident_id} -> {task_id}",
    )
    return (descriptor,)
