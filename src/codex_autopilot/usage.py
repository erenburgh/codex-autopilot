"""How many workers may be launched given the current rate-limit state.

The number the user declared is their decision and the ceiling. Here it
can only go down, and only when the limit is really near. Telling someone
on auto-billing "you are entitled to ten" would be presumptuous: they pay
as they go, and we have no grounds to restrict them.

The data comes from App Server as the `account/rateLimits/updated` event
and is read on demand through `account/rateLimits/read`:

    primary.usedPercent      how much of the window is used
    primary.windowDurationMins  the window length
    credits.unlimited        unlimited
    credits.hasCredits       credits exist, billing continues
    spendControlReached      the user set a cap themselves and reached it
    rateLimitReachedType     the limit is already exhausted
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class WorkerBudget:
    """A capacity decision together with its reason.

    `workers = None` means no ceiling: on an unlimited account there is
    nothing to restrict with, and the graph itself sets the number of
    simultaneous workers - as many tasks as are ready.
    """

    workers: int | None
    reason: str
    limited: bool


def _snapshot(limits: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(limits, Mapping):
        return {}
    inner = limits.get("rateLimits")
    return inner if isinstance(inner, Mapping) else limits


def worker_budget(
    declared: int,
    limits: Mapping[str, Any] | None,
    *,
    declared_by_user: bool = False,
) -> WorkerBudget:
    """How many workers to launch now, and why exactly that many.

    `declared_by_user` means the human named the number explicitly. Such a
    number is never raised - not even on unlimited: if they asked for
    three, it is three.
    """

    declared = max(1, int(declared))
    snapshot = _snapshot(limits)
    if not snapshot:
        # No data - no reason to cut. A silent reduction out of ignorance
        # would be the worst option: the user would not understand why the
        # run goes slower than they asked.
        return WorkerBudget(declared, "no rate-limit data", False)

    credits = snapshot.get("credits")
    credits = credits if isinstance(credits, Mapping) else {}
    # A cap set by the human outranks any credits: they set it precisely so
    # billing would stop. This check used to stand AFTER credits and so
    # never fired for the very accounts it was written for - those on
    # auto-billing.
    if snapshot.get("spendControlReached"):
        return WorkerBudget(1, "the spending cap set by the user has been reached", True)
    if snapshot.get("rateLimitReachedType"):
        return WorkerBudget(1, "the limit is already exhausted", True)

    if _burns_without_a_wall(credits):
        # Auto-billing is unlimited: the limit window is no wall for such an
        # account, billing carries on. No ceiling - as many tasks as the
        # graph opens at once will run.
        if declared_by_user:
            return WorkerBudget(declared, "unlimited billing, the number was set by the user", False)
        return WorkerBudget(None, "unlimited billing: no ceiling", False)

    primary = snapshot.get("primary")
    primary = primary if isinstance(primary, Mapping) else {}
    used = primary.get("usedPercent")
    if not isinstance(used, (int, float)):
        return WorkerBudget(declared, "window usage unknown", False)

    remaining = max(0.0, 100.0 - float(used))
    if remaining >= 50:
        return WorkerBudget(declared, f"{used:.0f}% of the window used", False)
    if remaining >= 25:
        workers = max(2, declared // 2)
        return WorkerBudget(min(declared, workers), f"{used:.0f}% of the window used", True)
    if remaining >= 10:
        return WorkerBudget(min(declared, 2), f"{used:.0f}% of the window used", True)
    return WorkerBudget(1, f"{used:.0f}% of the window used", True)


def capacity_notice(limits: Mapping[str, Any] | None, declared: int | None) -> str:
    """What to tell the human about capacity before the run starts.

    The user need not know their plan, nor that the number of workers can
    be set at all. Asking once, naming their own situation, is more honest
    than silently setting the template's ten - which is exactly how it
    stood for a whole 24-task run.
    """

    snapshot = _snapshot(limits)
    credits = snapshot.get("credits")
    credits = credits if isinstance(credits, Mapping) else {}
    plan_type = _human_plan_name(snapshot.get("planType"))

    if declared is not None:
        return (
            f"Parallel workers: {declared} - as you specified. "
            "You can change it at any time by naming another number."
        )
    if _burns_without_a_wall(credits):
        return (
            "Your billing is unlimited, so there is no ceiling on parallel workers: "
            "as many tasks run at once as the plan opens. "
            "To cap it, name a number."
        )
    fallback = default_workers(limits)
    if _is_plus(snapshot.get("planType")):
        return (
            f"Plan {plan_type}: {fallback} parallel workers by default - "
            "the limit window is narrow here, and ten would burn it in one run. "
            "You can set your own number."
        )
    if plan_type:
        return (
            f"Plan {plan_type}: {fallback} parallel workers by default, "
            "and they narrow by themselves as the limit window runs low. "
            "You can set your own number."
        )
    return (
        f"{fallback} parallel workers by default. You can set your own "
        "number; as the limit approaches they narrow by themselves."
    )


# The Plus plan is noticeably narrower than the rest: ten workers on it
# would burn the window in one run. The user's decision of 14 September.
PLUS_DEFAULT_WORKERS = 3
STANDARD_DEFAULT_WORKERS = 10
_PLUS_PLANS = frozenset({"plus", "chatgpt-plus", "plus-monthly"})


def _burns_without_a_wall(credits: Mapping[str, Any]) -> bool:
    """An account for which the limit window is not a wall.

    Unlimited and enabled auto-billing are the same situation: spending
    continues past the window, there is nothing to hit. Credits used to
    count as a mitigating circumstance and still narrowed capacity - that
    is, restricted exactly the one who pays for having no restrictions.
    """

    return bool(credits.get("unlimited") or credits.get("hasCredits"))


def default_workers(limits: Mapping[str, Any] | None) -> int:
    """How many workers to set when the human said nothing."""

    snapshot = _snapshot(limits)
    plan_type = str(snapshot.get("planType") or "").strip().lower()
    if plan_type in _PLUS_PLANS:
        return PLUS_DEFAULT_WORKERS
    return STANDARD_DEFAULT_WORKERS


# Internal plan names are not shown to the human: they read their plan as
# "Pro", while the App Server event calls it "prolite". Showing the slug
# would tell the user something false about their own subscription, and an
# unfamiliar slug would invent a plan they do not know.
_PLAN_NAMES = {
    "plus": "Plus",
    "chatgpt-plus": "Plus",
    "plus-monthly": "Plus",
    "pro": "Pro",
    "prolite": "Pro",
    "chatgpt-pro": "Pro",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
}


def _is_plus(raw: Any) -> bool:
    return str(raw or "").strip().lower() in _PLUS_PLANS


def _human_plan_name(raw: Any) -> str:
    """The plan name as the human knows it, or empty."""

    return _PLAN_NAMES.get(str(raw or "").strip().lower(), "")
