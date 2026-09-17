"""A test helper: the per-task checkpoint (M10-REV-005).

The completion gate no longer looks at the shared HANDOFF.md. Every task
has its own file, and the worker must update exactly its own. All
concurrently reserved tasks used to carry one hash of the shared file,
so the first writer closed the gate for everyone else.
"""

from __future__ import annotations

from pathlib import Path

from codex_autopilot.lifecycle import task_checkpoint_path


def bump_task_checkpoint(root: Path, task_id: str, note: str = "") -> Path:
    path = task_checkpoint_path(Path(root) / ".codex-autopilot", task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_text(encoding="utf-8") if path.is_file() else f"# {task_id}\n"
    path.write_text(f"{previous}\n{note or 'progress'}\n", encoding="utf-8")
    return path
