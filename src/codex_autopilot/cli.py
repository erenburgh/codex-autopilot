from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from . import __version__
from .appserver import AppServerClient
from .bootstrap import initialize_project, purge_project_state
from .config import DESKTOP_OWNED_SURFACE, STATE_DIR_NAME, load_config
from .control import arm, find_project_root, handle_interrupt_hook, handle_post_tool_hook, handle_prompt_hook, handle_stop_hook, pid_alive, reactivate_desktop_relay_owner, recreate_archived_desktop_retry, spawn_automatic_app_server_relay, status_text
from .hook_trust import HookPreflightError, HookTrustApprovalRequired
from .lifecycle import (
    adopt_automatic_dispatcher_successor,
    causal_predecessor,
    complete_desktop_worker,
    pause_desktop_run,
    record_automatic_app_server_exit,
    record_desktop_failure,
    reconcile_desktop_thread_identity,
    relay_session_status,
    run_automatic_app_server_turn,
)
from .language import DEFAULT_LANGUAGE, normalize_language
from .models import MODEL_IDS, PUBLIC_REASONING
from .plan import validate_migrating_plan
from .preflight import PreflightApprovalRequired, PreflightError, ProjectMemoryApprovalRequired, run_preflight
from .run_state import StateStore
# The gateway's refusal is raised right here, so it is imported here: a
# NameError instead of a refusal once cost a run an hour.
from .runtime_repair import RuntimeRepairError


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="codex-autopilot")
    top.add_argument("--version", action="version", version=__version__)
    # A machine entry point is hidden by having NO help, not by SUPPRESS:
    # argparse lists a subparser whenever `help` is present, so passing the
    # sentinel printed "==SUPPRESS==" sixteen times in `--help`, while the
    # six commands a person actually runs - which passed no help at all -
    # were the ones left out. The metavar keeps the whole internal
    # vocabulary out of the usage line too.
    sub = top.add_subparsers(dest="command", required=True, metavar="command")
    bootstrap = sub.add_parser("bootstrap", help="initialize a project from a structured plan")
    bootstrap.add_argument("--project", type=Path, default=Path.cwd())
    bootstrap.add_argument("--plan-file", type=Path, required=True)
    bootstrap.add_argument("--profile", choices=["adaptive", "host-settings"])
    bootstrap.add_argument("--skill-path", type=Path)
    bootstrap.add_argument("--replace", action="store_true")
    bootstrap.add_argument("--language", default=DEFAULT_LANGUAGE)
    bootstrap.add_argument("--app-server-project-id")
    bootstrap.add_argument("--desktop-project-id")
    start_skill = sub.add_parser("start-skill")
    start_skill.add_argument("--project", type=Path, default=Path.cwd())
    start_skill.add_argument("--plan-file", type=Path, required=True)
    start_skill.add_argument("--replace", action="store_true")
    start_skill.add_argument("--language", default=DEFAULT_LANGUAGE)
    start_skill.add_argument("--app-server-project-id")
    start_skill.add_argument("--desktop-project-id")
    start_skill.add_argument("--approve-project-memory-always", action="store_true")
    preflight = sub.add_parser("preflight", help="validate a target before creating Autopilot state")
    preflight.add_argument("--project", type=Path, default=Path.cwd())
    preflight.add_argument("--plan-file", type=Path, required=True)
    preflight.add_argument("--profile", choices=["adaptive", "host-settings"])
    preflight.add_argument("--skill-path", type=Path)
    preflight.add_argument("--approve-project-memory-always", action="store_true")
    preflight.add_argument("--language", default=DEFAULT_LANGUAGE)
    preflight.add_argument("--app-server-project-id")
    preflight.add_argument("--desktop-project-id")
    armed = sub.add_parser("arm")
    armed.add_argument("--project", type=Path, default=Path.cwd())
    automatic_relay = sub.add_parser("_relay_dispatch")
    automatic_relay.add_argument("--project", type=Path, required=True)
    automatic_relay.add_argument("--token", required=True)
    automatic_relay.add_argument("--initiator-thread", required=True)
    automatic_relay.add_argument("--initiator-turn", required=True)
    wake = sub.add_parser("_wake")
    wake.add_argument("--project", type=Path, required=True)
    wake.add_argument("--at", type=int, required=True)
    wake.add_argument("--owner", required=True)
    wake.add_argument("--owner-turn", required=True)
    sub.add_parser("_wake-sweep")
    recreate_archived = sub.add_parser("recreate-archived-retry")
    recreate_archived.add_argument("--project", type=Path, required=True)
    recreate_archived.add_argument("--reservation-token", required=True)
    recreate_archived.add_argument("--archived-thread-id", required=True)
    recreate_archived.add_argument("--predecessor-thread-id", required=True)
    timeline = sub.add_parser(
        "timeline",
        help="ladder of launch steps for the active tasks, one line per step",
    )
    timeline.add_argument("--project", type=Path, default=Path.cwd())
    timeline.add_argument("--task", action="append", default=[])
    relay_status = sub.add_parser("relay-status")
    relay_status.add_argument("--project", type=Path, default=Path.cwd())
    relay_status.add_argument("--token", required=True)
    relay_fail = sub.add_parser("relay-fail")
    relay_fail.add_argument("--project", type=Path, default=Path.cwd())
    relay_fail.add_argument("--token", required=True)
    relay_fail.add_argument("--reason", required=True)
    # The caller names the kind of failure: R23 counts repeats by it.
    relay_fail.add_argument("--failure-code", required=True)
    relay_fail.add_argument("--definitive", action="store_true")
    relay_fail.add_argument("--rate-limited", action="store_true")
    relay_fail.add_argument("--reset-at", type=int)
    relay_complete = sub.add_parser("relay-complete")
    relay_complete.add_argument("--project", type=Path, default=Path.cwd())
    relay_complete.add_argument("--thread-id", required=True)
    relay_complete.add_argument("--turn-id", required=True)
    relay_complete.add_argument("--status", choices=["ROTATE", "DONE", "BLOCKED", "ESCALATE"], required=True)
    reconcile_identity = sub.add_parser("reconcile-thread-identity")
    reconcile_identity.add_argument("--project", type=Path, default=Path.cwd())
    reconcile_identity.add_argument("--token", required=True)
    reconcile_identity.add_argument("--task-id", required=True)
    reconcile_identity.add_argument("--previous-thread-id", required=True)
    reconcile_identity.add_argument("--current-thread-id", required=True)
    relay_rearm = sub.add_parser("devops-rearm-relay-owner")
    relay_rearm.add_argument("--project", type=Path, default=Path.cwd())
    relay_rearm.add_argument("--incident-id")
    # A runtime code repair. The edit set and the test are passed as files,
    # not strings: an edit can span lines and modules, and through
    # command-line arguments it would arrive mangled by quotes and newlines.
    devops_repair = sub.add_parser("devops-repair-runtime")
    devops_repair.add_argument("--project", type=Path, default=Path.cwd())
    devops_repair.add_argument("--incident-id", required=True)
    devops_repair.add_argument("--patch-file", type=Path, required=True)
    devops_repair.add_argument("--test-file", type=Path, required=True)
    devops_repair.add_argument("--test-name", required=True)
    devops_revert = sub.add_parser("devops-revert-runtime-patch")
    devops_revert.add_argument("--project", type=Path, default=Path.cwd())
    devops_revert.add_argument("--patch-id", required=True)
    # A hired skill bundle is a side effect on the project, so it has a named
    # reversal. Listing is separate from removing: nobody should have to guess
    # an id to find out what is installed.
    skills_list = sub.add_parser(
        "skills", help="list the skill bundles this project has hired"
    )
    skills_list.add_argument("--project", type=Path, default=Path.cwd())
    revoke_skill = sub.add_parser(
        "revoke-skill", help="remove one hired skill bundle from this project"
    )
    revoke_skill.add_argument("--project", type=Path, default=Path.cwd())
    revoke_skill.add_argument("--skill-id", required=True)
    devops_resolve = sub.add_parser("devops-resolve-incident")
    devops_resolve.add_argument("--project", type=Path, default=Path.cwd())
    devops_resolve.add_argument("--incident-id", required=True)
    devops_resolve.add_argument("--healthcheck-name", required=True)
    devops_resolve.add_argument("--check", action="append", required=True)
    # What repaired it - mandatory, and a vocabulary identifier. Measured on
    # the v1.0 run: the flag was optional, and all sixteen repairs were
    # recorded with no action or as prose, so a signature with eighteen
    # repeats learned not one procedure. The vocabulary is not duplicated
    # here: the ledger checks the names, and that is where they live.
    devops_resolve.add_argument("--action", action="append", required=True)
    devops_resolve.add_argument(
        "--note",
        default="",
        help="circumstances in prose: they explain the repair and change nothing",
    )
    # A task stopped by a rule violation is lifted only by a human and only
    # with a recorded reason. There used to be no way to lift it at all:
    # "resume" refuses on BLOCKED for any reason but an escalation, rightly
    # - but no way back existed, and the run stood forever.
    unblock = sub.add_parser(
        "unblock",
        help="lift a task's stop by the user's decision, with a recorded reason",
    )
    unblock.add_argument("--project", type=Path, default=Path.cwd())
    unblock.add_argument("--task", required=True)
    unblock.add_argument("--reason", required=True)
    authorize_root = sub.add_parser(
        "authorize-project-root",
        help="authorize Autopilot to add this project's canonical root to the saved Codex project",
    )
    authorize_root.add_argument("--project", type=Path, default=Path.cwd())
    authorize_root.add_argument("--yes", action="store_true")
    authorize_root.add_argument("--revoke", action="store_true")
    for name, summary in (
        ("status", "what the run is doing right now, as one short card"),
        ("stop", "pause after the turns in flight finish; nothing is killed"),
        ("resume", "continue a paused run"),
        ("logs", "the run's own journal, newest last"),
    ):
        item = sub.add_parser(name, help=summary)
        item.add_argument("--project", type=Path, default=Path.cwd())
    doctor = sub.add_parser("doctor", help="check this machine can run Autopilot, and say what is missing")
    doctor.add_argument("--project", type=Path, default=Path.cwd())
    hook = sub.add_parser("hook")
    uninstall = sub.add_parser("uninstall", help="remove this installation; project state is set aside, never deleted")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--project", type=Path)
    uninstall.add_argument("--purge-project-state", action="store_true")
    sub.add_parser("memory-mcp")
    return top


def _profile_and_skill(args) -> tuple[str, Path]:
    profile = getattr(args, "profile", None) or os.environ.get("CODEX_AUTOPILOT_PROFILE") or "adaptive"
    skill_arg = getattr(args, "skill_path", None)
    skill = skill_arg or (Path(os.environ["CODEX_AUTOPILOT_SKILL_PATH"]) if os.environ.get("CODEX_AUTOPILOT_SKILL_PATH") else None)
    if skill is None and os.environ.get("CODEX_AUTOPILOT_INSTALL_ROOT"):
        plugin = f"codex-autopilot-{profile}"
        skill = Path(os.environ["CODEX_AUTOPILOT_INSTALL_ROOT"]) / "current" / "plugins" / plugin / "skills" / plugin / "SKILL.md"
    if profile not in {"adaptive", "host-settings"} or skill is None:
        raise ValueError("profile and installed SKILL.md path are required")
    return profile, skill


def _relay_executor_thread_id() -> str:
    thread_id = str(os.environ.get("CODEX_THREAD_ID") or "").strip()
    if not thread_id:
        raise RuntimeError(
            "Desktop relay requires the current CODEX_THREAD_ID; refusing an unowned mutation"
        )
    return thread_id


def _record_detached_dispatch_failure(cfg, token: str, error: BaseException) -> None:
    """A detached dispatcher's failure must be visible, not buried in a file.

    A crash of this process used to go only to
    logs/automatic-relay-<token>.log: no run journal record, no incident, no
    message to the user. The run looked alive meanwhile - status RUNNING,
    task active - and stood silently.
    """

    from .pipeline_engineer import (
        IncidentClass,
        IncidentPhase,
        IncidentSignal,
        PipelineIncidentError,
        PipelineIncidentStore,
        SideEffectOutcome,
    )
    from .run_state import utc_now

    now = utc_now()
    summary = f"{type(error).__name__}: {error}"
    try:
        state_store = StateStore(cfg.state_dir)
        state = state_store.load()
        session = next(
            (
                item
                for item in state.worker_sessions
                if item.get("reservation_token") == token
            ),
            None,
        )
        task_id = str((session or {}).get("task_id") or "")
    except Exception:  # the state is unreadable - the report must still go out
        task_id = ""

    try:
        store = PipelineIncidentStore(cfg.state_dir)
        incident = store.open_incident(
            IncidentSignal(
                signal_id=f"detached-dispatch-failed:{token}",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                summary=summary[:2000],
                affected_task_ids=(task_id,) if task_id else (),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
                system_state={"reservation_token": token},
            ),
            at=now,
        )
        incident_id = str(incident["incident_id"])
        phase = store.route_incident(incident_id, at=now)
        if phase is IncidentPhase.DEGRADED:
            phase = store.attempt_known_recovery(
                incident_id, at=now, owner_id="detached-dispatch"
            )
        if phase is IncidentPhase.AUTO_RECOVERY_FAILED:
            store.ensure_pipeline_engineer(incident_id, at=now)
        print(f"codex-autopilot: ticket {incident_id} opened for the dispatcher failure")
    except PipelineIncidentError as incident_error:
        print(f"codex-autopilot: the ticket could not be opened: {incident_error}")



def _ensure_wake(cfg, *, owner: str, owner_turn: str) -> None:
    """The wake-up is a courtesy, not a contract: its failure does not fail the turn."""

    from .wake import ensure_wake

    try:
        ensure_wake(cfg, owner=owner, owner_turn=owner_turn)
    except Exception as exc:  # noqa: BLE001 - the alarm may not bring down the dispatcher
        print(f"wake scheduling failed: {exc}", flush=True)


def _run_automatic_relay_dispatch(
    cfg,
    *,
    token: str,
    owner: str,
    owner_turn: str,
) -> int:
    """Run the v0.7-style local loop with one App Server process per task."""

    # The loop moves from task to task, reassigning its token. Outside, the
    # original one remained, and a failure on a later task was attributed to
    # the first: measured, ticket incident-78e67b38680498f1 for the M2
    # failure named M1, and the "on failure" ladder printed the steps of the
    # already completed M1. The engineer would have been sent to repair a
    # verified milestone. The cursor is shared: it always points at the task
    # the loop drives now.
    cursor = _RelayCursor(token)
    try:
        return _automatic_relay_loop(
            cfg, token=token, owner=owner, owner_turn=owner_turn, cursor=cursor
        )
    except BaseException as error:
        _print_relay_timeline(cfg, cursor.token, "on failure")
        _record_detached_dispatch_failure(cfg, cursor.token, error)
        raise


def _record_rate_limits(cfg, method: str, params: dict) -> None:
    """Remember the rate-limit snapshot so capacity is computed from fresh data.

    The scheduler narrows the number of workers by window usage. Without
    this record it would see only what was there at the start of the run,
    and keep ten going while the window runs out.
    """

    if method != "account/rateLimits/updated":
        return
    snapshot = (params or {}).get("rateLimits")
    if not isinstance(snapshot, dict):
        return
    try:
        StateStore(cfg.state_dir).record_rate_limits(snapshot)
    except Exception:
        # Capacity is an optimization, not a contract: updating it must never
        # fail the turn that is running at that moment.
        return


def _print_relay_timeline(cfg, token: str, headline: str) -> None:
    """Print the step ladder from the dispatcher itself, not on request.

    The dispatcher is the only one who knows what happens while the work
    runs. It used to stay silent to the end, and the only way to learn the
    progress was to ask.
    """

    from .launch_gate import render_launch_timeline

    try:
        state = StateStore(cfg.state_dir).load()
        session = next(
            (
                item
                for item in state.worker_sessions
                if item.get("reservation_token") == token
            ),
            None,
        )
        task_id = str((session or {}).get("task_id") or "")
        if not task_id:
            return
        print(f"\n=== {headline} ===")
        print(render_launch_timeline(state, [task_id]), flush=True)
    except Exception as error:  # the report may not fail the work
        print(f"codex-autopilot: the timeline is unavailable: {error}", flush=True)


@dataclass
class _RelayCursor:
    """The task the loop is driving right now."""

    token: str


def _sweep_finished_traces(cfg, directory: Path) -> None:
    """Bound the wire traces, and never stand between a run and its start."""

    from .config import DEFAULT_LOG_RETENTION_MB
    from .log_retention import sweep_logs

    budget_mb = getattr(
        getattr(cfg, "runtime", None), "log_retention_mb", DEFAULT_LOG_RETENTION_MB
    )
    try:
        budget_bytes = int(budget_mb) * 1024 * 1024
    except (TypeError, ValueError):
        return
    try:
        removed, freed = sweep_logs(directory, budget_bytes=budget_bytes)
    except Exception as exc:  # noqa: BLE001 - janitorial work, never fatal
        print(f"Logs: the sweep was skipped ({exc})")
        return
    if removed:
        print(
            f"Logs: removed {removed} finished trace(s), freed "
            f"{freed // (1024 * 1024)} MB "
            f"(runtime.log_retention_mb = {budget_mb})"
        )


def _automatic_relay_loop(
    cfg,
    *,
    token: str,
    owner: str,
    owner_turn: str,
    cursor: "_RelayCursor | None" = None,
) -> int:
    cursor = cursor or _RelayCursor(token)
    while True:
        cursor.token = token
        _print_relay_timeline(cfg, token, "before launching the task")
        dispatcher_log = (
            cfg.state_dir / "logs" / f"app-server-dispatcher-{token}.jsonl"
        )
        # Before this dispatcher starts writing its own trace, take the
        # finished ones out. Nothing else ever removed them, and a long run
        # left gigabytes of protocol beside a journal of a few megabytes.
        # The sweep is by whole files and never touches one written in the
        # last hour, so the trace about to be opened here is safe by age.
        #
        # Housekeeping must never be the reason a run does not start. Every
        # failure here - an unreadable directory, a config without the key,
        # a permission error mid-sweep - skips the sweep and says so. The
        # worst outcome of that is a full disk, which is the state this
        # whole feature was written to improve, not a dispatcher that dies
        # before its first turn.
        _sweep_finished_traces(cfg, dispatcher_log.parent)
        client = AppServerClient(
            cfg.desktop.binary,
            dispatcher_log,
            # Limits arrive by themselves, as events, during the work.
            # Capacity used to be computed from what preflight read at the
            # start: a 24-task run could eat the window and not learn of it
            # until the next launch.
            event_sink=lambda method, params: _record_rate_limits(cfg, method, params),
        )
        with client:
            outcome = run_automatic_app_server_turn(
                cfg,
                token,
                initiator_thread_id=owner,
                initiator_turn_id=owner_turn,
                connected_client=client,
            )
        _print_relay_timeline(cfg, token, "after the task's turn")
        proc = client.proc
        if proc is None or proc.poll() is None:
            raise RuntimeError("per-task App Server process did not fully exit")
        record_automatic_app_server_exit(
            cfg,
            token,
            dispatcher_pid=os.getpid(),
        )
        if not outcome.descriptors:
            # On its way out the dispatcher leaves a wake-up: otherwise
            # nobody raises a due retry, and the run waits for a human word.
            _ensure_wake(cfg, owner=owner, owner_turn=owner_turn)
            return 0
        if len(outcome.descriptors) == 1:
            successor = outcome.descriptors[0]
            owner, owner_turn = adopt_automatic_dispatcher_successor(
                cfg,
                completed_reservation_token=token,
                successor_reservation_token=successor.reservation_token,
            )
            token = successor.reservation_token
            continue
        for descriptor in outcome.descriptors:
            state = StateStore(cfg.state_dir).load()
            session = next(
                item
                for item in state.worker_sessions
                if item.get("reservation_token") == descriptor.reservation_token
            )
            relay_owner = str(session.get("relay_owner_thread_id") or "")
            # The same predicate as the single branch above and the Stop
            # hook. There used to be a selection of its own here by
            # status == "COMPLETED": an owner that ended its turn with
            # BLOCKED/ESCALATE never gets the COMPLETED status
            # (lifecycle_completion keeps worker_status) while turn_completed
            # is written - and the fan-out over its successors failed with a
            # bare StopIteration. Measured on 18 Sep by going through all
            # five candidates for "the turn is over": the drift was here
            # alone, the others answer different questions.
            predecessor = causal_predecessor(state, relay_owner)
            if predecessor is None:
                raise RuntimeError("automatic relay has no completed causal predecessor")
            spawn_automatic_app_server_relay(
                cfg.root,
                reservation_token=descriptor.reservation_token,
                initiator_thread_id=relay_owner,
                initiator_turn_id=str(predecessor["turn_id"]),
            )
        _ensure_wake(cfg, owner=owner, owner_turn=owner_turn)
        return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {"bootstrap", "start-skill", "preflight"}:
            profile, skill = _profile_and_skill(args)
            language = normalize_language(args.language)
            raw = json.loads(args.plan_file.read_text(encoding="utf-8"))
            # The same question bootstrap asks: is there a run being
            # migrated. A v0.8 plan is admitted only as its migration, and
            # that must be checked here too - or preflight would accept what
            # bootstrap then rejects.
            checked_plan = validate_migrating_plan(
                raw,
                profile,
                state_dir=args.project.resolve() / STATE_DIR_NAME,
            )
            preflight_result = run_preflight(
                args.project,
                plan=checked_plan,
                profile=profile,
                skill_path=skill,
                replace=getattr(args, "replace", False),
                approve_project_memory_always=getattr(args, "approve_project_memory_always", False),
                app_server_project_id=getattr(args, "app_server_project_id", None),
                desktop_project_id=getattr(args, "desktop_project_id", None),
            )
            if args.command == "preflight":
                return 0
            print("Starting Autopilot..." if args.command == "start-skill" else "Initializing Autopilot project...")
            plan = initialize_project(
                args.project,
                args.plan_file,
                profile=profile,
                skill_path=skill,
                replace=args.replace,
                language=language,
                project_id=preflight_result.project_id,
                desktop_project_id=getattr(args, "desktop_project_id", None),
                plan_verification=preflight_result.plan_verification,
            )
            if args.command == "start-skill":
                state = StateStore(args.project.resolve() / STATE_DIR_NAME).load()
                if state.status != "DONE":
                    arm(args.project)
            state = StateStore(args.project.resolve() / STATE_DIR_NAME).load()
            armed_text = (
                " Desktop reservation launch armed for this turn's Stop hook; the complete "
                "scheduler-selected task chain inherits the run authorization."
                if args.command == "start-skill" and state.status != "DONE"
                else ""
            )
            done_text = " Existing verified milestones already complete this plan." if state.status == "DONE" else ""
            print(f"Initialized {len(plan.milestones)} milestones ({profile}).{armed_text}{done_text}")
            return 0
        if args.command == "arm":
            arm(args.project)
            print("Desktop reservation armed for this turn's Stop hook; the causal task performs the fixed relay.")
            return 0
        if args.command == "resume":
            # The only surface is desktop_owned, and launching in it belongs
            # to the trusted Stop hook. The command stays as a pointer: a
            # silently missing resume would send people searching.
            raise RuntimeError(
                "resume is hook-owned: send the exact phrase "
                "'Resume Codex Autopilot.' in a Codex task opened on this "
                "project, so its trusted Stop hook launches the dispatcher"
            )
        if args.command == "_wake":
            from .wake import run_wake

            return run_wake(
                load_config(args.project),
                at_epoch=args.at,
                owner=args.owner,
                owner_turn=args.owner_turn,
            )
        if args.command == "_wake-sweep":
            # The launchd agent: a sweep of the known projects. It launches
            # nothing itself - it only arms a wake-up where a due retry waits
            # and no live wake-up exists.
            from .wake import sweep

            for root, decision in sweep().items():
                print(f"{root}: {decision}", flush=True)
            return 0
        if args.command == "_relay_dispatch":
            cfg = load_config(args.project)
            return _run_automatic_relay_dispatch(
                cfg,
                token=args.token,
                owner=args.initiator_thread,
                owner_turn=args.initiator_turn,
            )
        if args.command == "unblock":
            # Running this command is the human's decision: it does not
            # check whether they are right, it records what they decided.
            # Without a reason it does nothing - a record without a reason
            # is no better than a silent lift.
            from .plan import load_plan
            from .run_state import utc_now
            from .task_state import TaskState, transition_task

            cfg = load_config(args.project)
            store = StateStore(cfg.state_dir)
            state = store.load()
            plan = load_plan(cfg.state_dir, cfg.profile)
            task_id = str(args.task).strip()
            if task_id not in plan.task_map:
                raise SystemExit(f"the plan has no task {task_id!r}")
            if state.task_states.get(task_id) != TaskState.BLOCKED.value:
                raise SystemExit(
                    f"{task_id} is not stopped: it is now "
                    f"{state.task_states.get(task_id)}"
                )
            reason = str(args.reason).strip()
            if not reason:
                raise SystemExit("a reason is required: --reason")
            state.task_states = transition_task(
                plan, state.task_states, task_id, TaskState.READY
            )
            state.user_unblocks.append(
                {"task_id": task_id, "reason": reason, "at": utc_now()}
            )
            if state.status == "BLOCKED":
                state.status = "READY"
                state.phase = "PREPARING"
                state.last_error = None
            store.save(state)
            print(
                f"{task_id}: the stop was lifted by the user's decision — {reason}\n"
                "Continue the run with the phrase «Resume Codex Autopilot.» in a Codex task."
            )
            return 0
        if args.command == "devops-repair-runtime":
            # The engineer edits the runtime's code - but the gateway accepts
            # the edit, not the engineer. The requirements are those of every
            # recovery command: the owning thread and its own ticket.
            from .pipeline_engineer import PipelineIncidentStore
            from .run_state import utc_now
            from .runtime_repair import Edit, apply_runtime_patch

            _relay_executor_thread_id()
            cfg = load_config(args.project)
            timestamp = utc_now()
            # The ticket is checked before the edit, not after: otherwise a
            # foreign or closed ticket would leave the edit applied in the
            # live installation and recorded nowhere - non-existent for next
            # time.
            incidents = PipelineIncidentStore(cfg.state_dir)
            incidents.require_engineer_incident(args.incident_id)
            raw = json.loads(args.patch_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("edits"), list):
                raise RuntimeRepairError(
                    'the patch file is an object with an "edits" array: '
                    '{"edits": [{"module": "<file.py>", "old_file": "<path>", '
                    '"new_file": "<path>"}]}; omit old_file to add a new module'
                )
            edits = tuple(
                Edit(
                    module=str(item["module"]),
                    new=Path(str(item["new_file"])).read_text(encoding="utf-8"),
                    old=(
                        None
                        if not item.get("old_file")
                        else Path(str(item["old_file"])).read_text(encoding="utf-8")
                    ),
                )
                for item in raw["edits"]
            )
            record = apply_runtime_patch(
                edits=edits,
                test_name=args.test_name,
                test_source=args.test_file.read_text(encoding="utf-8"),
                at=timestamp,
            )
            incidents.record_runtime_patch(args.incident_id, patch=record.to_dict(), at=timestamp)
            print(json.dumps(record.to_dict(), ensure_ascii=False))
            return 0
        if args.command == "skills":
            from .hired_skills import hired_skill_records

            records = hired_skill_records(
                Path(args.project).expanduser().resolve() / STATE_DIR_NAME
            )
            print(json.dumps([dict(item) for item in records], ensure_ascii=False))
            return 0
        if args.command == "revoke-skill":
            from .hired_skills import HiredSkillError, revoke_hired_skill

            try:
                removed = revoke_hired_skill(
                    Path(args.project).expanduser().resolve() / STATE_DIR_NAME,
                    args.skill_id,
                )
            except HiredSkillError as exc:
                print(str(exc))
                return 2
            print(json.dumps(dict(removed), ensure_ascii=False))
            return 0
        if args.command == "devops-revert-runtime-patch":
            from .runtime_repair import revert_runtime_patch

            _relay_executor_thread_id()
            record = revert_runtime_patch(args.patch_id)
            print(json.dumps(record.to_dict(), ensure_ascii=False))
            return 0
        if args.command == "devops-resolve-incident":
            # The on-call engineer closes its own ticket, but only with a
            # passing healthcheck: closing without one is a claim, not an
            # observation. The mutation requires the owning thread, like the
            # other recovery commands.
            from .pipeline_engineer import HealthcheckResult, PipelineIncidentStore
            from .run_state import utc_now

            _relay_executor_thread_id()
            cfg = load_config(args.project)
            healthcheck = HealthcheckResult(
                name=args.healthcheck_name,
                passed=True,
                observed_at=utc_now(),
                checks=tuple(args.check),
            )
            phase = PipelineIncidentStore(cfg.state_dir).complete_pipeline_engineer(
                args.incident_id,
                success=True,
                at=utc_now(),
                healthcheck=healthcheck,
                actions=tuple(args.action),
                note=args.note,
            )
            print(json.dumps({"incident_id": args.incident_id, "phase": phase.value}, ensure_ascii=False))
            return 0
        if args.command == "authorize-project-root":
            # R6: the only entry that permits editing the saved project's
            # roots. The permission is stored as an accepted user decision
            # and names a specific project and a specific root - it does not
            # carry over to another project.
            from .memory import ProjectMemory
            from .project_association import project_root_authorization_statement

            cfg = load_config(args.project)
            if not cfg.desktop.project_id:
                print(
                    "This project has no configured App Server project; there is nothing to authorize.",
                    file=sys.stderr,
                )
                return 2
            statement = project_root_authorization_statement(
                cfg.desktop.project_id, cfg.root
            )
            memory = ProjectMemory(cfg.root)
            existing = memory.accepted_user_decision(statement)
            if args.revoke:
                if existing is None:
                    print(json.dumps({"authorized": False, "changed": False}, ensure_ascii=False))
                    return 0
                memory.set_decision_status(
                    str(existing["id"]),
                    "superseded",
                    actor="user",
                    reason="Authorization withdrawn by the user",
                )
                print(json.dumps({"authorized": False, "changed": True}, ensure_ascii=False))
                return 0
            if existing is not None:
                print(json.dumps({"authorized": True, "changed": False, "decision_id": existing["id"]}, ensure_ascii=False))
                return 0
            if not args.yes:
                print(
                    "Autopilot would add\n"
                    f"  {cfg.root}\n"
                    f"to the saved Codex project {cfg.desktop.project_id}.\n"
                    "This changes your Codex project, not only this run. "
                    "Re-run with --yes to authorize it, or --revoke to withdraw it later.",
                    file=sys.stderr,
                )
                return 2
            record = memory.propose_decision(
                statement=statement,
                origin="user",
                created_by="user",
                status="accepted",
                reason="Explicit user authorization for saved-project root mutation",
            )
            print(json.dumps({"authorized": True, "changed": True, "decision_id": record["id"]}, ensure_ascii=False))
            return 0
        if args.command == "devops-rearm-relay-owner":
            print(json.dumps(reactivate_desktop_relay_owner(args.project, incident_id=args.incident_id), ensure_ascii=False))
            return 0
        if args.command == "recreate-archived-retry":
            print(
                json.dumps(
                    recreate_archived_desktop_retry(
                        args.project,
                        reservation_token=args.reservation_token,
                        archived_thread_id=args.archived_thread_id,
                        predecessor_thread_id=args.predecessor_thread_id,
                    ),
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "timeline":
            from .launch_gate import render_launch_timeline

            cfg = load_config(args.project)
            state = StateStore(cfg.state_dir).load()
            tasks = list(args.task) or list(state.active_task_ids or ())
            if not tasks:
                print("no active tasks")
                return 0
            print(render_launch_timeline(state, tasks))
            return 0
        if args.command == "relay-status":
            print(json.dumps(relay_session_status(load_config(args.project), args.token), ensure_ascii=False))
            return 0
        if args.command == "relay-fail":
            descriptors = record_desktop_failure(
                load_config(args.project),
                args.token,
                reason=args.reason,
                failure_code=args.failure_code,
                definitive=args.definitive,
                rate_limited=args.rate_limited,
                reset_at=args.reset_at,
                reserve_other_ready=False,
                relay_executor_thread_id=_relay_executor_thread_id(),
            )
            print(json.dumps([item.to_dict() for item in descriptors], ensure_ascii=False))
            return 0
        if args.command == "relay-complete":
            outcome = complete_desktop_worker(
                load_config(args.project),
                thread_id=args.thread_id,
                turn_id=args.turn_id,
                final_message=f"AUTOPILOT_STATUS: {args.status}",
            )
            print(json.dumps({"matched": outcome.matched, "status": outcome.worker_status, "done": outcome.run_done, "descriptors": [item.to_dict() for item in outcome.descriptors]}, ensure_ascii=False))
            return 0
        if args.command == "reconcile-thread-identity":
            descriptor = reconcile_desktop_thread_identity(
                load_config(args.project),
                args.token,
                previous_thread_id=args.previous_thread_id,
                current_thread_id=args.current_thread_id,
                expected_task_id=args.task_id,
            )
            print(
                json.dumps(
                    {
                        "task_id": descriptor.task_id,
                        "reservation_token": descriptor.reservation_token,
                        "previous_thread_id": args.previous_thread_id,
                        "current_thread_id": args.current_thread_id,
                        "status": "ACTIVE",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "status":
            print(status_text(args.project))
            return 0
        if args.command == "stop":
            pause_desktop_run(load_config(args.project))
            print("Pause requested.")
            return 0
        if args.command == "logs":
            cfg = load_config(args.project)
            path = cfg.state_dir / "logs" / "dispatcher.log"
            print(path.read_text(encoding="utf-8", errors="replace") if path.exists() else "No dispatcher log yet.", end="")
            return 0
        if args.command == "doctor":
            return doctor(args.project)
        if args.command == "hook":
            payload = json.load(sys.stdin)
            event = payload.get("hook_event_name")
            result = handle_stop_hook(payload) if event == "Stop" else handle_post_tool_hook(payload) if event == "PostToolUse" else handle_prompt_hook(payload) if event == "UserPromptSubmit" else handle_interrupt_hook(payload) if event == "Interrupt" else {}
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "memory-mcp":
            from .memory_mcp import main as memory_mcp_main
            return memory_mcp_main([])
        if args.command == "uninstall":
            return uninstall(args)
    except (PreflightApprovalRequired, ProjectMemoryApprovalRequired, HookTrustApprovalRequired) as exc:
        print(f"codex-autopilot: {exc}", file=sys.stderr)
        return exc.exit_code
    except (PreflightError, HookPreflightError, ValueError, RuntimeError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"codex-autopilot: {exc}", file=sys.stderr)
        return 2
    return 2


def doctor(project: Path) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("macOS", sys.platform == "darwin", sys.platform))
    checks.append(("Python >=3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    codex = shutil.which("codex")
    checks.append(("Codex CLI", bool(codex), codex or "not found"))
    if codex:
        auth = subprocess.run([codex, "login", "status"], capture_output=True, text=True)
        checks.append(("Codex authentication", auth.returncode == 0, (auth.stdout or auth.stderr).strip()))
        try:
            with AppServerClient(codex, Path("/tmp/codex-autopilot-doctor.jsonl")) as client:
                profiles = client.list_permission_profiles(project.resolve())
                models = client.list_models()
            allowed = {item.get("id") for item in profiles if item.get("allowed") is not False}
            checks.append(("App Server :workspace", ":workspace" in allowed, str(sorted(allowed))))
            catalog = {str(item.get("model") or item.get("id")): item for item in models}
            # The catalog was listed and never compared. The runtime asks
            # App Server for one exact id and refuses everything else, so
            # the day that id is retired every installed copy stops at
            # once - and doctor, the command whose whole job is to say
            # what is missing, said PASS right up to it.
            from .models import catalog_verdicts

            for verdict in catalog_verdicts(list(models)):
                model = catalog.get(verdict.pinned)
                efforts = {str(item.get("reasoningEffort")) for item in (model or {}).get("supportedReasoningEfforts") or []}
                checks.append(
                    (f"Model {verdict.key}", verdict.state != "missing", verdict.message)
                )
                if model is not None:
                    checks.append((f"Model {verdict.key} Adaptive efforts", set(PUBLIC_REASONING).issubset(efforts), str(sorted(efforts))))
        except Exception as exc:
            checks.append(("App Server", False, str(exc)))
    for name, ok, details in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {details}")
    return 0 if all(ok for _, ok, _ in checks) else 1



WAKE_AGENT_LABEL = "com.codex-autopilot.wake"


def _wake_agent_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{WAKE_AGENT_LABEL}.plist"


def _remove_wake_agent() -> None:
    """Remove the sweep agent. Its absence is not an uninstall error."""

    plist = _wake_agent_plist()
    launchctl = shutil.which("launchctl")
    if launchctl and plist.is_file():
        subprocess.run(
            [launchctl, "bootout", f"gui/{os.getuid()}", str(plist)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    plist.unlink(missing_ok=True)


def uninstall(args) -> int:
    if not args.yes:
        print("Re-run with --yes. Project source and state remain unless --purge-project-state is also provided.", file=sys.stderr)
        return 2
    project = args.project.resolve() if args.project else find_project_root(Path.cwd())
    if project and (project / STATE_DIR_NAME / "config.toml").is_file():
        store = StateStore(project / STATE_DIR_NAME)
        state = store.load()
        if pid_alive(state.dispatcher_pid):
            store.request_pause()
            deadline = time.monotonic() + 35
            while pid_alive(state.dispatcher_pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if pid_alive(state.dispatcher_pid):
                print("Codex Autopilot dispatcher did not stop; uninstall was cancelled to keep the active run intact.", file=sys.stderr)
                return 1
    codex = shutil.which("codex")
    if codex:
        for profile in ("codex-autopilot-adaptive", "codex-autopilot-host-settings"):
            subprocess.run([codex, "plugin", "remove", f"{profile}@codex-autopilot-local"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([codex, "plugin", "marketplace", "remove", "codex-autopilot-local"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if args.purge_project_state:
        if args.project is None:
            raise ValueError("--purge-project-state requires --project")
        snapshot = purge_project_state(args.project)
        if snapshot is not None:
            print(f"Project state moved aside: {snapshot}")
    _remove_wake_agent()
    # The installer writes a managed allow-block into the Codex execpolicy,
    # outside Autopilot's own directory. Leaving it behind would leave a
    # standing grant for the very script this command is about to delete.
    from .execpolicy import remove as _remove_execpolicy, rules_path as _execpolicy_path

    policy = _execpolicy_path()
    try:
        if _remove_execpolicy(policy):
            print(f"Execpolicy: the managed Autopilot block was removed from {policy}")
    except OSError as exc:
        print(
            f"Execpolicy: the managed block could not be removed from {policy}: {exc}; "
            "remove it by hand.",
            file=sys.stderr,
        )
    install_root = os.environ.get("CODEX_AUTOPILOT_INSTALL_ROOT")
    if install_root:
        root = Path(install_root).expanduser().resolve()
        target = root / __version__
        current = root / "current"
        if current.is_symlink() and current.resolve() == target.resolve():
            current.unlink()
        shutil.rmtree(target, ignore_errors=True)
        try:
            root.rmdir()
        except OSError:
            pass
    try:
        from .launch_registry import registry_directory
        shutil.rmtree(registry_directory().parent, ignore_errors=True)
    except Exception:
        pass
    print("Codex Autopilot uninstalled. Project source and Git repository were preserved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
