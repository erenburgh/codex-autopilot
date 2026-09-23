"""What the on-call may do to a stopped task: return it, or ask the replanner.

Until now the engineer could repair the pipeline and close its ticket, and
that was all. A task the ticket had stopped stayed BLOCKED: closing a ticket
does not lift a stop, and only her ``unblock`` did. The on-call "repaired" and
left; the orphan sweep filed a new ticket about the very task; the second
closure sent it to her. Repairs were real and invisible, and every stop
became hers by construction - the opposite of her requirement.

Two actions close that gap (``engineer_authority.REPAIR_ACTIONS``):

- ``return_stopped_task`` (``devops-return-task``): a task the ticket holds
  goes from BLOCKED back to READY, or to IMPLEMENTED when a verdict was ever
  given - the same rule as her unblock (``owner_answers.unblock_target``).
  VERIFIED is unreachable: acceptance stays the verifier's (R29).
- ``request_plan_change`` (``devops-request-plan-change``): the replanner is
  asked on the task's behalf, exactly as a worker asks - the plan is changed
  only by the replanner, never here.

Both are bounded the way her rules bound everything the engineer does:

- only the engineer holding THIS ticket may act, from its own thread: the
  command reads CODEX_THREAD_ID and requires it to be the thread of the
  pending on-call session reserved for this incident. Any thread of the run
  used to pass ``require_engineer_incident`` - a worker of another task could
  lift a stop;
- only what the means table allows for this kind of stop
  (``engineer_authority.STOP_MEANS``, which the engineer cannot edit), and
  never a stop that is hers (PRODUCT_DECISION, ARCHITECTURE_DECISION);
- a task at the top of its hiring ladder returns only after a change that
  touched the cause (R23): a runtime patch on this ticket that is staged or
  installed and changed the acceptance path (``ladder_grants``: read from
  the staged texts over the explicit list in ``engineer_authority``), each
  patch good for one grant - or through a plan change. The gate is the
  task's ladder, not the ticket's kind, and a grant whose patch is later
  withdrawn, refused or reverted is revoked (``ladder_grants.revoke_grants``);
- the runtime patch commands are bound the same way (``require_patch_holder``,
  ``take_back_patch``): a patch buys a fresh hire and drains the whole run,
  and the independent check found both doors open to any thread of the run
  - a worker of the very task being judged included;
- the same task returned from the same stop twice, and back again, is not
  returned a third time. That counter is the door's own R23 bound
  (``stop_repeats``), per task and stop signature: a return is followed by
  the ticket's closure, the stop that comes back after two closures goes to
  her as RECOVERY_EXHAUSTED, and its report lists what each return did.
  A second counter here could never fire before it.

And the ticket of a stop may not be closed as if the stop were gone:
``require_stop_ticket_closable`` refuses a closure named with diagnostics
only, and one that leaves a task it holds BLOCKED with no plan change this
ticket asked for. Such a closure used to leave the task waiting for her in
silence - BLOCKED, no open ticket, nobody told. Since an infrastructure stop
only holds its task, closing its ticket IS the return, so the closure is
bound like one: this ticket's on-call from its own thread, only the means
of the table, and each repair it names must have happened.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .lifecycle_base import PENDING_SESSION_STATUSES, _append_event
from .run_state import StateStore, utc_now
from .task_state import TaskState, transition_task


class EngineerStopActionError(RuntimeError):
    """The on-call's action on a stopped task was refused; nothing changed."""


def require_engineer_thread(state: Any, incident_id: str, thread_id: str) -> dict[str, Any]:
    """The pending on-call session of this ticket, run from its own thread."""

    thread = str(thread_id or "").strip()
    for item in getattr(state, "worker_sessions", None) or ():
        if (
            item.get("kind") == "pipeline_engineer"
            and str(item.get("incident_id") or "") == incident_id
            and item.get("status") in PENDING_SESSION_STATUSES
            and thread
            and str(item.get("thread_id") or "") == thread
        ):
            return item
    raise EngineerStopActionError(
        f"only the on-call engineer holding ticket {incident_id} may act on its tasks, "
        "from its own thread (CODEX_THREAD_ID)"
    )


def _loaded_incident(cfg: Any, incident_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from .pipeline_engineer import PipelineIncidentStore

    store = PipelineIncidentStore(cfg.state_dir)
    incident = store.require_engineer_incident(incident_id)
    return store.load(), incident


def _is_stop(incident: Mapping[str, Any]) -> bool:
    from .pipeline_engineer import STOP_CODE_PREFIX

    return str(incident.get("code") or "").startswith(STOP_CODE_PREFIX)


def _allowed(incident: Mapping[str, Any], action: str) -> bool:
    from .stop_diagnosis import means_for

    return not _is_stop(incident) or action in means_for(incident)


def acceptance_patches(incident: Mapping[str, Any], state_dir: Any) -> list[dict[str, Any]]:
    """Runtime patches on this ticket that are live and changed the acceptance path.

    The ticket only says which patches are this engineer's; whether one is
    still staged or installed, and what it changed, is read from the staged
    entry itself (``ladder_grants``). The ticket's record is written at
    staging, and a patch withdrawn or refused afterwards used to count.
    """

    from pathlib import Path

    from .ladder_grants import acceptance_path_changes

    found = []
    for patch in incident.get("runtime_patches") or ():
        if not isinstance(patch, Mapping):
            continue
        changes = acceptance_path_changes(Path(state_dir), str(patch.get("patch_id") or ""))
        if changes:
            found.append({**dict(patch), "acceptance_changes": changes})
    return found


def _consumed_patch_ids(loaded: Mapping[str, Any]) -> set[str]:
    return {
        str(patch_id)
        for item in loaded.get("incidents") or ()
        for entry in item.get("returns") or ()
        for patch_id in (entry.get("grounds") or {}).get("runtime_patch_ids") or ()
    }


def return_stopped_task(
    cfg: Any, *, incident_id: str, task_id: str, thread_id: str, at: str | None = None
) -> dict[str, Any]:
    """Return one task this ticket stopped to work. Refuses rather than guesses."""

    from .engineer_authority import OWNER_STOP_REASONS
    from .owner_answers import unblock_target
    from .pipeline_engineer import PipelineIncidentStore
    from .plan import load_plan
    from .resources import ResourceLockCoordinator
    from .revision_budget import at_top_of_ladder, grant_fresh_hire
    from .stop_diagnosis import means_for

    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        loaded, incident = _loaded_incident(cfg, incident_id)
        session = require_engineer_thread(state, incident_id, thread_id)
        plan = load_plan(cfg.state_dir, cfg.profile)
        system = incident.get("system_state") or {}
        kind = str(system.get("stop_kind") or "")
        if task_id not in [str(item) for item in incident.get("affected_task_ids") or ()]:
            raise EngineerStopActionError(f"ticket {incident_id} does not hold {task_id}")
        if str(system.get("reason_code") or "") in OWNER_STOP_REASONS:
            raise EngineerStopActionError(
                f"{task_id} stopped with {system.get('reason_code')}: that decision is the "
                "owner's - diagnose it and escalate with the same code"
            )
        if not _allowed(incident, "return_stopped_task"):
            raise EngineerStopActionError(
                f"a {kind or 'stop'} is not lifted by a return; its means are: "
                + (", ".join(means_for(incident)) or "a diagnosis and an escalation")
            )
        if state.task_states.get(task_id) != TaskState.BLOCKED.value:
            raise EngineerStopActionError(
                f"{task_id} is {state.task_states.get(task_id)}, not BLOCKED: the ticket only "
                "holds it, and closing the ticket returns it"
            )
        grounds: dict[str, Any] = {}
        grant: dict[str, Any] | None = None
        # The ladder, not the ticket's kind: a task whose revoked grant sent
        # it back comes under a runtime_patch_refused ticket, still spent.
        if kind == "ladder_exhausted" or at_top_of_ladder(plan, state, task_id):
            consumed = _consumed_patch_ids(loaded)
            fresh = [
                str(patch.get("patch_id"))
                for patch in acceptance_patches(incident, cfg.state_dir)
                if str(patch.get("patch_id")) not in consumed
            ]
            if not fresh:
                raise EngineerStopActionError(
                    f"{task_id} is at the top of its hiring ladder: it returns only after a "
                    "runtime patch on this ticket that changed the acceptance path "
                    "(R23: the cause must change), or through devops-request-plan-change"
                )
            grounds = {"runtime_patch_ids": fresh}
            grant = grant_fresh_hire(plan, state, task_id, grounds=grounds)
        target = unblock_target(state, task_id)
        # VERIFIED is unreachable from here: the target is READY or
        # IMPLEMENTED, and acceptance stays the verifier's (R29).
        assert target in {TaskState.READY, TaskState.IMPLEMENTED}
        state.task_states = transition_task(plan, state.task_states, task_id, target)
        entry = {
            "task_id": task_id,
            "to": target.value,
            "grounds": grounds,
            "thread_id": str(session.get("thread_id") or ""),
            "at": timestamp,
        }
        if grant:
            # Kept so the grant can be taken back exactly if its patch is.
            entry["grant"] = {
                "rehires_before": grant["rehires_before"],
                "rehires_now": grant["rehires_now"],
            }
        PipelineIncidentStore(cfg.state_dir).record_engineer_action(
            incident_id, field="returns", event="stopped_task_returned", entry=entry, at=timestamp
        )
        _append_event(
            state,
            "stopped_task_returned",
            session,
            timestamp,
            detail=f"{incident_id}: {task_id} -> {target.value}"
            + (f" (fresh hire at {grant['effort']})" if grant else ""),
        )
        store.save(state)
    return {"returned": True, "task_id": task_id, "to": target.value, "grant": grant}


def request_plan_change(
    cfg: Any,
    *,
    incident_id: str,
    task_id: str,
    reason: str,
    thread_id: str,
    kind: str = "prerequisite",
    change: Mapping[str, Any] | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """Ask the replanner on a stopped task's behalf - the way its worker would."""

    from .pipeline_engineer import PipelineIncidentStore
    from .plan import load_plan
    from .resilience import (
        PLAN_CHANGE_REQUEST_KINDS,
        PlanChangeRequest,
        _validate_change_payload,
        register_plan_change_request,
    )
    from .resources import ResourceLockCoordinator

    timestamp = at or utc_now()
    text = str(reason or "").strip()
    if not text:
        raise EngineerStopActionError("a plan change needs its reason: --reason")
    if kind not in PLAN_CHANGE_REQUEST_KINDS:
        raise EngineerStopActionError(
            f"plan change kind must be one of {sorted(PLAN_CHANGE_REQUEST_KINDS)}"
        )
    payload = dict(change) if change is not None else {"description": text[:4000]}
    _validate_change_payload(kind, payload)
    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        _, incident = _loaded_incident(cfg, incident_id)
        session = require_engineer_thread(state, incident_id, thread_id)
        plan = load_plan(cfg.state_dir, cfg.profile)
        named = [str(item) for item in incident.get("affected_task_ids") or ()]
        named.append(str(incident.get("context_task_id") or ""))
        if task_id not in named or task_id not in plan.task_map:
            raise EngineerStopActionError(f"ticket {incident_id} is not about {task_id}")
        if not _allowed(incident, "request_plan_change"):
            raise EngineerStopActionError(
                "this kind of stop is not re-planned by the on-call; see stop_context.means"
            )
        if state.active_plan_change_id is not None:
            raise EngineerStopActionError(
                f"plan change {state.active_plan_change_id} is already active; one at a time"
            )
        # R11: a task with a live producer is not re-planned under it.
        if task_id in state.active_task_ids:
            raise EngineerStopActionError(f"{task_id} is running; it is not re-planned under a worker")
        if state.task_states.get(task_id) != TaskState.BLOCKED.value:
            # The replanner's reservation takes the requester from BLOCKED,
            # exactly as after a worker's own request.
            state.task_states = transition_task(plan, state.task_states, task_id, TaskState.BLOCKED)
        request = PlanChangeRequest(
            request_version=1,
            kind=kind,
            target_task_id=task_id,
            summary=text[:240],
            rationale=(
                f"Requested by the on-call engineer for ticket {incident_id} "
                f"({incident.get('code')}): {text}"
            )[:4000],
            change=payload,
        )
        record = register_plan_change_request(
            state,
            request,
            requester_task_id=task_id,
            requester_session_token=str(session["reservation_token"]),
            at=timestamp,
        )
        record["requested_by_incident"] = incident_id
        PipelineIncidentStore(cfg.state_dir).record_engineer_action(
            incident_id,
            field="plan_change_requests",
            event="plan_change_requested_by_engineer",
            entry={"task_id": task_id, "plan_change_id": record["id"], "at": timestamp},
            at=timestamp,
        )
        _append_event(
            state,
            "plan_change_requested_by_engineer",
            session,
            timestamp,
            detail=f"{incident_id}: {record['id']} for {task_id}",
        )
        store.save(state)
    return {"plan_change_id": record["id"], "task_id": task_id}


def require_stop_ticket_closable(
    cfg: Any, incident_id: str, actions: Sequence[str], thread_id: str
) -> None:
    """A stop ticket closes only when its stop is really dealt with, by its on-call.

    Closing a stop ticket lifts the hold on its tasks (R3, ``stop_holds``):
    a held task takes up its work again. For an infrastructure stop that is
    the main road back to work - so this door is bound exactly like the
    return it amounts to. The independent check reproduced the hole on the
    real CLI: a DEPENDENCY_DEFECT ticket (means: a plan change or a runtime
    repair) closed from a worker's thread naming return_stopped_task, and
    the held task went back to READY with the same defect - no replan, no
    patch, no return, and nobody bound to the ticket. Now:

    - only the on-call holding THIS ticket, from its own thread
      (``require_engineer_thread``, the same binding as every other door);
    - every repairing action named must be in the means table for this stop
      (``means_for``). A stop whose row is empty is hers (PRODUCT_DECISION,
      ARCHITECTURE_DECISION, DANGEROUS_PERMISSION, RECOVERY_EXHAUSTED): no
      closure at all, only a diagnosis and an escalation - a worker that
      declared recovery spent is held (R3: BLOCKED only once DevOps hands
      it up), and a closure used to send it straight back to work;
    - what is named must have happened: repair_runtime_code needs a live
      runtime patch of this ticket (staged or installed), request_plan_change
      a plan change this ticket asked for, return_stopped_task a return of
      this ticket or a held task the closure itself returns;
    - diagnostics alone repair nothing;
    - a task the ticket holds may not be left BLOCKED, unless the plan change
      this ticket asked for is active - the replanner returns it;
    - a permission request closes only as a runtime defect repaired: its
      means are the runtime patch alone, and that patch must be live.
    Other tickets close as before.
    """

    from pathlib import Path

    from .engineer_authority import READ_ONLY_DIAGNOSTIC_ACTIONS
    from .ladder_grants import patch_is_live
    from .pipeline_engineer import PipelineIncidentStore
    from .stop_diagnosis import means_for, means_key

    incident = PipelineIncidentStore(cfg.state_dir).require_engineer_incident(incident_id)
    if not _is_stop(incident):
        return
    state = StateStore(cfg.state_dir).load()
    require_engineer_thread(state, incident_id, thread_id)
    named = {str(item).strip() for item in actions if str(item).strip()}
    if named and named.issubset(READ_ONLY_DIAGNOSTIC_ACTIONS):
        raise EngineerStopActionError(
            "a stopped task is not resolved by diagnostics alone: repair it and return the "
            "task (return_stopped_task), ask the replanner (request_plan_change), or "
            "escalate with your diagnosis"
        )
    means = means_for(incident)
    outside = sorted(named - set(READ_ONLY_DIAGNOSTIC_ACTIONS) - set(means))
    if outside:
        if not means:
            raise EngineerStopActionError(
                f"this stop ({means_key(incident)}) is the owner's: it is not closed by the "
                "on-call - diagnose it and escalate with the same code"
            )
        raise EngineerStopActionError(
            f"{', '.join(outside)} is not among the means for this stop "
            f"({means_key(incident)}): {', '.join(means)}"
        )
    kind = str((incident.get("system_state") or {}).get("stop_kind") or "")
    live_patch = any(
        isinstance(patch, Mapping)
        and patch_is_live(Path(cfg.state_dir), str(patch.get("patch_id") or ""))
        for patch in incident.get("runtime_patches") or ()
    )
    if kind == "approval_required" and not live_patch:
        # Its task is held, not BLOCKED, so the BLOCKED test below never
        # stops this closure: named repair_runtime_code with nothing staged,
        # it sent the task straight back into the same request - the loop
        # her answer is built to break, run by the on-call instead.
        raise EngineerStopActionError(
            "a permission request closes only with a runtime patch of this ticket that "
            "removes the request (staged or installed); if the task itself needs the "
            "operation, escalate DANGEROUS_PERMISSION with your recommendation"
        )
    if "repair_runtime_code" in named and not live_patch:
        raise EngineerStopActionError(
            "repair_runtime_code is named, but this ticket has no live runtime patch (staged "
            "or installed): stage it with devops-repair-runtime first"
        )
    if "request_plan_change" in named and not incident.get("plan_change_requests"):
        raise EngineerStopActionError(
            "request_plan_change is named, but this ticket asked the replanner for nothing: "
            "use devops-request-plan-change first"
        )
    affected = [str(task) for task in incident.get("affected_task_ids") or ()]
    released = [
        task
        for task in affected
        if state.task_states.get(task)
        not in {TaskState.BLOCKED.value, TaskState.VERIFIED.value, TaskState.CANCELLED.value, None}
    ]
    if "return_stopped_task" in named and not (incident.get("returns") or released):
        raise EngineerStopActionError(
            "return_stopped_task is named, but this ticket returned nothing and holds no task "
            "its closure would return"
        )
    replanning = set()
    if state.active_plan_change_id is not None:
        for record in state.plan_changes or ():
            if (
                str(record.get("id")) == str(state.active_plan_change_id)
                and str(record.get("requested_by_incident") or "") == incident_id
            ):
                replanning.add(str(record.get("requester_task_id") or ""))
    still = [
        task
        for task in affected
        if state.task_states.get(task) == TaskState.BLOCKED.value and task not in replanning
    ]
    if still:
        raise EngineerStopActionError(
            f"ticket {incident_id} still holds {', '.join(still)} in BLOCKED: return it "
            "(devops-return-task), ask the replanner (devops-request-plan-change), or escalate "
            "to the owner - closing it would leave the task waiting for nobody"
        )


def require_patch_holder(cfg: Any, incident_id: str, thread_id: str) -> dict[str, Any]:
    """The runtime patch commands answer to the engineer of this ticket only.

    ``devops-repair-runtime`` used to check that the ticket was in the
    engineer's phase and accept any CODEX_THREAD_ID of the run. Since a
    patch on the acceptance path buys a task a fresh hire, and a staged
    patch drains the whole run, that let the worker of the very task being
    judged stage one. Now the same binding as a return: the pending on-call
    session of this ticket, from its own thread - and within the means
    table: a stop that is hers (the empty rows) is diagnosed, not patched.
    """

    _, incident = _loaded_incident(cfg, incident_id)
    require_engineer_thread(StateStore(cfg.state_dir).load(), incident_id, thread_id)
    if not _allowed(incident, "repair_runtime_code"):
        raise EngineerStopActionError(
            "this kind of stop is not repaired in code by the on-call; see stop_context.means"
        )
    return incident


def take_back_patch(
    cfg: Any, *, incident_id: str, patch_id: str, thread_id: str, at: str | None = None
) -> dict[str, Any]:
    """Withdraw a staged patch of this ticket, or stage the revert of an installed one.

    Bound like every other door of the on-call (``require_patch_holder``):
    the command used to check neither the ticket nor the thread. A staged
    patch may be withdrawn only by the engineer whose ticket staged it. In
    the same transaction every fresh hire that rested on the patch is
    revoked (``ladder_grants``), and a task sent back to BLOCKED is held by
    a ticket - otherwise a withdrawn patch still bought the hire, measured.
    """

    from .ladder_grants import hold_revoked, revoke_grants
    from .plan import load_plan
    from .resources import ResourceLockCoordinator
    from .runtime_install import INSTALLED, PENDING, patch_status, stage_revert, withdraw_staged

    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        _, incident = _loaded_incident(cfg, incident_id)
        require_engineer_thread(state, incident_id, thread_id)
        status = patch_status(cfg.state_dir, patch_id)
        if status == PENDING:
            own = {
                str(item.get("patch_id") or "")
                for item in incident.get("runtime_patches") or ()
                if isinstance(item, Mapping)
            }
            if patch_id not in own:
                raise EngineerStopActionError(
                    f"staged patch {patch_id} is not this ticket's; only the engineer who "
                    "staged it may withdraw it"
                )
            withdraw_staged(cfg.state_dir, patch_id)
            outcome: dict[str, Any] = {"patch_id": patch_id, "withdrawn": True}
        elif status == INSTALLED:
            stage_revert(cfg.state_dir, patch_id, at=timestamp)
            outcome = {"patch_id": patch_id, "revert_staged": True}
        else:
            raise EngineerStopActionError(
                f"patch {patch_id} is {status}: nothing to withdraw or revert"
            )
        plan = load_plan(cfg.state_dir, cfg.profile)
        blocked = revoke_grants(
            cfg,
            plan,
            state,
            (patch_id,),
            reason="withdrawn" if outcome.get("withdrawn") else "reverted",
            at=timestamp,
        )
        ticket = hold_revoked(
            cfg,
            state,
            blocked,
            stop_kind="runtime_patch_taken_back",
            reason=f"runtime patch {patch_id} was taken back; the fresh hire it bought is revoked",
            at=timestamp,
        )
        store.save(state)
    return {**outcome, "blocked_again": blocked, "ticket": ticket}
