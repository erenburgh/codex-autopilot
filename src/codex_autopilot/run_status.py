"""What the run as a whole is doing - derived, in one place, never decided.

Moved out of lifecycle_base (exactly 1500 lines) and widened.

A run's status used to be written by whoever stopped it. The stop door set
BLOCKED before the on-call had looked at anything; the engineer's
escalation set BLOCKED for the whole run over one task; and the wake-up
skipped anything BLOCKED as "a human's decision". The result, read end to
end on 23 Sep 2026: a ticket in the engineer's lane, the run BLOCKED, and no
engineer ever reserved.

BLOCKED is not a decision. It is what is left when there is nothing to do
and what remains waits for the owner. So it is derived here, after the
frontier has reserved what it could:

- DONE when every task is verified;
- PAUSED when she paused it - her pause outranks everything;
- RUNNING while any session is pending: a worker, a screening, the on-call
  (an engineer at work with no task slot used to derive as BLOCKED);
- WAITING for a rate limit or a retry time;
- WAITING (RUNTIME_PATCH_PENDING) while a proven runtime patch waits to be
  installed - the run drains for it (runtime_install);
- WAITING (PIPELINE_ENGINEER_PENDING) when a ticket needs the on-call and no
  engineer is reserved yet - the wake-up raises it; never BLOCKED;
- BLOCKED (AWAITING_OWNER) only when nothing can be taken and a stopped
  task or a ticket handed up waits for her answer;
- WAITING_DEPENDENCIES otherwise.

This is the only module outside the stop door that may write the run's
BLOCKED status; a structural test holds that.
"""

from __future__ import annotations

from typing import Any


def _finish_global_state(
    plan: Any,
    state: Any,
    descriptors: tuple[Any, ...],
    *,
    paused: bool = False,
    cfg: Any = None,
) -> None:
    from .lifecycle_base import PENDING_SESSION_STATUSES
    from .run_state import utc_now
    from .task_state import TaskState

    if all(value == TaskState.VERIFIED.value for value in state.task_states.values()):
        state.status = "DONE"
        state.phase = "DONE"
        state.completed_at = utc_now()
        return
    if paused:
        state.status = "PAUSED"
        state.phase = "PAUSED_DRAINING" if state.active_task_ids else "PAUSED"
        return
    pending = [
        item for item in state.worker_sessions if item.get("status") in PENDING_SESSION_STATUSES
    ]
    engineer_at_work = any(item.get("kind") == "pipeline_engineer" for item in pending)
    if state.active_plan_change_id is not None:
        if descriptors:
            state.status = "RUNNING"
            state.phase = "AWAITING_DESKTOP_CREATE"
        elif state.active_task_ids:
            replanning = any(
                item.get("kind") == "replanner" and item.get("task_id") in state.active_task_ids
                for item in pending
            )
            state.status = "RUNNING"
            state.phase = "PLAN_CHANGE_REPLANNING" if replanning else "PLAN_CHANGE_DRAINING"
        elif engineer_at_work:
            state.status = "RUNNING"
            state.phase = "PIPELINE_ENGINEER_ACTIVE"
        elif state.rate_limit_until is not None:
            state.status = "WAITING"
            state.phase = "WAITING_RATE_LIMIT"
        else:
            state.status = "WAITING"
            state.phase = "PLAN_CHANGE_WAITING_LOCKS"
        return
    if descriptors:
        state.status = "RUNNING"
        state.phase = "AWAITING_DESKTOP_CREATE"
    elif state.active_task_ids:
        state.status = "RUNNING"
        state.phase = "DESKTOP_WORKERS_ACTIVE"
    elif pending:
        state.status = "RUNNING"
        state.phase = "PIPELINE_ENGINEER_ACTIVE" if engineer_at_work else "DESKTOP_WORKERS_ACTIVE"
    elif state.rate_limit_until is not None or any(
        value == TaskState.RETRY_WAIT.value for value in state.task_states.values()
    ):
        state.status = "WAITING"
        state.phase = "WAITING_RATE_LIMIT"
    elif _patch_pending(cfg):
        # The run drained for a proven runtime patch; the wake-up installs
        # it once no dispatcher is alive, and the run then continues.
        state.status = "WAITING"
        state.phase = "RUNTIME_PATCH_PENDING"
    elif _engineer_needed(cfg, state):
        state.status = "WAITING"
        state.phase = "PIPELINE_ENGINEER_PENDING"
    elif _waiting_for_owner(cfg, state) and not _reservable(cfg, state):
        state.status = "BLOCKED"
        state.phase = "AWAITING_OWNER"
    else:
        state.status = "WAITING"
        state.phase = "WAITING_DEPENDENCIES"


def _engineer_needed(cfg: Any, state: Any) -> bool:
    if cfg is None:
        return False
    from .engineer_reservation import engineer_needed

    try:
        return engineer_needed(cfg, state)
    except Exception:  # noqa: BLE001 - an unreadable journal must not hide the owner's wait
        return False


def _waiting_for_owner(cfg: Any, state: Any) -> bool:
    from .task_state import TaskState

    if cfg is None:
        return any(value == TaskState.BLOCKED.value for value in state.task_states.values())
    from .engineer_reservation import waiting_for_owner

    try:
        return waiting_for_owner(cfg, state)
    except Exception:  # noqa: BLE001
        return any(value == TaskState.BLOCKED.value for value in state.task_states.values())


def _reservable(cfg: Any, state: Any) -> bool:
    if cfg is None:
        return False
    from .engineer_reservation import reservable_work

    try:
        return reservable_work(cfg, state)
    except Exception:  # noqa: BLE001
        return False


def _patch_pending(cfg: Any) -> bool:
    if cfg is None:
        return False
    from .runtime_install import runtime_patch_pending

    return runtime_patch_pending(cfg)
