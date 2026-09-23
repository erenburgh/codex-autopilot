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
on-call reads the rest from the journal it is pointed at. If the rules and
the ticket alone cannot fit, nothing is cut and the ceiling refuses as
before: that is a context-planning defect, not something to hide.
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
_HEAD = 2_000


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def engineer_payload(
    incident_package: Mapping[str, Any], rules: list[dict[str, str]], ceiling: int
) -> str:
    """The package with the rules, as JSON no longer than the room the frame leaves."""

    package = dict(incident_package)
    package["rules"] = rules
    room = ceiling - ENGINEER_FRAME_RESERVE
    payload = _dumps(package)
    while len(payload) > room:
        present = [key for key in TRIMMABLE if key in package and not _is_marker(package[key])]
        if not present:
            return payload
        largest = max(present, key=lambda key: (len(_dumps(package[key])), -TRIMMABLE.index(key)))
        text = _dumps(package[largest])
        package[largest] = {
            "truncated": True,
            "original_chars": len(text),
            "head": text[:_HEAD],
            "why": "the on-call prompt's context budget; the rules block is never cut (R17)",
        }
        payload = _dumps(package)
    return payload


def _is_marker(value: Any) -> bool:
    return isinstance(value, Mapping) and value.get("truncated") is True
