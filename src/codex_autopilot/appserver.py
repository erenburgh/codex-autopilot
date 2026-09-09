from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable


class AppServerError(RuntimeError):
    pass


class AppServerRpcError(AppServerError):
    def __init__(self, method: str, error: Any) -> None:
        self.method = method
        self.error = error
        super().__init__(f"{method} failed: {json.dumps(error, ensure_ascii=False)}")


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


class AppServerClient:
    """Small newline-delimited JSON-RPC client for Codex App Server."""

    def __init__(
        self,
        binary: str,
        log_path: Path,
        *,
        event_sink: Callable[[str, dict[str, Any]], None] | None = None,
        popen_factory=subprocess.Popen,
    ) -> None:
        self.binary = binary
        self.log_path = log_path
        self.event_sink = event_sink
        self.popen_factory = popen_factory
        self.proc = None
        self.log = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.pending_events: deque[dict[str, Any]] = deque()
        self.stderr_lines: queue.Queue[str] = queue.Queue()
        self.next_id = 1
        self.errors: list[dict[str, Any]] = []

    def connect(self) -> dict[str, Any]:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = self.log_path.open("a", encoding="utf-8")
        self.proc = self.popen_factory(
            [self.binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if not self.proc.stdin or not self.proc.stdout or not self.proc.stderr:
            raise AppServerError("App Server stdio pipes were not created")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-autopilot",
                    "title": "Codex Autopilot Desktop Native",
                    "version": "0.8.0-beta",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized")
        return result

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

    def start_thread(
        self,
        *,
        cwd: Path,
        permission_profile: str,
        project_id: str | None,
        model: str | None,
        ephemeral: bool = False,
        project_memory: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"cwd": str(cwd), "permissions": permission_profile, "ephemeral": ephemeral}
        if project_memory:
            runtime = os.environ.get("CODEX_AUTOPILOT_RUNTIME")
            if runtime:
                command = runtime
                arguments = ["memory-mcp"]
            else:
                command = sys.executable
                arguments = ["-m", "codex_autopilot.cli", "memory-mcp"]
            # App Server 0.153.4 replaces a named MCP entry at thread scope
            # instead of deep-merging it. Repeat the complete local stdio
            # transport and change cwd to bind it to this project.
            params["config"] = {
                "mcp_servers": {
                    "codex_autopilot_memory": {
                        "command": command,
                        "args": arguments,
                        "cwd": str(cwd),
                        "enabled": True,
                        "startup_timeout_sec": 10,
                        "tool_timeout_sec": 30,
                        # Production never grants MCP approval on the user's
                        # behalf. Codex asks the user, who may choose its
                        # built-in persistent "always" option per tool.
                        "tools": {"memory": {"approval_mode": "prompt"}},
                    }
                }
            }
        if project_id is not None:
            params["projectId"] = project_id
        if model is not None:
            params["model"] = model
        return self.request("thread/start", params)

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

    def start_turn(self, *, thread_id: str, prompt: str, effort: str | None, client_user_message_id: str, skill_name: str, skill_path: Path) -> dict[str, Any]:
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
        return self.request("turn/start", params)

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        return self.request("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]

    def list_threads(self, cwd: Path, search_term: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"cwd": str(cwd), "limit": 100}
        if search_term:
            params["searchTerm"] = search_term
        return self.request("thread/list", params).get("data", [])

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
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
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
