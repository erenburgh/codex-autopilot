"""The on-call's package is fitted to the room the rules leave; the rules never are.

Every stop calls the on-call - her requirement, without exceptions. The
on-call's prompt carries the rules block whole (R17), and with each rule's
check it grew from about 7 000 to about 16 600 characters. The rest of the
prompt is the ticket's package: the ticket, its journal, the stop's
diagnosis and the App Server's view of the task's threads, and the last two
have no bound of their own. A package past the shared ceiling raised
ContextBoundaryError out of the reservation, where nothing catches it: the
one stop that most needed the on-call - a large, tangled one - would have
been the one it never came to.

So the package is fitted here. What does not fit is the diagnostic
material, largest part first, each cut part replaced by a marker that says
it was cut and how large it was - never silently, never the rules, never
the ticket's identity, class, phase, allowed or forbidden actions. The
on-call reads the rest from the journal it is pointed at.

The ticket itself carries its own copies of the diagnostics - its summary
(with the stop's reason on record), system_state and recent_events, the
last two duplicated at the top of the package, and whatever an earlier
escalation left on it. The independent check found
them outside the fitting: the store bounds them (2 000, 16 000 and 20 x
2 000 characters), but nothing here relied on that, and "the rules and the
ticket alone" were left to the ceiling's refusal. Every field of the ticket
but its identity (``INCIDENT_IDENTITY``) is fitted too now, after the
top-level parts, so what cannot be cut is the ticket's identity,
the action lists and the rules - small by construction. If even that does
not fit (a rules block grown past the ceiling), the prompt is refused and
the ticket goes to the owner with the reason
(``engineer_reservation.hand_unpromptable_ticket_to_owner``): the one case
where the on-call cannot be called is still a signal, never a silence.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

# The prompt around the package: the static instructions, the tool list and
# the longest brief (stop + permission) measure about 16 000 characters;
# the reserve leaves room for a long skill path and project root.
ENGINEER_FRAME_RESERVE = 24_000
# Diagnostic parts, in the order they are given up when they are equally large.
TRIMMABLE = ("server_view", "recent_events", "stop_context", "system_state")
# What names the ticket - never cut. Every other field of the ticket (its
# summary, system_state, recent_events, an earlier escalation) is
# diagnostic and is given up after the top-level parts, largest first.
INCIDENT_IDENTITY = (
    "incident_id", "signal_id", "code", "classification", "signature", "phase",
    "affected_task_ids", "context_task_id", "runbook_id", "operation", "side_effect_outcome",
)
_HEAD = 2_000


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def engineer_payload(
    incident_package: Mapping[str, Any], rules: list[dict[str, str]], ceiling: int
) -> str:
    """The package with the rules, as JSON no longer than the room the frame leaves."""

    package = dict(incident_package)
    package["rules"] = rules
    incident = package.get("incident")
    ticket: dict[str, Any] = {}
    if isinstance(incident, Mapping):
        ticket = package["incident"] = dict(incident)
    ticket_parts = tuple(key for key in ticket if key not in INCIDENT_IDENTITY)
    room = ceiling - ENGINEER_FRAME_RESERVE
    payload = _dumps(package)
    for container, keys in ((package, TRIMMABLE), (ticket, ticket_parts)):
        while len(payload) > room:
            # A part no longer than its marker's head would only grow when cut.
            present = [
                key for key in keys
                if key in container and not _is_marker(container[key])
                and len(_dumps(container[key])) > _HEAD
            ]
            if not present:
                break
            largest = max(present, key=lambda key: (len(_dumps(container[key])), -keys.index(key)))
            text = _dumps(container[largest])
            container[largest] = {
                "truncated": True,
                "original_chars": len(text),
                "head": text[:_HEAD],
                "why": "the on-call prompt's context budget; the rules block is never cut (R17)",
            }
            payload = _dumps(package)
    return payload


def _is_marker(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("truncated") is True
