"""Тестовый помощник: задачный чекпойнт (M10-REV-005).

Гейт завершения больше не смотрит на общий HANDOFF.md. У каждой задачи
свой файл, и воркер обязан обновить именно свой. Раньше все параллельно
зарезервированные задачи несли один хэш общего файла, поэтому первый
записавший закрывал гейт всем остальным.
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
