from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable
import uuid

from .appserver import (
    AppServerClient,
    AppServerError,
    ApprovalRequired,
    TurnTimeout,
    final_agent_message,
)
from .config import DESKTOP_OWNED_SURFACE
from .hook_trust import (
    HookPreflightError,
    HookTrustApprovalRequired,
    require_trusted_stop_hook,
)
from .memory import MemoryError, probe_sqlite_fts5
from .models import resolve_selection
from .plan import Plan
from .project_association import (
    ProjectAssociationError,
    require_desktop_project_root,
    resolve_preflight_project,
)


MEMORY_SERVER_NAME = "codex_autopilot_memory"
REQUIRED_MEMORY_TOOLS = {"memory"}
MEMORY_PREFLIGHT_TITLE = "Codex Autopilot Preflight · Project Memory"
# Проба доверия не рассуждает: она делает один вызов инструмента и
# отвечает одной строкой. Прежде она шла на усилии рабочего воркера -
# на прогоне v1.0 это был xhigh, и ход дважды не уложился в пять минут,
# а на третий раз прошёл меньше чем за минуту. Autopilot ограничивает
# ЛЕСТНИЦУ ВОРКЕРОВ значениями medium..max; App Server принимает и
# minimal, и low, а проба воркером не является.
PROBE_REASONING = "low"
PROBE_TIMEOUT = 300.0
# Один таймаут не повод валить весь запуск: третий заход показал, что
# повтор решает. Прежде первый же валил.
PROBE_ATTEMPTS = 3

ANNOUNCEMENT = (
    "За весь запуск у вас могут спросить один раз, и только про одно: доверие "
    f"инструменту памяти `codex_autopilot_memory.memory` в отдельной задаче "
    f"«{MEMORY_PREFLIGHT_TITLE}». Ответ — кнопкой в этой задаче. Больше preflight "
    "ничего не спрашивает и ничего не ждёт от вас молча."
)
MEMORY_PREFLIGHT_OK = "MEMORY_PREFLIGHT_OK"
MEMORY_PREFLIGHT_PROMPT = f"""Codex Autopilot Project Memory trust preflight.

Perform exactly one harmless read-only call to the built-in MCP server
`codex_autopilot_memory`, tool `memory`, with `operation=current`.
Do not inspect or modify project files. Do not call any other tool.
After the MCP call completes, reply exactly: {MEMORY_PREFLIGHT_OK}
"""


class PreflightError(RuntimeError):
    pass


class PreflightApprovalRequired(PreflightError):
    exit_code = 77

    def __init__(self, codex_home: Path, detail: str) -> None:
        self.codex_home = codex_home
        self.detail = detail
        super().__init__(
            "Worker access: APPROVAL REQUIRED\n\n"
            f"Codex Autopilot needs one-time read/write access to the Codex App Server state directory: {codex_home}\n"
            "The official `codex app-server` process owns its SQLite state, WAL/SHM files, locks, plugin cache, and temporary wrappers there. "
            "Autopilot does not read those databases directly. Approve this exact directory through Codex's normal permission request, then rerun the start command.\n"
            f"App Server detail: {detail}"
        )


class ProjectMemoryApprovalRequired(PreflightError):
    exit_code = 77

    def __init__(self, thread_id: str, title: str) -> None:
        self.thread_id = thread_id
        self.title = title
        super().__init__(
            "Project Memory MCP: APPROVAL REQUIRED\n\n"
            f"No production worker or new run-state was created. Diagnostic preflight task: `{title}` (thread {thread_id}). "
            "Ask the user whether to approve `codex_autopilot_memory.memory` with Always. "
            "Only after explicit user confirmation, repeat the same command with "
            "`--approve-project-memory-always`; it answers this one App Server request through the supported flow. "
            "Autopilot never grants, infers, or bypasses approval on its own."
        )


@dataclass(slots=True)
class PreflightResult:
    project: Path
    codex_binary: str
    codex_home: Path | None = None
    checks: list[tuple[str, str, str]] = field(default_factory=list)
    next_model: str = "Host default"
    next_reasoning: str = "Host default"
    routing: str = "HOST SETTINGS"
    project_id: str | None = None
    desktop_project_id: str | None = None
    project_name: str | None = None
    project_source: str | None = None
    memory_preflight_thread_id: str | None = None

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append((name, status, detail))

    def lines(self) -> list[str]:
        values = ["Codex Autopilot preflight", "", f"Project: {self.project}"]
        values.extend(f"{name}: {status}" + (f" — {detail}" if detail else "") for name, status, detail in self.checks)
        values.extend([f"Routing: {self.routing}", f"Next worker: {self.next_model} / {self.next_reasoning}"])
        return values


def default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser().resolve()


def _looks_like_codex_home_denial(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "failed to initialize sqlite state runtime",
        "failed to initialize state runtime",
        "operation not permitted",
        "permission denied",
        "sandbox",
    )
    return any(marker in text for marker in markers)


def run_preflight(
    root: Path,
    *,
    plan: Plan,
    profile: str,
    skill_path: Path,
    binary: str = "codex",
    client_factory: Callable[..., Any] = AppServerClient,
    emit: Callable[[str], None] | None = print,
    replace: bool = False,
    approve_project_memory_always: bool = False,
    app_server_project_id: str | None = None,
    desktop_project_id: str | None = None,
) -> PreflightResult:
    project = root.expanduser().resolve()
    codex_binary = shutil.which(binary) if not Path(binary).is_absolute() else binary
    result = PreflightResult(
        project=project,
        codex_binary=codex_binary or binary,
        desktop_project_id=desktop_project_id,
    )

    def report(name: str, status: str, detail: str = "") -> None:
        result.add(name, status, detail)
        if emit:
            emit(f"{name}: {status}" + (f" — {detail}" if detail else ""))

    if emit:
        emit("Codex Autopilot preflight")
        emit("")
        # Единственное место прогона, где может понадобиться человек, названо до
        # первой длинной проверки. Без этой строки пользователь видит десять
        # "OK", потом тишину, и не знает, что решение ждут от него и в другой
        # задаче: замерено - полчаса "думаю" при том, что диалог висел рядом.
        emit(ANNOUNCEMENT)
        emit("")
        emit(f"Project: {project}")
    if not project.is_dir():
        report("Project", "FAIL", "target directory does not exist")
        raise PreflightError(f"target project directory does not exist: {project}")
    report("Target", "OK", "explicit project root resolved")
    if not (project / ".git").exists():
        report("Git", "FAIL", "existing repository required")
        raise PreflightError("Codex Autopilot requires an existing Git repository; it does not run `git init` or create commits automatically")
    report("Git", "OK", "existing repository")
    if not codex_binary:
        report("Runtime", "FAIL", "official Codex CLI not found")
        raise PreflightError("official Codex CLI/App Server is not installed")
    if not skill_path.is_file():
        report("Runtime", "FAIL", f"installed worker skill missing: {skill_path}")
        raise PreflightError(f"installed worker skill is missing: {skill_path}")
    report("Runtime", "OK", f"{codex_binary}; {skill_path}")

    log_path = Path(tempfile.gettempdir()) / f"codex-autopilot-preflight-{os.getpid()}.jsonl"
    client = client_factory(codex_binary, log_path)
    probe_thread_id: str | None = None
    probe_thread_ids: list[str] = []
    try:
        try:
            initialized = client.connect()
        except AppServerError as exc:
            if _looks_like_codex_home_denial(exc):
                report("App Server", "APPROVAL REQUIRED", str(exc))
                raise PreflightApprovalRequired(default_codex_home(), str(exc)) from exc
            report("App Server", "FAIL", str(exc))
            raise PreflightError(f"official Codex App Server is unavailable: {exc}") from exc
        codex_home = Path(str(initialized.get("codexHome") or default_codex_home())).expanduser().resolve()
        result.codex_home = codex_home
        report("App Server", "OK", str(initialized.get("userAgent") or codex_home))

        if not desktop_project_id:
            detail = (
                f"каталог {project} не принадлежит ни одному проекту Codex. "
                "Открой проект Codex и запусти Autopilot в нём: каждая "
                "задача создаётся внутри проекта, и без него её не видно."
            )
            report("Desktop project", "FAIL", detail)
            raise PreflightError(detail)
        if desktop_project_id:
            try:
                desktop_roots = require_desktop_project_root(
                    codex_home,
                    desktop_project_id,
                    project,
                )
            except ProjectAssociationError as exc:
                report("Desktop project rootPaths", "FAIL", str(exc))
                raise PreflightError(
                    f"Desktop project root mismatch: {exc}"
                ) from exc
            if desktop_roots is not None:
                report(
                    "Desktop project rootPaths",
                    "OK",
                    ", ".join(str(item) for item in desktop_roots),
                )

        plugin_root = installed_plugin_root(skill_path)
        expected_plugin_id = installed_plugin_id(plugin_root)
        # Проверка доверия Stop-хуку безусловна. Прежде она зависела от
        # аргументов, которые в продукте всегда истинны, - то есть условие
        # ничего не выбирало, но допускало зелёный префлайт при
        # недоверенном хуке. Запуск принадлежит именно этому хуку: без
        # доверия прогон не стартует, и зелёный префлайт был бы ложным.
        try:
            stop_hook = require_trusted_stop_hook(
                client,
                project,
                plugin_id=expected_plugin_id,
            )
        except HookTrustApprovalRequired as exc:
            report("Autopilot Stop hook", "APPROVAL REQUIRED", str(exc))
            raise
        except HookPreflightError as exc:
            report("Autopilot Stop hook", "FAIL", str(exc))
            raise PreflightError(str(exc)) from exc
        report(
            "Autopilot Stop hook",
            "OK",
            f"{stop_hook.plugin_id}; {stop_hook.trust_status}; {stop_hook.current_hash}",
        )

        try:
            saved_project, project_source = resolve_preflight_project(
                project,
                client.list_projects(),
                explicit_project_id=app_server_project_id,
            )
        except ProjectAssociationError as exc:
            report("Codex project metadata", "FAIL", str(exc))
            raise PreflightError(f"Codex project association is ambiguous: {exc}") from exc
        if saved_project:
            # This is App Server project metadata. Desktop sidebar placement is
            # checked independently against the local project's real rootPaths.
            result.project_id = str(saved_project["id"])
            result.project_name = str(saved_project.get("name") or saved_project["id"])
            result.project_source = project_source
            report(
                "Codex project metadata",
                "OK",
                f"{result.project_name} ({saved_project['id']}) via {project_source}; worker cwd remains {project}",
            )
        else:
            report(
                "Codex project metadata",
                "NONE",
                "no saved project contains the target root; task stays in Recents with canonical cwd",
            )

        if desktop_project_id:
            # Заранее созданные слоты были обходом вокруг мнимой
            # невозможности завести видимую задачу через App Server.
            # Замерено: thread/start с originator самого приложения даёт
            # задачу, видимую в сайдбаре проекта - шесть воркеров приёмки
            # 0.8.0 все оказались INSIDE. Механизм слотов снят в 0.8.1.
            report(
                "Desktop UI placement",
                "OK",
                f"dispatcher creates its own visible task in project {desktop_project_id}",
            )
        else:
            report(
                "Desktop UI placement",
                "TASKS/RECENTS",
                "no Desktop project was supplied; the task stays in Recents",
            )

        selection = None
        if profile == "adaptive":
            first = plan.milestones[0]
            selection = resolve_selection(
                client.list_models(),
                strategy=plan.model_strategy,
                execution_mode=first.execution_mode,
                requested_reasoning=first.reasoning or "medium",
                execution_reason=first.execution_mode_reason,
            )
            result.routing = "AUTO" if plan.model_strategy == "auto" else plan.model_strategy.upper()
            result.next_model = selection.display_name
            result.next_reasoning = selection.reasoning
        else:
            result.routing = "HOST SETTINGS"
            result.next_model = "Host default"
            result.next_reasoning = "Host default (no override)"

        try:
            profiles = client.list_permission_profiles(project)
            allowed = {entry.get("id") for entry in profiles if entry.get("allowed") is not False}
            if ":workspace" not in allowed:
                raise PreflightError(f":workspace permission profile unavailable; allowed={sorted(str(item) for item in allowed)}")
            create_project_id = (
                result.project_id
                if result.project_source in {"target", "explicit target"}
                else None
            )
            probe_thread = client.start_thread(
                cwd=project,
                permission_profile=":workspace",
                project_id=create_project_id,
                model=selection.model_id if selection else None,
                plugin_root=plugin_root,
                ephemeral=False,
                project_memory=True,
            )
            active = probe_thread.get("activePermissionProfile") or {}
            thread = probe_thread.get("thread") or {}
            if active.get("id") != ":workspace":
                raise PreflightError("App Server did not apply :workspace to the preflight thread")
            if Path(str(thread.get("cwd") or "")).resolve() != project:
                raise PreflightError("App Server did not preserve target project cwd")
            probe_thread_id = str(thread["id"])
            probe_thread_ids.append(probe_thread_id)
            result.memory_preflight_thread_id = probe_thread_id
            client.name_thread(probe_thread_id, MEMORY_PREFLIGHT_TITLE)
            if result.project_id and thread.get("projectId") != result.project_id:
                client.assign_thread_to_project(probe_thread_id, result.project_id)
                thread = client.read_thread(probe_thread_id)
            if result.project_id and thread.get("projectId") != result.project_id:
                raise PreflightError(
                    "App Server did not preserve the intended Codex project association"
                )
            report("Worker access", "OK", f":workspace at {project}")
        except PreflightError:
            raise
        except Exception as exc:
            if _looks_like_codex_home_denial(exc):
                report("Worker access", "APPROVAL REQUIRED", str(exc))
                raise PreflightApprovalRequired(codex_home, str(exc)) from exc
            report("Worker access", "FAIL", str(exc))
            raise PreflightError(f"worker access preflight failed: {exc}") from exc

        memory_probe = probe_sqlite_fts5(project)
        statuses = client.list_mcp_server_status(probe_thread_id)
        memory_status = next((item for item in statuses if item.get("name") == MEMORY_SERVER_NAME), None)
        if not memory_status:
            raise PreflightError(
                "built-in Project Memory MCP is not present in the active plugin. Reinstall Codex Autopilot v0.8 and start a fresh Codex task."
            )
        if memory_status.get("pluginId") != expected_plugin_id:
            raise PreflightError(
                "Project Memory MCP lost installed-plugin provenance; refusing a raw or transient capability server"
            )
        if memory_status.get("runtimeStatus") != "connected":
            failure = next(
                (
                    memory_status.get(key)
                    for key in ("error", "failureReason", "startupError")
                    if memory_status.get(key)
                ),
                memory_status.get("runtimeStatus"),
            )
            raise PreflightError(f"Project Memory MCP failed to start: {failure}")
        tools = set((memory_status.get("tools") or {}).keys())
        missing_tools = sorted(REQUIRED_MEMORY_TOOLS - tools)
        if missing_tools:
            raise PreflightError(f"Project Memory MCP tool contract is incomplete: {missing_tools}")
        memory_identity = client.call_mcp_tool(probe_thread_id, MEMORY_SERVER_NAME, "memory", {"operation": "current"})
        identity = memory_identity.get("structuredContent") or {}
        if Path(str(identity.get("project_root") or "")).resolve() != project:
            raise PreflightError("Project Memory MCP did not bind to the target project root")
        report("Project Memory transport", "OK", f"SQLite {memory_probe['sqlite']} + FTS5; local stdio MCP connected and target-bound")

        if emit:
            # Пять минут молчания без единого признака жизни - это то,
            # что человек видит как "ветка думает" и не знает, чего ждать.
            emit(
                f"Project Memory MCP: проверяю доверие в задаче «{MEMORY_PREFLIGHT_TITLE}» "
                f"(до {int(PROBE_TIMEOUT)} с на попытку, попыток {PROBE_ATTEMPTS})"
            )
        started_turn = None
        completed = None
        last_timeout: TurnTimeout | None = None
        try:
            for attempt in range(1, PROBE_ATTEMPTS + 1):
                started_turn = client.start_plain_turn(
                    thread_id=probe_thread_id,
                    prompt=MEMORY_PREFLIGHT_PROMPT,
                    effort=PROBE_REASONING,
                    client_user_message_id=str(uuid.uuid4()),
                    cwd=project,
                )
                try:
                    completed = client.wait_for_turn(
                        probe_thread_id,
                        started_turn["turn"]["id"],
                        timeout=PROBE_TIMEOUT,
                        what=f"Project Memory trust probe (attempt {attempt}/{PROBE_ATTEMPTS})",
                    )
                    break
                except TurnTimeout as exc:
                    last_timeout = exc
                    report(
                        "Project Memory MCP",
                        "RETRY",
                        f"attempt {attempt} of {PROBE_ATTEMPTS} did not finish within {int(PROBE_TIMEOUT)}s",
                    )
                    try:
                        client.interrupt_turn(probe_thread_id, started_turn["turn"]["id"])
                    except Exception:
                        # Прерывание - уборка, а не условие. Его отказ не
                        # должен подменять собой причину таймаута.
                        pass
            if completed is None:
                report("Project Memory MCP", "FAIL", str(last_timeout))
                raise PreflightError(str(last_timeout)) from last_timeout
        except ApprovalRequired as exc:
            params = exc.payload.get("params") or {}
            meta = params.get("_meta") or {}
            is_memory = (
                params.get("serverName") == MEMORY_SERVER_NAME
                and meta.get("codex_approval_kind") == "mcp_tool_call"
            )
            if not is_memory:
                raise PreflightError(_unexpected_approval_message(exc)) from exc
            advertised = _approval_persistence_options(exc.payload)
            if "always" not in advertised:
                report(
                    "Project Memory MCP",
                    "FAIL",
                    f"Codex did not advertise persistent Always approval; advertised={sorted(advertised)}",
                )
                raise PreflightError(
                    "Project Memory MCP did not offer supported persistent approval; refusing to bypass approval"
                ) from exc
            if approve_project_memory_always:
                client.respond_project_memory_approval(exc.payload, persist="always")
                report("Project Memory MCP approval", "USER AUTHORIZED", "Always response sent through the pending App Server request")
                completed = client.wait_for_turn(probe_thread_id, started_turn["turn"]["id"], timeout=300)
                _validate_memory_preflight_result(client, probe_thread_id, completed.turn)

                verification = client.start_thread(
                    cwd=project,
                    permission_profile=":workspace",
                    project_id=create_project_id,
                    model=selection.model_id if selection else None,
                    plugin_root=plugin_root,
                    ephemeral=False,
                    project_memory=True,
                )
                verification_active = verification.get("activePermissionProfile") or {}
                verification_thread = verification.get("thread") or {}
                if verification_active.get("id") != ":workspace":
                    raise PreflightError("App Server did not apply :workspace to the fresh persistence-verification task")
                if Path(str(verification_thread.get("cwd") or "")).resolve() != project:
                    raise PreflightError("App Server did not preserve target project cwd for the fresh persistence-verification task")
                verification_thread_id = str(verification_thread["id"])
                probe_thread_ids.append(verification_thread_id)
                client.name_thread(verification_thread_id, f"{MEMORY_PREFLIGHT_TITLE} · verification")
                if result.project_id and verification_thread.get("projectId") != result.project_id:
                    client.assign_thread_to_project(
                        verification_thread_id,
                        result.project_id,
                    )
                    verification_thread = client.read_thread(verification_thread_id)
                if result.project_id and verification_thread.get("projectId") != result.project_id:
                    raise PreflightError("App Server did not preserve project association for the fresh persistence-verification task")

                verification_statuses = client.list_mcp_server_status(verification_thread_id)
                verification_memory = next(
                    (item for item in verification_statuses if item.get("name") == MEMORY_SERVER_NAME),
                    None,
                )
                if not verification_memory or verification_memory.get("runtimeStatus") != "connected":
                    raise PreflightError("Project Memory MCP did not reconnect in the fresh persistence-verification task")
                if verification_memory.get("pluginId") != expected_plugin_id:
                    raise PreflightError("Project Memory MCP lost installed-plugin provenance in the fresh persistence-verification task")
                verification_tools = set((verification_memory.get("tools") or {}).keys())
                if REQUIRED_MEMORY_TOOLS - verification_tools:
                    raise PreflightError("Project Memory MCP contract changed in the fresh persistence-verification task")
                verification_identity = client.call_mcp_tool(
                    verification_thread_id,
                    MEMORY_SERVER_NAME,
                    "memory",
                    {"operation": "current"},
                )
                verification_root = (verification_identity.get("structuredContent") or {}).get("project_root")
                if Path(str(verification_root or "")).resolve() != project:
                    raise PreflightError("Project Memory MCP lost target binding in the fresh persistence-verification task")
                verification_turn = client.start_plain_turn(
                    thread_id=verification_thread_id,
                    prompt=MEMORY_PREFLIGHT_PROMPT,
                    effort=selection.reasoning if selection else None,
                    client_user_message_id=str(uuid.uuid4()),
                    cwd=project,
                )
                try:
                    verified = client.wait_for_turn(
                        verification_thread_id,
                        verification_turn["turn"]["id"],
                        timeout=300,
                    )
                except ApprovalRequired as verification_exc:
                    raise PreflightError(
                        "Project Memory MCP Always approval did not carry to a fresh task; refusing to start M1"
                    ) from verification_exc
                _validate_memory_preflight_result(client, verification_thread_id, verified.turn)
                report(
                    "Project Memory persistence",
                    "OK",
                    f"fresh task {verification_thread_id} completed without another approval",
                )
            else:
                report("Project Memory MCP", "APPROVAL REQUIRED", f"preflight task {probe_thread_id}; no production worker created")
                raise ProjectMemoryApprovalRequired(probe_thread_id, MEMORY_PREFLIGHT_TITLE) from exc
        else:
            _validate_memory_preflight_result(client, probe_thread_id, completed.turn)
        report("Project Memory MCP", "OK", "real model-to-MCP call completed without a trust interruption")

        if selection:
            report("Model metadata", "OK", f"{selection.model_id} supports {selection.reasoning}")
        else:
            report("Model metadata", "OK", "dispatcher will send neither model nor effort")

        if replace:
            retired = _archive_replaced_workers(project, client, exclude=set(probe_thread_ids))
            if retired:
                report("Previous run", "RETIRED", f"archived {len(retired)} worker task(s): {', '.join(retired)}")
        if emit:
            emit(f"Routing: {result.routing}")
            emit(f"Next worker: {result.next_model} / {result.next_reasoning}")
            emit("")
            emit("Preflight: PASS")
        return result
    except MemoryError as exc:
        report("Project Memory", "FAIL", str(exc))
        raise PreflightError(str(exc)) from exc
    finally:
        for thread_id in dict.fromkeys(probe_thread_ids):
            try:
                client.archive_thread(thread_id)
            except Exception:
                pass
        client.close()
        log_path.unlink(missing_ok=True)


def _unexpected_approval_message(exc: BaseException) -> str:
    """Назвать чужой approval человеческим языком, а не сырым JSON.

    Диспетчер не отвечает на approvals ни при каких условиях. Значит любой
    approval, кроме доверия инструменту памяти, здесь - тупик: он висит в
    интерфейсе, preflight его не закроет, и прогон не начнётся. Единственный
    замеренный источник такого тупика - модель, которая сама приложила запрос
    прав к команде `start-skill` и запустила её повторно.
    """
    payload = getattr(exc, "payload", None) or {}
    params = payload.get("params") or {}
    kind = str(params.get("kind") or "unknown")
    reason = str(params.get("reason") or "").strip()
    lines = [
        "preflight остановлен: во время проверки памяти пришёл approval, "
        f"который диспетчер не имеет права закрывать (kind={kind}).",
        "",
        "Диспетчер не отвечает на approvals никогда. Этот запрос не будет "
        "закрыт сам и прогон с ним не начнётся.",
    ]
    if reason:
        lines += ["", f"Текст запроса: {reason}"]
    lines += [
        "",
        "Что делать: не прикладывайте запрос прав к `start-skill` и не "
        "запускайте её повторно ради доступа. Отмените висящий запрос в "
        "интерфейсе и устраните причину, названную предыдущей строкой вывода "
        "preflight.",
    ]
    return "\n".join(lines)

def _validate_memory_preflight_result(client: Any, thread_id: str, turn: dict[str, Any]) -> None:
    # App Server 0.153.4 can emit a partial turn/completed snapshot after an
    # elicitation response even though thread/read already contains the full
    # authoritative item list. Fall back only after validation fails on a
    # completed turn; never turn a failed/incomplete turn into a pass.
    try:
        _validate_memory_preflight_turn(turn)
        return
    except PreflightError:
        if turn.get("status") != "completed":
            raise
    snapshot = client.read_thread(thread_id)
    turn_id = turn.get("id")
    full_turn = next(
        (item for item in snapshot.get("turns") or [] if item.get("id") == turn_id),
        None,
    )
    if full_turn is None:
        raise PreflightError("Project Memory MCP completed turn was missing from thread/read")
    _validate_memory_preflight_turn(full_turn)


def _validate_memory_preflight_turn(turn: dict[str, Any]) -> None:
    if turn.get("status") != "completed":
        raise PreflightError(f"Project Memory MCP preflight turn ended with status={turn.get('status')!r}")
    calls = [
        item
        for item in turn.get("items") or []
        if item.get("type") == "mcpToolCall"
        and item.get("server") == MEMORY_SERVER_NAME
        and item.get("tool") == "memory"
    ]
    if len(calls) != 1 or calls[0].get("status") != "completed":
        raise PreflightError("Project Memory MCP preflight did not complete exactly one memory tool call")
    if final_agent_message(turn).strip() != MEMORY_PREFLIGHT_OK:
        raise PreflightError("Project Memory MCP preflight returned an unexpected final response")


def _archive_replaced_workers(project: Path, client: Any, *, exclude: set[str]) -> list[str]:
    state_path = project / ".codex-autopilot" / "run-state.json"
    if not state_path.is_file():
        return []
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    candidates: list[str] = []
    for value in [raw.get("current_thread_id"), *(raw.get("previous_thread_ids") or [])]:
        if isinstance(value, str) and value not in exclude and value not in candidates:
            candidates.append(value)
    for item in raw.get("worker_history") or []:
        value = item.get("thread_id") if isinstance(item, dict) else None
        if isinstance(value, str) and value not in exclude and value not in candidates:
            candidates.append(value)
    retired: list[str] = []
    for thread_id in candidates:
        try:
            client.archive_thread(thread_id)
        except Exception as exc:
            # Desktop archival can remove the rollout from App Server before
            # the deterministic replace pass reaches it. "No rollout" means
            # the old worker is no longer resumable/active, so it is already
            # retired for duplicate-prevention purposes.
            detail = str(exc).lower()
            if "no rollout found for thread id" in detail or "thread not found" in detail:
                retired.append(thread_id)
                continue
            raise PreflightError(f"could not retire previous worker {thread_id}; refusing duplicate restart: {exc}") from exc
        retired.append(thread_id)
    return retired


def installed_plugin_root(skill_path: Path) -> Path:
    resolved = skill_path.expanduser().resolve()
    for candidate in resolved.parents:
        if (candidate / ".codex-plugin" / "plugin.json").is_file() and (candidate / ".mcp.json").is_file():
            return candidate
    raise PreflightError(f"could not resolve installed plugin root from skill: {resolved}")


def installed_plugin_id(plugin_root: Path) -> str:
    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    name = str(json.loads(manifest.read_text(encoding="utf-8")).get("name") or "")
    if not name:
        raise PreflightError(f"installed plugin manifest has no name: {manifest}")
    return f"{name}@codex-autopilot-local"


def _approval_persistence_options(request: dict[str, Any]) -> set[str]:
    value = (((request.get("params") or {}).get("_meta") or {}).get("persist"))
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {item for item in value if isinstance(item, str)}
    return set()
