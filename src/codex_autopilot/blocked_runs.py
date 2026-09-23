"""Every stop opens a ticket, and every ticket reaches the owner.

A run has several ways to stop: a task exhausts its hiring ladder, a
replanner uses every attempt, a verifier returns three unreadable verdicts,
plan verification is refused, the engineer closes a fault and leaves no
successor. They were written separately and stopped separately. Only one of
them - the engineer's own escalation - opened a ticket and handed it to the
owner. The rest set BLOCKED, wrote a reason into run state, and went quiet.

What that looks like from outside: the run stands still, the incident journal
is empty, and the owner asks why nobody came. Nobody was called. On a real
run (23 Sep 2026) 75 minutes ended at BLOCKED with no incident file on disk
at all.

So there is one door now. Whatever the reason, a stop:

- opens an incident, so the on-call has something to look at;
- hands it to the owner with the reason and the decision that is theirs;
- then sets the run's status.

Class is RUNTIME throughout: a run that cannot proceed is infrastructure, not
product quality. That distinction is not cosmetic - PRODUCTION is excluded
from the engineer's remit on purpose, because accepting work is the owner's
call and no automaton may make it. Opening a ticket does not change who
decides; it changes whether anyone is told.

The store executes nothing: no shell, no Codex task, no approval, no
transport. A stop that stops is still a stop. This only makes it visible.
"""

from __future__ import annotations

from typing import Any, Sequence


# Named so a reader of the incident journal can tell a stop from a transport
# fault at a glance, and so a runbook can never match one by accident: a run
# that stopped needs a person, not a retry.
STOP_CODE_PREFIX = "run_stopped:"


def stop_run(
    cfg: Any,
    state: Any,
    *,
    phase: str,
    reason: str,
    summary: str,
    at: str,
    task_ids: Sequence[str] = (),
    incident_id: str | None = None,
    system_state: dict[str, Any] | None = None,
    recent_events: Sequence[dict[str, str]] = (),
    route: bool = True,
    escalation_code: str = "",
) -> str | None:
    """Stop the run through the one door: ticket, owner, then status.

    `route` says whether to call the on-call now. An incident in the
    engineer's lane outranks every task - the dispatcher reserves the
    engineer instead of taking work - so a single task stopping while its
    neighbours can still run must not route: that would halt a run that is
    not stuck. Its ticket waits, and `route_pending_stops` calls the engineer
    at the moment the run really does have nothing left to do.

    Returns the incident id, or None when the ticket could not be written.
    Recording a stop may never prevent one, so a failure here is swallowed
    and the run still stops - with `phase` and `reason` on the state, which
    is what the status command reads.
    """

    filed = _file_and_escalate(
        cfg,
        state,
        phase=phase,
        reason=reason,
        summary=summary,
        at=at,
        task_ids=task_ids,
        incident_id=incident_id,
        route=route,
        escalation_code=escalation_code,
        system_state=system_state or {},
        recent_events=recent_events,
    )
    state.status = "BLOCKED"
    state.phase = phase
    if reason:
        state.last_error = reason
    return filed


def _file_and_escalate(
    cfg: Any,
    state: Any,
    *,
    phase: str,
    reason: str,
    summary: str,
    at: str,
    task_ids: Sequence[str],
    incident_id: str | None,
    route: bool,
    escalation_code: str,
    system_state: dict[str, Any],
    recent_events: Sequence[dict[str, str]],
) -> str | None:
    from .engineer_authority import IncidentClass, SideEffectOutcome
    from .pipeline_engineer import IncidentSignal, PipelineIncidentStore

    try:
        store = PipelineIncidentStore(cfg.state_dir)
        filed_here = incident_id is None
        if incident_id is None:
            run_id = str(getattr(state, "run_id", "") or "")
            incident = store.open_incident(
                IncidentSignal(
                    signal_id=f"{run_id}:{phase}:{':'.join(task_ids) or 'run'}",
                    code=f"{STOP_CODE_PREFIX}{phase}",
                    surface=IncidentClass.RUNTIME,
                    # What the owner will be asked, written on the ticket
                    # itself: a person opening the journal should see what is
                    # wanted of them, not a status word.
                    summary=(
                        f"{summary} The run is stopped and waiting for you. If "
                        f"this outcome is acceptable, the decision is yours to "
                        f"make: lift the stop with an instruction, or change the "
                        f"plan. Reason on record: {reason}"
                    ),
                    affected_task_ids=tuple(item for item in task_ids if item),
                    # `operation` names a transport call whose far-side
                    # outcome is in doubt. A stop is not one: nothing was
                    # sent, so nothing can be half-done.
                    operation="",
                    side_effect_outcome=SideEffectOutcome.NONE,
                    system_state={"run_id": run_id, "phase": phase, **system_state},
                    recent_events=tuple(recent_events),
                ),
                at=at,
            )
            incident_id = str(incident["incident_id"])
        # Opening a ticket is not the same as handing it to anyone. A fresh
        # incident sits in DEGRADED, and the on-call is reserved only for
        # incidents in the engineer's own phase - so a ticket left at DEGRADED
        # is a ticket nobody ever reads. Routing is idempotent and moves it
        # through "no runbook can help" to the engineer's lane.
        #
        # It also makes the stop answerable: resuming closes exactly the
        # tickets waiting on a human, and those are the ones the engineer
        # holds or has escalated. A DEGRADED ticket is not among them.
        if filed_here:
            # A ticket this stop opened goes to the on-call, who has not seen
            # it yet - unless the run can still do other work, in which case
            # it waits rather than taking the dispatcher away from them.
            if route:
                store.ensure_pipeline_engineer(incident_id, at=at)
        else:
            # A ticket handed in by the engineer is the other direction: it
            # has looked, it cannot make this call, and it is giving the
            # decision to the owner. That handover is the signal the owner
            # waits for, and without it a stop is silent again.
            # R13: the code comes from a closed list and the engineer is the
            # one who names it. A run phase is not one of them, and passing
            # one refused the escalation - silently, which is how the signal
            # to the owner went missing in the first place.
            store.escalate_incident_to_user(
                incident_id,
                reason_code=escalation_code,
                at=at,
                detail=(
                    f"{summary} The run is stopped and waiting for you. "
                    f"Reason on record: {reason}"
                ),
            )
        return incident_id
    except Exception:  # noqa: BLE001 - recording a stop may never prevent one
        return None



def route_pending_stops(cfg: Any) -> None:
    """Call the on-call for stops whose tickets are still waiting.

    A task that stopped while its neighbours worked filed a ticket and left
    it there on purpose: routing it would have taken the dispatcher away from
    work that could still be done. Once the run itself has nothing left to
    run, that reason is gone and the engineer is exactly who should look.

    Idempotent: routing a ticket already in the engineer's lane returns it
    unchanged, and one already handed to the owner is left alone.
    """

    from .pipeline_engineer import IncidentPhase, PipelineIncidentStore

    try:
        store = PipelineIncidentStore(cfg.state_dir)
        loaded = store.load().get("incidents") or {}
        incidents = loaded.values() if isinstance(loaded, dict) else loaded
        waiting = [
            str(item["incident_id"])
            for item in incidents
            if str(item.get("code", "")).startswith(STOP_CODE_PREFIX)
            and str(item.get("phase")) == IncidentPhase.DEGRADED.value
        ]
    except Exception:  # noqa: BLE001 - housekeeping may never stop a run
        return
    for incident_id in waiting:
        try:
            store.ensure_pipeline_engineer(incident_id, at=_now())
        except Exception:  # noqa: BLE001 - one sick ticket does not block the rest
            continue


def _now() -> str:
    from .run_state import utc_now

    return utc_now()
