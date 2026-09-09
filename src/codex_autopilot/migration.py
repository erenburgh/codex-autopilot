from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import uuid

from .memory import ProjectMemory
from .plan import Plan


@dataclass(frozen=True, slots=True)
class MigrationResult:
    migrated: bool
    backup: Path | None = None
    report: Path | None = None
    preserved_completed: int = 0
    decisions_imported: int = 0
    observations_imported: int = 0


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def detect_v07(state_dir: Path) -> dict | None:
    path = state_dir / "run-state.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if data.get("schema_version") == 3 else None


def _backup_state(state_dir: Path) -> tuple[Path, Path]:
    migration_root = state_dir / "migrations" / f"v0.7-to-v0.8-{_stamp()}"
    backup = migration_root / "backup"
    backup.mkdir(parents=True, exist_ok=False)
    for source in state_dir.iterdir():
        if source.name == "migrations":
            continue
        destination = backup / source.name
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
    return migration_root, backup


def _verified_completed_ids(state: dict, old_plan: dict) -> list[str]:
    ids: list[str] = []
    for entry in state.get("worker_history") or []:
        if entry.get("status") in {"ROTATE", "DONE"} and isinstance(entry.get("milestone_id"), str):
            if entry["milestone_id"] not in ids:
                ids.append(entry["milestone_id"])
    if not ids:
        milestones = old_plan.get("milestones") or []
        completed = len(milestones) if state.get("status") == "DONE" else int(state.get("milestone_index") or 0)
        ids = [str(item.get("id") or f"M{index + 1}") for index, item in enumerate(milestones[:completed])]
    return ids


def _matching_prefix(old_plan: dict, new_plan: Plan, completed_ids: list[str]) -> int:
    old_items = old_plan.get("milestones") or []
    by_id = {str(item.get("id") or f"M{index + 1}"): item for index, item in enumerate(old_items)}
    preserved = 0
    for index, new_item in enumerate(new_plan.milestones):
        if new_item.id not in completed_ids:
            break
        old = by_id.get(new_item.id)
        if not old:
            break
        if str(old.get("title") or "").strip() != new_item.title or str(old.get("objective") or "").strip() != new_item.objective:
            break
        if index != preserved:
            break
        preserved += 1
    return preserved


def _meaningful_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    ignored = {
        "record only decisions that constrain later milestones.",
        "no milestone has completed yet. inspect the repository before starting work.",
        "start with milestone m1. verify the repository state directly.",
    }
    result: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip().lstrip("-* ").strip()
        if not line or line.startswith("#") or line.lower() in ignored:
            continue
        result.append(line[:8_000])
    return result


def migrate_v07(root: Path, new_plan: Plan) -> MigrationResult:
    state_dir = root / ".codex-autopilot"
    state = detect_v07(state_dir)
    if state is None:
        return MigrationResult(False)
    migration_root, backup = _backup_state(state_dir)
    old_plan_path = backup / "plan.json"
    old_plan = json.loads(old_plan_path.read_text(encoding="utf-8")) if old_plan_path.is_file() else {"milestones": []}
    completed_ids = _verified_completed_ids(state, old_plan)
    preserved = _matching_prefix(old_plan, new_plan, completed_ids)

    memory = ProjectMemory(root)
    memory.initialize()
    for milestone_id in completed_ids:
        evidence = memory.record_evidence(
            kind="migration",
            summary=f"v0.7 durable worker history recorded {milestone_id} as ROTATE or DONE after checkpoint validation.",
            created_by="v0.8-migration",
            milestone_id=milestone_id,
            role="migration_checkpoint",
        )
        # Migration evidence preserves completion history but is explicitly
        # forbidden from supporting a Truth record.
        memory.mark_milestone_complete(
            milestone_id=milestone_id,
            run_id=str(state.get("run_id") or "v0.7"),
            worker_sequence=int(state.get("worker_sequence") or 0),
            source="v0.7-checkpoint-migration",
        )

    decisions = _meaningful_lines(backup / "DECISIONS.md")
    for line in decisions:
        memory.propose_decision(
            statement=line,
            origin="agent",
            status="proposed",
            created_by="v0.8-migration",
            reason="Imported conservatively from v0.7 DECISIONS.md; original user provenance was not provable.",
        )

    observations = 0
    for filename, label in (("HANDOFF.md", "v0.7 handoff"), ("PROJECT_STATE.md", "v0.7 project-state prose")):
        path = backup / filename
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                memory.add_observation(
                    statement=f"Imported {label} (advisory, unverified):\n{text[:7_500]}",
                    created_by="v0.8-migration",
                    confidence="low",
                    reason="Agent-generated v0.7 prose is never promoted to Truth.",
                )
                observations += 1

    report = migration_root / "MIGRATION_REPORT.md"
    report.write_text(
        "\n".join(
            [
                "# Codex Autopilot v0.7 → v0.8 migration report",
                "",
                f"- Backup: `{backup}`",
                f"- v0.7 completed milestone IDs retained in memory: {', '.join(completed_ids) or 'none'}",
                f"- Matching completed prefix used for execution resume: {preserved}",
                f"- DECISIONS.md entries imported as agent-origin proposed Decisions: {len(decisions)}",
                f"- HANDOFF/PROJECT_STATE entries imported as unverified Observations: {observations}",
                "- Truth records imported from agent prose: 0",
                "- Migration evidence may preserve a completion marker but cannot support Truth.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    memory.backup()
    return MigrationResult(True, backup, report, preserved, len(decisions), observations)
