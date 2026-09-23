"""Arming the run: one transaction, and never beside a live session.

``arm`` was written when the on-call worked alone: it loaded run-state,
wrote the launch request and saved run-state back with status READY/ARMED -
without the run's transaction, and with only ``state.dispatcher_pid`` as its
guard, which is always None in the desktop-owned mode. With the engineer
now working next to the workers (``rearm_run`` is one of its repair actions),
that read-modify-write would overwrite whatever a neighbour's dispatcher
saved in between, and declare READY/ARMED over sessions that are running.

So arming is now:

- under ``ResourceLockCoordinator.transaction`` like every other writer;
- refused while any OTHER session of the run is pending or has a live
  automatic dispatcher - the chain is alive, and its own completions carry
  it on; arming there would only race it. The calling engineer's own session
  (the thread the CLI names as the caller) does not count: it is the one
  asking.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class ArmRefused(RuntimeError):
    """The run is alive; arming it now would race the sessions that carry it."""


def _live_neighbours(state: Any, caller_thread: str) -> list[str]:
    from .lifecycle_base import PENDING_SESSION_STATUSES, _pid_alive

    live = []
    for item in state.worker_sessions:
        if caller_thread and str(item.get("thread_id") or "") == caller_thread:
            continue
        if item.get("status") in PENDING_SESSION_STATUSES or _pid_alive(
            item.get("automatic_dispatch_pid")
        ):
            live.append(f"{item.get('task_id')}/{item.get('kind')}:{item.get('status')}")
    return live


def arm_run(
    root: Path,
    *,
    register: Callable[[Path], None] | None = None,
    caller_thread: str = "",
) -> None:
    from .config import load_config
    from .control import pid_alive
    from .launch_registry import LaunchRegistry
    from .resources import ResourceLockCoordinator
    from .run_authorization import ensure_recorded
    from .run_state import StateStore, utc_now

    cfg = load_config(root)
    store = StateStore(cfg.state_dir)
    caller = str(caller_thread or "").strip()
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        if pid_alive(state.dispatcher_pid):
            raise RuntimeError(f"dispatcher is already running with pid {state.dispatcher_pid}")
        if state.status == "DONE":
            raise RuntimeError("the migrated or initialized roadmap is already DONE")
        live = _live_neighbours(state, caller)
        if live:
            raise ArmRefused(
                "the run is alive - these sessions carry it and their own completions "
                "raise what comes next: " + ", ".join(live[:8])
            )
        payload = {
            "project_root": str(cfg.root),
            "armed_at": utc_now(),
            "run_id": state.run_id,
        }
        request_id = LaunchRegistry().add(payload)
        payload["request_id"] = request_id
        store.arm(payload)
        # Arming is her start of the run, and so her durable authorization
        # for it (R4): recorded here, with its versioned list of covered
        # operations, in the same transaction.
        ensure_recorded(cfg, state, at=payload["armed_at"], granted_by="arm")
        state.status = "READY"
        state.phase = "ARMED"
        store.save(state)
    # The project joins the wake agent's sweep: from now on a due retry is
    # raised even after a reboot.
    if register is not None:
        register(cfg.root)
