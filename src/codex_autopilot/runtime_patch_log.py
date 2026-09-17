"""Что рантайм в себе починил - видимой строкой, а не только в журнале.

Правка кода рантайма молча меняет поведение установки. Пользователь
вправе знать, что она была: какой модуль, когда и чем доказана. Список
читается из каталога применённых правок, который ведёт шлюз.
"""

from __future__ import annotations

import json
from pathlib import Path


def applied_patches(runtime_root: Path) -> list[dict[str, object]]:
    """Применённые правки рантайма, новые сверху."""

    folder = Path(runtime_root) / "patches"
    if not folder.is_dir():
        return []
    records = []
    for manifest in folder.glob("*/patch.json"):
        try:
            records.append(json.loads(manifest.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # Испорченная запись не повод скрывать остальные.
            continue
    return sorted(records, key=lambda item: str(item.get("at")), reverse=True)


def render_applied_patches(runtime_root: Path) -> str:
    """Одна строка для карточки статуса."""

    records = applied_patches(runtime_root)
    if not records:
        return "Runtime patches: none"
    modules = sorted(
        {
            str(change.get("module"))
            for record in records
            for change in record.get("changes") or []
        }
    )
    return f"Runtime patches: {len(records)} ({', '.join(modules)})"
