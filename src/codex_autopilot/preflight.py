from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any, Callable

from .appserver import AppServerClient, AppServerError
from .memory import MemoryError, probe_sqlite_fts5
from .models import resolve_selection
from .plan import Plan


MEMORY_SERVER_NAME = "codex_autopilot_memory"
REQUIRED_MEMORY_TOOLS = {"memory"}


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


@dataclass(slots=True)
class PreflightResult:
    project: Path
    codex_binary: str
    codex_home: Path | None = None
    checks: list[tuple[str, str, str]] = field(default_factory=list)
    next_model: str = "Host default"
    next_reasoning: str = "Host default"
    routing: str = "HOST SETTINGS"

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
) -> PreflightResult:
    project = root.expanduser().resolve()
    codex_binary = shutil.which(binary) if not Path(binary).is_absolute() else binary
    result = PreflightResult(project=project, codex_binary=codex_binary or binary)

    def report(name: str, status: str, detail: str = "") -> None:
        result.add(name, status, detail)
        if emit:
            emit(f"{name}: {status}" + (f" — {detail}" if detail else ""))

    if emit:
        emit("Codex Autopilot preflight")
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

        try:
            profiles = client.list_permission_profiles(project)
            allowed = {entry.get("id") for entry in profiles if entry.get("allowed") is not False}
            if ":workspace" not in allowed:
                raise PreflightError(f":workspace permission profile unavailable; allowed={sorted(str(item) for item in allowed)}")
            probe_thread = client.start_thread(
                cwd=project,
                permission_profile=":workspace",
                project_id=None,
                model=None,
                ephemeral=True,
                project_memory=True,
            )
            active = probe_thread.get("activePermissionProfile") or {}
            thread = probe_thread.get("thread") or {}
            if active.get("id") != ":workspace":
                raise PreflightError("App Server did not apply :workspace to the preflight thread")
            if Path(str(thread.get("cwd") or "")).resolve() != project:
                raise PreflightError("App Server did not preserve target project cwd")
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
        statuses = client.list_mcp_server_status(thread["id"])
        memory_status = next((item for item in statuses if item.get("name") == MEMORY_SERVER_NAME), None)
        if not memory_status:
            raise PreflightError(
                "built-in Project Memory MCP is not present in the active plugin. Reinstall Codex Autopilot v0.8 and start a fresh Codex task."
            )
        if memory_status.get("runtimeStatus") != "connected":
            raise PreflightError(f"Project Memory MCP failed to start: {memory_status.get('runtimeStatus')}")
        tools = set((memory_status.get("tools") or {}).keys())
        missing_tools = sorted(REQUIRED_MEMORY_TOOLS - tools)
        if missing_tools:
            raise PreflightError(f"Project Memory MCP tool contract is incomplete: {missing_tools}")
        memory_identity = client.call_mcp_tool(thread["id"], MEMORY_SERVER_NAME, "memory", {"operation": "current"})
        identity = memory_identity.get("structuredContent") or {}
        if Path(str(identity.get("project_root") or "")).resolve() != project:
            raise PreflightError("Project Memory MCP did not bind to the target project root")
        report("Project Memory", "OK", f"SQLite {memory_probe['sqlite']} + FTS5; local stdio MCP connected")
        report(
            "Memory tool trust",
            "USER CONTROLLED",
            "before starting, call memory(operation=current) in this task and choose Always only if you trust the installed local plugin",
        )

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
            report("Model metadata", "OK", f"{selection.model_id} supports {selection.reasoning}")
        else:
            result.routing = "HOST SETTINGS"
            result.next_model = "Host default"
            result.next_reasoning = "Host default (no override)"
            report("Model metadata", "OK", "dispatcher will send neither model nor effort")
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
        client.close()
        log_path.unlink(missing_ok=True)
