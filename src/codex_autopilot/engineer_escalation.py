"""How the on-call's turn ends: its status line, its escalation, its successor.

Moved out of lifecycle_completion, which stood at the 1500-line limit while
the engineer's exit grew a second half: an escalation now holds only its own
ticket's tasks, and the run goes on around it.

R13: DevOps resolves infrastructure bugs on the user's behalf, and the user
takes no part in choosing the fix. So an escalation is not a second equal
exit but an exception, and it must name its reason with a code from the
closed list - and, since the owner has to decide something, it must carry
what they decide with.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .lifecycle_base import (
    PENDING_SESSION_STATUSES,
    RELAYABLE_SESSION_STATUSES,
    DesktopLifecycleError,
    LaunchDescriptor,
    _append_event,
    _pid_alive,
)
from .task_state import TaskState

ESCALATION_CODES = frozenset({
    "DANGEROUS_PERMISSION",
    "GLOBAL_CONFIG_CHANGE",
    "PROJECT_DAMAGE_RISK",
    "RECOVERY_EXHAUSTED",
    "PRODUCT_DECISION",
    "ARCHITECTURE_DECISION",
})
PIPELINE_ENGINEER_STATUS = re.compile(
    r"(?m)^PIPELINE_ENGINEER_STATUS:\s*(RESOLVED|ESCALATE_TO_USER(?:\s+\S+)?)\s*$"
)
ESCALATION_PREFIX = "AUTOPILOT_ESCALATION:"
MAX_DIAGNOSIS_CHARS = 1_500


def parse_pipeline_engineer_status(message: str) -> tuple[str, str]:
    """The engineer's final line: the outcome and, for an escalation, the reason code."""

    matches = PIPELINE_ENGINEER_STATUS.findall(message or "")
    last = next(
        (line.strip() for line in reversed((message or "").splitlines()) if line.strip()),
        "",
    )
    if len(matches) != 1 or last != f"PIPELINE_ENGINEER_STATUS: {matches[0]}":
        raise DesktopLifecycleError(
            "the on-call engineer must finish with exactly one line "
            "PIPELINE_ENGINEER_STATUS: RESOLVED or "
            "PIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER <CODE>"
        )
    parts = matches[0].split()
    if parts[0] == "RESOLVED":
        return "RESOLVED", ""
    if len(parts) != 2 or parts[1] not in ESCALATION_CODES:
        raise DesktopLifecycleError(
            "an escalation requires a reason code from the closed list (R13): "
            + ", ".join(sorted(ESCALATION_CODES))
        )
    return "ESCALATE_TO_USER", parts[1]


def parse_engineer_escalation(message: str) -> dict[str, Any]:
    """What the owner decides with - never lost, even when the line is missing.

    The engineer writes one ``AUTOPILOT_ESCALATION: {json}`` line before its
    status. Without it the owner used to get a bare code. Now a missing or
    unreadable line still escalates, with the bounded end of the engineer's
    own message as the diagnosis: a signal with a rough diagnosis beats a
    signal refused over its format. ``scope`` is ``task`` unless the engineer
    explicitly declared the whole run must wait.
    """

    lines = [line.strip() for line in (message or "").splitlines() if line.strip()]
    found = [line for line in lines if line.startswith(ESCALATION_PREFIX)]
    raw: Any = None
    if len(found) == 1:
        try:
            raw = json.loads(found[0][len(ESCALATION_PREFIX):].strip())
        except ValueError:
            raw = None
    if not isinstance(raw, Mapping):
        body = "\n".join(
            line
            for line in lines
            if not line.startswith(("PIPELINE_ENGINEER_STATUS:", ESCALATION_PREFIX))
        )
        return {"diagnosis": body[-MAX_DIAGNOSIS_CHARS:], "scope": "task", "declared": False}
    result = {
        key: raw[key]
        for key in ("diagnosis", "repaired", "decision_needed", "recommendation", "options")
        if key in raw
    }
    result["scope"] = "run" if str(raw.get("scope") or "") == "run" else "task"
    result["declared"] = True
    return result


ESCALATION_FORMAT = (
    'AUTOPILOT_ESCALATION: {"diagnosis": "<what happened and why>", "repaired": '
    '["<what you already repaired>"], "decision_needed": "<the one question for the owner>", '
    '"recommendation": "<what you recommend and why>", "options": [{"code": "<short code>", '
    '"means": "<what choosing it does>"}], "scope": "task"}'
)

STOP_TICKET_BRIEF = f"""This ticket is a stopped task (its code starts with run_stopped:). `stop_context` in the package is the stop's own diagnosis material: the kind of stop, the worker's reason code, the verifier's last issues, how far the hiring ladder went, the replanner's refusals, the end of the stopped session's final message, and `means` - what you may do about THIS kind of stop. Work it through yourself. Repair what is within your means; then either return the task to work with `scripts/codex-autopilot devops-return-task --project <root> --incident-id <id> --task <task>` or ask the replanner with `scripts/codex-autopilot devops-request-plan-change --project <root> --incident-id <id> --task <task> --reason <text>`, and close the ticket with devops-resolve-incident naming return_stopped_task or request_plan_change. A stop ticket is not closed with diagnostics alone, nor while a task it holds is still BLOCKED without the plan change you requested. A task at the top of its hiring ladder returns only after a runtime patch on this ticket that changed the acceptance path (the gate, the rubric, the verifier or the verifier's prompt), or through a plan change - a patch elsewhere buys no fresh budget, and the hire lasts only as long as the patch: withdrawn, refused at install or reverted, it is revoked. A staged patch drains the run until the wake-up installs it; the task you return starts after that, on the new code. A task whose own worker stopped it (stop kind worker_blocked) with PRODUCT_DECISION, ARCHITECTURE_DECISION, DANGEROUS_PERMISSION or RECOVERY_EXHAUSTED is never returned or patched by you: its `means` are empty - diagnose it and escalate with the same code. `stop_context.means` is the table's word for THIS stop; where this text and `means` seem to differ, `means` decides. Only when the decision is truly the owner's, write exactly one line before your status line:
{ESCALATION_FORMAT}
When `stop_context.owner_options` lists codes, offer those: the runtime acts on them. Use "scope": "run" only when the whole run must wait (revoked hook trust, a global setting). The owner answers with `owner_answer` from the package, and the run continues by itself - never ask her to write Resume."""

# A permission request is not a worker's DANGEROUS_PERMISSION stop, though
# its ticket carries that code as the one it is handed up with. The first
# brief said "a task stopped with DANGEROUS_PERMISSION is never returned or
# patched by you" and nothing about the request itself, while the means
# table gives this stop repair_runtime_code: the independent check read the
# prompt and found the repair branch of the design unreachable - the on-call
# was told to escalate what it was supposed to compare. So a ticket of this
# kind gets its own paragraph (``engineer_brief``), naming the fields it
# compares and the one road to a closure.
APPROVAL_TICKET_BRIEF = """This stop is a permission request (`stop_context.stop_kind` is approval_required): a turn asked for an approval, and nobody answers it for the owner - not you, not the runtime, never. Its reason code DANGEROUS_PERMISSION is the code it goes up with if it is hers; it does not make it hers before you look, and its `means` include repair_runtime_code. Compare `stop_context.approval.request` with `stop_context.approval.run_authorization` (what starting the run authorized, R4) and `stop_context.approval.permission_profile` (the profile the run's turns use). If the runtime asked for more than the run needs - a wrong permission profile, a command the runtime itself issued that the task never needed - it is a runtime defect: repair it with devops-repair-runtime (above) so the request is not raised again, and close the ticket naming repair_runtime_code; the held task then resumes on the patched code. Such a ticket closes only with a live runtime patch of yours on it: without one the same request would simply come back. If the task itself needs the operation, the decision is hers: escalate with DANGEROUS_PERMISSION, say what the request is and what it would reach, recommend one of `stop_context.owner_options` (replan: the task changes so it does not need it; retry: she grants the permission herself and the task runs once more). `stop_context.approval.answered_before` lists her earlier answers to this very request: if she already retried it, the same request came back, and a second retry is refused - recommend replan."""

ADVISORY_TICKET_BRIEF = f"""This ticket's class is PRODUCTION, POLICY or AMBIGUOUS_SIDE_EFFECT: it passes through you so the owner gets a diagnosis, not a bare code, but it is not yours to repair or to close - your allowed_actions are diagnostics only, and RESOLVED is refused for this class. Read `server_view` (the App Server's own thread/read of the affected task), the package and the journal; never repeat an ambiguous create or send. Then hand it up with PIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER and a code from the list, preceded by exactly one line:
{ESCALATION_FORMAT}"""


def engineer_brief(package: Mapping[str, Any]) -> str:
    """The part of the on-call's prompt that depends on what kind of ticket it holds."""

    from .engineer_authority import ADVISORY_INCIDENT_CLASSES
    from .pipeline_engineer import STOP_CODE_PREFIX

    incident = package.get("incident") or {}
    if str(incident.get("classification") or "") in {
        item.value for item in ADVISORY_INCIDENT_CLASSES
    }:
        return ADVISORY_TICKET_BRIEF
    if str(incident.get("code") or "").startswith(STOP_CODE_PREFIX):
        kind = str((package.get("stop_context") or {}).get("stop_kind") or "")
        if kind == "approval_required":
            return STOP_TICKET_BRIEF + "\n" + APPROVAL_TICKET_BRIEF
        return STOP_TICKET_BRIEF
    return ""


def engineer_prompt_refusal(package: Mapping[str, Any]) -> str:
    """Why this package may not become an on-call prompt - or "" when it may.

    Every class of the lane is accepted now, the advisory ones included:
    route_incident sends them to the on-call first. Ordinary tasks still
    cannot manufacture a privileged specialist - the ticket must be in the
    engineer's phase, and the forbidden list must be complete.
    """

    from .engineer_authority import ENGINEER_LANE_CLASSES, FORBIDDEN_ACTIONS, IncidentClass
    from .pipeline_engineer import IncidentPhase

    incident = package.get("incident")
    if not isinstance(incident, Mapping):
        return "Pipeline Engineer requires a structured incident"
    try:
        classification = IncidentClass(str(incident.get("classification") or ""))
        phase = IncidentPhase(str(incident.get("phase") or ""))
    except ValueError:
        return "Pipeline Engineer incident classification is invalid"
    if classification not in ENGINEER_LANE_CLASSES:
        return "Pipeline Engineer does not know this incident class"
    if phase is not IncidentPhase.PIPELINE_ENGINEER:
        return "Pipeline Engineer is available only for a routed incident"
    forbidden = tuple(str(item) for item in package.get("forbidden_actions") or ())
    if not set(FORBIDDEN_ACTIONS).issubset(forbidden):
        return "Pipeline Engineer package omitted mandatory forbidden actions"
    return ""


def read_engineer_outcome(cfg: Any, incident_id: str, final_message: str) -> tuple[str, str, str]:
    """The engineer's outcome as ``(status, code, refusal)``; never raises.

    An unreadable status line, RESOLVED on a ticket still open, a ticket
    that is gone - each used to raise DesktopLifecycleError before the
    completion's transaction. The dispatcher catches only
    WorkerProtocolError, so it died, the session stayed pending, and one
    engineer per run kept the lane shut until a sweep settled it by the
    server's word. Now the refusal is returned, and the completion records
    it as a protocol error (``record_engineer_protocol_error``): the session
    completes, the lane is free, and the per-ticket bound sends the ticket
    to her after the second such engineer.
    """

    from .pipeline_engineer import IncidentPhase, PipelineIncidentStore

    try:
        status, code = parse_pipeline_engineer_status(final_message)
    except DesktopLifecycleError as exc:
        return "PROTOCOL_ERROR", "", str(exc)
    incident = next(
        (
            item
            for item in PipelineIncidentStore(cfg.state_dir).load().get("incidents", [])
            if str(item.get("incident_id")) == incident_id
        ),
        None,
    )
    if incident is None:
        return "PROTOCOL_ERROR", "", f"the on-call engineer's incident {incident_id} was not found"
    if status == "RESOLVED" and str(incident.get("phase")) != IncidentPhase.RESOLVED.value:
        return (
            "PROTOCOL_ERROR",
            "",
            f"the engineer declared RESOLVED, but ticket {incident_id} stayed in phase "
            f"{incident.get('phase')}: closing is done by devops-resolve-incident "
            "with a passing healthcheck",
        )
    return status, code, ""


def record_engineer_protocol_error(
    cfg: Any, state: Any, session: dict[str, Any], *, error: str, final_message: str, at: str
) -> None:
    """A refused on-call outcome: the session ends, the ticket stays in the lane.

    Marked as a lost engineer, so ``hand_lost_engineers_to_owner`` sends the
    ticket to her as RECOVERY_EXHAUSTED after the second, with the end of
    each engineer's own message as the diagnosis. R13: the engineer's exit
    is a closed protocol, and breaking it is recorded.
    """

    from .engineer_reservation import LOST_ENGINEER_PROTOCOL_REASON
    from .rules import record_violation

    session["final_status"] = "PROTOCOL_ERROR"
    session["failure_reason"] = f"{LOST_ENGINEER_PROTOCOL_REASON}: {error}"[:2000]
    session["final_message_tail"] = (final_message or "")[-MAX_DIAGNOSIS_CHARS:]
    record_violation(
        cfg.state_dir,
        "R13",
        detail=f"the on-call for {session.get('incident_id')} ended outside its protocol: {error}"[:2000],
    )
    _append_event(state, "pipeline_engineer_protocol_error", session, at, detail=error[:2000])
    state.last_error = f"the on-call engineer's answer was refused: {error}"[:2000]


def escalate_engineer_ticket(
    cfg: Any,
    state: Any,
    session: dict[str, Any],
    *,
    incident_id: str,
    code: str,
    final_message: str,
    at: str,
) -> str:
    """The engineer's ESCALATE_TO_USER: the ticket goes up, the run goes on."""

    from .blocked_runs import escalate_to_owner

    escalation = parse_engineer_escalation(final_message)
    if not escalation.get("declared"):
        # The signal still goes: a rough diagnosis beats a refused one. But
        # an escalation without its report breaks R13's contract - she was
        # to receive what she decides with - and that is recorded.
        from .rules import record_violation

        record_violation(
            cfg.state_dir,
            "R13",
            detail=f"the on-call escalated {incident_id} ({code}) without an AUTOPILOT_ESCALATION line",
        )
    outcome = escalate_to_owner(
        cfg,
        incident_id,
        code=code,
        detail=str(escalation.get("diagnosis") or "")[:MAX_DIAGNOSIS_CHARS],
        at=at,
        escalation=escalation,
    )
    if outcome == "answered":
        # The owner answered while the engineer was still writing. Her
        # answer stands; the escalation is kept as a note, not a reopening.
        _append_event(state, "pipeline_engineer_escalation_after_answer", session, at, detail=incident_id)
    else:
        state.last_error = (
            f"the on-call engineer handed incident {incident_id} to the owner: {code}"
        )
    if outcome == "escalated":
        # R3: an infrastructure stop only HELD its task while the on-call
        # looked (stop_holds). Handing the ticket up is the "DevOps
        # exhausted" half of the rule - only now does the task go BLOCKED.
        from .stop_holds import block_escalated_tasks

        for task_id in block_escalated_tasks(cfg, state, incident_id):
            _append_event(
                state, "task_blocked_on_escalation", session, at, detail=f"{incident_id}: {task_id}"
            )
    return outcome


def _relayable_descriptors_without_a_thread(state: Any) -> tuple[Any, ...]:
    """Reserved work that nobody is left to raise.

    After an incident, sessions remain in states fit for a relay: the thread
    was never created, so there is nothing to duplicate. Their owner - the
    task that completed its turn before the incident - can no longer raise
    them: its process has exited. Returning their descriptors is how the run
    continues without an operator.

    A reservation whose own dispatcher is alive is not "nobody's": now that
    the engineer works next to the neighbours, a neighbour's fresh
    reservation is being created right now by its dispatcher, and adopting
    it here would put two dispatchers on one reservation.
    """

    return tuple(
        LaunchDescriptor.from_dict(dict(item["descriptor"]))
        for item in state.worker_sessions
        if item.get("status") in RELAYABLE_SESSION_STATUSES
        and isinstance(item.get("descriptor"), dict)
        and not str(item.get("thread_id") or "")
        and not (
            item.get("automatic_dispatch_state") in {"SCHEDULED", "RUNNING"}
            and _pid_alive(item.get("automatic_dispatch_pid"))
        )
    )


def _would_idle_forever(cfg: Any, state: Any) -> bool:
    """The run would stand forever: work is ready and nobody is there to do it.

    An empty successor list is legitimate in itself - when everything hangs
    on a blocked task, say. The sign of trouble is different: a task in READY
    and not one live session, so nobody will come and nothing will move.

    A READY task that an open ticket holds is not that sign: it waits for the
    ticket, by design. It used to count. Measured by the independent check:
    once the plan gate's ticket went to the owner, every engineer completion
    saw its READY tasks, filed NO_SUCCESSOR and called another engineer -
    five in a row, the run reading RUNNING while the decision was hers. So a
    task counts only when no ticket holds it and no rate limit holds the
    whole account (``reservable_work``, the same test the wake-up uses).
    """

    active = any(
        item.get("status") in PENDING_SESSION_STATUSES
        for item in state.worker_sessions
    )
    if active:
        return False
    from .engineer_reservation import reservable_work

    return reservable_work(cfg, state, frozenset({TaskState.READY.value}))
