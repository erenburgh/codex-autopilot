"""A Codex home with Desktop's own sidebar state, for the placement tests.

The shape is the one Desktop writes in ``.codex-global-state.json`` and
``desktop_sidebar`` reads: ``local-projects`` by id with ``rootPaths``,
``thread-project-assignments`` by thread id, ``projectless-thread-ids``.
Tests never read the developer's own ~/.codex: a helper that did would pass
or fail by whatever that machine's Desktop happens to hold.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence


def desktop_home(
    *,
    project_id: str = "desktop-project",
    roots: Sequence[str | Path] = (),
    assignments: Mapping[str, Any] | None = None,
    projectless: Sequence[str] = (),
    others: Mapping[str, Mapping[str, Any]] | None = None,
    project: Mapping[str, Any] | None = None,
) -> Path:
    home = Path(tempfile.mkdtemp(prefix="codex-autopilot-desktop-home-"))
    projects: dict[str, Any] = {
        project_id: {"id": project_id, "rootPaths": [str(item) for item in roots], **dict(project or {})},
    }
    projects.update({key: dict(value) for key, value in (others or {}).items()})
    (home / ".codex-global-state.json").write_text(
        json.dumps({
            "local-projects": projects,
            "thread-project-assignments": dict(assignments or {}),
            "projectless-thread-ids": list(projectless),
        }),
        encoding="utf-8",
    )
    return home
