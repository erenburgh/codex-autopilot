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
    _settle_lost_creates(cfg, plan, state, at)
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
    """A ticket for every orphaned stop.

    The second closure without a return is not a repair; a third engineer
    would only burn the limits on the same finding. That bound used to live
    here (and a copy in the plan gate); it is the door's now, for every stop
    alike (``stop_repeats``): the third orphan ticket about a task goes to
    the owner with what the first two closures did.
    """

    from .blocked_runs import stop_run

    try:
        orphans = orphan_blocked_tasks(cfg, state, [task.id for task in plan.tasks])
    except Exception:  # noqa: BLE001 - housekeeping may never stop a run
        return
    for task_id in orphans:
        stop_run(
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


def reservable_work(cfg: Any, state: Any, states: frozenset[str] = RESERVABLE_TASK_STATES) -> bool:
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
        value in states and task_id not in held
        for task_id, value in state.task_states.items()
    )


def _lost_create(item: dict[str, Any]) -> bool:
    """A pending session with no thread that cannot be raised again.

    A create that ended in doubt (AMBIGUOUS) or a legacy RESERVED record:
    a second create is forbidden (it could duplicate the thread), and with
    no thread there is nothing the server could be asked about.
    """

    return (
        not str(item.get("thread_id") or "")
        and item.get("status") not in RELAYABLE_SESSION_STATUSES
        and item.get("status") != "RELAYING"
    )


def stalled_reservation(state: Any, held: Any = frozenset()) -> bool:
    """A pending session whose dispatcher is gone - whatever its status.

    This used to ask only about reservations that can be raised again
    (created, never sent). The independent check drove the other case on
    the fakes: an on-call whose completion failed - an unreadable status
    line, RESOLVED declared without devops-resolve-incident - raised
    DesktopLifecycleError, the dispatcher (which catches only
    WorkerProtocolError) died, and the session stayed ACTIVE with a dead
    automatic_dispatch_pid. One engineer per run then kept every later one
    out, the status read RUNNING, and this predicate said "not stranded":
    a silent stop. The same held for a worker. The automatic dispatcher
    waits for its turn and consumes it (the worker's Stop hook never does),
    so a pending session without a live one is nobody's now.

    Each such session has someone who moves it once the run is raised:
    never created - raised again (``wake._revive_stalled``); with a thread -
    settled by the server's word (``wake._settle_dead_sessions``); a create
    in doubt - the reservation pass (``_settle_lost_creates``) files a
    ticket for a worker's, and retires an engineer's, which did no work.

    `held` are the tasks open tickets hold. A worker's create in doubt whose
    task a ticket holds is that ticket's to settle - in the lane the first
    clause of ``stranded_reason`` raises the engineer, and with the owner
    the run waits for her. Counting it here would raise a wake-up every
    sweep that can do nothing, each one a line in the run journal. An
    engineer's never is: its anchor is exactly the task its ticket holds,
    and while it stays pending no other engineer can come.
    """

    return any(
        item.get("status") in PENDING_SESSION_STATUSES
        and not _pid_alive(item.get("automatic_dispatch_pid"))
        and not (
            _lost_create(item)
            and item.get("kind") != "pipeline_engineer"
            and str(item.get("task_id") or "") in held
        )
        for item in state.worker_sessions
    )


def dead_session_tokens(state: Any) -> set[str]:
    """Pending sessions past their creation whose dispatcher is gone.

    These cannot simply be raised again - a thread exists and a turn may
    have run - so only the server's own answer (thread/read) may retire
    them. Reservations that were never created are raised again instead.
    """

    return {
        str(item.get("resource_ownership_token") or item.get("reservation_token") or "")
        for item in state.worker_sessions
        if item.get("status") in PENDING_SESSION_STATUSES
        and item.get("status") not in RELAYABLE_SESSION_STATUSES
        and str(item.get("thread_id") or "")
        and not _pid_alive(item.get("automatic_dispatch_pid"))
    }


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
    held = {str(task) for item in _open(cfg) for task in item.get("affected_task_ids") or ()}
    if stalled_reservation(state, held):
        return "pending session without a live dispatcher"
    return None


def _settle_lost_creates(cfg: Any, plan: Any, state: Any, at: str) -> None:
    """Give every create in doubt whose dispatcher is gone someone to settle it.

    Such a session was nobody's: the wake-up cannot raise it (a second
    create is forbidden) or ask the server about it (it has no thread), the
    frontier leaves pending sessions alone, and only a ticket below the
    retry ceiling had ever been filed for it - so the task waited for her
    Resume in silence.

    - A worker's: a ticket through the door (``lost_create``) holds the
      task, and the on-call settles the doubt with the tools it already has
      (``reconcile-thread-identity``, a definitive transport failure). A
      task an open ticket already holds is left to that ticket.
    - An engineer's: it never started a turn - there is no thread to start
      one on - so it did no work. It is retired, the lane is free, and the
      next engineer takes its ticket; two lost on one ticket send it to the
      owner (``hand_lost_engineers_to_owner``). Left pending, it kept every
      later engineer out for good.

    Housekeeping: a failure here never stops the reservation.
    """

    from .blocked_runs import stop_run

    try:
        held = {str(task) for item in _open(cfg) for task in item.get("affected_task_ids") or ()}
    except Exception:  # noqa: BLE001 - an unreadable journal is itself a matter for doctor
        return
    for item in state.worker_sessions:
        if not (
            item.get("status") in PENDING_SESSION_STATUSES
            and _lost_create(item)
            and not _pid_alive(item.get("automatic_dispatch_pid"))
        ):
            continue
        task_id = str(item.get("task_id") or "")
        if item.get("kind") == "pipeline_engineer":
            item["status"] = "RETRY_WAIT"
            item["failure_reason"] = (
                f"{LOST_ENGINEER_CREATE_REASON}: {item.get('failure_reason') or 'no thread'}"
            )[:2000]
            _append_event(state, "pipeline_engineer_create_lost", item, at)
            continue
        if task_id in held or task_id not in plan.task_map:
            continue
        stop_run(
            cfg,
            state,
            stop_kind="lost_create",
            phase="LOST_CREATE",
            reason=(
                f"{task_id}: the create of its {item.get('kind') or 'worker'} session ended "
                f"in doubt and its dispatcher is gone: {item.get('failure_reason') or ''}"
            )[:2000],
            summary=(
                f"{task_id} waits on a session whose thread may or may not exist; "
                "nobody was left to settle it."
            ),
            at=at,
            task_ids=(task_id,),
            system_state={"reservation_token": str(item.get("reservation_token") or "")},
        )
        held.add(task_id)


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


def stop_on_unverified_plan(cfg: Any, plan: Any, state: Any, error: Exception) -> None:
    """The plan gate refused the graph. That is a stop, and it holds the run.

    The refusal used to raise inside the frontier. Every completion that
    ends in a reservation - a worker's, the on-call's own - was rolled back
    with it: measured, the engineer escalated its ticket, the incident
    journal moved, and run-state kept the engineer ACTIVE forever. One
    engineer per run then kept every later engineer out, the status read
    RUNNING, and the wake-up saw nothing to raise. A silent stop again.

    Now the refusal files one ticket (``plan_unverified``) that holds every
    unfinished task - nothing may be built from this graph - and the on-call
    comes for it. While that ticket is open, no second one is filed. A repair
    that does not take, closed twice, goes to the owner with its diagnosis:
    the door's R23 bound (``stop_repeats``), which replaced the copy that
    lived here.
    """

    from .blocked_runs import stop_run

    try:
        if any(
            not item.get("resolved_at")
            for item in _incidents(cfg)
            if str((item.get("system_state") or {}).get("stop_kind")) == "plan_unverified"
        ):
            return
    except Exception:  # noqa: BLE001 - the door records its own failure below
        pass
    unfinished = tuple(
        task.id for task in plan.tasks if state.task_states.get(task.id) != TaskState.VERIFIED.value
    )
    stop_run(
        cfg,
        state,
        stop_kind="plan_unverified",
        phase="PLAN_UNVERIFIED",
        reason=f"the plan gate refused the canonical graph: {error}",
        summary="The canonical plan has no valid verification receipt; nothing may be built from it.",
        at=utc_now(),
        task_ids=unfinished,
    )


def stop_on_mismatched_task_states(cfg: Any, plan: Any, state: Any, error: Exception) -> None:
    """The durable task states do not fit the active graph. A stop, not a raise.

    ``_prepare_state`` refuses a state map that names tasks the graph does
    not have, or misses some it has. Inside the frontier that refusal was
    the same raise as the plan gate's (sweep items 25 and 27): it rolled
    back the completion that called the frontier, and stood in front of the
    engineer's reservation, so the one who could look never came. The
    independent check named it after the other inconsistent-state raises
    had become stops.

    Nothing may be scheduled from a map that does not fit the graph, so the
    ticket (``task_states_mismatch``) holds every task of the graph that is
    not VERIFIED, and only the on-call is reserved. It may not rewrite
    run-state by hand (her boundary); what it can do is find how the map
    and the graph parted - a plan change applied halfway, a restore - and
    repair that, or bring her the diagnosis. One ticket while it is open.
    """

    from .blocked_runs import stop_run

    try:
        if any(
            not item.get("resolved_at")
            for item in _incidents(cfg)
            if str((item.get("system_state") or {}).get("stop_kind")) == "task_states_mismatch"
        ):
            return
    except Exception:  # noqa: BLE001 - the door records its own failure below
        pass
    stop_run(
        cfg,
        state,
        stop_kind="task_states_mismatch",
        phase="TASK_STATES_MISMATCH",
        reason=f"the durable task states do not fit the active graph: {error}",
        summary="The run's task states and its graph disagree; nothing may be scheduled from them.",
        at=utc_now(),
        task_ids=tuple(
            task.id
            for task in plan.tasks
            if state.task_states.get(task.id) != TaskState.VERIFIED.value
        ),
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
    # R23 for the on-call itself: a ticket whose engineers were lost twice
    # (``hand_lost_engineers_to_owner``) goes to her, and the next ticket in
    # the lane is taken instead.
    while incident is not None and hand_lost_engineers_to_owner(cfg, state, incident):
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
        _graph_for_the_engineer(plan, state),
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


# How many engineers may be lost on one ticket - their dispatcher died and
# the server showed the turn over with no answer taken - before the ticket
# goes to the owner. The same two as ``stop_repeats.MAX_REPEATED_STOPS``.
MAX_LOST_ENGINEERS = 2
# Written by ``resilience.reconcile_running_work`` when the server showed the
# turn over, and by ``_settle_lost_creates`` for a create in doubt.
LOST_ENGINEER_REASON = "crash reconciliation observed pipeline engineer"
LOST_ENGINEER_CREATE_REASON = "lost pipeline engineer: create in doubt, dispatcher gone"
# Written by ``lifecycle_failures.record_desktop_failure`` when the engineer's
# own turn failed definitively (not her pause, not the account's limit).
LOST_ENGINEER_TURN_REASON = "lost pipeline engineer: its turn failed"


def lost_engineers(state: Any, incident_id: str) -> list[dict[str, Any]]:
    """The on-call sessions for this ticket that the wake-up had to retire."""

    return [
        item
        for item in state.worker_sessions
        if item.get("kind") == "pipeline_engineer"
        and str(item.get("incident_id") or "") == incident_id
        and str(item.get("failure_reason") or "").startswith(
            (LOST_ENGINEER_REASON, LOST_ENGINEER_CREATE_REASON, LOST_ENGINEER_TURN_REASON)
        )
    ]


def hand_lost_engineers_to_owner(cfg: Any, state: Any, incident: dict[str, Any]) -> bool:
    """Send a ticket to the owner once two engineers were lost on it. True if sent.

    Lifting a dead engineer (``wake._settle_dead_sessions``) frees the lane,
    and the next pass reserves a fresh engineer for the same ticket. If the
    completion fails for the same reason every time - the engineer cannot
    write a status line the runtime reads, or keeps declaring a repair the
    gateway never recorded - that is a loop R23 forbids: each round burns
    her limits on the same finding. So the third engineer is not reserved;
    the ticket goes to her with what happened to the two before it. Never
    raises: a reservation may not fail on bookkeeping.
    """

    incident_id = str(incident.get("incident_id") or "")
    lost = lost_engineers(state, incident_id)
    if len(lost) < MAX_LOST_ENGINEERS:
        return False
    from .blocked_runs import escalate_to_owner
    from .stop_holds import block_escalated_tasks, holds_its_tasks

    at = utc_now()
    diagnosis = (
        f"{len(lost)} on-call engineers for ticket {incident_id} ended without an "
        "answer the runtime could take: their turns failed, or their dispatcher "
        "died and the server showed the turn over. A third would meet the same end."
    )
    outcome = escalate_to_owner(
        cfg,
        incident_id,
        code="RECOVERY_EXHAUSTED",
        detail=diagnosis,
        at=at,
        escalation={
            "diagnosis": diagnosis,
            "repaired": [
                {
                    "session": item.get("reservation_token"),
                    "thread_id": item.get("thread_id"),
                    "failure": str(item.get("failure_reason") or "")[:600],
                }
                for item in lost
            ],
            "decision_needed": "whether the ticket's tasks go back to work, and how",
            "recommendation": (
                "read the engineers' threads and the dispatcher logs; the failure is "
                "in how the on-call's answer is taken, not in the ticket's own fault"
            ),
            "scope": "task",
        },
    )
    if outcome == "refused":
        # Still told (escalate_to_owner banners a refusal); the ticket stays
        # in the lane, and without this guard the loop would never end.
        return False
    if holds_its_tasks(incident):
        try:
            block_escalated_tasks(cfg, state, incident_id)
        except Exception:  # noqa: BLE001 - the ticket still holds them; she is told
            pass
    _append_event(
        state,
        "pipeline_engineer_lost_twice",
        lost[-1],
        at,
        detail=f"{incident_id} -> owner",
    )
    return True


def _graph_for_the_engineer(plan: Any, state: Any) -> Any:
    """The graph the engineer's descriptor may read: never a refused one's routing.

    The plan gate refuses the graph, and the on-call is still reserved - it
    is the one who comes for that very refusal. The invariant was that
    nothing is built from the unverified graph; the independent check found
    it held only as a comment: ``_build_descriptor`` took the model from the
    graph's ``model_strategy`` and the effort from the anchor task's
    ``reasoning``. Both are the graph's word, and a strategy corrupted by
    hand would even raise ModelRoutingError inside the reservation and roll
    back the completion that called it.

    Under a refused graph the engineer runs on her own Codex settings - no
    model and no effort are sent, exactly as for ``host-settings`` - the one
    choice that is hers and not the graph's. The anchor still gives only the
    task id; the working directory is the project root and the prompt is the
    incident package, neither read from the graph.
    """

    from dataclasses import replace

    from .plan_verification import PlanVerificationError, require_plan_verified

    try:
        require_plan_verified(plan, state)
    except PlanVerificationError:
        return replace(plan, model_strategy="host-settings")
    return plan
