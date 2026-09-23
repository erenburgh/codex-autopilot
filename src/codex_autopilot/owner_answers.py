"""Her answer moves the run. No Resume, no second step.

``unblock`` used to do three things wrong at once, read end to end:

- it ran without the run's transaction, so a dispatcher saving at the same
  moment could overwrite her decision or have its own overwritten;
- it lifted the stop and left the tickets: the task stayed held by an open
  ticket (``tasks_paused_by_incidents``), so her answer changed nothing the
  frontier could see; at the top of the hiring ladder the tally stayed spent,
  and the first REVISE stopped the task again at once;
- and it ended with "Continue the run with the phrase «Resume Codex
  Autopilot.»" - a second step she had to know about, for a run that knows
  how to raise itself.

``answer_task`` is her answer as one operation, under the run's transaction:

- a task in BLOCKED goes to READY, or to IMPLEMENTED when a verdict was ever
  given (``unblock_target`` - the rule the on-call's return shares);
- the open tickets that hold the task, waiting for her or in the on-call's
  lane, are closed as answered by her (``resolve_escalation_by_user``);
- an answer to the top of the hiring ladder grants a fresh hire at the effort
  the task reached (``revision_budget.grant_fresh_hire``): her decision is a
  change of cause by R23;
- the answer may be a choice among the on-call's options (``--option``). Two
  codes the runtime acts on itself: ``replan`` asks the replanner instead of
  returning the task, and for a permission request ``retry`` is allowed once
  per request - the same request after her retry is not retried again, which
  is what breaks the loop "answer, the same approval, answer";
- every answer is recorded in ``user_unblocks`` with its option, where the
  task's next worker reads it;
- and after the transaction the run is raised the way the launchd sweep
  raises it: the owner derived from the journal (``wake.derive_owner``) and
  ``ensure_wake`` - the same hook-trust gate, nothing bypassed.
"""

from __future__ import annotations

from typing import Any, Mapping

from .task_state import TaskState, transition_task


class OwnerAnswerError(RuntimeError):
    """Her answer could not be applied; nothing changed."""


def unblock_target(state: Any, task_id: str) -> TaskState:
    """Where a lifted stop returns a task.

    A stop is not always about the work. When the task already had a
    verdict - it has revision history - the implementation exists and only
    acceptance was in dispute, so it goes back to waiting for a verifier,
    not back to the start. Sending done work to READY was not merely
    wasteful: the re-run touched the staged workspace and the runtime then
    refused the result, "staged output changed after verification".
    """

    done_before = int((getattr(state, "task_revisions", None) or {}).get(task_id, 0)) > 0
    return TaskState.IMPLEMENTED if done_before else TaskState.READY


def _tickets_holding(loaded: Mapping[str, Any], task_id: str) -> list[dict[str, Any]]:
    from .pipeline_engineer import IncidentPhase

    waiting = {IncidentPhase.ESCALATE_TO_USER.value, IncidentPhase.PIPELINE_ENGINEER.value}
    return [
        dict(item)
        for item in loaded.get("incidents") or ()
        if not item.get("resolved_at")
        and str(item.get("phase")) in waiting
        and task_id in [str(task) for task in item.get("affected_task_ids") or ()]
    ]


def _options(tickets: list[dict[str, Any]]) -> dict[str, str]:
    from .stop_diagnosis import OWNER_OPTIONS

    found: dict[str, str] = {}
    for item in tickets:
        kind = str((item.get("system_state") or {}).get("stop_kind") or "")
        for option in OWNER_OPTIONS.get(kind, ()):
            found.setdefault(option["code"], option["means"])
        for option in (item.get("escalation") or {}).get("options") or ():
            if isinstance(option, Mapping) and option.get("code"):
                found.setdefault(str(option["code"]), str(option.get("means") or ""))
    return found


def answer_task(
    cfg: Any,
    task_id: str,
    reason: str,
    *,
    option: str = "",
    at: str | None = None,
    raise_run: bool = True,
    spawn: Any = None,
) -> dict[str, Any]:
    """Apply her answer about one task, then let the run continue by itself."""

    from .pipeline_engineer import PipelineIncidentStore
    from .plan import load_plan
    from .resources import ResourceLockCoordinator
    from .revision_budget import grant_fresh_hire
    from .run_state import StateStore, utc_now
    from .run_status import _finish_global_state

    timestamp = at or utc_now()
    text = str(reason or "").strip()
    if not text:
        raise OwnerAnswerError("a reason is required: --reason")
    choice = str(option or "").strip()
    store = StateStore(cfg.state_dir)
    incidents = PipelineIncidentStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        plan = load_plan(cfg.state_dir, cfg.profile)
        if task_id not in plan.task_map:
            raise OwnerAnswerError(f"the plan has no task {task_id!r}")
        tickets = _tickets_holding(incidents.load(), task_id)
        blocked = state.task_states.get(task_id) == TaskState.BLOCKED.value
        if not blocked and not tickets:
            raise OwnerAnswerError(
                f"{task_id} is not stopped: it is now {state.task_states.get(task_id)}"
            )
        offered = _options(tickets)
        if choice and offered and choice not in offered:
            raise OwnerAnswerError(
                f"{choice!r} is not among the options: " + ", ".join(sorted(offered))
            )
        kinds = {str((item.get("system_state") or {}).get("stop_kind") or "") for item in tickets}
        signature = next(
            (
                str((item.get("system_state") or {}).get("approval_signature") or "")
                for item in tickets
                if (item.get("system_state") or {}).get("approval_signature")
            ),
            "",
        )
        if "approval_required" in kinds:
            if choice not in {"retry", "replan"}:
                raise OwnerAnswerError(
                    "a permission request is answered with --option replan (change the plan "
                    "so the task does not need it) or --option retry (you granted it yourself)"
                )
            if choice == "retry" and retried_before(state, task_id, signature):
                raise OwnerAnswerError(
                    "the same permission request came back after your earlier retry: the grant "
                    "did not cover it. Answer with --option replan, or change the permission "
                    "and the request so they differ"
                )
        if choice == "replan" and state.active_plan_change_id is not None:
            raise OwnerAnswerError(
                f"plan change {state.active_plan_change_id} is already active; answer again after it"
            )
        closed: list[str] = []
        for item in tickets:
            incidents.resolve_escalation_by_user(
                str(item["incident_id"]), at=timestamp, note=f"{choice + ': ' if choice else ''}{text}"
            )
            closed.append(str(item["incident_id"]))
        decision = text + (f" [option {choice}: {offered.get(choice, '')}]" if choice else "")
        grant: dict[str, Any] | None = None
        moved_to = state.task_states.get(task_id)
        if choice == "replan":
            moved_to = _owner_plan_change(plan, state, task_id, decision, closed, timestamp)
        elif blocked:
            if owes_fresh_hire(plan, state, task_id, kinds):
                grant = grant_fresh_hire(
                    plan, state, task_id, grounds={"user_unblock": timestamp}
                )
            target = unblock_target(state, task_id)
            state.task_states = transition_task(plan, state.task_states, task_id, target)
            moved_to = target.value
        state.user_unblocks.append(
            {
                "task_id": task_id,
                "reason": decision,
                "at": timestamp,
                **({"option": choice} if choice else {}),
                **({"incident_ids": closed} if closed else {}),
                **({"approval_signature": signature} if signature else {}),
            }
        )
        state.last_error = None
        _finish_global_state(plan, state, (), paused=store.pause_requested(), cfg=cfg)
        store.save(state)
    raised = _raise_the_run(cfg, spawn=spawn) if raise_run else {"raised": False, "why": "not asked"}
    return {
        "task_id": task_id,
        "state": moved_to,
        "closed_tickets": closed,
        "option": choice,
        "grant": grant,
        **raised,
    }


def retried_before(state: Any, task_id: str, signature: str) -> bool:
    """Her retry of this very permission request was already spent.

    One retry per request, whichever door she answered through. Only
    ``unblock`` used to check it: a Resume lifted the same approval stop and
    recorded no option and no signature, so the rule never saw it and the
    loop "Resume, the same approval, Resume" had no bound - measured by the
    independent check, three rounds in a row, each costing an on-call turn.
    """

    return bool(signature) and any(
        isinstance(item, dict)
        and item.get("task_id") == task_id
        and item.get("option") == "retry"
        and item.get("approval_signature") == signature
        for item in getattr(state, "user_unblocks", None) or ()
    )


def owes_fresh_hire(plan: Any, state: Any, task_id: str, kinds: Any) -> bool:
    """Her answer about a task at the top of its hiring ladder buys a fresh hire.

    By the task's ladder, not only the ticket's kind - the same test as the
    on-call's return (``revision_budget.at_top_of_ladder``). A task whose
    grant was revoked comes back under a runtime_patch_refused or
    runtime_patch_taken_back ticket; ``unblock`` granted it a hire, Resume
    looked at the kind alone and sent it back spent, to stop again on its
    first REVISE. Both doors ask this one question now.
    """

    from .revision_budget import at_top_of_ladder

    return (
        "ladder_exhausted" in kinds
        or _at_ladder_top(state, task_id)
        or at_top_of_ladder(plan, state, task_id)
    )


def _at_ladder_top(state: Any, task_id: str) -> bool:
    """A BLOCKED task whose last stop was its exhausted hiring ladder."""

    for item in reversed(list(getattr(state, "lifecycle_journal", None) or ())):
        if str(item.get("task_id") or "") != task_id:
            continue
        event = str(item.get("event") or "")
        if event == "hiring_ladder_exhausted":
            return True
        if event in {"task_rehired", "revision_budget_reset_on_new_premises"}:
            return False
    return False


def _owner_plan_change(
    plan: Any, state: Any, task_id: str, decision: str, closed: list[str], at: str
) -> str:
    from .resilience import PlanChangeRequest, register_plan_change_request

    if task_id in state.active_task_ids:
        raise OwnerAnswerError(f"{task_id} is running; it is not re-planned under a worker")
    if state.task_states.get(task_id) != TaskState.BLOCKED.value:
        state.task_states = transition_task(plan, state.task_states, task_id, TaskState.BLOCKED)
    record = register_plan_change_request(
        state,
        PlanChangeRequest(
            request_version=1,
            kind="prerequisite",
            target_task_id=task_id,
            summary=f"The owner chose to change the plan for {task_id}"[:240],
            rationale=f"The owner's decision: {decision}"[:4000],
            change={"description": decision[:4000]},
        ),
        requester_task_id=task_id,
        requester_session_token=f"owner-answer:{task_id}:{at}",
        at=at,
    )
    record["requested_by_owner"] = True
    if closed:
        record["answering_incidents"] = list(closed)
    return TaskState.BLOCKED.value


def _raise_the_run(cfg: Any, *, spawn: Any = None) -> dict[str, Any]:
    """Raise the run on the causal owner's behalf, as the sweep would."""

    from .run_state import StateStore
    from .wake import derive_owner, ensure_wake

    try:
        state = StateStore(cfg.state_dir).load()
        owner = derive_owner(state)
        if owner is None:
            return {
                "raised": False,
                "why": (
                    "no completed turn to continue from yet; the wake-up sweep raises the run "
                    "once there is one, or start it once with its phrase"
                ),
            }
        pid = ensure_wake(cfg, owner=owner[0], owner_turn=owner[1], spawn=spawn)
    except Exception as exc:  # noqa: BLE001 - her answer is recorded either way
        return {"raised": False, "why": f"the wake-up could not be armed: {exc}"}
    if pid is None:
        return {"raised": False, "why": "nothing waits for the runtime right now"}
    return {"raised": True, "wake_pid": pid}


def render_answer(result: Mapping[str, Any]) -> str:
    """What she reads after answering: what changed, and that the run goes on."""

    lines = [
        f"{result['task_id']}: your decision is recorded"
        + (f" (option {result['option']})" if result.get("option") else "")
        + f"; the task is now {result['state']}."
    ]
    if result.get("closed_tickets"):
        lines.append("Answered tickets: " + ", ".join(result["closed_tickets"]) + ".")
    if result.get("grant"):
        lines.append(
            f"A fresh hire at effort {result['grant'].get('effort') or 'the current step'} "
            "was granted."
        )
    if result.get("raised"):
        lines.append("The run continues by itself.")
    else:
        lines.append(f"The run continues by itself: {result.get('why')}.")
    return "\n".join(lines)


class ResumeAnswers(tuple):
    """The tickets a Resume closed, and what it would not do and why.

    A tuple, so every caller that joined or compared the closed ids still
    does; ``held`` carries a sentence per stop the Resume left for her
    explicit answer.
    """

    held: tuple[str, ...] = ()


def answer_escalations(cfg: Any) -> ResumeAnswers:
    """Her Resume on a BLOCKED run: the answer to what was handed to her.

    Tickets handed to her are closed as answered, and the tasks they held
    leave BLOCKED by the same transition as ``answer_task`` - the same
    target, the same fresh hire at the top of the ladder
    (``owes_fresh_hire``) - recorded in ``user_unblocks``. A Resume used to
    close the tickets and leave the tasks BLOCKED, so the next reservation
    found nothing to do.

    A permission request is answered by a Resume as ``retry`` - she lets
    the task try once more - with the request's signature recorded, so the
    one-retry-per-request rule sees it (``retried_before``). When the same
    request is back after her retry, the Resume does not lift it again: the
    ticket stays with her and the Resume says what answers it (replan, or a
    permission that covers the request). That is what breaks "Resume, the
    same approval, Resume".

    A ticket the on-call has not looked at is never closed here. Tickets in
    DEGRADED or AUTO_RECOVERY_FAILED are routed to its lane instead - closing
    them would erase a fault nobody examined, and "first DevOps" is her own
    requirement. Tickets in the lane stay there; the one exception is a
    ticket its engineer already handed up by status line (older runs left it
    in the lane with the escalation recorded only on the run).
    """

    from .engineer_reservation import _needs_lane
    from .pipeline_engineer import IncidentPhase, PipelineIncidentStore
    from .plan import load_plan
    from .resources import ResourceLockCoordinator
    from .revision_budget import grant_fresh_hire
    from .run_state import StateStore, utc_now
    from .stop_diagnosis import owner_answer

    incidents = PipelineIncidentStore(cfg.state_dir)
    store = StateStore(cfg.state_dir)
    at = utc_now()
    closed: list[str] = []
    held: list[str] = []
    note = "the user resumed the run, answering the escalation"
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        plan = load_plan(cfg.state_dir, cfg.profile)
        escalated_in_lane = {
            str(item.get("incident_id") or "")
            for item in state.worker_sessions
            if item.get("kind") == "pipeline_engineer"
            and item.get("final_status") == "ESCALATE_TO_USER"
        }
        awaiting = set(incidents.incident_ids_awaiting_the_user())
        for item in incidents.load().get("incidents") or ():
            if item.get("resolved_at"):
                continue
            incident_id = str(item.get("incident_id"))
            if _needs_lane(item):
                incidents.ensure_pipeline_engineer(incident_id, at=at)
                continue
            if incident_id not in awaiting:
                continue
            if (
                str(item.get("phase")) == IncidentPhase.PIPELINE_ENGINEER.value
                and incident_id not in escalated_in_lane
            ):
                continue
            system = item.get("system_state") or {}
            kind = str(system.get("stop_kind") or "")
            signature = str(system.get("approval_signature") or "")
            tasks = [
                str(task)
                for task in item.get("affected_task_ids") or ()
                if state.task_states.get(str(task)) == TaskState.BLOCKED.value
                and str(task) in plan.task_map
            ]
            if kind == "approval_required" and any(
                retried_before(state, task_id, signature) for task_id in tasks
            ):
                held.append(
                    f"{', '.join(tasks)}: the same permission request ({signature}) came back "
                    "after your earlier retry, so Resume does not retry it again. Answer with "
                    "--option replan, or grant a permission that covers the request: "
                    + owner_answer(cfg, item)
                )
                continue
            incidents.resolve_escalation_by_user(incident_id, at=at, note=note)
            closed.append(incident_id)
            for task_id in tasks:
                if owes_fresh_hire(plan, state, task_id, {kind}):
                    grant_fresh_hire(plan, state, task_id, grounds={"user_unblock": at})
                state.task_states = transition_task(
                    plan, state.task_states, task_id, unblock_target(state, task_id)
                )
                state.user_unblocks.append(
                    {
                        "task_id": task_id,
                        "reason": note,
                        "at": at,
                        "incident_ids": [incident_id],
                        **(
                            {"option": "retry", "approval_signature": signature}
                            if kind == "approval_required"
                            else {}
                        ),
                    }
                )
        if held:
            state.last_error = " ".join(held)[:2000]
        store.save(state)
    answers = ResumeAnswers(closed)
    answers.held = tuple(held)
    return answers
