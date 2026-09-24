"""A thread outside the project is an R5 defect with a ticket - never a stop nobody reads.

What was wrong. The placement gate (``_require_thread_placement``) raised on
anything but INSIDE, for a thread of ANY kind, the on-call's included. With
an honest check (desktop_sidebar) that reads Desktop's own rule, three
ordinary situations answer OUTSIDE or UNOBSERVABLE: a Desktop build whose
rule changed, no state file, a run started below a project root. The
independent check walked the road: the dispatcher raised, cli opened
``detached_dispatch_failed``, the on-call was reserved - and its own thread
met the same gate and failed the same way. A loop: nobody could reach her.

Now placement never stops work (her requirement: the run goes on without
her; a stop is a runtime failure). A thread measured outside the project is:

- written on its session (``r5_placement_defect``) and in the run journal,
  with the separate facts R5 demands - App Server projectId, Desktop's rule
  and its reason, the Desktop version the rule was measured on;
- counted as an R5 violation (rules.record_violation);
- signalled once per cause per run as a ticket that holds no task
  (blocked_runs.stop_run, stop kind ``placement_defect``): the on-call looks
  first - a runtime defect (a wrong cwd) is its to repair; a decision that
  is hers (a Desktop project root, R6) goes up with its diagnosis, a
  recommendation, and the run's threads that are outside the project by id
  and title - she can move them in Desktop herself. A ticket per thread
  would raise an on-call per launch for one cause. Isolation is not hers to
  choose: the staged profile is the runtime's (isolation_probe), and its
  failure is a runtime defect.

Two more causes come from isolation_guard, with the same door: a thread
whose runtime roots came back wider than its workspace, and a canonical
root that changed outside the promoted manifest.
"""

from __future__ import annotations

from typing import Any, Mapping

STOP_KIND = "placement_defect"


def cause_of(after: str, observation: Mapping[str, Any], session: Mapping[str, Any]) -> str:
    rule = str(observation.get("desktop_rule") or "none")
    if session.get("placement_contract") == 1:
        return "isolation_not_proven"
    if observation.get("app_server_project_id_ok") is False:
        return "project_id_mismatch"
    return f"{after.lower()}:{rule}"


def _diagnosis(cause: str, observation: Mapping[str, Any], defect: Mapping[str, Any] | None = None) -> tuple[str, str, str]:
    """Diagnosis, recommendation and the code the on-call escalates with if it cannot repair.

    None of them hands her a fork the runtime can decide. The first version
    told her to choose between isolated tasks outside the project and a
    profile that denies writes to the root; that profile is now the
    runtime's own, decided and measured (isolation_probe).
    """

    defect = defect or {}
    reason = str(observation.get("desktop_reason") or "")
    version = observation.get("desktop_version") or "unknown"
    if cause == "isolation_not_proven":
        return (
            "Staged tasks keep their staged workspace as cwd, isolated but outside the project "
            f"in Desktop: {defect.get('placement_reason') or 'isolation of the root is not proven'}. "
            "The measurement is in .codex-autopilot/isolation-probe.json (outcome, reason, the "
            "staged profile and the Codex binary it was measured on).",
            "A runtime defect - yours to repair, not hers to choose. NOT_PROVEN because the probe "
            "could not run or the server lacked the staged profile: repair that and let the "
            "dispatcher measure again (it measures when the record does not match this root, "
            "profile and binary). ROOT_WRITABLE: the staged profile "
            "(isolation_probe.staged_profile_overrides) did not keep the root read-only on this "
            "Codex binary - repair it with devops-repair-runtime. Nothing is held meanwhile.",
            "RECOVERY_EXHAUSTED",
        )
    if cause == "runtime_roots_widened":
        return (
            f"Thread {defect.get('thread_id')} of {defect.get('task_id')} came back with runtime "
            f"roots {defect.get('observed_roots')}, wider than its workspace "
            f"{defect.get('workspace')}. Desktop rebuilds a thread's roots from its cwd and its own "
            "writable roots when she opens the thread or takes a turn in it; a turn run by "
            "Desktop's own server may not carry the task's staged profile.",
            "Nothing is held: the runtime's next turn replaced the roots with the workspace under "
            "the staged profile. After the task's promotion the canonical root is compared with "
            "the task's manifest; if a path changed outside it, that ticket names the paths.",
            "RECOVERY_EXHAUSTED",
        )
    if cause == "canonical_changed_outside_manifest":
        return (
            f"After {defect.get('task_id')} was promoted, the canonical root had changes no "
            f"promotion explains: {defect.get('paths')}. Either she edited them during the run, "
            "or a thread wrote the canonical root before its PASS "
            f"(runtime roots widened on this task's threads: {defect.get('roots_widened')}).",
            "Read the task's threads (runtime_roots_widened events) and the changed paths. A "
            "write by a thread is an isolation breach: repair the runtime with "
            "devops-repair-runtime. Her own edits need nothing. Nothing is held.",
            "PROJECT_DAMAGE_RISK",
        )
    if cause == "created_before_the_honest_check":
        return (
            "Threads of this run were created with their staged workspace as cwd before the "
            "placement check read Desktop's own rule; they were recorded INSIDE by projectId "
            "alone, and Desktop files them in no project.",
            "Nothing to repair in the runtime: new threads are filed at the project root. App "
            "Server has no measured call that moves a saved thread, and Desktop's state is its "
            "own. She can move the listed threads into the project in Desktop.",
            "GLOBAL_CONFIG_CHANGE",
        )
    if cause.startswith("unobservable"):
        return (
            f"Desktop's placement could not be read: {reason}. Desktop {version}.",
            "Check the Codex home the App Server reported and that the saved Desktop project "
            "exists; nothing in her Desktop state is written by the runtime.",
            "GLOBAL_CONFIG_CHANGE",
        )
    return (
        f"Desktop files the thread outside the project: {reason} (rule "
        f"{observation.get('desktop_rule')}, projectId ok: "
        f"{observation.get('app_server_project_id_ok')}, Desktop {version}).",
        "If the thread's cwd is not the project root, that is a runtime defect to repair. If "
        "the root is not one of the project's rootPaths, adding it is her decision (R6). "
        "The listed threads can be moved into the project in Desktop by her.",
        "GLOBAL_CONFIG_CHANGE",
    )


def outside_threads(cfg: Any, state: Any, codex_home: Any) -> list[dict[str, str]]:
    """The run's threads Desktop files outside the project, by its rule: id, title, task.

    Measured again from each session's recorded cwd, not read from its
    recorded placement: every thread created before the honest check was
    recorded INSIDE by the projectId alone - the five M01 threads of the
    beyondness run among them.
    """

    from .desktop_sidebar import INSIDE, read_desktop_state, sidebar_placement

    desktop, why = read_desktop_state(codex_home)
    seen: dict[str, dict[str, str]] = {}
    for session in getattr(state, "worker_sessions", None) or ():
        thread_id = str(session.get("thread_id") or "")
        if not thread_id or thread_id in seen or not session.get("actual_cwd"):
            continue
        placed = sidebar_placement(
            thread_id, session.get("actual_cwd"), cfg.desktop.desktop_project_id, desktop, unreadable=why
        )
        if placed.placement != INSIDE:
            seen[thread_id] = {
                "thread_id": thread_id,
                "title": str(session.get("actual_thread_name") or (session.get("descriptor") or {}).get("title") or ""),
                "task_id": str(session.get("task_id") or ""),
                "placement": placed.placement,
                "reason": placed.reason,
            }
    return list(seen.values())


def _file_once(cfg: Any, state: Any, *, cause: str, task_id: str, kind: str, after: str,
               observation: Mapping[str, Any], at: str, defect: Mapping[str, Any],
               key: str = "", list_outside: bool = True) -> str | None:
    """One ticket per ``key`` (the cause, unless given) per run; the run's outside threads in it.

    The key is part of the ticket's signal id (blocked_runs.stop_run
    ``signal_key``): two causes of one run are two tickets, each with its
    own diagnosis, even while the other is open.
    """

    from .blocked_runs import stop_run
    from .pipeline_engineer import PipelineIncidentStore

    key = key or cause
    filed = [
        item for item in PipelineIncidentStore(cfg.state_dir).load().get("incidents") or ()
        if (item.get("system_state") or {}).get("stop_kind") == STOP_KIND
        and ((item.get("system_state") or {}).get("signal_key") or (item.get("system_state") or {}).get("cause")) == key
        and str((item.get("system_state") or {}).get("run_id") or "") == str(state.run_id or "")
    ]
    if filed:
        return None
    diagnosis, recommendation, code = _diagnosis(cause, observation, defect)
    return stop_run(
        cfg,
        state,
        stop_kind=STOP_KIND,
        phase="PLACEMENT_DEFECT",
        reason=f"R5: {task_id} {kind} thread is {after} ({cause})",
        summary=f"R5: a {kind} thread of {task_id} is not in the project in Desktop, or not isolated. Nothing is held: the run goes on.",
        at=at,
        task_ids=(),
        context_task_id=task_id,
        signal_key=key,
        system_state={
            "held": False,
            "cause": cause,
            "signal_key": key,
            "reason_code": code,
            "diagnosis": diagnosis,
            "recommendation": recommendation,
            "defect": dict(defect),
            "outside_threads": outside_threads(cfg, state, observation.get("codex_home")) if list_outside else [],
        },
    )


def record_placement_defect(
    cfg: Any,
    reservation_token: str,
    *,
    after: str,
    observation: Mapping[str, Any],
    at: str,
) -> str | None:
    """Record the defect of this thread; file its cause's ticket once. Returns a new ticket id."""

    from .lifecycle_base import _append_event, _session_by_token
    from .resources import ResourceLockCoordinator
    from .rules import record_violation
    from .run_state import StateStore

    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        cause = cause_of(after, observation, session)
        task_id = str(session.get("task_id") or "")
        defect = {
            "rule": "R5",
            "placement": after,
            "cause": cause,
            "app_server_project_id_ok": observation.get("app_server_project_id_ok"),
            "desktop_rule": observation.get("desktop_rule"),
            "desktop_reason": observation.get("desktop_reason"),
            "desktop_version": observation.get("desktop_version"),
            "cwd": observation.get("cwd"),
            "placement_reason": session.get("placement_reason"),
            "at": at,
        }
        session["r5_placement_defect"] = defect
        _append_event(state, "r5_placement_defect", session, at, detail=f"{after}; {cause}")
        record_violation(
            cfg.state_dir, "R5",
            detail=f"{task_id} {session.get('kind')} thread {session.get('thread_id')}: {after} ({cause})",
        )
        incident_id = _file_once(
            cfg, state, cause=cause, task_id=task_id, kind=str(session.get("kind") or ""),
            after=after, observation=observation, at=at, defect=defect,
        )
        store.save(state)
    return incident_id


def signal_earlier_outside_threads(cfg: Any, reservation_token: str, *, observation: Mapping[str, Any], at: str) -> str | None:
    """Threads created outside the project before the honest check: one ticket per run.

    They were recorded INSIDE by projectId alone, so nothing else would ever
    name them. Moving a finished thread is not the runtime's to do: App
    Server has no measured call that moves a saved thread's cwd, and
    Desktop's state file is its own (desktop_sidebar). The ticket lists them
    by id and title; she can move them in Desktop.
    """

    from .lifecycle_base import _session_by_token
    from .resources import ResourceLockCoordinator
    from .run_state import StateStore

    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        earlier = [
            item for item in outside_threads(cfg, state, observation.get("codex_home"))
            if not any(
                session.get("thread_id") == item["thread_id"] and session.get("placement_contract")
                for session in state.worker_sessions
            )
        ]
        if not earlier:
            return None
        session = _session_by_token(state, reservation_token)
        incident_id = _file_once(
            cfg, state, cause="created_before_the_honest_check", task_id=str(session.get("task_id") or ""),
            kind=str(session.get("kind") or ""), after="OUTSIDE", observation=observation, at=at,
            defect={"rule": "R5", "threads": earlier},
        )
        store.save(state)
    return incident_id
