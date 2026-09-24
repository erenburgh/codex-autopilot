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

What the wake-up does not do: it does not wake a run she paused, does not
wake a finished one, does not race a live dispatcher, and does not fire
early if the limit was extended.

BLOCKED used to be on that list, as "stopped by a human". It never was: the
stop door wrote it before the on-call had looked, so the wake-up skipped
exactly the runs whose ticket was waiting for an engineer. BLOCKED is now
derived (run_status) and means only "everything left waits for her"; what
decides here is whether anything waits for the runtime.
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
    """A run nobody will move unless it is raised.

    The normal cycle leaves no dispatcher between turns on purpose: it
    launches a worker and exits, and the worker's own Stop hook raises the
    next one. So "no dispatcher" is not a fault - it is most of the run.

    It becomes one when the hook never fires. Measured 23 Sep 2026: a
    detached dispatch failed, the incident went to the on-call, and there
    the run sat - state saying RUNNING, a verifier marked ACTIVE, no process
    anywhere, and nothing that would ever raise one.

    Stranded means: no live dispatcher, and one of
    - a ticket waits for the on-call (in its lane, on its way there, or a
      stopped task nobody holds) with no engineer session pending;
    - work the frontier could take, and not one pending session;
    - a pending session whose dispatcher is gone - one never created (the
      Stop hook knew this case; the wake-up did not), one already running
      (an on-call whose completion failed killed its dispatcher and stayed
      ACTIVE, and the lane froze behind it with nobody told), and a create
      in doubt that no ticket holds yet.
    """

    # Her pause, the pause marker and a finished run are stops; nothing
    # else is. The marker is the authority - the status may lag behind it.
    if state.status in {"DONE", "PAUSED"}:
        return False
    if StateStore(cfg.state_dir).pause_requested():
        return False
    if _patch_drain_overdue(cfg):
        # A staged patch past its drain deadline is a stop even beside a
        # live dispatcher: that dispatcher is past its own bounds, and
        # without a wake-up nobody would ever refuse the patch and file the
        # ticket - the run would stand drained with nobody told.
        return True
    if _dispatcher_alive(state):
        return False
    try:
        from .engineer_reservation import stranded_reason

        return stranded_reason(cfg, _Sessions(state)) is not None
    except Exception:  # noqa: BLE001 - an unreadable journal wakes nothing
        return False


class _Sessions:
    """The state as the stranding check reads it, tolerant of older shapes."""

    def __init__(self, state: Any) -> None:
        self.status = getattr(state, "status", "")
        self.rate_limit_until = getattr(state, "rate_limit_until", None)
        self.worker_sessions = list(getattr(state, "worker_sessions", None) or [])
        self.task_states = dict(getattr(state, "task_states", None) or {})
        self.active_plan_change_id = getattr(state, "active_plan_change_id", None)
        self.plan_changes = list(getattr(state, "plan_changes", None) or [])


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
    revive: Callable[[Config], Any] | None = None,
    observe: Callable[[Config], dict[str, str]] | None = None,
) -> int:
    """Sleep until the due time and raise the dispatcher - or leave quietly if not allowed."""

    from .department_audit import audit_lead_sessions
    from .project_roots_audit import refresh_run_roots_audit
    from .resilience import append_resilience_event

    store = StateStore(cfg.state_dir)
    while True:
        state = store.load()
        if state.status == "DONE" or store.pause_requested():
            _finish(cfg, store, "wake_skipped", detail={"why": "run is finished or paused"})
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
        if _dispatcher_alive(state) and not _patch_drain_overdue(cfg):
            _finish(cfg, store, "wake_skipped", detail={"why": "a dispatcher is already running"})
            return 0
        break

    # A proven runtime patch is installed here, outside the engineer's
    # sandbox, and only when no dispatcher of this run is alive, scheduled
    # or running (runtime_install). This process then leaves: it imported
    # the old tree, and the next sweep raises the run on the new one. Past
    # the drain deadline the patch is refused and ticketed instead, and the
    # wake-up goes on to raise the on-call for that ticket.
    patched = _install_staged_patch(cfg, store)
    if patched is not None:
        return 0

    if reserve is None:
        from .lifecycle import reserve_ready_frontier as reserve
    if spawn_relay is None:
        from .control import spawn_automatic_app_server_relay as spawn_relay

    # The same gate as a hook-driven launch. The sleeping process carries no
    # proof of trust with it, so it asks again; revoked trust is a reason not
    # to raise the retry, not a reason to skip the check.
    from .hook_trust import HookPreflightError

    try:
        descriptors = tuple(
            reserve(
                cfg,
                now_epoch=int(now()),
                relay_owner_thread_id=owner,
            )
        )
        # After the gate, never before it: sessions whose dispatcher died
        # mid-turn are settled by the server's own word, and whatever that
        # freed - the on-call's lane, above all - is reserved at once.
        settled = _settle_dead_sessions(cfg, observe=observe)
        audit_lead_sessions(cfg)  # R30: leads that outlived their acceptance
        refresh_run_roots_audit(cfg, None, occasion="wake")  # R6: Desktop's side
        if settled:
            descriptors += tuple(
                reserve(cfg, now_epoch=int(now()), relay_owner_thread_id=owner)
            )
    except HookPreflightError as exc:
        # The one stop that cannot go through the on-call: raising the
        # engineer passes the same trust gate, and going around it is hers
        # to decide, never ours. So she is told directly, with what to do.
        detail = {
            "why": "hook trust is not in place",
            "error": str(exc),
            "diagnosis": "the Stop hook is not trusted, so no session of this run can be raised",
            "recommendation": "restore trust in the Codex Autopilot hook; the run then continues by itself",
        }
        _signal_owner_once(cfg, "hook_trust", detail["recommendation"])
        _finish(cfg, store, "wake_skipped", detail=detail)
        return 0
    if not descriptors:
        # A reservation made earlier whose dispatcher died is not new, and
        # the frontier does not return it. The Stop hook already raised
        # these; the wake-up gave up with "reserved nothing".
        try:
            pids = list((revive or _revive_stalled)(cfg))
            why = "raised stalled reservations" if pids else "the frontier reserved nothing"
        except Exception as exc:  # noqa: BLE001 - the ownership check refused; leave a trace
            pids, why = [], f"stalled reservations could not be raised: {exc}"
        _finish(
            cfg,
            store,
            "wake_dispatched" if pids else "wake_skipped",
            detail={"why": why, "pids": pids},
        )
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


def _install_staged_patch(cfg: Config, store: StateStore) -> str | None:
    """Install the run's staged runtime patch; None when there is nothing to do.

    A refused patch (the tree moved under it, or this runtime is not an
    installation) is filed as a stop for the on-call - a staged patch that
    nobody installs would drain the run in silence - and the wake-up goes on
    to raise the run.
    """

    from .runtime_install import install_when_quiet

    # One transaction around the install and what a refusal revokes: once a
    # refused patch leaves `pending` the drain is over, and a fresh hire it
    # bought must be gone before any reservation can see the task READY.
    with ResourceLockCoordinator(store, cfg.root).transaction():
        try:
            outcome = install_when_quiet(cfg)
        except Exception as exc:  # noqa: BLE001 - the attempt is recorded; the run is not stopped by it
            outcome = {"deferred": f"the staged patch could not be installed: {exc}"}
        if outcome is not None and outcome.get("refused"):
            state = store.load()
            _file_refused_patches(cfg, state, outcome["refused"])
            store.save(state)
    if outcome is None:
        return None
    if outcome.get("deferred"):
        _finish(cfg, store, "wake_skipped", detail={"why": "runtime patch deferred", "reason": outcome["deferred"]})
        return "deferred"
    if outcome.get("installed"):
        _finish(
            cfg,
            store,
            "runtime_patch_installed",
            detail={"tree": outcome.get("tree"), "entries": [item["entry"] for item in outcome["installed"]]},
        )
        return "installed"
    return None


def _file_refused_patches(cfg: Config, state: Any, refused: list[dict[str, Any]]) -> None:
    """A refused patch is a stop for the on-call, and takes back what it bought.

    A task returned from the top of its ladder on this patch got a fresh
    hire while the patch was only staged. Measured by the independent check:
    the refusal filed a ticket and revoked nothing, so the task ran its fresh
    budget on the old code. Now the grant is revoked here (``ladder_grants``)
    and the ticket holds the task it sent back to BLOCKED. A refused revert
    leaves its patch installed, so it revokes nothing.
    """

    from .blocked_runs import stop_run
    from .ladder_grants import revoke_grants
    from .plan import load_plan

    plan = load_plan(cfg.state_dir, cfg.profile)
    for item in refused:
        at = utc_now()
        entry = str(item["entry"])
        blocked = (
            []
            if entry.startswith("revert-")
            else revoke_grants(cfg, plan, state, (entry,), reason="refused at install", at=at)
        )
        stop_run(
            cfg,
            state,
            stop_kind="runtime_patch_refused",
            phase="RUNTIME_PATCH_REFUSED",
            reason=f"staged runtime patch {entry} was not installed: {item['reason']}",
            summary="A proven runtime patch could not be installed and was set aside.",
            at=at,
            task_ids=tuple(blocked),
            system_state={"entry": entry, "revoked_fresh_hires": list(blocked)},
        )


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


def _settle_dead_sessions(
    cfg: Config, *, observe: Callable[[Config], dict[str, str]] | None = None
) -> tuple[str, ...]:
    """Retire pending sessions whose dispatcher died and whose turn is over.

    Such a session cannot be raised again - its thread exists and its turn
    may have run - and nothing else ever retired it: the frontier leaves a
    pending session alone, and ``_revive_stalled`` raises only reservations
    that were never created. Measured by the independent check: the
    on-call's completion raised before its transaction, the dispatcher
    died, the session stayed ACTIVE, and one engineer per run kept the lane
    shut for good.

    The answer is the server's, not ours - the same observation and the
    same reconciliation as her Resume (``observe_worker_states`` over
    thread/read, then ``reconcile_desktop_runtime``). Only "terminal" and
    "absent" retire anything; "active" and "unknown" are retained, as
    Resume retains them. An active turn is not cut short: the next sweep
    asks again, and once the turn is over it is retired then (the worker's
    Stop hook observes an automatic turn but never consumes it - only the
    dispatcher did). A retired worker goes to RETRY_WAIT; a retired
    engineer frees the lane, and two lost on one ticket send that ticket to
    her (``engineer_reservation.hand_lost_engineers_to_owner``). A create
    in doubt has no thread to read; the reservation pass settles those
    (``engineer_reservation._settle_lost_creates``).

    Returns the tokens retired. Housekeeping: a failure is left in the
    journal and retires nothing.
    """

    from .engineer_reservation import dead_session_tokens

    store = StateStore(cfg.state_dir)
    try:
        dead = dead_session_tokens(store.load())
        if not dead:
            return ()
        if observe is None:
            from .lifecycle import observe_worker_states as observe
        over = {
            token: value
            for token, value in observe(cfg).items()
            if token in dead and value in {"terminal", "absent"}
        }
        if not over:
            return ()
        from .lifecycle import reconcile_desktop_runtime

        reconcile_desktop_runtime(cfg, authoritative_states=over)
    except Exception as exc:  # noqa: BLE001 - leave a trace, never break the wake-up
        from .resilience import append_resilience_event

        try:
            with ResourceLockCoordinator(store, cfg.root).transaction():
                state = store.load()
                append_resilience_event(
                    state, "dead_sessions_unsettled", at=utc_now(), detail={"error": str(exc)}
                )
                store.save(state)
        except Exception:  # noqa: BLE001 - the trace is a courtesy
            pass
        return ()
    return tuple(sorted(over))


def _revive_stalled(cfg: Config) -> tuple[int, ...]:
    """Raise reservations whose dispatcher died - each by its own causal owner."""

    from .control import _reservations_without_a_live_dispatcher, _spawn_automatic_descriptors
    from .lifecycle_base import RELAYABLE_SESSION_STATUSES

    state = StateStore(cfg.state_dir).load()
    raisable = {
        str(item.get("reservation_token"))
        for item in state.worker_sessions
        if item.get("status") in RELAYABLE_SESSION_STATUSES
        or (item.get("status") == "RELAYING" and not str(item.get("thread_id") or ""))
    }
    stalled = tuple(
        item
        for item in _reservations_without_a_live_dispatcher(cfg)
        if item.reservation_token in raisable
    )
    if not stalled:
        return ()
    return _spawn_automatic_descriptors(
        cfg, stalled, triggering_thread_id="", triggering_turn_id=""
    )


def _signal_owner_once(cfg: Config, key: str, message: str) -> None:
    """Tell her once per cause, not once per sweep: a banner every few minutes gets switched off."""

    from .blocked_runs import _tell_owner

    try:
        _record_signal(cfg, key, message)
    except Exception:  # noqa: BLE001 - telling her may never break the sweep
        return
    _tell_owner(cfg, message)


def _record_signal(cfg: Config, key: str, message: str) -> None:
    from .resilience import append_resilience_event

    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        last = next(
            (
                item
                for item in reversed(state.resilience_journal)
                if item.get("event") == "owner_signalled"
            ),
            None,
        )
        if last is not None and (last.get("detail") or {}).get("key") == key:
            raise _AlreadyTold()
        append_resilience_event(
            state, "owner_signalled", at=utc_now(), detail={"key": key, "message": message}
        )
        store.save(state)


class _AlreadyTold(Exception):
    """The same cause was already signalled; saying it again is noise."""


def _dispatcher_alive(state: Any) -> bool:
    if _pid_alive(getattr(state, "dispatcher_pid", None)):
        return True
    return any(
        item.get("automatic_dispatch_state") == "RUNNING"
        and _pid_alive(item.get("automatic_dispatch_pid"))
        for item in getattr(state, "worker_sessions", None) or []
    )


def _patch_drain_overdue(cfg: Config) -> bool:
    from .runtime_install import drain_overdue

    return drain_overdue(cfg)


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
    from .department_audit import audit_lead_sessions

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
        # R30: a lead that outlived its acceptance is found by the server's
        # own word, once per lead, whether or not anything is due - and
        # whether or not the run is still going. It ran after the DONE and
        # pause skip, and a lead is read only LEAD_AUDIT_DELAY_SECONDS after
        # it finished: the leads of the run's last ten minutes, the last
        # task's above all, were never read. A finished run has nothing left
        # to read once they are, and then no connection is opened.
        audit_lead_sessions(cfg)
        if state.status == "DONE" or StateStore(cfg.state_dir).pause_requested():
            outcome[raw] = "stopped"
            continue
        if due_wake_epoch(state, cfg) is None:
            outcome[raw] = "nothing due"
            continue
        owner = derive_owner(state)
        if owner is None:
            # Something is due and there is no causal owner to raise it on
            # behalf of. Raising it on someone else's behalf is the
            # ownership guard's to refuse, not ours to skip - so she is
            # told, once, instead of the sweep passing by in silence.
            _signal_owner_once(
                cfg,
                "no_completed_owner",
                "the run has work due but no completed turn to continue from; "
                "resume it once and it continues by itself",
            )
            outcome[raw] = "no completed owner"
            continue
        pid = ensure_wake(cfg, owner=owner[0], owner_turn=owner[1], spawn=spawn)
        outcome[raw] = f"wake {pid}"
    return outcome
