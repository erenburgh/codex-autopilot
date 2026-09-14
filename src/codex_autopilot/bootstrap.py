from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from datetime import datetime, timezone

from .config import DESKTOP_OWNED_SURFACE, STATE_DIR_NAME
from .language import DEFAULT_LANGUAGE, is_russian, normalize_language
from .memory import ProjectMemory
from .migration import detect_v07, migrate_v07
from .plan import Plan, save_plan, validate_plan
from .run_state import RunState, StateStore, utc_now
from .task_state import TaskState, initial_task_states


def initialize_project(
    root: Path,
    plan_file: Path,
    *,
    profile: str,
    skill_path: Path,
    replace: bool = False,
    language: str = DEFAULT_LANGUAGE,
    project_id: str | None = None,
    desktop_project_id: str | None = None,
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
    language = normalize_language(language)
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
    if replace:
        # Тикеты принадлежат прогону, который их завёл: run_id в них не
        # хранится, а дежурный инженер старше любой работы. Прежде новый
        # прогон наследовал чужие открытые тикеты и вставал на них сразу,
        # ещё до первой задачи. Файл не удаляется, а откладывается: это
        # запись о поломке, и она может понадобиться.
        incidents = state_dir / "pipeline-incidents.json"
        if incidents.is_file():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            incidents.replace(state_dir / f"pipeline-incidents.{stamp}.json")
        for lock in ("pipeline-recovery.lock", "resource-coordinator.lock"):
            (state_dir / lock).unlink(missing_ok=True)
    save_plan(state_dir, plan)
    _write_config(
        root,
        profile,
        skill_path,
        plan=plan,
        language=language,
        project_id=project_id,
        desktop_project_id=desktop_project_id,
    )
    completed = min(completed, len(plan.milestones))
    current_index = min(completed, len(plan.milestones) - 1)
    (root / "ROADMAP.md").write_text(
        _roadmap(plan, completed, language=language), encoding="utf-8"
    )
    _write_milestone(state_dir, plan, current_index, language=language)
    # Задачные чекпойнты (M10-REV-005): у каждой задачи свой файл,
    # чтобы параллельные воркеры не закрывали гейт друг другу.
    (state_dir / "handoff").mkdir(exist_ok=True)
    (state_dir / "HANDOFF.md").write_text(
        _initial_handoff(language),
        encoding="utf-8",
    )
    memory = ProjectMemory(root)
    memory.initialize()
    memory.render_views()
    first = plan.milestones[current_index]
    task_states = initial_task_states(plan)
    for task in plan.tasks[:completed]:
        task_states[task.id] = TaskState.VERIFIED.value
    if completed < len(plan.tasks):
        task_states[first.id] = TaskState.READY.value
    ready_ids = [
        task.id for task in plan.tasks if task_states[task.id] == TaskState.READY.value
    ]
    state = RunState(
        status="DONE" if completed == len(plan.milestones) else "READY",
        phase="DONE" if completed == len(plan.milestones) else "PREFLIGHT_PASSED",
        milestone_index=current_index,
        milestone_id=first.id,
        graph_version=plan.graph_version,
        execution_strategy=plan.execution_strategy,
        max_parallel_workers=plan.max_parallel_workers,
        computer_use_slots=plan.computer_use_slots,
        task_states=task_states,
        task_attempts={task.id: 0 for task in plan.tasks},
        task_revisions={task.id: 0 for task in plan.tasks},
        scheduler_sequence=len(ready_ids),
        task_ready_since={task_id: index for index, task_id in enumerate(ready_ids, 1)},
        planned_execution_mode=first.execution_mode,
        execution_mode=first.execution_mode,
        preflight_completed_at=utc_now(),
        prep_app_server_exited_at=utc_now(),
        project_id=project_id,
        desktop_project_id=desktop_project_id,
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


def _write_config(
    root: Path,
    profile: str,
    skill_path: Path,
    *,
    plan: Plan,
    language: str,
    project_id: str | None = None,
    desktop_project_id: str | None = None,
) -> None:
    lines = [
        f"profile = {_toml_string(profile)}",
        f"language = {_toml_string(language)}",
        "",
        "[project]",
        f"root = {_toml_string(str(root))}",
        "",
        "[desktop]",
        'binary = "codex"',
        'permission_profile = ":workspace"',
        f"skill_path = {_toml_string(str(skill_path.resolve()))}",
    ]
    if project_id:
        lines.append(f"project_id = {_toml_string(project_id)}")
    if desktop_project_id:
        lines.append(f"desktop_project_id = {_toml_string(desktop_project_id)}")
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
        "[runtime]",
        f"execution_strategy = {_toml_string(plan.execution_strategy)}",
        # Системный банер, когда задача проверена, встала или прогон
        # завершён. Выключено: включать побочный эффект на чужой машине
        # без спроса нельзя. См. docs/DESKTOP_RUNTIME.md.
        "desktop_notifications = false",
        f"max_parallel_workers = {plan.max_parallel_workers}",
        f"computer_use_slots = {plan.computer_use_slots}",
        # Поверхность одна. Поле пишется явно, чтобы конфиг читался
        # без знания умолчаний, а проверка при чтении отвергает
        # устаревший файл, называющий снятую поверхность.
        f"worker_surface = {_toml_string(DESKTOP_OWNED_SURFACE)}",
        "",
        "[git]",
        "auto_commit = false",
        "",
    ])
    (root / STATE_DIR_NAME / "config.toml").write_text("\n".join(lines), encoding="utf-8")


def _roadmap(
    plan: Plan,
    completed: int = 0,
    *,
    language: str = DEFAULT_LANGUAGE,
) -> str:
    russian = is_russian(language)
    lines = ["# Дорожная карта" if russian else "# Roadmap", "", f"Цель: {plan.goal}" if russian else f"Goal: {plan.goal}", ""]
    for index, item in enumerate(plan.milestones):
        checked = "x" if index < completed else " "
        effort_label = "рассуждение" if russian else "reasoning"
        effort = f" — {effort_label}: {item.reasoning}" if item.reasoning else ""
        mode_reason = "Причина режима" if russian else "Mode reason"
        dod = "Критерий готовности" if russian else "DoD"
        lines.extend([f"- [{checked}] {item.id}: {item.title} — {item.execution_mode}{effort}", f"  - {item.objective}", f"  - {mode_reason}: {item.execution_mode_reason}"])
        lines.extend(f"  - {dod}: {criterion}" for criterion in item.definition_of_done)
    return "\n".join(lines) + "\n"


def mark_roadmap(
    root: Path,
    plan: Plan,
    completed: int,
    *,
    language: str = DEFAULT_LANGUAGE,
) -> None:
    (root / "ROADMAP.md").write_text(
        _roadmap(plan, completed, language=language), encoding="utf-8"
    )


def _write_milestone(
    state_dir: Path,
    plan: Plan,
    index: int,
    *,
    language: str = DEFAULT_LANGUAGE,
) -> None:
    item = plan.milestones[index]
    russian = is_russian(language)
    lines = [
        f"# {item.id}: {item.title}",
        "",
        "## Задача" if russian else "## Objective",
        item.objective,
        "",
        "## Критерии готовности" if russian else "## Definition of Done",
        *[f"- {criterion}" for criterion in item.definition_of_done],
        "",
        "## Режим выполнения" if russian else "## Execution mode",
        item.execution_mode,
        "",
        "## Причина выбора режима" if russian else "## Execution mode reason",
        item.execution_mode_reason,
    ]
    if item.reasoning:
        lines.extend(["", "## Уровень рассуждения" if russian else "## Adaptive reasoning", item.reasoning])
    (state_dir / "MILESTONE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def select_milestone(
    state_dir: Path,
    plan: Plan,
    index: int,
    *,
    language: str = DEFAULT_LANGUAGE,
) -> None:
    _write_milestone(state_dir, plan, index, language=language)


def _initial_handoff(language: str) -> str:
    if is_russian(language):
        return (
            "# Передача контекста (справочно)\n\n"
            "Completed: в этом запуске пока ничего.\n"
            "Changed: ничего.\n"
            "Risks: риски не зафиксированы.\n"
            "Relevant memory: запросить встроенный Project Memory MCP.\n"
            "Next: изучить и выполнить текущую задачу.\n"
        )
    return (
        "# Handoff (advisory)\n\n"
        "Completed: none in this run.\n"
        "Changed: none.\n"
        "Risks: none recorded.\n"
        "Relevant memory: query the built-in Project Memory MCP.\n"
        "Next: inspect and execute the current milestone.\n"
    )


def purge_project_state(root: Path) -> None:
    state_dir = root.resolve() / STATE_DIR_NAME
    if state_dir.exists():
        shutil.rmtree(state_dir)
