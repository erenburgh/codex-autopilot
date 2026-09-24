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
from .config import DESKTOP_OWNED_SURFACE, STATE_DIR_NAME
from .hook_trust import (
    HookPreflightError,
    HookTrustApprovalRequired,
    require_trusted_stop_hook,
)
from .memory import MemoryError, probe_sqlite_fts5
from .models import catalog_verdicts, resolve_selection
from .plan import DEFAULT_MAX_PARALLEL_WORKERS, Plan
from .plan_verification import (
    INITIAL_PLAN_VERIFICATION,
    PLAN_VERIFICATION_ROLE,
    PlanVerificationError,
    PlanVerificationReceipt,
    build_plan_verification_prompt,
    deterministic_plan_issues,
    format_plan_verification_issues,
    load_active_memory_constraints,
    make_plan_verification_receipt,
    parse_plan_verification_result,
    validate_verdict_references,
)
from .usage import capacity_notice
from .project_association import (
    ProjectAssociationError,
    require_desktop_project_root,
    resolve_preflight_project,
)


MEMORY_SERVER_NAME = "codex_autopilot_memory"
REQUIRED_MEMORY_TOOLS = {"memory"}
MEMORY_PREFLIGHT_TITLE = "Codex Autopilot Preflight · Project Memory"
# The trust probe does not reason: it makes one tool call and answers with
# one line. It used to run at the production worker's effort - xhigh on the
# v1.0 run, and the turn twice missed the five-minute mark, then passed in
# under a minute on the third try. Autopilot bounds the WORKER LADDER to
# medium..max; App Server accepts minimal and low too, and the probe is not
# a worker.
PROBE_REASONING = "low"
PROBE_TIMEOUT = 300.0
# One timeout is no reason to fail the whole launch: the third try showed
# a retry resolves it. The first used to fail everything.
PROBE_ATTEMPTS = 3

ANNOUNCEMENT = (
    "Across the whole launch you may be asked once, and about one thing only: trust "
    f"for the memory tool `codex_autopilot_memory.memory` in a separate task "
    f"«{MEMORY_PREFLIGHT_TITLE}». Answer with the button in that task. Preflight asks "
    "nothing else and silently waits for nothing else from you."
)
MEMORY_PREFLIGHT_OK = "MEMORY_PREFLIGHT_OK"
PLAN_VERIFICATION_TITLE = (
    "Plan Verification Architect | Verify PLAN | Proposed Graph"
)
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


def runtime_command_path() -> Path:
    """The same path the skill itself launches from.

    A hard-coded `~/Library/Application Support/...` would work only for
    someone whose runtime sits in the default place. The plugin launcher
    honours CODEX_AUTOPILOT_RUNTIME, and the command handed to a person must
    point there too - or it is correct on exactly one machine.
    """

    override = os.environ.get("CODEX_AUTOPILOT_RUNTIME")
    if override:
        return Path(override)
    # The runtime knows its place: <install_root>/current/runtime/src/...
    candidate = Path(__file__).absolute().parents[3] / "bin/codex-autopilot"
    if candidate.is_file():
        return candidate
    return Path.home() / "Library/Application Support/CodexAutopilot/current/bin/codex-autopilot"


def _plan_file_for(project: Path) -> Path:
    """The plan with which the permission command runs without questions."""

    state_dir = project / STATE_DIR_NAME
    existing = state_dir / "plan.json"
    return existing if existing.is_file() else state_dir / "bootstrap-plan.json"


def approval_command(
    project: Path,
    plan_file: Path,
    profile: str,
    *,
    app_server_project_id: str | None = None,
    desktop_project_id: str | None = None,
    language: str | None = None,
) -> str:
    """A command ready to run, not a description of how to assemble it.

    It used to say "repeat the same command with the flag": the model was
    expected to assemble it, and it never once reached the person. And
    there is no dialog at all - the tool request goes to the dispatcher's
    connection, which never answers approvals. So the only path to the
    person is text that can be copied and run.
    """

    runtime = runtime_command_path()
    parts = [
        f'"{runtime}"',
        "preflight",
        f'--project "{project}"',
        f'--plan-file "{plan_file}"',
        f"--profile {profile}",
    ]
    # Without the project identifiers preflight refuses at the placement
    # check: "the directory belongs to no Codex project". The first command
    # handed to a user was exactly that - incomplete - and failed not on the
    # permission but earlier.
    if app_server_project_id:
        parts.append(f"--app-server-project-id {app_server_project_id}")
    if desktop_project_id:
        parts.append(f"--desktop-project-id {desktop_project_id}")
    if language:
        parts.append(f"--language {language}")
    parts.append("--approve-project-memory-always")
    return " ".join(parts)


class ProjectMemoryApprovalRequired(PreflightError):
    exit_code = 77

    def __init__(
        self,
        thread_id: str,
        title: str,
        command: str | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.title = title
        self.command = command
        ready = (
            f"\n\nRun this command in the terminal - running it is your consent:\n\n{command}\n"
            if command
            else ""
        )
        super().__init__(
            "Project Memory MCP: APPROVAL REQUIRED\n\n"
            f"No worker and no run state have been created. Diagnostic task: `{title}` (thread {thread_id}).\n"
            "One permission is needed - for the memory tool `codex_autopilot_memory.memory`, answered Always. "
            "There will be no pop-up: the request goes to the dispatcher's connection, and it never answers approvals."
            f"{ready}"
            "Autopilot neither grants, derives nor bypasses this permission itself."
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
    plan_verification: PlanVerificationReceipt | None = None
    # The isolation measurement (isolation_probe); bootstrap writes it into
    # the new run's state - preflight removes what its probe created.
    isolation: dict[str, Any] | None = None

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


def _run_permission_profile(project: Path) -> str:
    """The profile the run's threads will use: the project's config, or the default."""

    from .config import DesktopConfig, load_config

    try:
        return load_config(project).desktop.permission_profile
    except Exception:  # noqa: BLE001 - a project not initialized yet uses the default
        return DesktopConfig().permission_profile


def _measure_isolation(
    client_factory: Callable[..., Any],
    project: Path,
    binary: str,
    initialized: Any,
    report: Callable[..., None],
) -> dict[str, Any]:
    """Can a staged task's thread be filed at the root and still not write it (isolation_probe).

    Measured as the dispatcher will use it: the task's staged profile, on
    this root, with a workspace under this project's state directory, on a
    short-lived App Server launched with the profile's definition.

    No outcome stops the launch. The first version failed preflight on a
    writable root and handed her the choice between isolated tasks outside
    the project and a profile that denies the root - a stop no on-call ever
    saw, over a choice that is the runtime's. The deny profile is now the
    runtime's own design; if it does not hold, that is a runtime defect: the
    finding is reported here (FAIL or NOT PROVEN, never silent), staged
    tasks keep their workspace as cwd, and the first such thread's R5 ticket
    takes the record to the on-call (placement_defects).

    What preflight creates for the probe it removes; bootstrap writes the
    returned record into the run.
    """

    from .isolation_probe import PASS, ROOT_WRITABLE, probe_isolation, probe_workspace

    state_dir = project / STATE_DIR_NAME
    created_state_dir = not state_dir.exists()
    workspace = probe_workspace(state_dir)
    log_path = Path(tempfile.gettempdir()) / f"codex-autopilot-isolation-{os.getpid()}.jsonl"
    try:
        record = probe_isolation(
            lambda overrides: client_factory(binary, log_path, config_overrides=overrides),
            root=project,
            workspace=workspace,
            base_profile=_run_permission_profile(project),
            binary=binary,
            codex_version=str((initialized or {}).get("userAgent") or "") or None,
        )
    finally:
        shutil.rmtree(workspace.parent, ignore_errors=True)
        if created_state_dir:
            try:
                state_dir.rmdir()
            except OSError:
                pass
        log_path.unlink(missing_ok=True)
    where = f"cwd = {project}, profile {record['profile']}, workspace {record['workspace']}"
    if record["outcome"] == PASS:
        report("Isolation", "OK", f"staged tasks are filed at {project} and write only their workspace ({where})")
    else:
        status = "FAIL" if record["outcome"] == ROOT_WRITABLE else "NOT PROVEN"
        report(
            "Isolation",
            status,
            f"ISOLATION: {record['reason']} ({where}). Staged tasks keep their workspace as cwd, "
            "isolated but outside the project in Desktop; each is an R5 defect whose ticket takes "
            "this measurement to the on-call as a runtime defect",
        )
    return record


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
        # The one place in the run where a human may be needed is named
        # before the first long check. Without this line the user sees ten
        # "OK", then silence, and does not know a decision is awaited from
        # them in another task: measured - half an hour of "thinking" while
        # the dialog hung next door.
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
    deterministic_issues = deterministic_plan_issues(plan)
    if deterministic_issues:
        detail = format_plan_verification_issues(deterministic_issues)
        report("Plan verification admission", "FAIL", detail)
        raise PreflightError(
            "proposed plan failed deterministic coverage admission: " + detail
        )
    report(
        "Plan verification admission",
        "OK",
        "deterministic coverage admits independent judgment",
    )
    _reject_unprobed_capabilities(plan, report)
    if not codex_binary:
        report("Runtime", "FAIL", "official Codex CLI not found")
        raise PreflightError("official Codex CLI/App Server is not installed")
    if not skill_path.is_file():
        report("Runtime", "FAIL", f"installed worker skill missing: {skill_path}")
        raise PreflightError(f"installed worker skill is missing: {skill_path}")
    report("Runtime", "OK", f"{codex_binary}; {skill_path}")
    try:
        cache_status, cache_detail = plugin_cache_state(installed_plugin_root(skill_path))
    except PreflightError as exc:
        # The skill is not inside the installed plugin - nothing to compare,
        # and no reason to stop the check: the path was verified above.
        cache_status, cache_detail = "WARN", str(exc)
    report("Plugin cache", cache_status, cache_detail)
    if cache_status == "FAIL":
        raise PreflightError(
            "Codex is loading the wrong copy of the plugin: " + cache_detail
            + ". Reinstall Autopilot - the installer clears the cache and verifies the result."
        )

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
                f"the directory {project} belongs to no Codex project. "
                "Open a Codex project and start Autopilot inside it: every "
                "task is created inside a project, and without one it cannot be seen."
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
            else:
                # Not a pass: without Desktop's state no thread's placement
                # can be observed, and each one is recorded as an R5 defect
                # (placement_defects) - said here, before the run, not after.
                report(
                    "Desktop project rootPaths",
                    "UNOBSERVABLE",
                    f"{codex_home / '.codex-global-state.json'} does not exist: Desktop placement "
                    "cannot be observed; every thread's placement will be recorded as an R5 defect",
                )

        plugin_root = installed_plugin_root(skill_path)
        expected_plugin_id = installed_plugin_id(plugin_root)
        # The Stop-hook trust check is unconditional. It used to depend on
        # arguments that are always true in the product - the condition
        # chose nothing, yet allowed a green preflight with an untrusted
        # hook. The launch belongs to this very hook: without trust the run
        # does not start, and a green preflight would be a lie.
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
            # Pre-created slots were a workaround for a supposed
            # impossibility of creating a visible task through App Server.
            # Measured: thread/start with the app's own originator yields a
            # task visible in the project sidebar - all six 0.8.0 acceptance
            # workers came out INSIDE. The slot mechanism was removed in
            # 0.8.1.
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
        plan_selection = None
        if profile == "adaptive":
            first = plan.milestones[0]
            models = client.list_models()
            # A pinned model that is still served but no longer the newest
            # is not an error and changes nothing about this run. It is
            # said out loud once, here, because the alternative is finding
            # out on the day the pinned one is retired - when every
            # installed copy refuses at the same moment.
            for verdict in catalog_verdicts(list(models)):
                if verdict.state == "superseded":
                    report(f"Model {verdict.key}", "WARN", verdict.message)
                elif verdict.state == "missing":
                    # "Not served to this account" is what it looks like
                    # from here, and it may instead be "not visible through
                    # THIS client". Desktop carries its own Codex; a
                    # separately installed CLI can be versions behind and
                    # list a different catalog. Say which it is before the
                    # reader goes looking for a billing problem.
                    from .codex_binaries import binaries_serving

                    elsewhere = [
                        item
                        for item in binaries_serving(verdict.pinned)
                        if item != str(codex_binary)
                    ]
                    if elsewhere:
                        report(
                            f"Model {verdict.key}",
                            "FAIL",
                            f"{verdict.pinned} is not listed by this project's "
                            f"Codex ({codex_binary}), but it IS listed by "
                            f"{', '.join(elsewhere)}. Set desktop.binary to that "
                            f"one, or pin a model this client serves.",
                        )
            selection = resolve_selection(
                models,
                strategy=plan.model_strategy,
                execution_mode=first.execution_mode,
                requested_reasoning=first.reasoning or "medium",
                execution_reason=first.execution_mode_reason,
            )
            if plan.goal_contract is not None:
                plan_selection = resolve_selection(
                    models,
                    strategy=plan.model_strategy,
                    execution_mode="code",
                    requested_reasoning="high",
                    execution_reason=(
                        "Independent semantic plan verification uses a bounded "
                        "structured prompt and no GUI interaction."
                    ),
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
            # Five minutes of silence without a sign of life is what a person
            # sees as "the thread is thinking", not knowing what to expect.
            emit(
                f"Project Memory MCP: checking trust in the task «{MEMORY_PREFLIGHT_TITLE}» "
                f"(up to {int(PROBE_TIMEOUT)} s per attempt, {PROBE_ATTEMPTS} attempts)"
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
                        # The interrupt is cleanup, not a condition. Its
                        # failure must not replace the timeout's reason.
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
                raise ProjectMemoryApprovalRequired(
                    probe_thread_id,
                    MEMORY_PREFLIGHT_TITLE,
                    approval_command(
                        project,
                        _plan_file_for(project),
                        profile,
                        app_server_project_id=result.project_id or app_server_project_id,
                        desktop_project_id=desktop_project_id,
                    ),
                ) from exc
        else:
            _validate_memory_preflight_result(client, probe_thread_id, completed.turn)
        report("Project Memory MCP", "OK", "real model-to-MCP call completed without a trust interruption")

        if plan.goal_contract is not None:
            try:
                result.plan_verification = _run_initial_plan_verification(
                    client,
                    project=project,
                    plan=plan,
                    project_id=result.project_id,
                    selection=plan_selection,
                    thread_ids=probe_thread_ids,
                )
            except PlanVerificationError as exc:
                report("Plan verification", "FAIL", str(exc))
                raise PreflightError(str(exc)) from exc
            report(
                "Plan verification",
                "PASS",
                f"fresh verifier bound graph v{plan.graph_version} to "
                f"{result.plan_verification.plan_sha256}",
            )
        else:
            report(
                "Plan verification",
                "COMPATIBILITY",
                "persisted pre-v1 plan has no Goal Contract",
            )

        if selection:
            report("Model metadata", "OK", f"{selection.model_id} supports {selection.reasoning}")
        else:
            report("Model metadata", "OK", "dispatcher will send neither model nor effort")

        result.isolation = _measure_isolation(client_factory, project, codex_binary, initialized, report)
        if replace:
            retired = _archive_replaced_workers(project, client, exclude=set(probe_thread_ids))
            if retired:
                report("Previous run", "RETIRED", f"archived {len(retired)} worker task(s): {', '.join(retired)}")
        if emit:
            emit(f"Routing: {result.routing}")
            emit(f"Next worker: {result.next_model} / {result.next_reasoning}")
            # A person need not know their plan, nor that the number of
            # workers is settable at all. Saying it once, naming their own
            # situation, is more honest than silently setting the template's
            # ten - which is exactly how it stood for a whole run.
            try:
                limits = client.rate_limits()
            except Exception:
                limits = None
            # The number counts as set by the human if it differs from the
            # default: the template fills it in itself, and passing that off
            # as the user's choice would be a substitution.
            declared_workers = (
                plan.max_parallel_workers
                if plan.max_parallel_workers != DEFAULT_MAX_PARALLEL_WORKERS
                else None
            )
            emit(
                "Capacity: "
                + capacity_notice(
                    limits, declared_workers, running=plan.max_parallel_workers
                )
            )
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


def _run_initial_plan_verification(
    client: Any,
    *,
    project: Path,
    plan: Plan,
    project_id: str | None,
    selection: Any,
    thread_ids: list[str],
) -> PlanVerificationReceipt:
    """Run one fresh, tool-free semantic judgment before run-state exists."""

    constraints = load_active_memory_constraints(project)
    prompt = build_plan_verification_prompt(
        plan,
        constraints,
        mode=INITIAL_PLAN_VERIFICATION,
    )
    created = client.start_thread(
        cwd=project,
        permission_profile=":workspace",
        project_id=project_id,
        model=selection.model_id if selection else None,
        plugin_root=None,
        ephemeral=False,
        project_memory=False,
    )
    active = created.get("activePermissionProfile") or {}
    thread = created.get("thread") or {}
    if active.get("id") != ":workspace":
        raise PlanVerificationError(
            "App Server did not apply :workspace to the plan verifier"
        )
    if Path(str(thread.get("cwd") or "")).resolve() != project:
        raise PlanVerificationError(
            "App Server did not preserve canonical cwd for the plan verifier"
        )
    thread_id = str(thread.get("id") or "")
    if not thread_id:
        raise PlanVerificationError("App Server returned no plan verifier thread id")
    thread_ids.append(thread_id)
    client.name_thread(thread_id, PLAN_VERIFICATION_TITLE)
    if project_id and thread.get("projectId") != project_id:
        client.assign_thread_to_project(thread_id, project_id)
        thread = client.read_thread(thread_id)
    if project_id and thread.get("projectId") != project_id:
        raise PlanVerificationError(
            "App Server did not preserve project association for the plan verifier"
        )
    started = client.start_plain_turn(
        thread_id=thread_id,
        prompt=prompt,
        effort=selection.reasoning if selection else None,
        client_user_message_id=str(uuid.uuid4()),
        cwd=project,
    )
    turn_id = str((started.get("turn") or {}).get("id") or "")
    if not turn_id:
        raise PlanVerificationError("App Server returned no plan verifier turn id")
    try:
        completed = client.wait_for_turn(
            thread_id,
            turn_id,
            timeout=PROBE_TIMEOUT,
            what="fresh independent plan verification",
        )
    except ApprovalRequired as exc:
        raise PlanVerificationError(
            "plan verifier requested an approval even though its prompt forbids tools"
        ) from exc
    turn = completed.turn
    if turn.get("status") != "completed":
        raise PlanVerificationError(
            f"plan verifier turn ended with status={turn.get('status')!r}"
        )
    allowed_items = {"agentMessage", "reasoning", "userMessage"}
    disallowed = sorted(
        {
            str(item.get("type") or "unknown")
            for item in turn.get("items") or []
            if item.get("type") not in allowed_items
        }
    )
    if disallowed:
        raise PlanVerificationError(
            "plan verifier used forbidden tool or side-effect items: "
            + ", ".join(disallowed)
        )
    verdict = parse_plan_verification_result(final_agent_message(turn))
    validate_verdict_references(plan, verdict)
    if verdict.verdict != "PASS":
        raise PlanVerificationError(
            "proposed plan was rejected by the fresh verifier: "
            + format_plan_verification_issues(verdict.issues)
        )
    return make_plan_verification_receipt(
        plan,
        verdict,
        mode=INITIAL_PLAN_VERIFICATION,
        verifier_thread_id=thread_id,
        verifier_turn_id=turn_id,
    )


def _reject_unprobed_capabilities(
    plan: Plan,
    report: Callable[[str, str, str], None],
) -> None:
    """Fail before App Server side effects when trust cannot be preflighted.

    ``required_capabilities`` is currently an opaque scheduler constraint: it
    has no trust classification or registered safe probe.  Treating an opaque
    declaration as already trusted would defer the first real check to Worker
    1.  Until Capability Broker introduces typed probes, every named
    capability therefore fails closed here.  Empty declarations need no
    additional trust beyond the built-in checks performed below.
    """

    declared = sorted(
        {
            capability
            for task in plan.tasks
            for capability in task.required_capabilities
        }
    )
    if declared:
        detail = (
            "no registered pre-worker trust probe for declared capabilities: "
            + ", ".join(declared)
        )
        report("Declared capabilities", "FAIL", detail)
        raise PreflightError(detail)
    report("Declared capabilities", "OK", "none require an additional trust probe")
def _unexpected_approval_message(exc: BaseException) -> str:
    """Name a foreign approval in human language, not raw JSON.

    The dispatcher never answers approvals under any conditions. So any
    approval but trust for the memory tool is a dead end here: it hangs in
    the interface, preflight will not close it, and the run will not start.
    The only measured source of such a dead end is a model that attached a
    permission request to the `start-skill` command itself and reran it.
    """
    payload = getattr(exc, "payload", None) or {}
    params = payload.get("params") or {}
    kind = str(params.get("kind") or "unknown")
    reason = str(params.get("reason") or "").strip()
    lines = [
        "preflight stopped: an approval arrived during the memory check, "
        f"and the dispatcher has no right to close it (kind={kind}).",
        "",
        "The dispatcher never answers approvals. This request will not close "
        "by itself and the run will not start with it pending.",
    ]
    if reason:
        lines += ["", f"Request text: {reason}"]
    lines += [
        "",
        "What to do: do not attach the permission request to `start-skill` and do "
        "not rerun it for access. Cancel the pending request in the "
        "interface and remove the cause named by the previous output line "
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


def plugin_cache_dirs(plugin_name: str) -> list[Path]:
    """The plugin copies Codex sees - not what lies in the installation."""

    home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    base = home / "plugins" / "cache" / "codex-autopilot-local" / plugin_name
    return sorted(
        manifest.parent.parent for manifest in base.glob("*/.codex-plugin/plugin.json")
    )


def plugin_cache_state(plugin_root: Path) -> tuple[str, str]:
    """Does what Codex loads match what is installed.

    Codex reads the plugin from its cache, the runtime from the install
    directory. While the old copy stayed in the cache, Codex loaded it: the
    user had 0.9.7 installed and 0.9.0 running - with the old 30-second
    Interrupt declaration. Codex clamps it to 3, rewrites the file, the hash
    changes, and Stop-hook trust is lost on every load. From outside it looks
    like "the hooks drop by themselves", and trusting them again does not
    fix it - a minute later they drop again.
    """

    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    name = str(payload.get("name") or "")
    installed = str(payload.get("version") or "")
    if ".local." not in installed:
        # The marker is set by the installer. Without it this is a source
        # tree, not an installation: comparing it with a cache is pointless.
        return "WARN", f"plugin {name} is not from the installation ({installed}) - nothing to compare"
    cached = plugin_cache_dirs(name)
    if not cached:
        return "WARN", f"Codex has not yet taken plugin {name} into its cache"
    if len(cached) > 1:
        versions = ", ".join(item.name for item in cached)
        return "FAIL", f"several copies of {name} in the Codex cache: {versions}"
    cached_version = str(
        json.loads(
            (cached[0] / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        ).get("version")
        or ""
    )
    if cached_version != installed:
        return "FAIL", f"Codex loads {cached_version}, installed is {installed}"
    return "OK", f"{installed} - one copy, the same as installed"


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
