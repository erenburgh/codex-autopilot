"""Every stop opens a ticket, and every ticket reaches the on-call first.

A run has several ways to stop: a task exhausts its hiring ladder, a
replanner uses every attempt, a verifier returns three unreadable verdicts,
plan verification is refused, the engineer closes a fault and leaves no
successor. They were written separately and stopped separately. Only one of
them - the engineer's own escalation - opened a ticket and handed it to the
owner. The rest set BLOCKED, wrote a reason into run state, and went quiet.

What that looks like from outside: the run stands still, the incident journal
is empty, and the owner asks why nobody came. On a real run (23 Sep 2026) 75
minutes ended at BLOCKED with no incident file on disk at all.

0.13.0 made every stop open a ticket. It did not make every ticket reach
anyone. Two stops (the ladder, a worker's own BLOCKED) opened theirs with
routing switched off so the on-call would not take the dispatcher from the
neighbours; the ticket was to be routed "when the run has nothing left" - by
a branch that ran after the reservation, so nobody reserved the engineer,
and the door itself set the run to BLOCKED, which the wake-up skipped. Read
end to end: the engineer never came. The owner's requirement is the other
way round, without exceptions: any stop calls the on-call, and the on-call
decides what reaches the owner.

So the door now does three things, and no longer a fourth:

- opens an incident - a fresh one for every stop, never an old closed one;
- routes it to the on-call, always (the engineer works next to the
  neighbours, not instead of them - see ``engineer_reservation``) - unless
  the on-call already closed this very stop twice and it came back: then
  R23 sends it to the owner with a report (``stop_repeats``);
- records the reason where the status command reads it;
- and does NOT set the run's status. BLOCKED is derived in ``run_status``
  from what is left to do, in one place, after the engineer was reserved.

Class is RUNTIME throughout: a run that cannot proceed is infrastructure, not
product quality. Opening a ticket does not change who decides; it changes
whether anyone is told. The store executes nothing: no shell, no Codex task,
no approval, no transport.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .pipeline_engineer import STOP_CODE_PREFIX

__all__ = ["STOP_CODE_PREFIX", "escalate_to_owner", "stop_run"]


def stop_run(
    cfg: Any,
    state: Any,
    *,
    stop_kind: str,
    phase: str,
    reason: str,
    summary: str,
    at: str,
    task_ids: Sequence[str] = (),
    context_task_id: str = "",
    plan_change_id: str = "",
    system_state: dict[str, Any] | None = None,
    recent_events: Sequence[dict[str, str]] = (),
) -> str | None:
    """Stop a task through the one door: ticket, on-call, reason.

    `task_ids` are the tasks the stop HOLDS: the ticket pauses exactly those
    until it closes. `context_task_id` only anchors the engineer's thread
    (its cwd and title) for a stop that holds nothing - a run-level stop, a
    reservation that found no successor. Mixing the two paused the very
    ready tasks a NO_SUCCESSOR ticket was about.

    Returns the incident id, or None when the ticket could not be written.
    Recording a stop may never prevent one, but it may not be silent either:
    a failure is announced to the owner and left in the run journal, and the
    task, still BLOCKED without a ticket, is picked up again by the orphan
    sweep of the next reservation.
    """

    affected = tuple(dict.fromkeys(str(item) for item in task_ids if item))
    try:
        filed: str | None = _file(
            cfg,
            state,
            stop_kind=stop_kind,
            phase=phase,
            reason=reason,
            summary=summary,
            at=at,
            affected=affected,
            context_task_id=str(context_task_id or ""),
            plan_change_id=str(plan_change_id or ""),
            system_state=system_state or {},
            recent_events=recent_events,
        )
    except Exception as exc:  # noqa: BLE001 - recording a stop may never prevent one
        filed = None
        _tell_owner(
            cfg,
            f"{', '.join(affected) or 'the run'} stopped and no ticket could be written: {reason}",
        )
        _journal(state, "run_stop_unfiled", at, affected, {"stop_kind": stop_kind, "error": str(exc)})
    if reason:
        state.last_error = reason
    if filed is not None:
        _journal(state, "run_stop_filed", at, affected, {"stop_kind": stop_kind, "incident_id": filed})
    return filed


def _file(
    cfg: Any,
    state: Any,
    *,
    stop_kind: str,
    phase: str,
    reason: str,
    summary: str,
    at: str,
    affected: tuple[str, ...],
    context_task_id: str,
    plan_change_id: str,
    system_state: dict[str, Any],
    recent_events: Sequence[dict[str, str]],
) -> str:
    from .engineer_authority import IncidentClass, SideEffectOutcome
    from .pipeline_engineer import IncidentPhase, IncidentSignal, PipelineIncidentStore

    store = PipelineIncidentStore(cfg.state_dir)
    run_id = str(getattr(state, "run_id", "") or "")
    # The ticket id is a hash of the signal id. It used to be run:phase:task,
    # and the ladder and a worker's BLOCKED both passed phase="BLOCKED": the
    # second stop of the same task - after the owner answered the first -
    # got the old RESOLVED ticket back from open_incident, and the on-call
    # never came. The kind, the plan change and an ordinal make every stop
    # its own ticket; the same stop repeated while its ticket is still open
    # reuses it, so a replayed completion does not file twice.
    base = f"{run_id}:{stop_kind}:{plan_change_id or '-'}:{':'.join(affected) or 'run'}"
    same = [
        item
        for item in store.load().get("incidents") or []
        if str(item.get("signal_id") or "") == base
        or str(item.get("signal_id") or "").startswith(base + "#")
    ]
    still_open = next((item for item in reversed(same) if not item.get("resolved_at")), None)
    if still_open is not None:
        incident = still_open
    else:
        incident = store.open_incident(
            IncidentSignal(
                signal_id=f"{base}#{len(same) + 1}",
                code=f"{STOP_CODE_PREFIX}{phase}",
                surface=IncidentClass.RUNTIME,
                summary=(
                    f"{summary} The on-call looks first: what it can repair it "
                    f"repairs; a decision that is yours comes to you with its "
                    f"diagnosis. Reason on record: {reason}"
                ),
                affected_task_ids=affected,
                context_task_id=context_task_id,
                # `operation` names a transport call whose far-side outcome is
                # in doubt. A stop is not one: nothing was sent.
                operation="",
                side_effect_outcome=SideEffectOutcome.NONE,
                system_state={
                    "run_id": run_id,
                    "phase": phase,
                    "stop_kind": stop_kind,
                    "plan_change_id": plan_change_id,
                    **system_state,
                },
                recent_events=tuple(recent_events),
            ),
            at=at,
        )
    incident_id = str(incident["incident_id"])
    # Always routed. A fresh ticket sits in DEGRADED, and the on-call is
    # reserved only for tickets in its own lane - a ticket left in DEGRADED
    # is a ticket nobody reads. Routing is idempotent.
    if str(incident.get("phase")) in {
        IncidentPhase.DEGRADED.value,
        IncidentPhase.AUTO_RECOVERY_FAILED.value,
    }:
        store.ensure_pipeline_engineer(incident_id, at=at)
    if still_open is None:
        # R23: the same stop the on-call already closed twice goes to the
        # owner with a report, not to a third engineer (stop_repeats).
        from .stop_repeats import bound_repeated_stop

        bound_repeated_stop(cfg, state, incident_id, at=at, reason=reason)
    return incident_id


def escalate_to_owner(
    cfg: Any,
    incident_id: str,
    *,
    code: str,
    detail: str,
    at: str,
    escalation: Mapping[str, Any] | None = None,
) -> str:
    """The on-call hands one ticket to the owner - and only that ticket.

    It used to go through the door with the ticket id and stop the WHOLE
    run: BLOCKED without a reservation or a derived status, so tasks that
    had nothing to do with the ticket froze with it. Now the escalation
    moves the ticket to ESCALATE_TO_USER and nothing else: the ticket's own
    tasks stay held by it, everything else goes on. The whole run waits only
    when the on-call says so (``scope: run`` -> ``blocks_run``).

    Returns "escalated", "answered" (the owner closed the ticket while the
    engineer was still writing - the escalation is a note, the ticket is not
    reopened) or "refused" (the store would not take it; the owner is told
    anyway, because a decision nobody sees is one never asked).
    """

    from .pipeline_engineer import PipelineIncidentStore

    scope = str((escalation or {}).get("scope") or "task")
    try:
        store = PipelineIncidentStore(cfg.state_dir)
        loaded = store.load()
        answered = any(
            str(item.get("event")) == "escalation_resolved_by_user"
            and str(item.get("incident_id")) == incident_id
            for item in loaded.get("journal") or []
        )
        if answered:
            return "answered"
        store.escalate_incident_to_user(
            incident_id,
            reason_code=code,
            at=at,
            detail=detail,
            escalation=escalation,
            blocks_run=scope == "run",
        )
        incident = next(
            (
                item
                for item in loaded.get("incidents") or []
                if str(item.get("incident_id")) == incident_id
            ),
            {},
        )
        tasks = ", ".join(incident.get("affected_task_ids") or ()) or "the run"
        _tell_owner(cfg, f"{tasks} waits for your decision: {code}")
        return "escalated"
    except Exception:  # noqa: BLE001 - a refused escalation must still be heard
        _tell_owner(cfg, f"ticket {incident_id} needs your decision ({code}): {detail}")
        return "refused"


def _tell_owner(cfg: Any, message: str) -> None:
    """A banner, when she turned banners on. Never raises, never waits."""

    runtime = getattr(cfg, "runtime", None)
    # `is True`, not truthiness: a stand-in config answers every attribute.
    if getattr(runtime, "desktop_notifications", False) is not True:
        return
    try:
        from .notify import notify

        notify(cfg, "Codex Autopilot", getattr(getattr(cfg, "root", None), "name", ""), message)
    except Exception:  # noqa: BLE001 - a banner may never fail the run
        return


def _journal(
    state: Any, event: str, at: str, affected: tuple[str, ...], detail: dict[str, Any]
) -> None:
    try:
        from .resilience import append_resilience_event

        append_resilience_event(
            state, event, at=at, task_id=affected[0] if affected else None, detail=detail
        )
    except Exception:  # noqa: BLE001 - the journal line is a courtesy
        return
