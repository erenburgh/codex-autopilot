from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import uuid
from datetime import datetime, timezone

from .codex_binaries import newest_codex_binary
from .config import (
    DEFAULT_SKILL_SCREENING,
    DESKTOP_OWNED_SURFACE,
    STATE_DIR_NAME,
    durable_skill_path,
)
from .language import DEFAULT_LANGUAGE, is_russian, normalize_language
from .department_runtime import ensure_all_department_rubrics
from .memory import ProjectMemory
from .migration import detect_v07, migrate_v07
from .plan import Plan, save_plan, validate_migrating_plan
from .resilience import append_resilience_event
from .plan_verification import (
    DEFAULT_FULL_REVALIDATION_PATCHES,
    PlanVerificationReceipt,
    PlanVerificationVerdict,
    record_plan_verification,
    validate_plan_verification_receipt,
)
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
    plan_verification: PlanVerificationReceipt | dict[str, object] | None = None,
    skill_screening: str = DEFAULT_SKILL_SCREENING,
    roots_audit: dict[str, object] | None = None,
) -> Plan:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project directory does not exist: {root}")
    if not (root / ".git").exists():
        raise ValueError("Codex Autopilot public beta requires an existing Git repository. Run `git init` - a repository with no commits is fine. Autopilot never changes Git identity or creates commits by default.")
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
    plan = validate_migrating_plan(
        raw,
        profile,
        state_dir=state_dir,
        state_payload=existing_raw,
    )
    verification_receipt = validate_plan_verification_receipt(
        plan,
        plan_verification,
        require_evidence=False,
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    # R28: overwriting the state without a snapshot is refused. The
    # snapshot is taken BEFORE the first mutation - below, the logs are
    # removed and the plan, the state, the config and ROADMAP.md are
    # overwritten. A copy, not a move: memory and handoff/ must go on living
    # for the new run. With no previous run-state there is nothing to
    # snapshot: a copy of an empty past is a junk neighbour on every first
    # launch.
    previous_state = (
        archive_state_dir(state_dir, reason="replaced", move=False)
        if replace and existing_raw
        else None
    )
    migration = migrate_v07(root, plan) if detect_v07(state_dir) else None
    completed = migration.preserved_completed if migration else 0
    for stale in ("BLOCKED.json", "pause-requested", "launch-request.json"):
        (state_dir / stale).unlink(missing_ok=True)
    if replace and (state_dir / "logs").exists():
        shutil.rmtree(state_dir / "logs")
    if replace:
        # Tickets belong to the run that opened them: no run_id is stored in
        # them, and the on-call engineer outranks any work. A new run used
        # to inherit someone else's open tickets and stop on them at once,
        # before its first task. The file is not deleted but set aside: it
        # is a record of a fault, and it may be needed.
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
        skill_screening=skill_screening,
        project_id=project_id,
        desktop_project_id=desktop_project_id,
    )
    completed = min(completed, len(plan.milestones))
    current_index = min(completed, len(plan.milestones) - 1)
    (root / "ROADMAP.md").write_text(
        _roadmap(plan, completed, language=language), encoding="utf-8"
    )
    _write_milestone(state_dir, plan, current_index, language=language)
    # Per-task checkpoints (M10-REV-005): each task has its own file, so
    # parallel workers do not close the gate for one another.
    (state_dir / "handoff").mkdir(exist_ok=True)
    (state_dir / "HANDOFF.md").write_text(
        _initial_handoff(language),
        encoding="utf-8",
    )
    memory = ProjectMemory(root)
    memory.initialize()
    # R6: preflight's roots audit becomes the run's record and her proposed
    # decisions here, where Project Memory first exists. A failure to record
    # it is said and does not stop the start.
    roots_record = None
    if roots_audit:
        from .project_roots_audit import RootsAudit, sync_roots_decisions

        try:
            audit = RootsAudit.from_dict(roots_audit)
            roots_record = (audit, sync_roots_decisions(memory, audit))
        except Exception as exc:  # noqa: BLE001 - a record, never a stop
            print(f"codex-autopilot: the project roots findings were not recorded: {exc}")
    # R30: every department's version-1 rubric before the first task, while
    # the bootstrap is the only writer. A failure is not fatal here: the
    # verifier's reservation writes it, or stops that one task for the on-call.
    ensure_all_department_rubrics(memory, plan)
    finalized_plan_verification = None
    if verification_receipt is not None:
        finalized, _evidence_id, _verification_id = record_plan_verification(
            memory,
            plan,
            PlanVerificationVerdict("PASS"),
            mode=verification_receipt.mode,
            verifier_thread_id=verification_receipt.verifier_thread_id,
            verifier_turn_id=verification_receipt.verifier_turn_id,
            receipt=verification_receipt,
        )
        if finalized is None:  # pragma: no cover - PASS above makes this impossible
            raise RuntimeError("PASS plan verification did not produce a receipt")
        finalized_plan_verification = finalized.to_dict()
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
    migrated_state_schema: int | None = None
    if plan.legacy_serial and existing_raw:
        raw_schema = existing_raw.get("schema_version")
        inherited_schema = existing_raw.get("migrated_from_schema")
        if isinstance(inherited_schema, int) and not isinstance(inherited_schema, bool):
            migrated_state_schema = inherited_schema
        elif isinstance(raw_schema, int) and not isinstance(raw_schema, bool):
            # A schema-5 state without the explicit marker was emitted by
            # early v0.9 builds after converting a v0.8 plan.  Its lineage is
            # v0.8 run-state schema 4, not schema 5 itself.
            migrated_state_schema = 4 if raw_schema == 5 else raw_schema
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
        migrated_from_schema=migrated_state_schema,
        scheduler_sequence=len(ready_ids),
        task_ready_since={task_id: index for index, task_id in enumerate(ready_ids, 1)},
        planned_execution_mode=first.execution_mode,
        execution_mode=first.execution_mode,
        preflight_completed_at=utc_now(),
        prep_app_server_exited_at=utc_now(),
        project_id=project_id,
        desktop_project_id=desktop_project_id,
        completed_at=utc_now() if completed == len(plan.milestones) else None,
        plan_verification=finalized_plan_verification,
    )
    if previous_state is not None:
        # The previous state's journal went with it; the path is recorded in
        # the very first event of the new one - otherwise the snapshot is
        # recoverable only from the memory that it existed.
        append_resilience_event(
            state,
            "state_replaced",
            detail={"previous_state": str(previous_state), "reason": "--replace"},
        )
    if roots_record is not None:
        from .project_roots_audit import record_roots_audit

        record_roots_audit(state, roots_record[0], roots_record[1], occasion="preflight")
    # The roster before the first task (staffing): a snapshot stamped with
    # the plan digest, never plan.json. One that does not assemble is the
    # first reservation's stop to the on-call; a failure to write it is said
    # here, and that reservation builds it again.
    try:
        from .staffing import refresh_roster

        refresh_roster(state_dir, plan, state, occasion="bootstrap", memory=memory)
    except Exception as exc:  # noqa: BLE001 - the gate rebuilds it before any task
        print(f"codex-autopilot: the run's roster was not written: {exc}")
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
    skill_screening: str,
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
        # Explicit, and chosen at creation: Desktop carries its own Codex
        # and a separately installed CLI is a different file on a different
        # schedule, serving a different model catalog. Resolving this from
        # PATH by accident is how a project pinned to a current model was
        # told its account no longer had it. See codex_binaries.
        "# The Codex this project talks to, chosen at creation as the newest",
        "# on the machine. Desktop carries its own inside the app bundle and a",
        "# separately installed CLI is a different file - they do not serve the",
        "# same models. Change this line to move the project to another one.",
        f"binary = {_toml_string(newest_codex_binary())}",
        'permission_profile = ":workspace"',
        f"skill_path = {_toml_string(str(durable_skill_path(skill_path)))}",
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
        "# attempts per failure signature, not per task",
        "maximum_attempts = 5",
        "",
        "[runtime]",
        f"execution_strategy = {_toml_string(plan.execution_strategy)}",
        # A system banner when a task is verified, stopped, or the run is
        # done. Off: a side effect on someone else's machine must not be
        # enabled unasked. See docs/DESKTOP_RUNTIME.md.
        "desktop_notifications = false",
        # A banner only when a decision waits for her (an escalation). Off
        # until she turns it on; see docs/PIPELINE_ENGINEER.md.
        "escalation_notifications = false",
        f"max_parallel_workers = {plan.max_parallel_workers}",
        f"computer_use_slots = {plan.computer_use_slots}",
        f"full_plan_revalidation_patches = {DEFAULT_FULL_REVALIDATION_PATCHES}",
        # Hiring. Written explicitly so the file reads without knowing the
        # defaults, and so a project can be initialized with it off without
        # editing the file afterwards. "always" screens every task before it
        # gets a worker; "auto" only when there is already something to hire
        # from; "never" is off. Each screening is one more Codex thread per
        # task, and the status card reports what was spent.
        f"skill_screening = {_toml_string(skill_screening)}",
        # There is one surface. The field is written explicitly so the
        # config reads without knowing the defaults, and the read-time
        # check rejects a stale file naming the removed surface.
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


def purge_project_state(root: Path) -> Path | None:
    """Set the project state aside rather than delete it (R28).

    Before: three lines with rmtree - no copy, no record, and no test
    executed the branch. The snapshot is the previous directory itself,
    atomically renamed into a sibling; whether to remove it is the
    human's call.
    """

    state_dir = root.resolve() / STATE_DIR_NAME
    if not state_dir.exists():
        return None
    # A live run is not removed: uninstall stops the dispatcher itself, but
    # the function is public, and the guard stands where the data is.
    state_path = state_dir / "run-state.json"
    try:
        existing = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None
    except (OSError, ValueError):
        existing = None
    if existing and existing.get("status") == "RUNNING" and _pid_alive(existing.get("dispatcher_pid")):
        raise RuntimeError("Codex Autopilot is still running in this project; stop it before purging its state")
    return archive_state_dir(state_dir, reason="purged", move=True)


def archive_state_dir(state_dir: Path, *, reason: str, move: bool) -> Path:
    """A restorable snapshot of the state directory - a sibling, outside what is removed.

    The name ``<state>.<reason>-<stamp>`` follows the same convention as
    ``.codex-autopilot.stuck-<time>``: the runtime already counts such
    directories as its own (scope._is_runtime_state, the staging copy), so
    the snapshot is not charged to the worker as a write outside its scope.
    The stamp is the one migration uses: the time plus eight hex, so two
    snapshots in the same second cannot collide.

    ``move`` is a rename: atomic, and the snapshot is the previous directory
    itself (purge). Otherwise a copy: the previous directory goes on living
    (replace).
    """

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    snapshot = state_dir.with_name(f"{state_dir.name}.{reason}-{stamp}")
    if move:
        state_dir.rename(snapshot)
    else:
        shutil.copytree(state_dir, snapshot, symlinks=True)
        # ROADMAP.md lies in the project root and is overwritten together
        # with the state - without it the snapshot is incomplete.
        roadmap = state_dir.parent / "ROADMAP.md"
        if roadmap.is_file():
            shutil.copy2(roadmap, snapshot / "ROADMAP.md")
    restore = (
        f"mv {snapshot} {state_dir}"
        if move
        else f"stop the new run, then rm -rf {state_dir} && mv {snapshot} {state_dir} "
        f"(and put ROADMAP.md back from the snapshot into {state_dir.parent})"
    )
    (snapshot / "SNAPSHOT.md").write_text(
        "# Codex Autopilot state snapshot\n\n"
        f"- reason: {reason}\n"
        f"- when: {stamp[:16]} UTC\n"
        f"- from: {state_dir}\n"
        f"- how: {'moved whole' if move else 'copied; the previous directory goes on living'}\n\n"
        f"Restore: `{restore}`\n",
        encoding="utf-8",
    )
    return snapshot
