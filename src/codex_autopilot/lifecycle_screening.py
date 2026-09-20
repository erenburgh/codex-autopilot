"""The screening session: hiring a worker before that worker exists.

A worker's prompt - and with it the skill stack - is frozen into the launch
descriptor inside the reservation transaction, which holds a lock, loads the
plan and asks no model anything.  A step that reasons about the task cannot
run there.  So screening runs one reservation earlier, as a session of its
own, the way the on-call Pipeline Engineer does: reserved by the frontier,
created through App Server like every other thread, completed through the
ordinary completion path.  Its answer lands in run state, and the next
frontier pass builds the worker prompt from it.

The screener takes no worker slot, no resource lock and no task state.  It
is not production work, and a screener waiting on the lock its own worker
will need would deadlock the frontier it was reserved from.

Failure here is asymmetric on purpose.  The predictable part of Autopilot -
a task is created, handed over, runs to DONE - is the core; skills are
superstructure.  So a skill that cannot be resolved never reaches a prompt,
and a screening that cannot answer never stops the task: after a bounded
number of attempts the task is reserved unscreened and the run records that
it ran unscreened, and why.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .lifecycle_base import (
    PENDING_SESSION_STATUSES,
    LaunchDescriptor,
    _append_event,
    _session_kind,
    _stable_id,
    task_checkpoint,
)
from .memory import ProjectMemory
from .plan import Plan
from .resources import ResourceLockCoordinator
from .run_state import RunState, StateStore, utc_now
from .scope import scope_baseline
from .skill_packs import SkillPackError
from .hired_skills import (
    HiredSkillError,
    admit_skill_bundle,
    installed_skill_bundles,
)
from .skill_screening import (
    BUNDLE_ORIGIN_LOCAL,
    BUNDLE_ORIGIN_MARKET,
    SKILL_LIBRARY_DIRNAME,
    HiringDecision,
    SkillRequisition,
    apply_admitted_bundles,
    ScreeningProtocolError,
    SkillLibraryError,
    parse_screening_result,
    record_hiring,
    recorded_hiring,
    resolve_requisition,
)


# How many screening turns one task contract gets. Two: the first attempt,
# and one more after a protocol refusal whose reason the screener can read.
# A third would spend the user's limits on a session that has already failed
# the same contract twice, and the task can run without it.
MAX_SCREENING_ATTEMPTS = 2

SCREENING_ACTIONS = frozenset({"proceed", "wait", "reserve"})


@dataclass(frozen=True, slots=True)
class ScreeningGate:
    """What the frontier should do with a task that wants a worker."""

    action: str
    descriptor: LaunchDescriptor | None = None


def screening_gate(
    cfg: Config,
    plan: Plan,
    state: RunState,
    *,
    task_id: str,
    memory_audit_before: int,
    relay_owner_thread_id: str,
    build_descriptor: Any,
) -> ScreeningGate:
    """Decide whether this task may be reserved now, and hire it if not.

    ``build_descriptor`` is passed in rather than imported: the reservation
    module already owns descriptor assembly, and importing it here would
    close a cycle with the frontier that calls this.
    """

    if not screening_applies(cfg, plan):
        return ScreeningGate("proceed")
    if recorded_hiring(
        state.task_hiring, task_id=task_id, graph_version=state.graph_version
    ) is not None:
        return ScreeningGate("proceed")
    # Pending is asked across every graph version, attempts only within the
    # current one. A replan rewrites task contracts under the same ids, so an
    # in-flight screener belongs to the contract it was briefed on - but it is
    # still a live Codex thread for this task, and a gate that could not see
    # it hired a second screener beside it. Waiting is self-healing: that
    # screener's own completion notices the drift, fails with it on record,
    # and the frontier pass it triggers screens the new contract afresh.
    if any(
        item.get("status") in PENDING_SESSION_STATUSES
        for item in _screening_sessions(state, task_id, any_graph_version=True)
    ):
        return ScreeningGate("wait")
    sessions = _screening_sessions(state, task_id)
    if len(sessions) >= MAX_SCREENING_ATTEMPTS:
        record_hiring(
            state.task_hiring,
            task_id=task_id,
            graph_version=state.graph_version,
            decision=HiringDecision(task_id=task_id),
            requisition=None,
            screened_by={"attempts": len(sessions)},
            at=utc_now(),
            unscreened=(
                f"screening did not produce a readable requisition in "
                f"{len(sessions)} attempts; the task runs with no skills"
            ),
        )
        _append_event(
            state,
            "screening_exhausted",
            _synthetic_session(task_id),
            utc_now(),
            detail=f"{task_id}: {len(sessions)} attempts",
        )
        return ScreeningGate("proceed")
    return ScreeningGate(
        "reserve",
        _reserve_screening_in_state(
            cfg,
            plan,
            state,
            task_id=task_id,
            memory_audit_before=memory_audit_before,
            relay_owner_thread_id=relay_owner_thread_id,
            build_descriptor=build_descriptor,
        ),
    )


def screening_applies(cfg: Config, plan: Plan) -> bool:
    """Whether this run screens its tasks at all.

    Under "auto" a run with nothing to hire from is not screened: with an
    empty catalog a screening turn can only answer "nothing available", and
    that answer costs one Codex thread per task out of the user's limits.
    A project that wants its unmet needs recorded from the first run - the
    intake for qualifying new skills - sets "always".
    """

    mode = getattr(cfg.runtime, "skill_screening", "never")
    if mode == "never":
        return False
    if mode == "always":
        return True
    return bool(plan.skill_packs) or any(
        (cfg.state_dir / SKILL_LIBRARY_DIRNAME).glob("*.json")
    )


def complete_screening_session(
    cfg: Config,
    *,
    session: dict[str, Any],
    thread_id: str,
    turn_id: str,
    final_message: str,
    at: str | None,
    now_epoch: int | None = None,
    dispatcher_authorized: bool = False,
) -> Any:
    """Record the hire, then let the frontier reserve the task it hired for.

    A requisition that cannot be read is a refusal of this session, not of
    the task: the session is marked FAILED, and the next frontier pass either
    screens once more or gives up on screening and runs the task unscreened.
    """

    from .lifecycle_base import CompletionOutcome
    from .lifecycle_completion import _active_session_by_thread
    from .lifecycle_reservations import _reserve_in_state
    from .plan import load_plan

    timestamp = at or utc_now()
    task_id = str(session.get("task_id") or "")
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        current = _active_session_by_thread(state, thread_id)
        if current is None or current.get("reservation_token") != session.get(
            "reservation_token"
        ):
            return CompletionOutcome(False, None, (), state.status == "DONE")
        plan = load_plan(cfg.state_dir, cfg.profile)
        current["turn_id"] = turn_id
        current["completed_at"] = timestamp
        failure = _record_requisition(
            cfg,
            plan,
            state,
            session=current,
            task_id=task_id,
            final_message=final_message,
            thread_id=thread_id,
            turn_id=turn_id,
            at=timestamp,
        )
        # BLOCKED, not a status of its own: the session is over either way,
        # and fence_superseded_sessions treats anything non-terminal with a
        # thread as still able to produce. A bespoke "FAILED" was retired to
        # RETIRED_SUPERSEDED the moment the task it screened for got its
        # worker, and the journal then said the screening was superseded
        # when in fact it had failed - which is the reason the task runs
        # unscreened.
        current["status"] = "BLOCKED" if failure else "COMPLETED"
        current["final_status"] = "BLOCKED" if failure else "ROTATE"
        if failure:
            current["failure_reason"] = failure
        # The causality barrier reads this event for every kind of session.
        # The engineer once wrote only its own completion event and no
        # successor was ever raised; the screener must not repeat that.
        _append_event(state, "turn_completed", current, timestamp, detail=current["final_status"])
        _append_event(
            state,
            "screening_failed" if failure else "screening_completed",
            current,
            timestamp,
            detail=failure or task_id,
        )
        descriptors = _reserve_in_state(
            cfg,
            plan,
            state,
            memory_audit_before=ProjectMemory(cfg.root).audit_highwater(),
            relay_owner_thread_id=thread_id,
            now_epoch=now_epoch,
        )
        if dispatcher_authorized:
            current["automatic_successor_tokens"] = [
                item.reservation_token for item in descriptors
            ]
            current["automatic_dispatch_state"] = (
                "ADVANCING" if descriptors else "COMPLETED"
            )
        store.save(state)
    return CompletionOutcome(True, str(current["final_status"]), descriptors, False)


def _record_requisition(
    cfg: Config,
    plan: Plan,
    state: RunState,
    *,
    session: dict[str, Any],
    task_id: str,
    final_message: str,
    thread_id: str,
    turn_id: str,
    at: str,
) -> str:
    """Parse, resolve and store one hire.  Returns "" or the refusal."""

    from .ai_studio import AIStudioRuntime

    graph_version = int(session.get("graph_version") or state.graph_version)
    if graph_version != state.graph_version:
        # The graph moved while this screener was thinking. The task it was
        # briefed on no longer exists under that contract, so its answer is
        # about vanished work.
        return (
            f"the plan advanced to graph version {state.graph_version} while "
            f"task {task_id} was being screened for version {graph_version}"
        )
    try:
        requisition = parse_screening_result(final_message, task_id=task_id)
    except ScreeningProtocolError as exc:
        return str(exc)
    runtime = AIStudioRuntime(
        plan, cfg.root, language=cfg.language, skill_path=cfg.skill_path
    )
    try:
        decision = resolve_requisition(
            requisition,
            runtime._skill_catalog(),
            qualification_evidence_store=runtime.memory,
        )
    except (SkillLibraryError, SkillPackError) as exc:
        # The catalog itself is broken, which is not the screener's fault and
        # not something a second screening turn could fix.
        return f"the skill catalog cannot be read: {exc}"
    admitted, refused = _admit_bundles(cfg, requisition, task_id=task_id)
    decision = apply_admitted_bundles(decision, admitted, refused)
    record_hiring(
        state.task_hiring,
        task_id=task_id,
        graph_version=graph_version,
        decision=decision,
        requisition=requisition,
        screened_by={
            "thread_id": thread_id,
            "turn_id": turn_id,
            "reservation_token": str(session.get("reservation_token") or ""),
        },
        at=at,
    )
    return ""


def _admit_bundles(
    cfg: Config, requisition: SkillRequisition, *, task_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Admit every bundle the screener staged, refusing loudly, not silently.

    Returns what was admitted and, separately, why anything was not: a
    refused bundle must leave its requisition item unmet with the refusal on
    record, never a claim that a skill is there when it is not.

    Nothing here can write outside the project. ``admit_skill_bundle``
    resolves every destination under ``.codex-autopilot/hired-skills`` and
    can name no other path, so the Codex plugin cache, the Codex skills
    directory, hooks and MCP configuration are unreachable from this call.
    """

    admitted: dict[str, dict[str, Any]] = {}
    refused: dict[str, str] = {}
    installed, unreadable = installed_skill_bundles()
    by_name = {entry["name"]: entry for entry in installed}
    for item in requisition.items:
        if item.installed:
            entry = by_name.get(item.installed)
            if entry is None:
                # Lead with the real cause. A skill that is present but
                # unreadable was reported as "not installed", which sends
                # somebody looking for a skill that is sitting right there -
                # the same defect as a refusal naming a missing SKILL.md in
                # a directory that carries one.
                blocked = [
                    line for line in unreadable if line.startswith(f"{item.installed} ")
                ]
                if blocked:
                    refused[item.capability] = (
                        f"{item.installed}: installed but could not be read - "
                        + "; ".join(blocked)
                    )
                    continue
                present = ", ".join(sorted(by_name)) or "nothing is installed"
                refused[item.capability] = (
                    f"{item.installed}: not installed in this machine's Codex "
                    f"skills directory; present: {present}"
                    + (f". unreadable: {'; '.join(unreadable)}" if unreadable else "")
                )
                continue
            # Provenance comes from the read, not from the requisition: this
            # record is local because THIS code found it in her Codex home.
            admitted[item.capability] = {
                "origin": BUNDLE_ORIGIN_LOCAL,
                "name": entry["name"],
                "path": entry["path"],
                "digest": entry["digest"],
                "note": (
                    "already installed on this machine; the market was not "
                    "consulted for this capability"
                ),
            }
            continue
        if item.bundle is None:
            continue
        try:
            record = admit_skill_bundle(
                cfg.state_dir,
                staged_path=cfg.state_dir / item.bundle.staged_path,
                name=item.bundle.name,
                provider=item.bundle.provider,
                locator=item.bundle.locator,
            )
        except HiredSkillError as exc:
            refused[item.capability] = f"{item.bundle.name}: {exc}"
            continue
        admitted[item.capability] = {
            "origin": BUNDLE_ORIGIN_MARKET,
            "id": record["id"],
            "name": record["name"],
            "provider": record["provider"],
            "path": record["path"],
            "digest": record["digest"],
        }
    return admitted, refused


def _reserve_screening_in_state(
    cfg: Config,
    plan: Plan,
    state: RunState,
    *,
    task_id: str,
    memory_audit_before: int,
    relay_owner_thread_id: str,
    build_descriptor: Any,
) -> LaunchDescriptor:
    worker_sequence = state.worker_sequence + 1
    attempt = len(_screening_sessions(state, task_id)) + 1
    token = _stable_id(
        state,
        f"reservation:{task_id}:screening:{state.graph_version}:{worker_sequence}",
    )
    state.worker_sequence = worker_sequence
    operation_id = _stable_id(state, f"operation:{token}")
    client_id = f"autopilot-{_stable_id(state, f'client:{token}')[:24]}"
    descriptor = build_descriptor(
        cfg,
        plan,
        state,
        task_id=task_id,
        kind="screening",
        attempt=attempt,
        token=token,
        operation_id=operation_id,
        client_id=client_id,
    )
    session: dict[str, Any] = {
        "reservation_token": token,
        # No resource lock: the screener reads the project and writes
        # nothing the task is about, and holding the task's resources would
        # block the very worker it is hiring.
        "resource_ownership_token": None,
        "operation_id": operation_id,
        "client_user_message_id": client_id,
        "task_id": task_id,
        "kind": "screening",
        "attempt": attempt,
        "worker_sequence": worker_sequence,
        "graph_version": state.graph_version,
        "status": "CREATE_REQUESTED",
        "thread_id": None,
        "turn_id": None,
        "host_id": None,
        "relay_owner_thread_id": relay_owner_thread_id,
        "created_at": descriptor.created_at,
        "memory_audit_before": memory_audit_before,
        "checkpoint_before": task_checkpoint(cfg.state_dir, task_id),
        "scope_baseline": scope_baseline(Path(descriptor.cwd)),
        "descriptor": descriptor.to_dict(),
    }
    state.worker_sessions.append(session)
    _append_event(state, "screening_reserved", session, descriptor.created_at, detail=task_id)
    return descriptor


def _screening_sessions(
    state: RunState, task_id: str, *, any_graph_version: bool = False
) -> tuple[dict[str, Any], ...]:
    """Screening sessions for this task, by default only the current graph."""

    return tuple(
        item
        for item in state.worker_sessions
        if _session_kind(item) == "screening"
        and item.get("task_id") == task_id
        and (
            any_graph_version
            or int(item.get("graph_version") or 0) == state.graph_version
        )
    )


def _synthetic_session(task_id: str) -> dict[str, Any]:
    """A journal-shaped record for an event that belongs to no session."""

    return {
        "operation_id": f"screening-exhausted:{task_id}",
        "task_id": task_id,
        "attempt": MAX_SCREENING_ATTEMPTS,
        "reservation_token": f"screening-exhausted:{task_id}",
    }
