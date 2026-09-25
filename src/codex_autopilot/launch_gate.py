"""The launch gate: confirm that the task came up, do not announce it.

The failure this was written for looked like this: the session answered
"dispatcher started, pid such-and-such", ended, and nothing happened. The
message was a statement of intent, not an observation of a result: waiting
for the dispatcher confirmed only that its process reached some phase, and
said nothing about the task itself. That function (`wait_for_dispatcher`)
was removed in 0.8.1 together with the headless path; the measurement this
gate was written for did not go stale with it.

Here the whole chain is checked from the run's records, without calling
App Server: the reservation, the bound thread, creation in the project, the
acknowledged send, the visible launch report, a live dispatcher and the
absence of a failure after launch.

A check with no data is marked unchecked and is NOT counted as passed.
"Could not confirm" and "confirmed" are different things, and passing the
second off as the first is the very defect.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import Config
from .run_state import RunState, StateStore

# Events written only by a step of the live path that actually happened.
CREATED_EVENTS = frozenset(
    {
        "app_server_thread_created",
        "app_server_project_scoped_create",
        "create_acknowledged",
    }
)
ACKNOWLEDGED_EVENTS = frozenset({"start_acknowledged", "wait_registered"})
REPORT_EVENTS = frozenset({"visible_launch_report_ready"})

# Where Desktop keeps its own interface records. Success on the App Server
# side does not change them, so visibility is checked only here.
DESKTOP_UI_KEYS = ("thread-project-assignments", "sidebar-project-thread-orders")
FAILURE_EVENTS = frozenset(
    {
        "create_failed",
        "start_failed",
        "prep_failed",
        "interrupt_observed",
        "retry_scheduled",
        "slot_history_rejected",
    }
)

# M11-R5: how long to wait for the placement measurement before treating
# it as never made. Placement is measured right after creation, in the same
# dispatcher pass, so the margin here is deliberately large: the deadline
# is not for the normal course but for the case when nobody is left to
# measure.
PLACEMENT_MEASUREMENT_DEADLINE_SECONDS = 180.0

ACTIVE_STATUS = "ACTIVE"

__all__ = [
    "ABSENT",
    "INSIDE",
    "OUTSIDE",
    "desktop_placement",
    "render_launch_timeline",
    "LaunchCheck",
    "LaunchVerdict",
    "launch_verdict",
    "await_launch",
    "launch_checklist",
    "launch_confirmed",
    "render_launch_checklist",
]


class LaunchVerdict(str, Enum):
    """Three launch states, not two.

    Creating a thread through App Server takes tens of seconds, and the
    hook lives for thirty. While steps are missing but the dispatcher is
    alive and no failure is recorded, that is IN PROGRESS, not BROKEN.
    Confusing the two turns a normal launch into a false ticket - the very
    noise that makes checks go unread.
    """

    CONFIRMED = "CONFIRMED"
    IN_PROGRESS = "IN_PROGRESS"
    FAILED = "FAILED"


# Items whose absence means breakage, not incompleteness.
DECISIVE_CHECKS = frozenset({"reserved", "dispatcher_alive", "no_failure_after_launch"})

# M11-R5. Items that fail the verdict only on an explicit False, not on
# "nothing to check with". Placement is exactly that: there is a window
# between thread creation and the measurement record, and a refusal on the
# unmeasured state would breed false tickets - which is exactly why the
# item was made non-deciding altogether. But a measured OUTSIDE or ABSENT
# is a result, not a window, and it used to change nothing: the verdict
# held at IN_PROGRESS, no ticket opened. An expired unmeasured state turns
# into False separately, by deadline.
DECISIVE_ON_FAILURE = frozenset({"visible_in_desktop"})


@dataclass(frozen=True, slots=True)
class LaunchCheck:
    """One checklist item.

    ``passed=None`` means there was nothing to check with. That is not a pass.
    """

    id: str
    task_id: str
    passed: bool | None
    detail: str
    # False: a failure already carried by its own ticket - shown, and it
    # does not make a second one (an R5 placement defect, placement_defects).
    decisive: bool = True

    @property
    def mark(self) -> str:
        if self.passed is True:
            return "+"
        if self.passed is False:
            return "-"
        return "?"


def launch_checklist(
    cfg: Config,
    state: RunState,
    *,
    task_ids: Sequence[str],
    pid_alive: Callable[[Any], bool] | None = None,
    now: Callable[[], float] | None = None,
) -> tuple[LaunchCheck, ...]:
    """Check from the run's records that the named tasks really came up.

    ``now`` returns epoch time and is needed only for the placement
    measurement deadline; it is separate from the monotonic waiting clock
    because it is compared with the thread creation stamp, which is wall
    clock.
    """

    alive = pid_alive or _pid_alive
    checks: list[LaunchCheck] = []
    for task_id in task_ids:
        session = _latest_session(state, task_id)
        if session is None:
            checks.append(
                LaunchCheck(
                    "reserved",
                    task_id,
                    False,
                    "no reservation found: the task was never taken up",
                )
            )
            continue
        checks.append(LaunchCheck("reserved", task_id, True, "reservation present"))

        thread_id = str(session.get("thread_id") or "")
        checks.append(
            LaunchCheck(
                "thread_bound",
                task_id,
                bool(thread_id),
                f"thread {thread_id}" if thread_id else "no thread bound",
            )
        )

        token = str(session.get("reservation_token") or "")
        events = _events_for(state, token)
        checks.append(
            _event_check(
                "created_in_project", task_id, events, CREATED_EVENTS,
                ok="thread created through App Server in the run's project",
                bad="no record of the thread being created",
            )
        )
        # The send is confirmed by a durable record, not the momentary
        # status. A turn that completed before the check moves the status
        # past ACTIVE - and a fast worker was declared never launched.
        acknowledged = session.get("status") == ACTIVE_STATUS or any(
            str(item.get("event") or "") in ACKNOWLEDGED_EVENTS for item in events
        )
        checks.append(
            LaunchCheck(
                "send_acknowledged",
                task_id,
                acknowledged,
                f"session status {session.get('status')!r}"
                + ("" if acknowledged else "; the send was not acknowledged"),
            )
        )
        checks.append(
            _event_check(
                "launch_report_written", task_id, events, REPORT_EVENTS,
                ok="the runtime reached the launch report",
                bad="the runtime did not reach the launch report",
            )
        )
        checks.append(
            _desktop_visibility(
                task_id,
                thread_id,
                session,
                now=now,
                required=cfg.runtime.required_thread_placement,
            )
        )

        pid = session.get("automatic_dispatch_pid")
        if pid is None:
            pid = state.dispatcher_pid
        # A completed turn needs no live dispatcher: it exits normally when
        # the work is done. A fast worker used to get False here and a
        # launch_not_confirmed ticket - while the same checklist said "turn
        # completed".
        finished = any(str(item.get("event") or "") == "turn_completed" for item in events)
        checks.append(
            LaunchCheck(
                "dispatcher_alive",
                task_id,
                True if finished else (alive(pid) if pid is not None else None),
                "turn completed, the dispatcher is no longer needed"
                if finished
                else (f"dispatcher pid {pid}" if pid is not None else "dispatcher pid not recorded"),
            )
        )

        failure = _failure_after_launch(events)
        checks.append(
            LaunchCheck(
                "no_failure_after_launch",
                task_id,
                failure is None,
                "no failures after launch"
                if failure is None
                else f"a failure was recorded after launch: {failure}",
            )
        )
    return tuple(checks)


def launch_verdict(checks: Iterable[LaunchCheck]) -> LaunchVerdict:
    """Confirmed, still in progress or failed - by the presence of signs of breakage."""

    items = list(checks)
    if not items:
        return LaunchVerdict.FAILED
    if all(item.passed is True or (item.passed is False and not item.decisive) for item in items):
        return LaunchVerdict.CONFIRMED
    broken = [
        item
        for item in items
        if (item.id in DECISIVE_CHECKS and item.passed is not True)
        or (item.id in DECISIVE_ON_FAILURE and item.passed is False and item.decisive)
    ]
    return LaunchVerdict.FAILED if broken else LaunchVerdict.IN_PROGRESS


def launch_confirmed(checks: Iterable[LaunchCheck]) -> bool:
    """The launch is confirmed only if every item passed.

    An unchecked item is not a confirmation. A failed item that is not
    decisive - a failure a ticket of its own already carries - is shown and
    does not unconfirm the launch it is not about.
    """

    return launch_verdict(checks) is LaunchVerdict.CONFIRMED


def render_launch_checklist(checks: Sequence[LaunchCheck]) -> str:
    if not checks:
        return "Launch checklist: nothing to check — no task was named."
    lines: list[str] = []
    for task_id in dict.fromkeys(item.task_id for item in checks):
        lines.append(f"{task_id}:")
        for item in checks:
            if item.task_id == task_id:
                lines.append(f"  [{item.mark}] {item.id}: {item.detail}")
    verdict = {
        LaunchVerdict.CONFIRMED: "LAUNCH CONFIRMED",
        LaunchVerdict.IN_PROGRESS: (
            "LAUNCH IN PROGRESS — dispatcher alive, no failures, some steps still ahead"
        ),
        LaunchVerdict.FAILED: "LAUNCH FAILED — this is a failure, not a success",
    }[launch_verdict(checks)]
    return verdict + "\n" + "\n".join(lines)


# Launch steps in human language. The order comes from the journal, not
# from here: the journal is the real sequence.
TIMELINE_STEPS = {
    "reservation_created": "slot reserved",
    "create_requested": "thread creation requested",
    "app_server_create_claimed": "creation started",
    "app_server_thread_created": "thread created",
    "app_server_project_scoped_create": "created in the project's space",
    "prep_completed": "working directory prepared",
    "automatic_turn_claimed": "turn claimed",
    "start_acknowledged": "work started",
    "turn_completed": "turn completed",
    "implementation_completed": "implementation completed",
    "verification_started": "verification started",
    "verification_passed": "verification passed",
}

TIMELINE_FAILURES = {
    "create_failed": "thread creation failed",
    "start_failed": "start failed",
    "prep_failed": "directory preparation failed",
    "interrupt_observed": "work interrupted",
    "retry_scheduled": "retry scheduled",
    "scope_violation_recorded": "outside the declared scope",
    "rule_declaration_missing": "the report lists no applied rules",
    "scope_not_observed": "the scope could not be checked",
}


def render_launch_timeline(state: RunState, task_ids: Sequence[str]) -> str:
    """A timeline of launch steps with repairs, not a snapshot of the end state.

    A snapshot omits what matters most: it cannot show whether a repair was
    needed. The placement gate may see ABSENT, move the thread into the
    project and see INSIDE - in a snapshot that is one tick, and it looks as
    if everything went by itself.
    """

    lines: list[str] = []
    for task_id in task_ids:
        session = _latest_session(state, task_id)
        if session is None:
            lines.append(f"{task_id}:")
            lines.append("  [✗] slot not reserved — the task was never taken up")
            continue
        lines.append(f"{task_id}:")
        for event in _current_attempt(
            _events_for(state, str(session.get("reservation_token") or ""))
        ):
            lines.extend(_timeline_line(event))
    return "\n".join(lines)


def _current_attempt(
    events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep one - the last - record of every step.

    A reservation outlives several attempts, and its journal accumulates
    them all. In the timeline that looked like a contradiction: "the move
    did not help" from the first attempt stood next to "placement confirmed"
    from the second, and where the task was now could not be read. We show
    the current state, not the history: the latest observation per step.
    """

    latest: dict[str, Mapping[str, Any]] = {}
    for event in events:
        latest[str(event.get("event") or "")] = event
    return sorted(latest.values(), key=lambda item: int(item.get("sequence") or 0))


def _timeline_line(event: Mapping[str, Any]) -> list[str]:
    name = str(event.get("event") or "")
    detail = str(event.get("detail") or "")
    if name == "desktop_placement_verified":
        return _placement_lines(detail)
    if name in TIMELINE_FAILURES:
        text = TIMELINE_FAILURES[name]
        return [f"  [✗] {text}" + (f" — {detail[:90]}" if detail else "")]
    if name in TIMELINE_STEPS:
        suffix = ""
        if name == "app_server_thread_created" and detail:
            suffix = f" — {detail[:40]}"
        return [f"  [✓] {TIMELINE_STEPS[name]}{suffix}"]
    return []


def _placement_lines(detail: str) -> list[str]:
    """Placement in Desktop: show both the check and the repair."""

    before, _, after = detail.partition(" -> ")
    before, after = before.strip(), after.strip()
    names = {
        INSIDE: "in the project",
        OUTSIDE: "visible, but outside the project",
        ABSENT: "unknown to Desktop",
    }
    if before == after == INSIDE:
        return ["  [✓] placement in the project confirmed"]
    lines = [f"  [✗] placement: {names.get(before, before)}"]
    if after == INSIDE:
        lines.append("  [→] moved into the project")
        lines.append("  [✓] placement in the project confirmed")
    else:
        lines.append(f"  [✗] the move did not help: {names.get(after, after)}")
    return lines


def await_launch(
    cfg: Config,
    *,
    task_ids: Sequence[str],
    timeout: float = 20.0,
    interval: float = 0.25,
    pid_alive: Callable[[Any], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> tuple[LaunchCheck, ...]:
    """Wait for launch confirmation or the deadline, and return the checklist as is.

    A time limit is mandatory: the hook lives for 30 seconds, and the gate
    may not hang longer. An expired deadline is a negative result, returned
    as an honest checklist rather than an exception.
    """

    rest = sleep or time.sleep
    now = monotonic or time.monotonic
    store = StateStore(cfg.state_dir)
    deadline = now() + timeout
    checks = launch_checklist(cfg, store.load(), task_ids=task_ids, pid_alive=pid_alive)
    while launch_verdict(checks) is LaunchVerdict.IN_PROGRESS and now() < deadline:
        rest(interval)
        checks = launch_checklist(
            cfg, store.load(), task_ids=task_ids, pid_alive=pid_alive
        )
    return checks


def _desktop_visibility(
    task_id: str,
    thread_id: str,
    session: Mapping[str, Any],
    *,
    now: Callable[[], float] | None = None,
    required: str = "in_project",
) -> LaunchCheck:
    """Is the thread visible, per the measurement made at placement.

    The creation path does the measuring: it holds a live server connection,
    and asking placement anew on every timeline poll would spin up an
    app-server once a second. Here the recorded result is read.

    The item is not deciding while the measurement may not exist yet: there
    is a window between thread creation and the placement record, and a
    refusal on it would breed false tickets.

    M11-R5. But an unmeasured state is not eternal. If the thread was created
    long ago and placement was never recorded, nobody is left to measure -
    the dispatcher died between creation and the gate. This case used to
    stay undecided forever: the verdict held at IN_PROGRESS, no ticket
    opened, the task simply did not move. Now an expired deadline is a
    negative result, and it goes into one normalized ticket alongside
    OUTSIDE and ABSENT.
    """

    if not thread_id:
        return LaunchCheck(
            "visible_in_desktop", task_id, None, "nothing to look for: no thread bound"
        )
    if required == "any":
        return LaunchCheck(
            "visible_in_desktop", task_id, None, "placement not required by the config"
        )
    placement = str(session.get("desktop_placement") or "")
    defect = session.get("r5_placement_defect")
    if placement in {OUTSIDE, UNOBSERVABLE} and isinstance(defect, Mapping) and required != "visible":
        # The thread was created and works; that it is not in the project is
        # an R5 defect with its own ticket (placement_defects). A launch
        # ticket on top would raise a second on-call for the same cause - the
        # on-call whose own thread may measure the same way.
        return LaunchCheck(
            "visible_in_desktop", task_id, False,
            f"R5 defect recorded: {placement} ({defect.get('cause')}); signalled, the run goes on",
            decisive=False,
        )
    if placement == INSIDE:
        return LaunchCheck(
            "visible_in_desktop", task_id, True, "thread in the project and visible in the sidebar"
        )
    if placement == OUTSIDE:
        # With required="visible", outside the project is still a visible
        # thread, and the placement gate lets it through. Declaring it a
        # failure here would open a ticket for what the config allowed.
        if required == "visible":
            return LaunchCheck(
                "visible_in_desktop",
                task_id,
                True,
                "thread visible; outside the project, which the config allows",
            )
        return LaunchCheck(
            "visible_in_desktop", task_id, False, "the server knows the thread, but it is outside the project"
        )
    if placement == ABSENT:
        return LaunchCheck(
            "visible_in_desktop", task_id, False, "the server does not know the thread: it was not persisted"
        )
    waited = _seconds_since_create(session, now=now)
    if waited is not None and waited > PLACEMENT_MEASUREMENT_DEADLINE_SECONDS:
        return LaunchCheck(
            "visible_in_desktop",
            task_id,
            False,
            f"placement not measured {int(waited)} s after the thread was created: "
            "nobody is left to measure it",
        )
    return LaunchCheck(
        "visible_in_desktop", task_id, None, "placement not measured yet"
    )


def _seconds_since_create(
    session: Mapping[str, Any], *, now: Callable[[], float] | None = None
) -> float | None:
    """How long since the thread's creation was confirmed, or None.

    None means "nothing to count the deadline from", not "the deadline has
    not expired": without a timestamp no expiry can be declared, and the two
    must not be swapped here - that is exactly the substitution the whole
    module was written against.
    """

    raw = session.get("create_acknowledged_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        created = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    current = (
        datetime.fromtimestamp(now(), tz=timezone.utc)
        if now is not None
        else datetime.now(timezone.utc)
    )
    return (current - created).total_seconds()


def _latest_session(state: RunState, task_id: str) -> Mapping[str, Any] | None:
    matches = [
        item for item in state.worker_sessions if str(item.get("task_id") or "") == task_id
    ]
    return matches[-1] if matches else None


def _events_for(state: RunState, token: str) -> list[Mapping[str, Any]]:
    if not token:
        return []
    return sorted(
        (
            item
            for item in state.lifecycle_journal
            if str(item.get("reservation_token") or "") == token
        ),
        key=lambda item: int(item.get("sequence") or 0),
    )


def _event_check(
    check_id: str,
    task_id: str,
    events: Sequence[Mapping[str, Any]],
    wanted: frozenset[str],
    *,
    ok: str,
    bad: str,
) -> LaunchCheck:
    found = next((item for item in events if str(item.get("event") or "") in wanted), None)
    if found is None:
        return LaunchCheck(check_id, task_id, False, bad)
    return LaunchCheck(check_id, task_id, True, f"{ok} (#{found.get('sequence')})")


def _failure_after_launch(events: Sequence[Mapping[str, Any]]) -> str | None:
    """A failure recorded after the thread had been created."""

    launched_at = next(
        (
            int(item.get("sequence") or 0)
            for item in events
            if str(item.get("event") or "") in CREATED_EVENTS
        ),
        None,
    )
    if launched_at is None:
        return None
    for item in events:
        if int(item.get("sequence") or 0) <= launched_at:
            continue
        name = str(item.get("event") or "")
        if name in FAILURE_EVENTS:
            return f"{name} (#{item.get('sequence')})"
    return None


def _pid_alive(pid: Any) -> bool:
    from .control import pid_alive as control_pid_alive

    return control_pid_alive(pid)


# Where the thread is according to the server itself. Desktop draws the
# sidebar from its list: measured on threads a person sees with their own
# eyes - the app's records in .codex-global-state.json say nothing of them,
# and the server knows them.
ABSENT = "ABSENT"      # the server does not know the thread: it was not saved
OUTSIDE = "OUTSIDE"    # the server knows it, but Desktop files it outside the project
INSIDE = "INSIDE"      # projectId matches AND Desktop files it in the project
UNOBSERVABLE = "UNOBSERVABLE"  # Desktop's placement could not be read - never INSIDE


def desktop_placement(
    thread_id: str,
    *,
    project_id: str | None = None,
    desktop_project_id: str | None = None,
    client: Any = None,
    binary: str = "codex",
    log_path: Path | None = None,
) -> str:
    """Ask where the thread is: App Server's projectId and Desktop's own rule.

    The old check read the keys of .codex-global-state.json. Measured: three
    threads a person saw in the project sidebar lived only in
    electron-persisted-atom-state and were absent from
    thread-project-assignments entirely - that check called them OUTSIDE.
    The check after it asked only App Server for projectId and called a
    thread INSIDE when it matched - and every staged worker of the
    art run, projectId set and cwd a subfolder of the root, was
    INSIDE by it and invisible in the project. Both halves are asked now
    (``measure_placement``).

    A thread with not one turn is not persisted on the server: four probes
    created empty vanished from thread/list completely. So ABSENT means not
    "invisible" but "no longer there".
    """

    if not thread_id:
        return ABSENT
    if client is not None:
        return measure_placement(client, thread_id, project_id, desktop_project_id)[0]

    from .appserver import AppServerClient

    destination = log_path or Path(tempfile.gettempdir()) / "codex-autopilot-placement.jsonl"
    try:
        with AppServerClient(binary, destination) as fresh:
            return measure_placement(fresh, thread_id, project_id, desktop_project_id)[0]
    except Exception:
        return ABSENT


def measure_placement(
    client: Any, thread_id: str, project_id: str | None, desktop_project_id: str | None
) -> tuple[str, dict[str, Any]]:
    """One thread/read: the placement and the observation R5 keeps separate.

    INSIDE only when BOTH hold: App Server's projectId is the configured one
    and Desktop's own rule files the thread in the Desktop project
    (desktop_sidebar). R5: the status tells "projectId set" from "visible
    in the project" and never passes the first off as the second - the
    observation carries them as two fields, with Desktop's rule, its reason,
    the thread's cwd and the Desktop version the rule was measured on.

    Editability is observed, not gated on (M11-R5). Measured on a live
    server: ``canAcceptDirectInput`` arrives null both in ``thread/read`` of
    an unloaded thread and in all thirty rows of ``thread/list`` - it does
    not tell "cannot be edited" from "nobody holds it". ``status.type ==
    "notLoaded"`` is the state in which nobody holds the thread. (This was
    ``placement_observation``, which read the thread a second time.)
    """

    from .desktop_sidebar import (
        INSIDE as SIDEBAR_INSIDE,
        MEASURED_ON,
        UNOBSERVABLE as SIDEBAR_UNOBSERVABLE,
        codex_home_of,
        desktop_version,
        observe,
    )

    try:
        thread = client.read_thread(thread_id)
    except Exception as exc:
        # A vanished thread answers "thread not found"; the connection may
        # also simply have dropped, but either way we have no placement.
        return ABSENT, {"observed": False, "reason": str(exc)}
    if not thread:
        return ABSENT, {"observed": False, "reason": "thread/read returned nothing"}
    assigned = str(thread.get("projectId") or "")
    # None: no App Server project is configured, so there is no such fact to require.
    project_ok = (assigned == str(project_id)) if project_id else None
    home = codex_home_of(client)
    sidebar = observe(thread_id, thread.get("cwd"), desktop_project_id, home)
    status = thread.get("status")
    observation = {
        "observed": True,
        "can_accept_direct_input": thread.get("canAcceptDirectInput"),
        "status_type": str(status.get("type")) if isinstance(status, Mapping) else None,
        "originator": thread.get("originator"),
        "thread_source": thread.get("threadSource"),
        "cwd": thread.get("cwd"),
        "app_server_project_id": assigned or None,
        "app_server_project_id_ok": project_ok,
        "desktop_rule": sidebar.rule,
        "desktop_reason": sidebar.reason,
        "desktop_placement": sidebar.placement,
        "desktop_version": desktop_version(),
        "desktop_rule_measured_on": MEASURED_ON,
        "codex_home": str(home) if home else None,
    }
    if project_ok is False:
        return OUTSIDE, observation
    if sidebar.placement == SIDEBAR_INSIDE:
        return INSIDE, observation
    if sidebar.placement == SIDEBAR_UNOBSERVABLE:
        return UNOBSERVABLE, observation
    return OUTSIDE, observation
