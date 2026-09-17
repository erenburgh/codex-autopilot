"""What the runtime repaired in itself - as a visible line, not only in the journal.

A repair of the runtime's code silently changes how the installation
behaves. The user is entitled to know it happened: which module, when, and
what proved it. The list is read from the catalogue of applied patches the
gateway maintains.
"""

from __future__ import annotations

import json
from pathlib import Path


def applied_patches(runtime_root: Path) -> list[dict[str, object]]:
    """Applied runtime patches, newest first."""

    folder = Path(runtime_root) / "patches"
    if not folder.is_dir():
        return []
    records = []
    for manifest in folder.glob("*/patch.json"):
        try:
            records.append(json.loads(manifest.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # A corrupt record is no reason to hide the others.
            continue
    return sorted(records, key=lambda item: str(item.get("at")), reverse=True)


def render_applied_patches(runtime_root: Path) -> str:
    """One line for the status card."""

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
