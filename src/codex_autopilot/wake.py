"""The run's wake-up: a due retry rises by itself, not on a human's word.

Measured on the v1.0 run. A task hit a rate limit, the runtime honestly
recorded the retry time - and that was all: when the last dispatcher
exited, no live process remained, and nobody was left to raise the due
retry. The run stood until the owner typed "Resume" - every few hours, by
hand, for an action the runtime knew how to do itself.

Nothing is bypassed here, and that was checked from the outside. The first
draft skipped the hook-trust gate by analogy with the dispatcher's
successors - but those skip it inside an already checked synchronous
operation, while the wake-up rises hours later, when nobody holds proof of
trust. So before raising the dispatcher the wake-up passes the same gate
as a hook-driven launch: if the human revoked trust meanwhile, the retry is
not raised. The owner is the same, and the ownership check in
reserve_ready_frontier and spawn_automatic_app_server_relay is the same.

What the wake-up does not do: it does not wake a run stopped by a human
(pause, BLOCKED), does not wake a finished one, does not race a live
dispatcher, and does not fire early if the limit was extended.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .resources import ResourceLockCoordinator
from .run_state import StateStore, utc_now

# Never sleep longer than one nap: the due time may be extended meanwhile.
MAX_NAP_SECONDS = 300


def due_wake_epoch(state: Any, cfg: Config | None = None) -> int | None:
    """When this run next needs raising, or None if it does not.

    Two reasons, not one. A task waiting on a retry has a time; a stranded
    run needs raising now.
    """

    due = [
        int(retry_at)
        for task_id, retry_at in (state.task_retry_at or {}).items()
        if state.task_states.get(task_id) == "RETRY_WAIT"
    ]
    if cfg is not None and is_stranded(cfg, state):
        due.append(int(time.time()))
    return min(due) if due else None


def is_stranded(cfg: Config, state: Any) -> bool:
    """A run with a ticket waiting on the on-call and nobody left to run it.

    The normal cycle leaves no dispatcher between turns on purpose: it
    launches a worker and exits, and the worker's own Stop hook raises the
    next one. So "no dispatcher" is not a fault - it is most of the run.

    It becomes one when the hook never fires. Measured 23 Sep 2026: a
    detached dispatch failed, the incident went to the on-call, and there
    the run sat - state saying RUNNING, a verifier marked ACTIVE, no process
    anywhere, and nothing that would ever raise one. The owner had to type
    "Resume" for something the runtime knew how to do, which is the same
    hole this module was written to close for retries.

    The signal is narrow on purpose: a ticket in the engineer's own lane
    means the on-call is needed and has not run. Anything looser would race
    a dispatcher that is simply waiting for a worker to think.
    """

    if state.status in {"BLOCKED", "DONE"}:
        return False
    if isinstance(state.dispatcher_pid, int) and _pid_alive(state.dispatcher_pid):
        return False
    try:
        from .pipeline_engineer import IncidentPhase, PipelineIncidentStore

        loaded = PipelineIncidentStore(cfg.state_dir).load().get("incidents") or {}
        incidents = loaded.values() if isinstance(loaded, dict) else loaded
        return any(
            str(item.get("phase")) == IncidentPhase.PIPELINE_ENGINEER.value
            for item in incidents
        )
    except Exception:  # noqa: BLE001 - an unreadable journal wakes nothing
        return False


def ensure_wake(
    cfg: Config,
    *,
    owner: str,
    owner_turn: str,
    spawn: Callable[..., int] | None = None,
) -> int | None:
    """Arm a wake-up if there is something to wake and nobody waits already.

    Returns the pid of the sleeping process, or None if there is nothing to
    wake. A live wake-up with a no-later time counts as sufficient: a second
    one next to it would only race for the same reservation.
    """

    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        due = due_wake_epoch(state, cfg)
        if due is None:
            return None
        if (
            isinstance(state.wake_pid, int)
            and _pid_alive(state.wake_pid)
            and isinstance(state.wake_at, int)
            and state.wake_at <= due
        ):
            return state.wake_pid
        pid = (spawn or _spawn_wake)(cfg, owner=owner, owner_turn=owner_turn, at_epoch=due)
        state.wake_pid = pid
        state.wake_at = due
        store.save(state)
        return pid


def run_wake(
    cfg: Config,
    *,
    at_epoch: int,
    owner: str,
    owner_turn: str,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    reserve: Callable[..., tuple[Any, ...]] | None = None,
    spawn_relay: Callable[..., int] | None = None,
) -> int:
    """Sleep until the due time and raise the dispatcher - or leave quietly if not allowed."""

    from .resilience import append_resilience_event

    store = StateStore(cfg.state_dir)
    while True:
        state = store.load()
        if state.status in {"BLOCKED", "DONE"} or store.pause_requested():
            _finish(cfg, store, "wake_skipped", detail={"why": "run is stopped or paused"})
            return 0
        due = due_wake_epoch(state, cfg)
        if due is None:
            _finish(cfg, store, "wake_skipped", detail={"why": "nothing waits for a retry"})
            return 0
        target = max(due, int(at_epoch))
        if isinstance(state.rate_limit_until, int):
            target = max(target, state.rate_limit_until)
        remaining = target - now()
        if remaining > 0:
            sleep(min(remaining, MAX_NAP_SECONDS))
            continue
        if _dispatcher_alive(state):
            _finish(cfg, store, "wake_skipped", detail={"why": "a dispatcher is already running"})
            return 0
        break

    if reserve is None:
        from .lifecycle import reserve_ready_frontier as reserve
    if spawn_relay is None:
        from .control import spawn_automatic_app_server_relay as spawn_relay

    # The same gate as a hook-driven launch. The sleeping process carries no
    # proof of trust with it, so it asks again; revoked trust is a reason not
    # to raise the retry, not a reason to skip the check.
    from .hook_trust import HookPreflightError

    try:
        descriptors = reserve(
            cfg,
            now_epoch=int(now()),
            relay_owner_thread_id=owner,
        )
    except HookPreflightError as exc:
        _finish(
            cfg,
            store,
            "wake_skipped",
            detail={"why": "hook trust is not in place", "error": str(exc)},
        )
        return 0
    if not descriptors:
        _finish(cfg, store, "wake_skipped", detail={"why": "the frontier reserved nothing"})
        return 0
    pids = [
        spawn_relay(
            cfg.root,
            reservation_token=item.reservation_token,
            initiator_thread_id=owner,
            initiator_turn_id=owner_turn,
        )
        for item in descriptors
    ]
    _finish(
        cfg,
        store,
        "wake_dispatched",
        detail={
            "tasks": [item.task_id for item in descriptors],
            "pids": pids,
        },
    )
    return 0


def _finish(cfg: Config, store: StateStore, event: str, *, detail: dict[str, Any]) -> None:
    """The wake-up's last record - under the same lock as every other.

    Measured by the reviewer: a read-modify-write without a transaction,
    right after the wake-up spawned the relays, raced the fresh dispatcher
    writing to the same file under its own lock, and lost its changes
    silently.
    """

    from .resilience import append_resilience_event

    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        append_resilience_event(state, event, at=utc_now(), detail=detail)
        state.wake_pid = None
        state.wake_at = None
        store.save(state)


def _dispatcher_alive(state: Any) -> bool:
    if _pid_alive(state.dispatcher_pid):
        return True
    return any(
        item.get("automatic_dispatch_state") == "RUNNING"
        and _pid_alive(item.get("automatic_dispatch_pid"))
        for item in state.worker_sessions
    )


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True



def wake_command(cfg: Config, *, owner: str, owner_turn: str, at_epoch: int) -> list[str]:
    """The sleeping process's arguments - exactly what the ``_wake`` parser accepts.

    Taken out of the launch so it can be checked: the reviewer broke a flag
    name, and 947 tests stayed green because the real launch never ran
    anywhere. Now a test runs these arguments through the real CLI parser.
    """

    return [
        sys.executable,
        "-m",
        "codex_autopilot.cli",
        "_wake",
        "--project",
        str(cfg.root),
        "--at",
        str(int(at_epoch)),
        "--owner",
        owner,
        "--owner-turn",
        owner_turn,
    ]


def _spawn_wake(cfg: Config, *, owner: str, owner_turn: str, at_epoch: int) -> int:
    log_dir = cfg.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / f"wake-{at_epoch}.log").open("a", encoding="utf-8")
    env = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1])
    entries = [item for item in str(env.get("PYTHONPATH") or "").split(os.pathsep) if item]
    if source_root not in entries:
        entries.insert(0, source_root)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    try:
        proc = subprocess.Popen(
            wake_command(cfg, owner=owner, owner_turn=owner_turn, at_epoch=at_epoch),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    finally:
        log.close()
    return proc.pid


# ---------------------------------------------------------------------------
# Survives a reboot: a scheduled sweep instead of one sleeping process
# ---------------------------------------------------------------------------
#
# A sleeping process dies with the machine. So next to it there is a second
# path a reboot does not touch: a launchd agent sweeps the known projects
# every few minutes and arms a wake-up wherever a due retry waits and no
# live wake-up exists. The owner and the turn come from the run state
# itself - from the last completed turn of the causal owner, exactly as the
# dispatcher finds them for its successors.


def projects_registry_path() -> Path:
    """The list of projects the agent sweeps.

    Lives in the install root, not next to the launch registry: that one
    lives in a temporary directory macOS clears on reboot - and the agent is
    needed precisely after one. Uninstalling the runtime takes the list with
    it.
    """

    configured = os.environ.get("CODEX_AUTOPILOT_INSTALL_ROOT")
    root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / "Library" / "Application Support" / "CodexAutopilot"
    )
    return root / "projects.json"


def register_project(root: Path, *, path: Path | None = None) -> None:
    """Remember a project for the sweep. Registering twice is not an error."""

    import json

    target = path or projects_registry_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    roots = set(registered_projects(path=target))
    roots.add(str(Path(root).resolve()))
    target.write_text(json.dumps(sorted(roots), ensure_ascii=False, indent=2), encoding="utf-8")


def registered_projects(*, path: Path | None = None) -> list[str]:
    import json

    target = path or projects_registry_path()
    if not target.is_file():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(item) for item in raw if isinstance(item, str)] if isinstance(raw, list) else []


def derive_owner(state: Any) -> tuple[str, str] | None:
    """The causal owner for the wake-up - from the journal, not from arguments.

    The question is the dispatcher's own, so the answer is too: this asks
    ``causal_predecessor``, the single predicate for "the owner's turn is
    over". It used to keep a narrower copy that accepted only a
    ``turn_completed`` event, while the dispatcher accepts three proofs - the
    event, a closed session carrying the same turn, and an observed
    interrupt. An interrupted turn is exactly the case that leaves nobody to
    raise the successor, and for it this returned None: after a reboot the
    sweep skipped the project quietly and the retry waited for a human, the
    one thing the alarm exists to prevent.

    If there is no such owner, there is nobody on whose behalf to wake - and
    the agent skips the project quietly.

    The import is local: wake is reached from the hook and the CLI, and
    lifecycle pulls control back in, so a module-level import here would
    close a ring.
    """

    from .lifecycle import causal_predecessor

    for session in reversed(state.worker_sessions):
        owner = str(session.get("relay_owner_thread_id") or "")
        if not owner:
            continue
        predecessor = causal_predecessor(state, owner)
        if predecessor is not None and str(predecessor.get("turn_id") or ""):
            return owner, str(predecessor["turn_id"])
    return None


def sweep(
    *,
    roots: list[str] | None = None,
    spawn: Callable[..., int] | None = None,
    load: Callable[[Path], Config] | None = None,
) -> dict[str, str]:
    """One sweep: for every project, arm a wake-up if one is needed.

    Returns what was decided per root; the agent prints it to its log.
    """

    from .config import load_config as _load_config

    outcome: dict[str, str] = {}
    for raw in roots if roots is not None else registered_projects():
        root = Path(raw)
        if not (root / ".codex-autopilot" / "config.toml").is_file():
            outcome[raw] = "gone"
            continue
        try:
            cfg = (load or _load_config)(root)
            state = StateStore(cfg.state_dir).load()
        except Exception as exc:  # noqa: BLE001 - one sick project does not break the sweep
            outcome[raw] = f"unreadable: {exc}"
            continue
        if state.status in {"BLOCKED", "DONE"} or StateStore(cfg.state_dir).pause_requested():
            outcome[raw] = "stopped"
            continue
        if due_wake_epoch(state, cfg) is None:
            outcome[raw] = "nothing due"
            continue
        owner = derive_owner(state)
        if owner is None:
            outcome[raw] = "no completed owner"
            continue
        pid = ensure_wake(cfg, owner=owner[0], owner_turn=owner[1], spawn=spawn)
        outcome[raw] = f"wake {pid}"
    return outcome
