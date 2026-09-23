"""R3: an infrastructure stop holds its task; only the on-call's escalation blocks it.

Her rule R3 (RULES.md): a task that could not proceed for an infrastructure
reason does not go to BLOCKED. The predecessor files a ticket, DevOps looks,
repairs, hands control back, and the same action runs again. BLOCKED is
allowed only for a product or policy matter, or once DevOps itself is
exhausted.

0.13.x did the opposite at three places. A worker that answered
``BLOCKED ENVIRONMENT_FAILURE``, a verifier that could not be read three times
running, and a task no verifier could be routed for all went straight to
TaskState.BLOCKED, and only then was the on-call called. Read end to end:
the engineer repaired the environment, closed its ticket - and the task stayed
BLOCKED, because a closed ticket does not lift a stop. The orphan sweep then
filed a ticket about the very task the engineer had just repaired, and the
second closure sent it to the owner. The repair was real; the rule made it
invisible.

So the three infrastructure stops now HOLD their task instead: it stays in a
state it can resume from, and the ticket's pause (``tasks_paused_by_incidents``)
keeps it out of every reservation while the on-call works. When the ticket
closes, the pause lifts and the task takes up the same action again - a
fresh worker, a fresh verifier. BLOCKED arrives only when the on-call hands
the ticket up (``block_escalated_tasks``): that is the "DevOps exhausted"
half of R3, and it is what the owner's unblock expects to find.

Product and policy stops - a worker's PRODUCT_DECISION, the top of the hiring
ladder, a refused plan - still block at once: those are hers by the rule.
"""

from __future__ import annotations

from typing import Any

from .task_state import IllegalTaskTransition, TaskState, transition_task

# The worker's closed-list codes that name an infrastructure cause. The rest
# (DANGEROUS_PERMISSION, DEPENDENCY_DEFECT, CONTRADICTORY_CONTRACT,
# PRODUCT_DECISION, ARCHITECTURE_DECISION) are product or policy matters.
# UNSPECIFIED is here on purpose: a stop that names no cause has not shown it
# is hers, and R3 forbids BLOCKED until that is shown. RECOVERY_EXHAUSTED is
# the worker's own means, not DevOps's - the engineer has not looked yet.
INFRASTRUCTURE_WORKER_REASONS = frozenset(
    {"ENVIRONMENT_FAILURE", "MISSING_RESOURCE", "RECOVERY_EXHAUSTED", "UNSPECIFIED"}
)


def stop_worker_task(plan: Any, state: Any, task_id: str, reason_code: str) -> bool:
    """Move a task whose worker stopped itself. True when it is only held.

    An infrastructure stop returns the task to READY (through RETRY_WAIT, the
    edge a failed attempt already takes): the ticket holds it, and when the
    ticket closes the same work is taken up again. Anything else is BLOCKED.
    """

    if reason_code in INFRASTRUCTURE_WORKER_REASONS:
        for step in (TaskState.RETRY_WAIT, TaskState.READY):
            state.task_states = transition_task(plan, state.task_states, task_id, step)
        return True
    state.task_states = transition_task(plan, state.task_states, task_id, TaskState.BLOCKED)
    return False


def block_escalated_tasks(cfg: Any, state: Any, incident_id: str) -> list[str]:
    """The on-call handed a ticket up: now, and only now, its tasks are BLOCKED.

    A task with a live producer is left alone - it is not waiting, it is
    working. A task already BLOCKED (the ladder, a product stop) stays as it
    is. Every other task the ticket holds becomes BLOCKED, which is what the
    status card, the orphan sweep and her unblock read as "waits for you".
    """

    from .lifecycle_base import PENDING_SESSION_STATUSES
    from .pipeline_engineer import PipelineIncidentStore
    from .plan import load_plan

    incident = next(
        (
            item
            for item in PipelineIncidentStore(cfg.state_dir).load().get("incidents") or ()
            if str(item.get("incident_id")) == incident_id
        ),
        None,
    )
    if incident is None:
        return []
    plan = load_plan(cfg.state_dir, cfg.profile)
    blocked: list[str] = []
    for task_id in (str(item) for item in incident.get("affected_task_ids") or ()):
        if task_id not in plan.task_map or task_id in state.active_task_ids:
            continue
        if any(
            item.get("task_id") == task_id
            and item.get("kind") != "pipeline_engineer"
            and item.get("status") in PENDING_SESSION_STATUSES
            for item in state.worker_sessions
        ):
            continue
        if state.task_states.get(task_id) in {TaskState.BLOCKED.value, TaskState.VERIFIED.value}:
            continue
        try:
            state.task_states = transition_task(
                plan, state.task_states, task_id, TaskState.BLOCKED
            )
        except IllegalTaskTransition:
            continue
        blocked.append(task_id)
    return blocked
