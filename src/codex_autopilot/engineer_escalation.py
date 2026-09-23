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


def _would_idle_forever(state: Any) -> bool:
    """The run would stand forever: work is ready and nobody is there to do it.

    An empty successor list is legitimate in itself - when everything hangs
    on a blocked task, say. The sign of trouble is different: a task in READY
    and not one live session, so nobody will come and nothing will move.
    """

    active = any(
        item.get("status") in PENDING_SESSION_STATUSES
        for item in state.worker_sessions
    )
    if active:
        return False
    return any(
        value == TaskState.READY.value for value in (state.task_states or {}).values()
    )
