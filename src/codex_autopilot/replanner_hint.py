"""What the next replanner reads about the attempts that were refused.

The retry hint passed only ``rejections[-1]["reason"]`` as one line and
always the same paragraph about top-level fields. A refusal with several
violations became one run-on sentence, a violation the model had already
been told about looked new, and the refusals of a change the on-call raised
after another one used every attempt were gone - the fresh replanner started
blind on the very plan that had just been refused three times.

Now every attempt is listed with its structured issues, the last one as a
numbered list, each marked when the same issue was already refused before -
the model sees that it repeated itself. Nothing is truncated: the prompt's
own ceiling refuses the launch instead (R17), and identical messages are
listed once, never replaced by a count.
"""

from __future__ import annotations

from typing import Any, Mapping

TOP_LEVEL_PATH = "plan"


def replanner_rejections(change: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every refused attempt the replanner should know of, oldest first."""

    attempts: list[dict[str, Any]] = []
    for source, key in (("inherited", "inherited_rejections"), ("own", "rejections")):
        for item in change.get(key) or ():
            if not isinstance(item, Mapping):
                continue
            entry: dict[str, Any] = {
                "attempt": len(attempts) + 1,
                "reason": str(item.get("reason") or ""),
                "issues": _issues(item),
            }
            if source == "inherited":
                entry["from_plan_change"] = str(item.get("plan_change_id") or "")
            attempts.append(entry)
    return attempts


def _issues(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues = [dict(issue) for issue in item.get("issues") or () if isinstance(issue, Mapping)]
    if issues:
        return issues
    # A refusal recorded before issues existed is one issue: its reason.
    return [{"stage": "", "path": "", "message": str(item.get("reason") or "")}]


def retry_hint(attempts: list[dict[str, Any]]) -> str:
    """The numbered list of the last refusal, with repeats marked."""

    last = attempts[-1]
    seen: dict[tuple[str, str], int] = {}
    for attempt in attempts[:-1]:
        for issue in attempt["issues"]:
            seen[(str(issue.get("path") or ""), str(issue.get("message") or ""))] = attempt["attempt"]
    lines: list[str] = []
    listed: set[str] = set()
    top_level = False
    for issue in last["issues"]:
        message = str(issue.get("message") or "")
        if message in listed:
            continue
        listed.add(message)
        path = str(issue.get("path") or "")
        accepted = issue.get("accepted") or ()
        line = f"{len(lines) + 1}. {message}"
        if accepted and "accepted fields are" not in message:
            line += f" (accepted: {', '.join(str(item) for item in accepted)})"
        earlier = seen.get((path, message))
        if earlier is not None:
            line += f" [repeated from attempt {earlier}]"
        lines.append(line)
        top_level = top_level or (path == TOP_LEVEL_PATH and bool(accepted))
    count = len(lines)
    text = (
        f"\n\nThe previous attempt was rejected for {count} "
        f"reason{'s' if count != 1 else ''}; the graph is unchanged; fix all of them in one "
        "reply:\n" + "\n".join(lines)
    )
    if len(attempts) > 1:
        text += (
            f"\nEarlier refused attempts ({len(attempts) - 1}) are in rejected_attempts; "
            "an issue marked repeated was already refused once and must not come back."
        )
    if top_level:
        from .plan_fields import PLAN_FIELDS

        text += (
            "\nTop-level plan fields are limited to this list and it cannot be extended: "
            + ", ".join(sorted(PLAN_FIELDS))
            + ". Anything else belongs inside tasks and roles; the nested sets are in "
            "constraints.allowed_fields."
        )
    return text


def allowed_plan_values() -> dict[str, list[str]]:
    """The enumerations a plan field must be one of, as the parser checks them."""

    from .acceptance import ACCEPTANCE_CLASSES
    from .models import EXECUTION_MODES
    from .plan import (
        EXECUTION_STRATEGIES,
        RESOURCE_ACCESS_MODES,
        RESOURCE_KINDS,
        VERIFICATION_CHECK_KINDS,
        VERIFICATION_POLICIES,
    )

    return {
        "plan.execution_strategy": sorted(EXECUTION_STRATEGIES),
        "plan.tasks[].execution_mode": sorted(EXECUTION_MODES),
        "plan.tasks[].acceptance_class": sorted(ACCEPTANCE_CLASSES),
        "plan.tasks[].verification.policy": sorted(VERIFICATION_POLICIES),
        "plan.tasks[].verification.deterministic_checks[].kind": sorted(VERIFICATION_CHECK_KINDS),
        "plan.tasks[].resources[].kind": sorted(RESOURCE_KINDS),
        "plan.tasks[].resources[].access": sorted(RESOURCE_ACCESS_MODES),
    }


def inherit_rejections(state: Any, record: dict[str, Any], plan_change_id: str) -> int:
    """Carry an exhausted change's refusals into the one raised after it.

    When the replanner used every attempt, the on-call (or her answer) raises
    a new plan change for the same task - a fresh budget, which the old
    change could not give. The new record started with no history: the fresh
    replanner met the same plan knowing nothing of the three refusals. Its
    budget counts only its own ``rejections``; the inherited ones are shown,
    not counted.
    """

    if not plan_change_id or plan_change_id == record.get("id"):
        return 0
    previous = next(
        (item for item in getattr(state, "plan_changes", None) or () if item.get("id") == plan_change_id),
        None,
    )
    if previous is None:
        return 0
    carried = [
        {**dict(item), "plan_change_id": str(item.get("plan_change_id") or plan_change_id)}
        for item in [*(previous.get("inherited_rejections") or ()), *(previous.get("rejections") or ())]
        if isinstance(item, Mapping)
    ]
    if carried:
        record["inherited_rejections"] = carried
    return len(carried)
