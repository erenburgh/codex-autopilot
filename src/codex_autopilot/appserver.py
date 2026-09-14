from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Any, Callable

from . import __version__


INITIALIZE_TIMEOUT = 180.0


class AppServerError(RuntimeError):
    pass


class AppServerRpcError(AppServerError):
    def __init__(self, method: str, error: Any) -> None:
        self.method = method
        self.error = error
        super().__init__(f"{method} failed: {json.dumps(error, ensure_ascii=False)}")


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


class ProjectRootDrift(AppServerError):
    """Канонический корень отсутствует среди корней сохранённого проекта.

    R6: это расхождение состояния, а не задача клиента. Прежде оно молча
    чинилось вызовом ``project/update``, то есть рантайм менял сохранённый
    проект пользователя, ничего об этом не сказав. Теперь он отказывает,
    а мутация остаётся отдельным, отдельно разрешённым действием.
    """

    def __init__(self, project_id: str, root: Path, existing: tuple[Path, ...]) -> None:
        self.project_id = project_id
        self.root = root
        self.existing = existing
        shown = ", ".join(str(item) for item in existing) or "none"
        super().__init__(
            f"saved project {project_id} does not contain the canonical root "
            f"{root}; its roots are: {shown}"
        )


class TurnTimeout(AppServerError):
    """Ход модели не уложился в бюджет, хотя App Server отвечал исправно.

    Прежде это место поднимало общий `Timed out waiting for App Server`.
    На живом прогоне v1.0 оно дважды отправило и модель, и человека
    чинить App Server и права, тогда как App Server был здоров: не
    уложился ход модели, запущенный на усилии рабочего воркера.
    """


class ApprovalRequired(AppServerError):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(
            "Worker requested approval. The dispatcher never answers approvals: "
            + json.dumps(payload, ensure_ascii=False)
        )


class PauseRequested(AppServerError):
    pass


@dataclass(slots=True)
class TurnResult:
    thread_id: str
    turn: dict[str, Any]
    errors: list[dict[str, Any]]


# Originator, которым само приложение поднимает свой App Server. Ветка,
# созданная под другим значением, принадлежит "другому приложению": она
# видна в сайдбаре, но требует ручного перехвата, а пайплайн нажать эту
# кнопку не может. Значение взято из самого Codex Desktop, где оно стоит
# значением по умолчанию для CODEX_INTERNAL_ORIGINATOR_OVERRIDE.
DESKTOP_ORIGINATOR = "Codex Desktop"


class AppServerClient:
    """Short-lived or explicitly headless JSON-RPC client for App Server.

    A hook-owned dispatcher may use one client process for a bounded
    Desktop-owned production turn. ``thread/unsubscribe`` only removes this
    connection's subscription; the server may retain a last-subscriber thread
    during its inactivity grace period, so process exit is the lifecycle
    barrier and no Desktop ownership handoff is inferred from unsubscribe.
    """

    def __init__(
        self,
        binary: str,
        log_path: Path,
        *,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
        originator: str | None = DESKTOP_ORIGINATOR,
        popen_factory=subprocess.Popen,
    ) -> None:
        self.binary = binary
        self.log_path = log_path
        self.event_sink = event_sink
        self.originator = originator
        self.popen_factory = popen_factory
        self.proc = None
        self.log = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.pending_events: deque[dict[str, Any]] = deque()
        self.stderr_lines: queue.Queue[str] = queue.Queue()
        self.next_id = 1
        self.errors: list[dict[str, Any]] = []
        self.subscribed_thread_ids: set[str] = set()

    def connect(self) -> dict[str, Any]:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("a", encoding="utf-8")
        process_env = None
        if self.originator:
            process_env = dict(os.environ)
            process_env["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"] = self.originator
        self.proc = self.popen_factory(
            [self.binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=process_env,
        )
        if not self.proc.stdin or not self.proc.stdout or not self.proc.stderr:
            raise AppServerError("App Server stdio pipes were not created")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            result = self._initialize_request()
        except AppServerError as exc:
            if "Timed out waiting for App Server" not in str(exc):
                raise
            raise AppServerError(
                f"App Server did not answer `initialize` within {INITIALIZE_TIMEOUT:.0f} s. "
                "Это отказ рукопожатия, а не отказ в правах: запрашивать доступ к "
                "CODEX_HOME и повторять команду бесполезно. Обычная причина - "
                "установленный плагин или skill, который App Server не может "
                "загрузить; его жалобы видны в stderr ниже. "
                f"Исходный текст: {exc}"
            ) from exc
        self.notify("initialized")
        return result

    def _initialize_request(self) -> dict[str, Any]:
        return self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-autopilot",
                    "title": "Codex Autopilot Desktop Native",
                    # Версия берётся из пакета, а не из прибитой строки.
                    # Замерено на живом сервере: userAgent сообщал
                    # "codex-autopilot; 0.8.0-beta", когда установлена была
                    # 0.8.2. Ровно эта ошибка чинилась в 0.8.1 у
                    # MCP-сервера памяти - здесь она осталась второй копией.
                    "version": __version__,
                },
                "capabilities": {
                    "experimentalApi": True,
                    "mcpServerOpenaiFormElicitation": True,
                },
            },
            # Холодный старт App Server загружает каждый установленный плагин и
            # каждый skill, включая чужие и сломанные. Замерено на живой машине:
            # один плагин с невалидным YAML растянул рукопожатие за стандартные
            # 60 секунд, preflight упал на "Timed out waiting for App Server", а
            # модель приняла таймаут за отказ в правах. Бюджет рукопожатия
            # отделён от обычного запроса именно поэтому.
            timeout=INITIALIZE_TIMEOUT,
        )

    def _record(self, direction: str, payload: Any) -> None:
        if self.log:
            self.log.write(json.dumps({"at": time.time(), "direction": direction, "payload": _redact_log_payload(payload)}, ensure_ascii=False) + "\n")
            self.log.flush()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            raw = raw.rstrip("\n")
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                self._record("stdout-non-json", raw)
                continue
            self._record("received", message)
            self.messages.put(message)

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            line = raw.rstrip("\n")
            self._record("stderr", line)
            self.stderr_lines.put(line)

    def send(self, payload: dict[str, Any]) -> None:
        if self.proc is None or self.proc.poll() is not None or not self.proc.stdin:
            raise AppServerError("App Server is not running")
        self._record("sent", payload)
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.send(payload)

    def request(self, method: str, params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        deferred: list[dict[str, Any]] = []
        try:
            while True:
                message = self._get(deadline)
                if message.get("id") == request_id and "method" not in message:
                    if "error" in message:
                        raise AppServerRpcError(method, message["error"])
                    return message.get("result") or {}
                if "method" in message:
                    if "id" in message:
                        self._inspect_event(message)
                    else:
                        self.pending_events.append(message)
                elif "id" in message:
                    deferred.append(message)
        finally:
            for message in deferred:
                self.messages.put(message)

    def _get(self, deadline: float, maximum_wait: float | None = None) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerError("Timed out waiting for App Server")
        poll_deadline = min(deadline, time.monotonic() + maximum_wait) if maximum_wait is not None else deadline
        last_empty: queue.Empty | None = None
        while time.monotonic() < poll_deadline:
            try:
                # Short slices surface an App Server process exit quickly.
                return self.messages.get(timeout=min(0.25, poll_deadline - time.monotonic()))
            except queue.Empty as exc:
                last_empty = exc
                if self.proc is not None and self.proc.poll() is not None:
                    details: list[str] = []
                    while not self.stderr_lines.empty():
                        details.append(self.stderr_lines.get_nowait())
                    raise AppServerError(
                        f"App Server exited with code {self.proc.returncode}; stderr="
                        + " | ".join(details[-10:])
                    ) from exc
        if maximum_wait is not None and poll_deadline < deadline:
            raise TimeoutError from last_empty
        details: list[str] = []
        while not self.stderr_lines.empty():
            details.append(self.stderr_lines.get_nowait())
        raise AppServerError("Timed out waiting for App Server; stderr=" + " | ".join(details[-10:])) from last_empty

    def _inspect_event(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if "id" in message and method:
            raise ApprovalRequired(message)
        if method == "error":
            self.errors.append(params)
        if self.event_sink and method:
            self.event_sink(method, params)

    def wait_for_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        timeout: float,
        pause_requested: Callable[[], bool] | None = None,
        what: str = "model turn",
    ) -> TurnResult:
        deadline = time.monotonic() + timeout
        deferred: list[dict[str, Any]] = []
        try:
            while True:
                if pause_requested and pause_requested():
                    try:
                        self.interrupt_turn(thread_id, turn_id)
                    finally:
                        raise PauseRequested("Pause requested")
                try:
                    message = self.pending_events.popleft() if self.pending_events else self._get(deadline, maximum_wait=1)
                except TimeoutError:
                    continue
                except AppServerError as exc:
                    if "Timed out waiting for App Server" not in str(exc):
                        raise
                    raise TurnTimeout(
                        f"{what} did not finish within {timeout:g}s on thread "
                        f"{thread_id}. App Server answered throughout, so this is the "
                        f"model turn and not the transport. Detail: {exc}"
                    ) from exc
                self._inspect_event(message)
                params = message.get("params") or {}
                if message.get("method") == "turn/completed" and params.get("threadId") == thread_id and (params.get("turn") or {}).get("id") == turn_id:
                    return TurnResult(thread_id, params["turn"], list(self.errors))
                if "id" in message and "method" not in message:
                    deferred.append(message)
        finally:
            for message in deferred:
                self.messages.put(message)

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=15)

    def list_permission_profiles(self, cwd: Path) -> list[dict[str, Any]]:
        return self.request("permissionProfile/list", {"cwd": str(cwd)}).get("data", [])

    def list_hooks(self, cwd: Path) -> list[dict[str, Any]]:
        """Read the supported lifecycle-hook inventory for one canonical cwd."""
        return self.request("hooks/list", {"cwds": [str(cwd)]}).get("data", [])

    def start_thread(
        self,
        *,
        cwd: Path,
        permission_profile: str,
        project_id: str | None,
        model: str | None,
        plugin_root: Path | None = None,
        ephemeral: bool = False,
        project_memory: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": str(cwd), "permissions": permission_profile, "ephemeral": ephemeral}
        if project_memory:
            if plugin_root is None:
                raise AppServerError("installed plugin root is required for Project Memory")
            resolved_plugin = plugin_root.expanduser().resolve()
            manifest = resolved_plugin / ".codex-plugin" / "plugin.json"
            mcp_config = resolved_plugin / ".mcp.json"
            if not manifest.is_file() or not mcp_config.is_file():
                raise AppServerError(f"invalid installed plugin root: {resolved_plugin}")
            plugin_name = str(json.loads(manifest.read_text(encoding="utf-8")).get("name") or "")
            if not plugin_name:
                raise AppServerError(f"installed plugin manifest has no name: {manifest}")
            # Use the normally installed plugin server. A raw thread config
            # loses plugin provenance, while a thread-selected capability is
            # intentionally ineligible for persistent approval in Codex core.
            # The installed .mcp.json omits cwd, so App Server binds the local
            # stdio process to this thread's canonical workspace root.
            params["runtimeWorkspaceRoots"] = [str(cwd)]
        if project_id is not None:
            params["projectId"] = project_id
        # threadSource намеренно не передаётся и не принимается. Значение
        # agent_created_thread помечало задачу созданной другим приложением
        # и требовало ручного перехвата; v0.7 не передавала его вовсе.
        if model is not None:
            params["model"] = model
        result = self.request("thread/start", params)
        thread_id = (result.get("thread") or {}).get("id")
        if isinstance(thread_id, str):
            self.subscribed_thread_ids.add(thread_id)
        return result

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        """Load a Desktop-created worker slot without changing its persisted cwd."""
        result = self.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        self.subscribed_thread_ids.add(thread_id)
        return result

    def unsubscribe_thread(self, thread_id: str) -> dict[str, Any]:
        """Remove this connection's subscription, without promising an unload."""
        result = self.request("thread/unsubscribe", {"threadId": thread_id}, timeout=15)
        self.subscribed_thread_ids.discard(thread_id)
        return result

    def list_mcp_server_status(self, thread_id: str) -> list[dict[str, Any]]:
        return self.request(
            "mcpServerStatus/list",
            {"threadId": thread_id, "detail": "full", "limit": 100},
        ).get("data", [])

    def call_mcp_tool(self, thread_id: str, server: str, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request(
            "mcpServer/tool/call",
            {"threadId": thread_id, "server": server, "tool": tool, "arguments": arguments or {}},
        )

    def name_thread(self, thread_id: str, name: str) -> None:
        self.request("thread/name/set", {"threadId": thread_id, "name": name})

    def assign_thread_to_project(
        self,
        thread_id: str,
        project_id: str,
    ) -> dict[str, Any]:
        """Persistently assign an already-created thread to a saved project."""

        if not project_id:
            raise AppServerError("project assignment requires a non-empty project id")
        result = self.request(
            "thread/metadata/update",
            {"threadId": thread_id, "projectId": project_id},
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerError(
                "thread/metadata/update returned no thread metadata"
            )
        return thread

    def archive_thread(self, thread_id: str) -> None:
        self.request("thread/archive", {"threadId": thread_id})

    def respond_project_memory_approval(self, request: dict[str, Any], *, persist: str) -> None:
        """Answer one already-surfaced MCP request after explicit user consent."""
        if request.get("method") != "mcpServer/elicitation/request" or "id" not in request:
            raise AppServerError("not an MCP elicitation request")
        if persist not in {"session", "always"}:
            raise AppServerError("MCP approval persistence must be session or always")
        params = request.get("params") or {}
        meta = params.get("_meta") or {}
        advertised = meta.get("persist")
        allowed = {advertised} if isinstance(advertised, str) else set(advertised or [])
        if persist not in allowed:
            raise AppServerError(
                f"MCP request does not advertise {persist!r} persistence; advertised={sorted(allowed)}"
            )
        self.send({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {
                "action": "accept",
                "content": None,
                "_meta": {"persist": persist},
            },
        })

    def start_turn(
        self,
        *,
        thread_id: str,
        prompt: str,
        effort: str | None,
        client_user_message_id: str,
        skill_name: str,
        skill_path: Path,
        cwd: Path,
        permission_profile: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        self.errors = []
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [
                {"type": "text", "text": prompt},
                {"type": "skill", "name": skill_name, "path": str(skill_path)},
            ],
            "clientUserMessageId": client_user_message_id,
        }
        if effort is not None:
            params["effort"] = effort
        params["cwd"] = str(cwd)
        params["runtimeWorkspaceRoots"] = [str(cwd)]
        if permission_profile is not None:
            params["permissions"] = permission_profile
        if model is not None:
            params["model"] = model
        return self.request("turn/start", params)

    def start_plain_turn(
        self,
        *,
        thread_id: str,
        prompt: str,
        effort: str | None,
        client_user_message_id: str,
        cwd: Path,
        permission_profile: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Start a model turn without injecting the production worker skill."""
        self.errors = []
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "clientUserMessageId": client_user_message_id,
        }
        if effort is not None:
            params["effort"] = effort
        params["cwd"] = str(cwd)
        params["runtimeWorkspaceRoots"] = [str(cwd)]
        if permission_profile is not None:
            params["permissions"] = permission_profile
        if model is not None:
            params["model"] = model
        return self.request("turn/start", params)

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        return self.request("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]


    def list_projects(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.request("project/list", params)
            result.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return result

    def read_project(self, project_id: str) -> dict[str, Any]:
        return self.request("project/read", {"projectId": project_id})["project"]

    def verify_project_root(self, project_id: str, root: Path) -> dict[str, Any]:
        """Read ``project_id`` and require ``root`` to be one of its roots.

        Read-only by construction: it raises :class:`ProjectRootDrift` instead
        of repairing the saved project. This verifies the App Server project
        namespace only. Membership here does not prove that the Electron saved
        project's ``rootPaths`` or the Desktop sidebar assignment match; those
        are checked independently.
        """

        canonical_root = root.expanduser().resolve()
        project = self.read_project(project_id)
        if str(project.get("id") or "") != project_id:
            raise AppServerError("project/read returned an unexpected project")
        roots = project.get("roots")
        if not isinstance(roots, list):
            raise AppServerError("project/read returned invalid project roots")
        existing_paths = [
            Path(str(item.get("path"))).expanduser().resolve()
            for item in roots
            if isinstance(item, dict) and item.get("path")
        ]
        # Членство - вложенность, а не равенство: ровно то правило, по
        # которому проект выбирается в preflight (``_project_contains``).
        # Прежняя проверка на равенство считала расхождением обычный
        # случай, когда канонический каталог лежит внутри корня проекта,
        # и дописывала туда ещё один корень при каждом создании.
        if any(_is_within(canonical_root, item) for item in existing_paths):
            return project
        raise ProjectRootDrift(project_id, canonical_root, tuple(existing_paths))

    def ensure_project_root(
        self,
        project_id: str,
        root: Path,
        *,
        authorized: bool = False,
    ) -> dict[str, Any]:
        """Verify membership; add the root only when ``authorized`` is true.

        R6 fails closed. ``authorized`` is not a convenience flag: the caller
        passes it only after finding a recorded user Decision that names this
        exact project and root. Without one, the drift surfaces as
        :class:`ProjectRootDrift` and nothing is written.
        """

        try:
            return self.verify_project_root(project_id, root)
        except ProjectRootDrift as drift:
            if not authorized:
                raise
            canonical_root = drift.root
            existing_paths = list(drift.existing)
        updated_roots = [
            {"path": str(path)} for path in (*existing_paths, canonical_root)
        ]
        updated = self.request(
            "project/update",
            {"projectId": project_id, "roots": updated_roots},
        ).get("project")
        if not isinstance(updated, dict):
            raise AppServerError("project/update returned no project")
        updated_paths = {
            Path(str(item.get("path"))).expanduser().resolve()
            for item in updated.get("roots") or []
            if isinstance(item, dict) and item.get("path")
        }
        if str(updated.get("id") or "") != project_id or canonical_root not in updated_paths:
            raise AppServerError("project/update did not preserve the canonical root")
        return updated

    def rate_limits(self) -> dict[str, Any]:
        return self.request("account/rateLimits/read", {})

    def list_models(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"includeHidden": True, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.request("model/list", params)
            result.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return result

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            for thread_id in tuple(self.subscribed_thread_ids):
                try:
                    self.unsubscribe_thread(thread_id)
                except AppServerError:
                    # Process termination remains the final ownership release.
                    pass
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.subscribed_thread_ids.clear()
        if self.log:
            self.log.close()
            self.log = None

    def __enter__(self) -> "AppServerClient":
        self.connect()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


RATE_LIMIT_CODES = {
    "usageLimitExceeded", "rateLimitExceeded", "sessionBudgetExceeded", "rate_limit_reached",
    "workspace_owner_credits_depleted", "workspace_member_credits_depleted",
    "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached",
}


def is_rate_limit_error(error: Any) -> bool:
    if isinstance(error, dict):
        if error.get("codexErrorInfo") in RATE_LIMIT_CODES or error.get("rateLimitReachedType") in RATE_LIMIT_CODES:
            return True
        return any(is_rate_limit_error(value) for value in error.values())
    if isinstance(error, list):
        return any(is_rate_limit_error(value) for value in error)
    return error in RATE_LIMIT_CODES


def rate_limit_reset_at(snapshot: dict[str, Any]) -> int | None:
    buckets = snapshot.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = snapshot.get("rateLimits")
        buckets = {"legacy": legacy} if isinstance(legacy, dict) else {}
    resets: list[int] = []
    for bucket in buckets.values():
        if not isinstance(bucket, dict):
            continue
        windows = [bucket.get("primary"), bucket.get("secondary")]
        exhausted = [
            window for window in windows
            if isinstance(window, dict) and window.get("usedPercent", 0) >= 100
        ]
        reached = bucket.get("rateLimitReachedType") in RATE_LIMIT_CODES or bucket.get("spendControlReached") is True
        if exhausted:
            for window in exhausted:
                if isinstance(window.get("resetsAt"), int):
                    resets.append(window["resetsAt"])
        elif reached:
            # Some App Server versions identify a reached bucket without saying
            # which window caused it. In that ambiguous case, wait for every
            # reported window to reset rather than retrying too early.
            for window in windows:
                if isinstance(window, dict) and isinstance(window.get("resetsAt"), int):
                    resets.append(window["resetsAt"])
        if exhausted or reached:
            individual = bucket.get("individualLimit")
            if isinstance(individual, dict) and isinstance(individual.get("resetsAt"), int):
                resets.append(individual["resetsAt"])
    return max(resets) if resets else None


def final_agent_message(turn: dict[str, Any]) -> str:
    final = ""
    for item in turn.get("items", []):
        if item.get("type") == "agentMessage" and isinstance(item.get("text"), str) and item.get("phase") == "final_answer":
            final = item["text"]
    return final


_SENSITIVE_LOG_KEYS = {
    "text",
    "content",
    "arguments",
    "structuredContent",
    "user_instruction",
    "environment_probe",
}


def _redact_log_payload(value: Any, key: str | None = None) -> Any:
    """Keep protocol diagnostics without copying prompts or memory payloads to logs."""
    if key in _SENSITIVE_LOG_KEYS:
        if isinstance(value, str):
            return f"<redacted chars={len(value)} sha256={hashlib.sha256(value.encode()).hexdigest()[:16]}>"
        return "<redacted>"
    if isinstance(value, dict):
        return {item_key: _redact_log_payload(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_log_payload(item) for item in value]
    return value
