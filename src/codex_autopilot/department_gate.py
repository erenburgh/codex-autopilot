"""A verifier needs a lead and a rubric; without them, that one task stops (R30).

"Fail closed" at the verifier's launch used to be a raise. Nothing caught it
but a ModelRoutingError (and a ContextBoundaryError only for screening): the
exception rolled back the whole reservation pass - and the completion of a
NEIGHBOUR that had called it - and no ticket was filed. One task with no lead
froze the run in silence, the opposite of her rule: the run goes on without
her, a stop is a runtime fault, and every stop calls the on-call.

Now the verifier's reservation asks first (``admit_verifier``), under the
coordinator lock it already holds: its route, the department of the task's
profession, and the department's rubric - writing version 1 when there is
none, which is how R30 lands on a run already in progress. A refusal is a
stop of that task alone, through the one door (``blocked_runs.stop_run``):
the task stays IMPLEMENTED, held by a ticket for the on-call, and the
neighbours are reserved in the same pass. A refusal while the prompt is
being built (``build_or_hold``) is the same stop, with what the reservation
had already taken given back.

What the on-call can do about it (``engineer_authority.STOP_MEANS``): have
the plan changed so the task's profession names its lead
(``request_plan_change`` - the replanner's graph is refused until every
touched task has one, ``validate_department_leads``); supersede a stray
record that made the department's rubric history ambiguous
(``supersede_rubric_record``, audited in Project Memory); repair the runtime;
or return the task once the cause is gone.
"""

from __future__ import annotations

from typing import Any, Callable

from .department_acceptance import DepartmentAcceptanceError, rubric_scope, stray_rubric_records
from .department_runtime import load_task_department_acceptance
from .engineer_stop_actions import EngineerStopActionError

DEPARTMENT_STOP_KIND = "department_lead"


def admit_verifier(cfg: Any, plan: Any, state: Any, task: Any, memory: Any) -> str | None:
    """The verifier's execution mode, or None when this task was stopped."""

    from .lifecycle_base import _append_event, _latest_task_session
    from .models import ModelRoutingError
    from .run_state import utc_now
    from .verification import VerificationProtocolError, verifier_route

    try:
        route = verifier_route(plan, task)
    except ModelRoutingError as exc:
        _append_event(
            state, "verifier_routing_blocked", _latest_task_session(state, task.id), utc_now(),
            detail=str(exc),
        )
        # It used to stop with only last_error, then (0.13) BLOCKED and a
        # ticket. R3: routing is infrastructure - the task stays IMPLEMENTED,
        # held by the ticket until the on-call looks.
        _stop(cfg, state, task.id, "verifier_routing", "VERIFIER_ROUTING_BLOCKED", str(exc),
              f"No verifier could be routed for {task.id}.")
        return None
    except VerificationProtocolError as exc:
        stop_for_department(cfg, plan, state, task.id, str(exc))
        return None
    try:
        load_task_department_acceptance(memory, plan, task, ensure=True)
    except Exception as exc:  # noqa: BLE001 - any refusal here is this task's stop, never a raise
        stop_for_department(cfg, plan, state, task.id, str(exc))
        return None
    return route.execution_mode


def stop_for_department(cfg: Any, plan: Any, state: Any, task_id: str, reason: str) -> None:
    task = plan.task_map.get(task_id)
    role = getattr(task, "role", "?")
    ambiguous = "ambiguous" in reason or "supersedes" in reason
    recommendation = (
        "supersede the stray record in the department's rubric scope "
        "(devops-supersede-rubric), then return the task"
        if ambiguous
        else f"ask the replanner (devops-request-plan-change) to name the Lead Role of "
        f"profession {role!r} in verification.verifier_role of its tasks - one lead, "
        "not the profession itself"
    )
    _stop(
        cfg, state, task_id, DEPARTMENT_STOP_KIND, "DEPARTMENT_LEAD_BLOCKED",
        f"R30: {task_id} cannot be accepted by a department lead: {reason}",
        f"No department lead can accept {task_id}.",
        system_state={"diagnosis": reason, "recommendation": recommendation},
    )


def _stop(
    cfg: Any, state: Any, task_id: str, kind: str, phase: str, reason: str, summary: str,
    *, system_state: dict[str, Any] | None = None,
) -> None:
    from .blocked_runs import stop_run
    from .run_state import utc_now

    stop_run(
        cfg, state, stop_kind=kind, phase=phase, reason=reason, summary=summary,
        at=utc_now(), task_ids=(task_id,), system_state=system_state,
    )


def build_or_hold(
    cfg: Any,
    plan: Any,
    state: Any,
    task_id: str,
    build: Callable[[], Any],
    *,
    token: str,
    restore: dict[str, Any],
) -> Any:
    """Build a follow-up's descriptor; on a refusal give back what was taken and stop.

    ``restore`` is what the reservation changed before building: the task's
    attempt counter and revision bookkeeping, the worker sequence. The
    locks acquired under ``token`` are released.
    """

    from .ai_studio import ContextBoundaryError
    from .resources import release_resources_in_state
    from .run_state import utc_now
    from .verification import VerificationProtocolError

    try:
        return build()
    except (DepartmentAcceptanceError, ContextBoundaryError, VerificationProtocolError) as exc:
        release_resources_in_state(state, token, reason=f"launch refused: {exc}"[:500], now=utc_now())
        state.worker_sequence = restore["worker_sequence"]
        for field in ("task_attempts", "task_revisions", "task_revision_basis"):
            mapping = getattr(state, field)
            if restore.get(field) is None:
                mapping.pop(task_id, None)
            else:
                mapping[task_id] = restore[field]
        if isinstance(exc, ContextBoundaryError) and not isinstance(
            exc.__cause__, DepartmentAcceptanceError
        ):
            # Not a lead or a rubric: a prompt that cannot be built (R17 budget,
            # an unresolvable skill stack). The same stop, its own kind.
            _stop(cfg, state, task_id, "launch_refused", "LAUNCH_REFUSED",
                  f"{task_id}: {exc}", f"The follow-up session of {task_id} could not be built.")
        else:
            stop_for_department(cfg, plan, state, task_id, str(exc))
        return None


def snapshot(state: Any, task_id: str) -> dict[str, Any]:
    return {
        "worker_sequence": state.worker_sequence,
        **{
            field: getattr(state, field).get(task_id)
            for field in ("task_attempts", "task_revisions", "task_revision_basis")
        },
    }


def supersede_rubric_record(
    cfg: Any, *, incident_id: str, record_id: str, reason: str, thread_id: str, at: str | None = None
) -> dict[str, Any]:
    """The on-call's repair of an ambiguous rubric history: retire one stray record.

    Only the engineer holding a department_lead ticket, from its own thread,
    and only a stray record (``stray_rubric_records``): never the first
    record of a version, which is what earlier leads attested. It used to
    refuse every record the runtime wrote, and two runtime v1s - measured -
    left nothing it could retire. The record is not deleted - its status
    becomes superseded, with the ticket and the reason in Project Memory's
    audit - so the history reads 1..n again and the task can be returned.
    """

    from .engineer_stop_actions import _loaded_incident, require_engineer_thread
    from .memory import ProjectMemory
    from .pipeline_engineer import PipelineIncidentStore
    from .run_state import StateStore, utc_now

    timestamp = at or utc_now()
    text = str(reason or "").strip()
    if not text:
        raise EngineerStopActionError("a supersession needs its reason: --reason")
    state = StateStore(cfg.state_dir).load()
    _, incident = _loaded_incident(cfg, incident_id)
    require_engineer_thread(state, incident_id, thread_id)
    if str((incident.get("system_state") or {}).get("stop_kind") or "") != DEPARTMENT_STOP_KIND:
        raise EngineerStopActionError("only a department_lead stop supersedes a rubric record")
    memory = ProjectMemory(cfg.root)
    try:
        record = memory.get_record(record_id)
    except Exception as exc:  # noqa: BLE001 - named for the on-call
        raise EngineerStopActionError(str(exc)) from exc
    scope = str(record.get("scope") or "")
    prefix = rubric_scope("x")[:-1]
    if not scope.startswith(prefix):
        raise EngineerStopActionError(f"{record_id} is not in a department rubric scope")
    strays = stray_rubric_records(memory, scope[len(prefix):])
    if record_id not in strays:
        raise EngineerStopActionError(
            f"{record_id} is part of the department's canonical rubric history (the first "
            "verified record of its version); it is not superseded - the stray records are: "
            + (", ".join(strays) or "none")
        )
    memory._set_record_status(
        record_id, str(record.get("category") or "truth"), "superseded",
        f"pipeline-engineer:{incident_id}", f"{incident_id}: {text}"[:2000],
    )
    PipelineIncidentStore(cfg.state_dir).record_engineer_action(
        incident_id,
        field="rubric_supersessions",
        event="rubric_record_superseded_by_engineer",
        entry={"record_id": record_id, "scope": scope, "at": timestamp},
        at=timestamp,
    )
    return {"record_id": record_id, "status": "superseded"}
