"""R23 for stops: the same stop, closed twice by the on-call, goes to the owner.

Her rule R23: N attempts with the same normalized failure signature stop the
loop and produce a report instead of the next attempt; the report names the
signature, the count and what changed between the attempts; the count is
reset only by a change that touches the cause.

A stop had that bound only by accident. Until 61e80a6 an infrastructure stop
went BLOCKED at once; the closed ticket left an orphan, and the orphan sweep
sent its third ticket to the owner. R3 then made the stop HOLD its task
instead (``stop_holds``): the on-call repairs, closes the ticket, the task
takes up the same action again. Correct by R3 - and the only bound was gone.
The independent check drove the real path six rounds (a worker answering
``BLOCKED ENVIRONMENT_FAILURE``, the on-call closing its ticket): six
tickets, six engineers, the task back in READY every time, no signal to her.
The plan gate and the orphan sweep each carried a copy of the bound; a
NO_SUCCESSOR ticket or a verifier nobody could route had none at all.

So the bound lives in the door, once, for every stop:

- the signature is the run, the stop's kind, its plan change, the tasks it holds,
  the reason code it named and the cause it names, if any (``signal_key``) -
  a different code or cause is a different failure;
- the attempts are the tickets with that signature the on-call CLOSED;
- her answer to a ticket that was handed to her is the change that touches
  the cause (her decision) and starts the count again. A ticket she swept
  shut with Resume while it still sat in the on-call's lane was not a
  question she answered, so it counts like any other closure;
- at ``MAX_REPEATED_STOPS`` closures the new ticket is not given to a third
  engineer - a third would find what the first two found. It goes to her as
  RECOVERY_EXHAUSTED, with the report, and the tasks the stop only held
  become BLOCKED: that is R3's "after DevOps is exhausted".

The engineer is not skipped: it came, twice, for this very stop. What is
skipped is the next attempt R23 forbids.
"""

from __future__ import annotations

from typing import Any, Mapping

# How many times the on-call may close the same stop and see it come back
# before the next one goes to the owner. Two is the bound the orphan sweep
# and the plan gate already used, each on its own.
MAX_REPEATED_STOPS = 2
MAX_REPORTED_CLOSURES = 5
MAX_NOTE_CHARS = 600

# What she is asked, by the kind of stop. Anything not named here gets the
# general question: whether the tasks go back to work.
_QUESTIONS = {
    "plan_unverified": (
        "whether the current plan is the one to run",
        "verify the plan again or change it through the replanner",
    ),
    "orphan_block": (
        "whether the stopped task goes back to work",
        "read the last ticket's diagnosis, then unblock the task or change the plan",
    ),
    "no_successor": (
        "whether the run may continue while the reservation leaves ready work idle",
        "read the last ticket's diagnosis; the reservation needs a repair the on-call could not make",
    ),
}


def repeat_signature(incident: Mapping[str, Any]) -> str:
    """The normalized failure signature of one stop ticket (R23)."""

    system = incident.get("system_state") or {}
    tasks = ",".join(sorted(str(item) for item in incident.get("affected_task_ids") or ()))
    parts = [
        str(system.get("run_id") or ""),
        str(system.get("stop_kind") or ""),
        str(system.get("plan_change_id") or ""),
        tasks or "run",
        str(system.get("reason_code") or ""),
    ]
    # A stop that names its cause (blocked_runs ``signal_key``) is that
    # cause's failure: R5 placement tickets hold no task and share a reason
    # code, and without the key two closures of isolation_not_proven sent
    # a first runtime_roots_widened straight to her.
    if system.get("signal_key"):
        parts.append(str(system["signal_key"]))
    return "|".join(parts)


def _answered_by_her(incident: Mapping[str, Any], journal: list[Mapping[str, Any]]) -> bool:
    if not incident.get("escalated_at") or not incident.get("resolved_at"):
        return False
    return any(
        str(item.get("event")) == "escalation_resolved_by_user"
        and str(item.get("incident_id")) == str(incident.get("incident_id"))
        for item in journal
    )


def closures_since_her_answer(
    loaded: Mapping[str, Any], incident: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Earlier tickets of the same stop that were closed and came back."""

    from .pipeline_engineer import STOP_CODE_PREFIX

    signature = repeat_signature(incident)
    journal = list(loaded.get("journal") or [])
    closed: list[dict[str, Any]] = []
    for item in loaded.get("incidents") or []:
        if str(item.get("incident_id")) == str(incident.get("incident_id")):
            break
        if not str(item.get("code") or "").startswith(STOP_CODE_PREFIX):
            continue
        if repeat_signature(item) != signature:
            continue
        if _answered_by_her(item, journal):
            closed = []
        elif item.get("resolved_at"):
            closed.append(dict(item))
    return closed


def _what_each_closure_did(loaded: Mapping[str, Any], closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ledger = loaded.get("signatures") or {}
    report = []
    for item in closed[-MAX_REPORTED_CLOSURES:]:
        entry = ledger.get(str(item.get("signature") or "")) or {}
        resolution = next(
            (
                row
                for row in reversed(entry.get("resolutions") or [])
                if str(row.get("at")) == str(item.get("resolved_at"))
            ),
            {},
        )
        report.append(
            {
                "incident_id": item.get("incident_id"),
                "closed_at": item.get("resolved_at"),
                "actions": list(resolution.get("actions") or ()),
                "note": str(resolution.get("note") or "")[:MAX_NOTE_CHARS],
                "healthcheck": list((item.get("healthcheck") or {}).get("checks") or ()),
                # What the on-call returned to work, and on what grounds: the
                # lift that did not hold is the heart of this report.
                "returns": [dict(entry) for entry in item.get("returns") or ()][-3:],
            }
        )
    return report


def bound_repeated_stop(
    cfg: Any, state: Any, incident_id: str, *, at: str, reason: str = ""
) -> bool:
    """Send a stop the on-call has already closed twice to the owner. True if sent.

    Called by the door for a freshly opened ticket only; a stop repeated
    while its ticket is still open reuses that ticket and is not a new
    attempt. Never raises: the door's own contract is that recording a stop
    may never prevent one.
    """

    from .blocked_runs import escalate_to_owner
    from .pipeline_engineer import PipelineIncidentStore

    try:
        loaded = PipelineIncidentStore(cfg.state_dir).load()
        incident = next(
            item for item in loaded.get("incidents") or [] if str(item.get("incident_id")) == incident_id
        )
        closed = closures_since_her_answer(loaded, incident)
    except Exception:  # noqa: BLE001 - an unreadable journal is itself a matter for doctor
        return False
    if len(closed) < MAX_REPEATED_STOPS:
        return False
    system = incident.get("system_state") or {}
    stop_kind = str(system.get("stop_kind") or "")
    tasks = ", ".join(incident.get("affected_task_ids") or ()) or "the run"
    decision, recommendation = _QUESTIONS.get(
        stop_kind,
        (
            f"whether {tasks} goes back to work, and on what premise",
            "read what the on-call repaired each time; change the premise "
            "(the environment, the resource, the plan) before unblocking",
        ),
    )
    repaired = _what_each_closure_did(loaded, closed)
    diagnosis = (
        f"The same stop came back after the on-call closed it {len(closed)} times "
        f"(signature {repeat_signature(incident)}; attempt {len(closed) + 1}). "
        f"Reason on record now: {reason or incident.get('summary') or ''}"
    )
    outcome = escalate_to_owner(
        cfg,
        incident_id,
        code="RECOVERY_EXHAUSTED",
        detail=diagnosis,
        at=at,
        escalation={
            "diagnosis": diagnosis,
            "repaired": repaired,
            "decision_needed": decision,
            "recommendation": recommendation,
            "scope": "task",
        },
    )
    if outcome != "escalated":
        return False
    from .stop_holds import block_escalated_tasks, holds_its_tasks

    if holds_its_tasks(incident):
        try:
            block_escalated_tasks(cfg, state, incident_id)
        except Exception:  # noqa: BLE001 - the ticket still holds them; the owner is told
            pass
    return True
