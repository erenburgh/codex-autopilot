from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

from .config import STATE_DIR_NAME
from .memory import ProjectMemory
from .migration import detect_v07, migrate_v07
from .plan import Plan, save_plan, validate_plan
from .run_state import RunState, StateStore, utc_now


def initialize_project(
    root: Path,
    plan_file: Path,
    *,
    profile: str,
    skill_path: Path,
    replace: bool = False,
) -> Plan:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project directory does not exist: {root}")
    if not (root / ".git").exists():
        raise ValueError("Codex Autopilot public beta requires an existing Git repository. Run `git init` if appropriate; Autopilot never changes Git identity or creates commits by default.")
    if profile not in {"adaptive", "host-settings"}:
        raise ValueError("profile must be adaptive or host-settings")
    if not skill_path.is_file():
        raise ValueError(f"installed skill is missing: {skill_path}")
    state_dir = root / STATE_DIR_NAME
    state_path = state_dir / "run-state.json"
    existing_raw = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None
    if existing_raw and existing_raw.get("status") == "RUNNING" and _pid_alive(existing_raw.get("dispatcher_pid")):
        raise RuntimeError("Codex Autopilot is already running in this project")
    if existing_raw and not replace:
        raise RuntimeError("project is already initialized; use resume or pass --replace for a new run")
    raw = json.loads(plan_file.read_text(encoding="utf-8"))
    plan = validate_plan(raw, profile)
    state_dir.mkdir(parents=True, exist_ok=True)
    migration = migrate_v07(root, plan) if detect_v07(state_dir) else None
    completed = migration.preserved_completed if migration else 0
    for stale in ("BLOCKED.json", "pause-requested", "launch-request.json"):
        (state_dir / stale).unlink(missing_ok=True)
    if replace and (state_dir / "logs").exists():
        shutil.rmtree(state_dir / "logs")
    save_plan(state_dir, plan)
    _write_config(root, profile, skill_path)
    completed = min(completed, len(plan.milestones))
    current_index = min(completed, len(plan.milestones) - 1)
    (root / "ROADMAP.md").write_text(_roadmap(plan, completed), encoding="utf-8")
    _write_milestone(state_dir, plan, current_index)
    (state_dir / "HANDOFF.md").write_text(
        "# Handoff (advisory)\n\nCompleted: none in this run.\nChanged: none.\nRisks: none recorded.\nRelevant memory: query the built-in Project Memory MCP.\nNext: inspect and execute the current milestone.\n",
        encoding="utf-8",
    )
    memory = ProjectMemory(root)
    memory.initialize()
    memory.render_views()
    first = plan.milestones[current_index]
    state = RunState(
        status="DONE" if completed == len(plan.milestones) else "READY",
        phase="DONE" if completed == len(plan.milestones) else "PREFLIGHT_PASSED",
        milestone_index=current_index,
        milestone_id=first.id,
        planned_execution_mode=first.execution_mode,
        execution_mode=first.execution_mode,
        preflight_completed_at=utc_now(),
        completed_at=utc_now() if completed == len(plan.milestones) else None,
    )
    store = StateStore(state_dir)
    store.save(state)
    plan_file.unlink(missing_ok=True)
    if migration and migration.report:
        print(f"Migrated v0.7 state conservatively; report: {migration.report}")
    return plan


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _write_config(root: Path, profile: str, skill_path: Path) -> None:
    lines = [
        f"profile = {_toml_string(profile)}",
        "",
        "[project]",
        f"root = {_toml_string(str(root))}",
        "",
        "[desktop]",
        'binary = "codex"',
        'permission_profile = ":workspace"',
        f"skill_path = {_toml_string(str(skill_path.resolve()))}",
    ]
    lines.extend([
        "turn_timeout_seconds = 14400",
        "reconcile_timeout_seconds = 300",
        "",
        "[memory]",
        'backend = "sqlite+fts5"',
        'database = ".codex-autopilot/memory.sqlite3"',
        'mcp_server = "codex_autopilot_memory"',
        "",
        "[retry]",
        "initial_seconds = 30",
        "maximum_seconds = 900",
        "maximum_attempts = 96",
        "",
        "[git]",
        "auto_commit = false",
        "",
    ])
    (root / STATE_DIR_NAME / "config.toml").write_text("\n".join(lines), encoding="utf-8")


def _roadmap(plan: Plan, completed: int = 0) -> str:
    lines = ["# Roadmap", "", f"Goal: {plan.goal}", ""]
    for index, item in enumerate(plan.milestones):
        checked = "x" if index < completed else " "
        effort = f" — reasoning: {item.reasoning}" if item.reasoning else ""
        lines.extend([f"- [{checked}] {item.id}: {item.title} — {item.execution_mode}{effort}", f"  - {item.objective}", f"  - Mode reason: {item.execution_mode_reason}"])
        lines.extend(f"  - DoD: {criterion}" for criterion in item.definition_of_done)
    return "\n".join(lines) + "\n"


def mark_roadmap(root: Path, plan: Plan, completed: int) -> None:
    (root / "ROADMAP.md").write_text(_roadmap(plan, completed), encoding="utf-8")


def _write_milestone(state_dir: Path, plan: Plan, index: int) -> None:
    item = plan.milestones[index]
    lines = [
        f"# {item.id}: {item.title}",
        "",
        "## Objective",
        item.objective,
        "",
        "## Definition of Done",
        *[f"- {criterion}" for criterion in item.definition_of_done],
        "",
        "## Execution mode",
        item.execution_mode,
        "",
        "## Execution mode reason",
        item.execution_mode_reason,
    ]
    if item.reasoning:
        lines.extend(["", "## Adaptive reasoning", item.reasoning])
    (state_dir / "MILESTONE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def select_milestone(state_dir: Path, plan: Plan, index: int) -> None:
    _write_milestone(state_dir, plan, index)


def purge_project_state(root: Path) -> None:
    state_dir = root.resolve() / STATE_DIR_NAME
    if state_dir.exists():
        shutil.rmtree(state_dir)
