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


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="codex-autopilot")
    top.add_argument("--version", action="version", version=__version__)
    sub = top.add_subparsers(dest="command", required=True)
    bootstrap = sub.add_parser("bootstrap", help="initialize a project from a structured plan")
    bootstrap.add_argument("--project", type=Path, default=Path.cwd())
    bootstrap.add_argument("--plan-file", type=Path, required=True)
    bootstrap.add_argument("--profile", choices=["adaptive", "host-settings"])
    bootstrap.add_argument("--skill-path", type=Path)
    bootstrap.add_argument("--replace", action="store_true")
    bootstrap.add_argument("--language", default=DEFAULT_LANGUAGE)
    bootstrap.add_argument("--app-server-project-id")
    bootstrap.add_argument("--desktop-project-id")
    start_skill = sub.add_parser("start-skill", help=argparse.SUPPRESS)
    start_skill.add_argument("--project", type=Path, default=Path.cwd())
    start_skill.add_argument("--plan-file", type=Path, required=True)
    start_skill.add_argument("--replace", action="store_true")
    start_skill.add_argument("--language", default=DEFAULT_LANGUAGE)
    start_skill.add_argument("--app-server-project-id")
    start_skill.add_argument("--desktop-project-id")
    start_skill.add_argument("--approve-project-memory-always", action="store_true", help=argparse.SUPPRESS)
    preflight = sub.add_parser("preflight", help="validate a target before creating Autopilot state")
    preflight.add_argument("--project", type=Path, default=Path.cwd())
    preflight.add_argument("--plan-file", type=Path, required=True)
    preflight.add_argument("--profile", choices=["adaptive", "host-settings"])
    preflight.add_argument("--skill-path", type=Path)
    preflight.add_argument("--approve-project-memory-always", action="store_true", help=argparse.SUPPRESS)
    preflight.add_argument("--language", default=DEFAULT_LANGUAGE)
    preflight.add_argument("--app-server-project-id")
    preflight.add_argument("--desktop-project-id")
    armed = sub.add_parser("arm", help=argparse.SUPPRESS)
    armed.add_argument("--project", type=Path, default=Path.cwd())
    automatic_relay = sub.add_parser("_relay_dispatch", help=argparse.SUPPRESS)
    automatic_relay.add_argument("--project", type=Path, required=True)
    automatic_relay.add_argument("--token", required=True)
    automatic_relay.add_argument("--initiator-thread", required=True)
    automatic_relay.add_argument("--initiator-turn", required=True)
    recreate_archived = sub.add_parser("recreate-archived-retry", help=argparse.SUPPRESS)
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
    relay_status = sub.add_parser("relay-status", help=argparse.SUPPRESS)
    relay_status.add_argument("--project", type=Path, default=Path.cwd())
    relay_status.add_argument("--token", required=True)
    relay_fail = sub.add_parser("relay-fail", help=argparse.SUPPRESS)
    relay_fail.add_argument("--project", type=Path, default=Path.cwd())
    relay_fail.add_argument("--token", required=True)
    relay_fail.add_argument("--reason", required=True)
    # Вид отказа называет вызывающий: по нему R23 считает повторы.
    relay_fail.add_argument("--failure-code", required=True)
    relay_fail.add_argument("--definitive", action="store_true")
    relay_fail.add_argument("--rate-limited", action="store_true")
    relay_fail.add_argument("--reset-at", type=int)
    relay_complete = sub.add_parser("relay-complete", help=argparse.SUPPRESS)
    relay_complete.add_argument("--project", type=Path, default=Path.cwd())
    relay_complete.add_argument("--thread-id", required=True)
    relay_complete.add_argument("--turn-id", required=True)
    relay_complete.add_argument("--status", choices=["ROTATE", "DONE", "BLOCKED", "ESCALATE"], required=True)
    reconcile_identity = sub.add_parser("reconcile-thread-identity", help=argparse.SUPPRESS)
    reconcile_identity.add_argument("--project", type=Path, default=Path.cwd())
    reconcile_identity.add_argument("--token", required=True)
    reconcile_identity.add_argument("--task-id", required=True)
    reconcile_identity.add_argument("--previous-thread-id", required=True)
    reconcile_identity.add_argument("--current-thread-id", required=True)
    relay_rearm = sub.add_parser("devops-rearm-relay-owner", help=argparse.SUPPRESS)
    relay_rearm.add_argument("--project", type=Path, default=Path.cwd())
    relay_rearm.add_argument("--incident-id")
    devops_resolve = sub.add_parser("devops-resolve-incident", help=argparse.SUPPRESS)
    devops_resolve.add_argument("--project", type=Path, default=Path.cwd())
    devops_resolve.add_argument("--incident-id", required=True)
    devops_resolve.add_argument("--healthcheck-name", required=True)
    devops_resolve.add_argument("--check", action="append", required=True)
    # Чем починено - обязательно и идентификатором из словаря. Замерено
    # на прогоне v1.0: флаг был необязательным, и все шестнадцать
    # починок записались без единого действия либо прозой, поэтому по
    # подписи с восемнадцатью повторами не выучилось ни одной процедуры.
    # Сам словарь не дублируется здесь: имена проверяет реестр, у него
    # они и живут.
    devops_resolve.add_argument("--action", action="append", required=True)
    devops_resolve.add_argument(
        "--note",
        default="",
        help="обстоятельства прозой: объясняют починку и ни на что не влияют",
    )
    # Задача, остановленная нарушением правила, снимается только
    # человеком и только с записанной причиной. Прежде снять её было
    # нечем вовсе: "продолжи" отказывает на BLOCKED по любой причине,
    # кроме эскалации, и правильно делает - но обратного пути не
    # существовало, и прогон стоял навсегда.
    unblock = sub.add_parser(
        "unblock",
        help="снять остановку задачи решением пользователя, с записанной причиной",
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
    for name in ("status", "stop", "resume", "logs"):
        item = sub.add_parser(name)
        item.add_argument("--project", type=Path, default=Path.cwd())
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--project", type=Path, default=Path.cwd())
    hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--project", type=Path)
    uninstall.add_argument("--purge-project-state", action="store_true")
    sub.add_parser("memory-mcp", help=argparse.SUPPRESS)
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
    """Отказ отсоединённого диспетчера должен быть виден, а не лежать в файле.

    Раньше падение этого процесса уходило только в
    logs/automatic-relay-<токен>.log: ни записи в журнале прогона, ни
    инцидента, ни сообщения пользователю. Прогон при этом выглядел
    работающим - статус RUNNING, задача активна, - и стоял молча.
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
    except Exception:  # состояние нечитаемо - отчёт всё равно должен уйти
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
        print(f"codex-autopilot: тикет {incident_id} открыт по отказу диспетчера")
    except PipelineIncidentError as incident_error:
        print(f"codex-autopilot: тикет завести не удалось: {incident_error}")


def _run_automatic_relay_dispatch(
    cfg,
    *,
    token: str,
    owner: str,
    owner_turn: str,
) -> int:
    """Run the v0.7-style local loop with one App Server process per task."""

    # Цикл переходит от задачи к задаче, переприсваивая свой token. Снаружи
    # оставался исходный, и отказ на поздней задаче приписывался первой:
    # замерено, тикет incident-78e67b38680498f1 по отказу M2 назвал M1, а
    # лестница "на отказе" напечатала шаги уже завершённой M1. Инженера
    # послали бы чинить проверенную веху. Курсор общий: он всегда указывает
    # на задачу, которую цикл ведёт сейчас.
    cursor = _RelayCursor(token)
    try:
        return _automatic_relay_loop(
            cfg, token=token, owner=owner, owner_turn=owner_turn, cursor=cursor
        )
    except BaseException as error:
        _print_relay_timeline(cfg, cursor.token, "на отказе")
        _record_detached_dispatch_failure(cfg, cursor.token, error)
        raise


def _record_rate_limits(cfg, method: str, params: dict) -> None:
    """Запомнить снимок лимитов, чтобы ёмкость считалась по свежим данным.

    Планировщик сужает число воркеров по расходу окна. Без этой записи
    он видел бы только то, что было на старте прогона, и продолжал бы
    держать десяток, когда окно уже кончается.
    """

    if method != "account/rateLimits/updated":
        return
    snapshot = (params or {}).get("rateLimits")
    if not isinstance(snapshot, dict):
        return
    try:
        StateStore(cfg.state_dir).record_rate_limits(snapshot)
    except Exception:
        # Ёмкость - оптимизация, а не контракт: её обновление никогда не
        # должно валить ход, который в этот момент идёт.
        return


def _print_relay_timeline(cfg, token: str, headline: str) -> None:
    """Печатать лестницу шагов из самого диспетчера, а не по запросу.

    Диспетчер - единственный, кто знает, что происходит, пока идёт работа.
    Раньше он молчал до конца, и узнать ход дела можно было только спросив.
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
    except Exception as error:  # отчёт не вправе ронять работу
        print(f"codex-autopilot: лента недоступна: {error}", flush=True)


@dataclass
class _RelayCursor:
    """Задача, которую цикл ведёт прямо сейчас."""

    token: str


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
        _print_relay_timeline(cfg, token, "перед запуском задачи")
        dispatcher_log = (
            cfg.state_dir / "logs" / f"app-server-dispatcher-{token}.jsonl"
        )
        client = AppServerClient(
            cfg.desktop.binary,
            dispatcher_log,
            # Лимиты приходят сами, событием, по ходу работы. Прежде
            # ёмкость считалась от того, что preflight прочитал на старте:
            # прогон на двадцать четыре задачи мог выесть окно и не узнать
            # об этом до следующего запуска.
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
        _print_relay_timeline(cfg, token, "после хода задачи")
        proc = client.proc
        if proc is None or proc.poll() is None:
            raise RuntimeError("per-task App Server process did not fully exit")
        record_automatic_app_server_exit(
            cfg,
            token,
            dispatcher_pid=os.getpid(),
        )
        if not outcome.descriptors:
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
            predecessor = next(
                item
                for item in reversed(state.worker_sessions)
                if item.get("thread_id") == relay_owner
                and item.get("status") == "COMPLETED"
                and item.get("turn_id")
            )
            spawn_automatic_app_server_relay(
                cfg.root,
                reservation_token=descriptor.reservation_token,
                initiator_thread_id=relay_owner,
                initiator_turn_id=str(predecessor["turn_id"]),
            )
        return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {"bootstrap", "start-skill", "preflight"}:
            profile, skill = _profile_and_skill(args)
            language = normalize_language(args.language)
            raw = json.loads(args.plan_file.read_text(encoding="utf-8"))
            # Тот же вопрос, что и у bootstrap: есть ли прогон, который
            # мигрируют. План v0.8 впускается только как его миграция, и
            # проверить это надо здесь тоже - иначе preflight принимал бы
            # то, что bootstrap следом отвергнет.
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
            # Единственная поверхность - desktop_owned, и запуск в ней
            # принадлежит доверенному Stop-хуку. Команда оставлена как
            # указатель: молча отсутствующая resume заставляла бы искать.
            raise RuntimeError(
                "resume is hook-owned: send the exact phrase "
                "'Resume Codex Autopilot.' in a Codex task opened on this "
                "project, so its trusted Stop hook launches the dispatcher"
            )
        if args.command == "_relay_dispatch":
            cfg = load_config(args.project)
            return _run_automatic_relay_dispatch(
                cfg,
                token=args.token,
                owner=args.initiator_thread,
                owner_turn=args.initiator_turn,
            )
        if args.command == "unblock":
            # Запуск этой команды и есть решение человека: она не
            # проверяет, прав ли он, она записывает, что он решил. Без
            # причины не работает - запись без причины ничем не лучше
            # молчаливого снятия.
            from .plan import load_plan
            from .run_state import utc_now
            from .task_state import TaskState, transition_task

            cfg = load_config(args.project)
            store = StateStore(cfg.state_dir)
            state = store.load()
            plan = load_plan(cfg.state_dir, cfg.profile)
            task_id = str(args.task).strip()
            if task_id not in plan.task_map:
                raise SystemExit(f"в плане нет задачи {task_id!r}")
            if state.task_states.get(task_id) != TaskState.BLOCKED.value:
                raise SystemExit(
                    f"{task_id} не остановлена: сейчас "
                    f"{state.task_states.get(task_id)}"
                )
            reason = str(args.reason).strip()
            if not reason:
                raise SystemExit("нужна причина: --reason")
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
                f"{task_id}: остановка снята решением пользователя — {reason}\n"
                "Продолжи прогон фразой «Resume Codex Autopilot.» в задаче Codex."
            )
            return 0
        if args.command == "devops-resolve-incident":
            # Дежурный инженер закрывает свой тикет сам, но только с
            # пройденной проверкой здоровья: закрытие без неё - это
            # заявление, а не наблюдение. Мутация требует владеющей
            # ветки, как и остальные команды восстановления.
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
            # R6: единственный вход, которым разрешается правка корней
            # сохранённого проекта. Разрешение хранится как принятое
            # решение пользователя и называет конкретный проект и
            # конкретный корень - на другой проект оно не переносится.
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
                print("активных задач нет")
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
            for key, model_id in MODEL_IDS.items():
                model = catalog.get(model_id)
                efforts = {str(item.get("reasoningEffort")) for item in (model or {}).get("supportedReasoningEfforts") or []}
                checks.append((f"Model {key}", model is not None, model_id if model else "unavailable"))
                checks.append((f"Model {key} Adaptive efforts", set(PUBLIC_REASONING).issubset(efforts), str(sorted(efforts))))
        except Exception as exc:
            checks.append(("App Server", False, str(exc)))
    for name, ok, details in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {details}")
    return 0 if all(ok for _, ok, _ in checks) else 1


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
        purge_project_state(args.project)
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
