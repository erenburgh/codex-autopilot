from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict, dataclass
import hashlib
import html
import json
import os
from pathlib import Path
import re
import time
import uuid
from typing import Any, Callable

from .ai_studio import AIStudioRuntime
from .appserver import (
    AppServerClient,
    AppServerError,
    AppServerRpcError,
    PauseRequested,
    final_agent_message,
    is_rate_limit_error,
)
from .bootstrap import mark_roadmap, select_milestone
from .config import Config, DESKTOP_OWNED_SURFACE
from .hook_trust import require_trusted_stop_hook_for_config
from .language import is_russian
from .lifecycle_prompts import (
    _evidence_selectors,
    _replanner_prompt,
    _revision_prompt,
    _verification_contract,
    _verifier_prompt,
    _worker_prompt,
)
from .memory import ProjectMemory
from .models import MODEL_IDS, ModelRoutingError, logical_model
from .pipeline_engineer import (
    IncidentClass,
    IncidentSignal,
    PipelineIncidentStore,
    SideEffectOutcome,
)
from .preflight import installed_plugin_root
from .plan import Plan, Task, VerificationCheck, atomic_json, load_plan, plan_to_dict
from .resilience import (
    PLAN_CHANGE_RESULT_PREFIX,
    PlanChangeConflictError,
    PlanChangeProtocolError,
    RuntimeReconciliation,
    active_plan_change,
    append_resilience_event,
    commit_plan_change,
    parse_plan_change_request,
    parse_plan_change_result,
    reconcile_plan_change_state,
    reconcile_running_work,
    recover_plan_change_transaction,
    register_plan_change_request,
    validate_replanner_result,
)
from .resources import (
    DurableResourceLock,
    LockOwner,
    ResourceLockCoordinator,
    acquire_resources_in_state,
    build_scheduler_availability,
    release_resources_in_state,
)
from .run_state import RunState, StateStore, utc_now
from .scheduler import schedule
from .task_state import (
    TaskState,
    migrate_v08_task_states,
    transition_task,
    validate_task_states,
)
from .thread_titles import replanner_thread_title, task_phase_thread_title
from .verification import (
    DeterministicCheckResult,
    VerificationIssue,
    VerificationProtocolError,
    VerificationVerdict,
    VERIFICATION_PREFIX,
    deterministic_issues,
    parse_verifier_result,
    run_deterministic_checks,
    verifier_route,
)


DESCRIPTOR_SCHEMA_VERSION = 2
DESKTOP_SLOT_READY = "AUTOPILOT_SLOT_READY"
WORKSPACE_HANDOFF_OK = "AUTOPILOT_WORKSPACE_READY"
WORKSPACE_HANDOFF_PROMPT = (
    "Codex Autopilot workspace handoff. Do not inspect or modify files and do not call tools. "
    f"Reply exactly: {WORKSPACE_HANDOFF_OK}"
)
PENDING_SESSION_STATUSES = frozenset(
    {
        "RESERVED",
        "CREATE_REQUESTED",
        "RELAYING",
        "CREATED",
        "PREPARING",
        "PREPARED",
        "SEND_RELAYING",
        "ACTIVE",
        "AMBIGUOUS",
    }
)
RELAYABLE_SESSION_STATUSES = frozenset(
    {"CREATE_REQUESTED", "CREATED", "PREPARING", "PREPARED"}
)
SUCCESS_STATUSES = frozenset({"ROTATE", "DONE"})
ALLOWED_STATUSES = frozenset({"ROTATE", "DONE", "BLOCKED", "ESCALATE"})
IMPLEMENTATION_SESSION_KINDS = frozenset({"worker", "implementation"})
SESSION_KINDS = IMPLEMENTATION_SESSION_KINDS | frozenset(
    {"verifier", "revision", "replanner"}
)


from .lifecycle_base import (
    CompletionOutcome,
    DesktopLifecycleError,
    LaunchDescriptor,
    _append_event,
    _bind_resource_identity,
    _client_process_exited,
    _dispatcher_owns_reservation,
    _materialize,
    _require_desktop_owned,
    _require_relay_executor,
    _session_by_token,
    _thread_cwd,
    _wait_for_dispatcher_ownership,
    acknowledge_desktop_send,
)
from .lifecycle_failures import (
    _record_app_server_create_failure,
    _record_app_server_project_assignment,
    _record_created_app_server_ambiguity,
    record_desktop_failure,
)


def app_server_creation_contract(
    cfg: Config,
    descriptor: LaunchDescriptor,
) -> dict[str, Any]:
    """Return the exact project-scoped v0.7-style create contract."""

    params: dict[str, Any] = {
        "cwd": str(cfg.root),
        "permissions": cfg.desktop.permission_profile,
        "ephemeral": False,
        "runtimeWorkspaceRoots": [str(cfg.root)],
    }
    if cfg.desktop.project_id:
        params["projectId"] = cfg.desktop.project_id
    if descriptor.model:
        params["model"] = descriptor.model
    contract: dict[str, Any] = {
        "method": "thread/start",
        "params": params,
        "name": descriptor.title,
    }
    if cfg.desktop.project_id:
        contract["project_root_precondition"] = {
            "method": "project/update-if-missing",
            "params": {
                "projectId": cfg.desktop.project_id,
                "root": str(cfg.root),
            },
        }
    return contract

def create_desktop_thread_via_app_server(
    cfg: Config,
    reservation_token: str,
    *,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    at: str | None = None,
    now_epoch: int | None = None,
    relay_executor_thread_id: str | None = None,
    dispatcher_pid: int | None = None,
    connected_client: AppServerClient | None = None,
) -> dict[str, Any]:
    """Create a persistent canonical-cwd task through the local dispatcher.

    One bounded App Server connection owns ``thread/start``, ``turn/start``,
    and the authoritative completion wait for this task. The outer v0.7-style
    dispatcher closes that process before it advances to a successor. No model
    continuation or Codex App task API participates.
    """

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if not _dispatcher_owns_reservation(
            session,
            dispatcher_pid=dispatcher_pid,
        ):
            require_trusted_stop_hook_for_config(cfg)
        if session.get("status") != "CREATE_REQUESTED":
            raise DesktopLifecycleError(
                "App Server create was already claimed or the reservation is not launchable"
            )
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        session["status"] = "RELAYING"
        session["creation_transport"] = "app_server_thread_start"
        session["app_server_creation_contract"] = app_server_creation_contract(
            cfg, descriptor
        )
        _append_event(state, "app_server_create_claimed", session, utc_now())
        store.save(state)

    client: Any = None
    thread_id = ""
    create_invoked = False
    actual_cwd: Path | None = None
    actual_name: str | None = None
    actual_project_id: str | None = None
    log_path = cfg.state_dir / "logs" / f"app-server-create-{reservation_token}.jsonl"
    owns_client = connected_client is None
    client_context = (
        client_factory(cfg.desktop.binary, log_path)
        if owns_client
        else nullcontext(connected_client)
    )
    try:
        with client_context as connected:
            client = connected
            profiles = client.list_permission_profiles(cfg.root)
            allowed = {
                str(item.get("id"))
                for item in profiles
                if item.get("allowed") is not False and item.get("id")
            }
            if cfg.desktop.permission_profile not in allowed:
                raise DesktopLifecycleError(
                    "configured permission profile is unavailable to App Server create"
                )
            if cfg.desktop.project_id:
                project = client.ensure_project_root(
                    cfg.desktop.project_id,
                    cfg.root,
                )
                if str(project.get("id") or "") != cfg.desktop.project_id:
                    raise DesktopLifecycleError(
                        "configured App Server project could not be verified"
                    )
            create_invoked = True
            started = client.start_thread(
                cwd=cfg.root,
                permission_profile=cfg.desktop.permission_profile,
                # v0.7 invariant: create the task in the saved project, with a
                # cwd that is already one of that project's durable roots.
                project_id=cfg.desktop.project_id,
                model=descriptor.model,
                plugin_root=installed_plugin_root(cfg.skill_path),
                ephemeral=False,
                # v0.7 не передавала threadSource вовсе, и её задачи
                # появлялись в сайдбаре проекта обычными ветками.
                # "agent_created_thread" помечает ветку как созданную
                # агентом: приложение показывает её как созданную в другом
                # приложении и требует ручного перехвата. Именно этот
                # параметр и отличал 0.8 от работавшей 0.7.
            )
            thread = started.get("thread") or {}
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                raise DesktopLifecycleError("App Server thread/start returned no thread id")
            active_profile = started.get("activePermissionProfile") or {}
            if active_profile and active_profile.get("id") != cfg.desktop.permission_profile:
                raise DesktopLifecycleError(
                    "App Server thread/start applied an unexpected permission profile"
                )
            client.name_thread(thread_id, descriptor.title)
            metadata = client.read_thread(thread_id)
            if str(metadata.get("id") or "") != thread_id:
                raise DesktopLifecycleError(
                    "App Server thread/read returned an unexpected created thread"
                )
            actual_cwd = _thread_cwd(metadata) or _thread_cwd(thread)
            if actual_cwd != cfg.root:
                raise DesktopLifecycleError(
                    "App Server-created task does not use the canonical project cwd"
                )
            actual_name = metadata.get("name")
            if actual_name != descriptor.title:
                raise DesktopLifecycleError(
                    "App Server did not preserve the deterministic task title"
                )
            raw_project_id = metadata.get("projectId")
            actual_project_id = (
                str(raw_project_id) if raw_project_id is not None else None
            )
            if cfg.desktop.project_id and actual_project_id != cfg.desktop.project_id:
                raise DesktopLifecycleError(
                    "App Server did not preserve the configured project association"
                )
            if cfg.desktop.project_id:
                # Явная привязка ветки к сохранённому проекту после создания.
                # Такой шаг уже выполнялся в живом прогоне 11.09 (событие
                # app_server_project_assigned), но кода, который его делал, не
                # осталось ни в одном коммите и ни в одной установленной
                # версии - работа была потеряна. Создание с projectId и
                # явная привязка - разные вызовы, и второй пропал.
                assigned = client.assign_thread_to_project(
                    thread_id, cfg.desktop.project_id
                )
                _record_app_server_project_assignment(
                    cfg,
                    reservation_token,
                    thread_id=thread_id,
                    project_id=str(assigned.get("projectId") or ""),
                    at=at,
                )
        if owns_client and (client is None or not _client_process_exited(client)):
            raise DesktopLifecycleError(
                "App Server create process did not fully exit before Desktop handoff"
            )
    except Exception as exc:
        if thread_id:
            _record_created_app_server_ambiguity(
                cfg,
                reservation_token,
                thread_id=thread_id,
                reason=str(exc),
                at=at,
            )
        else:
            definitive = (not create_invoked) or (
                isinstance(exc, AppServerRpcError) and exc.method == "thread/start"
            )
            _record_app_server_create_failure(
                cfg,
                reservation_token,
                reason=str(exc),
                definitive=definitive,
                at=at,
                now_epoch=now_epoch,
            )
        raise DesktopLifecycleError(str(exc)) from exc

    timestamp = at or utc_now()
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session.get("status") != "RELAYING" or session.get("thread_id"):
            raise DesktopLifecycleError(
                "App Server create reservation changed before acknowledgement"
            )
        session["thread_id"] = thread_id
        session["status"] = "PREPARED"
        session["actual_cwd"] = str(actual_cwd)
        session["actual_thread_name"] = actual_name
        session["title_verification"] = "verified by App Server thread/read"
        session["actual_project_id"] = actual_project_id
        session["project_association_verification"] = (
            "verified project-scoped thread/start App Server projectId and canonical cwd by thread/read; "
            "Desktop rootPaths/sidebar placement require separate verification"
            if cfg.desktop.project_id
            else "App Server projectId not configured"
        )
        session["create_acknowledged_at"] = timestamp
        if owns_client:
            session["prep_app_server_exited_at"] = timestamp
            session["app_server_create_exited_at"] = timestamp
            state.prep_app_server_exited_at = timestamp
        else:
            session["automatic_dispatch_connection_pid"] = dispatcher_pid
            session["app_server_create_exited_at"] = None
        state.current_thread_id = thread_id
        state.phase = "AWAITING_DESKTOP_SEND"
        state.last_error = None
        _bind_resource_identity(state, reservation_token, thread_id=thread_id)
        lifecycle_events = [
            ("app_server_thread_created", thread_id),
            ("create_acknowledged", "thread/start"),
            ("prep_completed", str(actual_cwd)),
        ]
        if cfg.desktop.project_id:
            lifecycle_events.insert(
                2,
                ("app_server_project_scoped_create", str(actual_project_id)),
            )
        lifecycle_events.insert(
            2,
            (
                "app_server_create_process_exited"
                if owns_client
                else "app_server_dispatcher_connection_retained",
                "full process exit" if owns_client else f"pid={dispatcher_pid}",
            ),
        )
        for event, detail in lifecycle_events:
            _append_event(state, event, session, timestamp, detail=detail)
        store.save(state)
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
    _materialize((descriptor,))
    return {
        "reservation_token": reservation_token,
        "thread_id": thread_id,
        "status": "PREPARED",
        "cwd": str(actual_cwd),
        "app_server_project_id": actual_project_id,
        "app_server_process_exited_at": timestamp if owns_client else None,
    }

def _thread_is_gone(
    cfg: Config,
    reservation_token: str,
    *,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    connected_client: AppServerClient | None = None,
) -> bool:
    """Ветки, к которой привязана резервация, на App Server больше нет.

    Проверяется чтением: отсутствие ветки - это ответ сервера, а не вывод
    из наших записей. Любая другая ошибка чтения исчезновением не
    считается, иначе временный сбой связи приводил бы к пересозданию
    живой ветки и раздвоению работы.
    """

    state = StateStore(cfg.state_dir).load()
    session = _session_by_token(state, reservation_token)
    thread_id = str(session.get("thread_id") or "")
    if not thread_id:
        return False
    context = (
        client_factory(cfg.desktop.binary, cfg.state_dir / "logs" / "thread-probe.jsonl")
        if connected_client is None
        else nullcontext(connected_client)
    )
    try:
        with context as client:
            client.read_thread(thread_id)
    except AppServerRpcError as error:
        return "not found" in str(error).lower()
    except Exception:
        return False
    return False


def _reset_to_create_requested(cfg: Config, reservation_token: str) -> None:
    """Отвязать резервацию от исчезнувшей ветки и дать создать новую."""

    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        lost = str(session.get("thread_id") or "")
        session["status"] = "CREATE_REQUESTED"
        for field in (
            "thread_id",
            "turn_id",
            "actual_cwd",
            "actual_thread_name",
            "actual_project_id",
            "app_server_project_id",
            "creation_transport",
            "app_server_create_exited_at",
            "create_acknowledged_at",
            "desktop_placement",
            "title_verification",
            "project_association_verification",
        ):
            session[field] = None
        _append_event(
            state,
            "lost_thread_recreate_requested",
            session,
            utc_now(),
            detail=f"thread {lost} no longer exists on App Server",
        )
        store.save(state)


def _creator_process_is_gone(session: Mapping[str, Any]) -> bool:
    """Процесс, создавший ветку, больше не существует.

    Отметку о своём выходе он ставит сам, штатно завершаясь. Если он упал -
    например, на отказе гейта размещения, - отметки нет, и следующий
    диспетчер не может взять ход: сессия остаётся неподъёмной навсегда.

    Барьер защищает ровно от одного: от второго писателя в ту же ветку.
    Мёртвый pid это доказывает.
    """

    from .control import pid_alive

    pid = session.get("automatic_dispatch_connection_pid")
    if pid is None:
        pid = session.get("automatic_dispatch_pid")
    if not isinstance(pid, int):
        return False
    return not pid_alive(pid)


def _require_thread_placement(
    cfg: Config,
    reservation_token: str,
    *,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    connected_client: AppServerClient | None = None,
    at: str | None,
) -> str:
    """Измерить размещение ветки и не пустить работу без него.

    Порядок повторяет рабочий цикл v0.7: слот создан, ветка создана, и
    только после подтверждённого размещения задача начинает работу.
    Размещение спрашивается у сервера - из его списка Desktop и рисует
    сайдбар. Прежняя версия читала ключи .codex-global-state.json и
    называла OUTSIDE ветки, которые человек видел глазами; на её
    показаниях был построен ложный вывод о неустранимой невидимости.

    Досылать привязку тут нечем: thread/metadata/update проходит успешно,
    ничего не меняя, а дописывать в состояние приложения за его спиной -
    это то, чем прежняя версия маскировала неверный диагноз. Ветка выходит
    в нужный проект уже из thread/start.
    """

    from .launch_gate import INSIDE, OUTSIDE, desktop_placement

    required = cfg.runtime.required_thread_placement
    if required == "any":
        return "any"
    if not cfg.desktop.project_id:
        # Сохранённого проекта нет - размещать не во что, и требовать
        # нечего. Проверять при этом настоящий каталог Codex было бы
        # зависимостью от машины, а не от прогона.
        return "unconfigured"
    timestamp = at or utc_now()
    state = StateStore(cfg.state_dir).load()
    session = _session_by_token(state, reservation_token)
    thread_id = str(session.get("thread_id") or "")
    if not thread_id:
        raise DesktopLifecycleError("placement gate requires a created Desktop thread")

    context = (
        client_factory(cfg.desktop.binary, cfg.state_dir / "logs" / "placement.jsonl")
        if connected_client is None
        else nullcontext(connected_client)
    )
    with context as client:
        after = desktop_placement(
            thread_id, project_id=cfg.desktop.project_id, client=client
        )
    before = str(session.get("desktop_placement") or "")

    _record_placement_outcome(
        cfg, reservation_token, before=before, after=after, at=timestamp
    )
    satisfied = after == INSIDE or (required == "visible" and after in {INSIDE, OUTSIDE})
    if not satisfied:
        raise DesktopLifecycleError(
            f"задача не начата: ветка {thread_id} в состоянии {after}, "
            f"а требуется {required}. Невидимую задачу нельзя открыть; "
            "смягчить требование можно через runtime.required_thread_placement"
        )
    return after


def _record_placement_outcome(
    cfg: Config,
    reservation_token: str,
    *,
    before: str,
    after: str,
    at: str,
) -> None:
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        session["desktop_placement"] = after
        _append_event(
            state,
            "desktop_placement_verified",
            session,
            at,
            detail=f"{before} -> {after}",
        )
        store.save(state)


def run_automatic_app_server_turn(
    cfg: Config,
    reservation_token: str,
    *,
    initiator_thread_id: str,
    initiator_turn_id: str,
    client_factory: Callable[..., AppServerClient] = AppServerClient,
    now_epoch: int | None = None,
    connected_client: AppServerClient | None = None,
) -> CompletionOutcome:
    """Run one reserved worker without a model-mediated relay.

    The trusted Stop hook starts this function in a detached local process. It
    waits until the causal predecessor turn is durably complete, creates the
    persistent App Server task, starts the production turn itself, waits for the
    authoritative completion event, and returns the structured result to the
    same dispatcher loop. Codex App ``create_thread`` and
    ``send_message_to_thread`` are not part of this transport.
    """
    # поздний импорт: развязка обратной зависимости модулей
    from .lifecycle_completion import complete_desktop_worker

    _require_desktop_owned(cfg)
    owner = str(initiator_thread_id or "").strip()
    owner_turn = str(initiator_turn_id or "").strip()
    if not owner or not owner_turn:
        raise DesktopLifecycleError(
            "automatic App Server relay requires the causal predecessor thread and turn"
        )
    initial = StateStore(cfg.state_dir).load()
    session = _session_by_token(initial, reservation_token)
    _require_relay_executor(session, owner)
    dispatcher_authorized = _wait_for_dispatcher_ownership(
        cfg,
        reservation_token,
        owner_thread_id=owner,
    )
    if not dispatcher_authorized:
        require_trusted_stop_hook_for_config(cfg)
    if session.get("status") not in {"CREATE_REQUESTED", "PREPARED"}:
        if session.get("status") == "COMPLETED":
            return CompletionOutcome(False, None, (), initial.status == "DONE")
        raise DesktopLifecycleError(
            f"automatic relay cannot start from {session.get('status')!r}"
        )

    wait_log = cfg.state_dir / "logs" / f"app-server-wait-{reservation_token}.jsonl"
    deadline = time.monotonic() + cfg.desktop.reconcile_timeout_seconds
    wait_context = (
        client_factory(cfg.desktop.binary, wait_log)
        if connected_client is None
        else nullcontext(connected_client)
    )
    with wait_context as wait_client:
        while True:
            predecessor = wait_client.read_thread(owner)
            turn = next(
                (
                    item
                    for item in predecessor.get("turns") or []
                    if item.get("id") == owner_turn
                ),
                None,
            )
            # Только устойчивое "completed" открывает ворота воркера, как в
            # v0.7. Пока синхронный Stop-хук работает, второй App Server
            # наблюдает этот же ход как "interrupted" - замерено в рабочем
            # прогоне 0.7: ход 01a097aa-4832 виден сначала interrupted,
            # затем completed. Принимать interrupted значило бы открывать
            # ворота ровно в тот момент, от которого барьер и защищает.
            if turn and turn.get("status") == "completed":
                break
            if time.monotonic() >= deadline:
                raise DesktopLifecycleError(
                    "causal predecessor did not reach durable completed state; "
                    f"последний статус хода: {(turn or {}).get('status')!r}"
                )
            # Раз в секунду, а не четыре: read_thread тянет всю историю
            # ветки целиком. На живом прогоне это дало 42 МБ журнала за
            # две минуты ожидания.
            time.sleep(1.0)

    session = _session_by_token(StateStore(cfg.state_dir).load(), reservation_token)
    if session.get("status") == "PREPARED" and _thread_is_gone(
        cfg,
        reservation_token,
        client_factory=client_factory,
        connected_client=connected_client,
    ):
        # v0.7 создавала ветку и тут же ею пользовалась - одним соединением,
        # без разрыва. v0.8 создаёт ветку в одном процессе, требует его
        # полного выхода и стартует ход другим процессом позже. В этом
        # промежутке ветка живёт без подписчика, и после перезапуска её
        # может уже не быть: turn/start отвечает "thread not found", а
        # резервация остаётся навсегда привязанной к мёртвому идентификатору.
        # Исчезнувшая ветка - повод создать новую, а не повод встать.
        _reset_to_create_requested(cfg, reservation_token)
        session = _session_by_token(StateStore(cfg.state_dir).load(), reservation_token)

    if session.get("status") == "CREATE_REQUESTED":
        create_desktop_thread_via_app_server(
            cfg,
            reservation_token,
            client_factory=client_factory,
            now_epoch=now_epoch,
            relay_executor_thread_id=owner,
            dispatcher_pid=(os.getpid() if dispatcher_authorized else None),
            connected_client=connected_client,
        )

    # Гейт размещения: задача не начинает работу, пока её ветка не доведена
    # до требуемого состояния в Desktop. Невидимую задачу нельзя открыть и
    # прочитать, а в этом весь смысл видимых воркеров.
    _require_thread_placement(
        cfg,
        reservation_token,
        client_factory=client_factory,
        connected_client=connected_client,
        at=None,
    )

    descriptor = claim_automatic_app_server_turn(
        cfg,
        reservation_token,
        relay_executor_thread_id=owner,
        dispatcher_pid=(os.getpid() if dispatcher_authorized else None),
    )
    state_after_create = StateStore(cfg.state_dir).load()
    session = _session_by_token(state_after_create, reservation_token)
    thread_id = str(session["thread_id"])
    turn_id = ""
    completed_turn: dict[str, Any] | None = None
    production_log = (
        cfg.state_dir / "logs" / f"app-server-production-{reservation_token}.jsonl"
    )
    client: Any = None
    production_context = (
        client_factory(cfg.desktop.binary, production_log)
        if connected_client is None
        else nullcontext(connected_client)
    )
    try:
        with production_context as production_client:
            client = production_client
            if connected_client is None:
                resumed = production_client.resume_thread(thread_id)
                thread = resumed.get("thread") or {}
            else:
                thread = production_client.read_thread(thread_id)
            if _thread_cwd(thread) != cfg.root:
                raise DesktopLifecycleError(
                    "App Server production task is not bound to the canonical cwd"
                )
            if cfg.desktop.project_id and thread.get("projectId") != cfg.desktop.project_id:
                raise DesktopLifecycleError(
                    "App Server production task lost its configured project association"
                )
            started = production_client.start_turn(
                thread_id=thread_id,
                prompt=descriptor.prompt,
                effort=(
                    descriptor.thinking
                    if descriptor.thinking
                    else None
                ),
                client_user_message_id=str(session["client_user_message_id"]),
                skill_name=cfg.skill_name,
                skill_path=cfg.skill_path,
                cwd=cfg.root,
                permission_profile=cfg.desktop.permission_profile,
                model=(
                    descriptor.model
                    if descriptor.model
                    else None
                ),
            )
            turn_id = str((started.get("turn") or {}).get("id") or "")
            if not turn_id:
                raise DesktopLifecycleError("App Server turn/start returned no turn id")
            acknowledge_desktop_send(
                cfg,
                reservation_token,
                thread_id=thread_id,
                relay_executor_thread_id=owner,
            )
            result = production_client.wait_for_turn(
                thread_id,
                turn_id,
                timeout=cfg.desktop.turn_timeout_seconds,
                pause_requested=StateStore(cfg.state_dir).pause_requested,
            )
            completed_turn = dict(result.turn)
            if not completed_turn.get("error") and result.errors:
                completed_turn["error"] = (
                    result.errors[-1].get("error") or result.errors[-1]
                )
        if connected_client is None and (
            client is None or not _client_process_exited(client)
        ):
            raise DesktopLifecycleError(
                "automatic App Server production process did not fully exit"
            )
    except PauseRequested:
        record_desktop_failure(
            cfg,
            reservation_token,
            reason="automatic App Server worker paused",
            definitive=True,
            thread_id=thread_id,
            turn_id=turn_id or None,
            now_epoch=now_epoch,
            reserve_other_ready=False,
            relay_executor_thread_id=owner,
        )
        raise
    except Exception as exc:
        state = StateStore(cfg.state_dir).load()
        current = _session_by_token(state, reservation_token)
        if current.get("status") in {"SEND_RELAYING", "ACTIVE"}:
            rpc_method = exc.method if isinstance(exc, AppServerRpcError) else None
            record_desktop_failure(
                cfg,
                reservation_token,
                reason=str(exc),
                definitive=(rpc_method == "turn/start" or bool(turn_id)),
                thread_id=thread_id,
                turn_id=turn_id or None,
                rate_limited=is_rate_limit_error(getattr(exc, "error", None)),
                now_epoch=now_epoch,
                reserve_other_ready=False,
                relay_executor_thread_id=owner,
            )
        raise DesktopLifecycleError(str(exc)) from exc

    assert completed_turn is not None
    if completed_turn.get("status") != "completed":
        reason = json.dumps(
            completed_turn.get("error") or completed_turn,
            ensure_ascii=False,
            sort_keys=True,
        )
        record_desktop_failure(
            cfg,
            reservation_token,
            reason=f"App Server production turn ended non-completed: {reason}",
            definitive=True,
            thread_id=thread_id,
            turn_id=turn_id,
            rate_limited=is_rate_limit_error(completed_turn.get("error")),
            now_epoch=now_epoch,
            reserve_other_ready=False,
            relay_executor_thread_id=owner,
        )
        raise DesktopLifecycleError(reason)

    # The local dispatcher is authoritative. The worker Stop hook observes an
    # owned automatic turn but never consumes it or starts its successor.
    return complete_desktop_worker(
        cfg,
        thread_id=thread_id,
        turn_id=turn_id,
        final_message=final_agent_message(completed_turn),
        now_epoch=now_epoch,
        dispatcher_reservation_token=reservation_token,
        dispatcher_pid=(os.getpid() if dispatcher_authorized else None),
    )

def record_automatic_app_server_exit(
    cfg: Config,
    reservation_token: str,
    *,
    dispatcher_pid: int,
    at: str | None = None,
) -> None:
    """Journal the per-task App Server full-exit barrier.

    The dispatcher process may continue with a successor, but each task gets a
    fresh App Server subprocess. Recording the barrier before successor
    adoption proves the completed task has no surviving transport writer.
    """

    _require_desktop_owned(cfg)
    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        if session.get("automatic_dispatch_pid") != dispatcher_pid:
            raise DesktopLifecycleError(
                "App Server exit acknowledgement does not match dispatcher ownership"
            )
        if not session.get("completed_at"):
            raise DesktopLifecycleError(
                "App Server exit acknowledgement requires an authoritative completed turn"
            )
        if session.get("app_server_worker_exited_at"):
            return
        session["app_server_worker_exited_at"] = timestamp
        session["automatic_dispatch_connection_pid"] = None
        if session.get("automatic_dispatch_state") == "COMPLETED":
            session["automatic_dispatch_pid"] = None
        _append_event(
            state,
            "app_server_worker_process_exited",
            session,
            timestamp,
            detail="full process exit before successor adoption",
        )
        store.save(state)

def adopt_automatic_dispatcher_successor(
    cfg: Config,
    *,
    completed_reservation_token: str,
    successor_reservation_token: str,
) -> tuple[str, str]:
    """Move the v0.7-style dispatcher loop to its exact reserved successor."""

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        completed = _session_by_token(state, completed_reservation_token)
        successor = _session_by_token(state, successor_reservation_token)
        if (
            completed.get("automatic_dispatch_state") != "ADVANCING"
            or completed.get("automatic_dispatch_pid") != os.getpid()
            or successor_reservation_token
            not in set(completed.get("automatic_successor_tokens") or [])
        ):
            raise DesktopLifecycleError(
                "current dispatcher does not own the completed-to-successor transition"
            )
        if successor.get("status") not in {"CREATE_REQUESTED", "PREPARED"}:
            raise DesktopLifecycleError("automatic successor is not launchable")
        owner = str(successor.get("relay_owner_thread_id") or "")
        if not owner:
            raise DesktopLifecycleError("automatic successor has no causal owner")
        predecessor = next(
            (
                item
                for item in reversed(state.worker_sessions)
                if item.get("thread_id") == owner
                and item.get("status") == "COMPLETED"
                and item.get("turn_id")
            ),
            None,
        )
        if predecessor is None:
            raise DesktopLifecycleError(
                "automatic successor has no completed causal predecessor turn"
            )
        completed["automatic_dispatch_state"] = "COMPLETED"
        completed["automatic_dispatch_pid"] = None
        successor["automatic_dispatch_state"] = "RUNNING"
        successor["automatic_dispatch_pid"] = os.getpid()
        successor["automatic_dispatch_adopted_at"] = utc_now()
        store.save(state)
        return owner, str(predecessor["turn_id"])

def claim_automatic_app_server_turn(
    cfg: Config,
    reservation_token: str,
    *,
    relay_executor_thread_id: str,
    dispatcher_pid: int | None = None,
) -> LaunchDescriptor:
    """Claim the one production ``turn/start`` for the local dispatcher."""

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session.get("status") != "PREPARED":
            raise DesktopLifecycleError(
                "automatic production turn was already claimed or creation is incomplete"
            )
        if session.get("creation_transport") != "app_server_thread_start":
            raise DesktopLifecycleError(
                "automatic production requires an App Server-created task"
            )
        if _thread_cwd({"cwd": session.get("actual_cwd")}) != cfg.root:
            raise DesktopLifecycleError(
                "automatic production requires the canonical task cwd"
            )
        retained_connection = bool(
            _dispatcher_owns_reservation(session, dispatcher_pid=dispatcher_pid)
            and session.get("automatic_dispatch_connection_pid") == dispatcher_pid
        )
        creator_gone = _creator_process_is_gone(session)
        if (
            not session.get("app_server_create_exited_at")
            and not retained_connection
            and not creator_gone
        ):
            raise DesktopLifecycleError(
                "automatic production requires either the v0.7 dispatcher connection "
                "or the legacy creator process exit barrier"
            )
        if creator_gone and not session.get("app_server_create_exited_at"):
            # Барьер существует ради одного: доказать, что создатель больше
            # не пишет в эту ветку. Мёртвый процесс это доказывает не хуже
            # штатной отметки, которую он не успел поставить, упав.
            session["app_server_create_exited_at"] = utc_now()
            _append_event(
                state,
                "app_server_create_exit_inferred",
                session,
                session["app_server_create_exited_at"],
                detail=(
                    "creator pid "
                    f"{session.get('automatic_dispatch_connection_pid')} is gone"
                ),
            )
        session["status"] = "SEND_RELAYING"
        timestamp = utc_now()
        _append_event(state, "automatic_turn_claimed", session, timestamp)
        _append_event(state, "start_requested", session, timestamp)
        state.phase = "AWAITING_APP_SERVER_TURN_ACK"
        store.save(state)
        return LaunchDescriptor.from_dict(dict(session["descriptor"]))
