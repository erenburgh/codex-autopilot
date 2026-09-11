from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ProjectAssociationError(RuntimeError):
    pass


def require_desktop_project_root(
    codex_home: Path,
    desktop_project_id: str,
    target_root: Path,
) -> tuple[Path, ...] | None:
    """Validate the Desktop project's real ``rootPaths`` when available.

    App Server ``project/update`` and ``thread/metadata/update`` can succeed in
    the App Server project namespace without changing the Electron sidebar's
    local-project metadata.  When the Desktop state file is present, treating
    that success as UI placement would be a false positive.
    """

    state_path = codex_home.expanduser().resolve() / ".codex-global-state.json"
    if not state_path.is_file():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectAssociationError(
            f"could not read Desktop project metadata from {state_path}: {exc}"
        ) from exc
    projects = payload.get("local-projects")
    project = projects.get(desktop_project_id) if isinstance(projects, dict) else None
    if not isinstance(project, dict):
        raise ProjectAssociationError(
            f"saved Desktop Codex Project {desktop_project_id!r} was not found"
        )
    raw_roots = project.get("rootPaths")
    if not isinstance(raw_roots, list) or not raw_roots or not all(
        isinstance(item, str) and item.strip() for item in raw_roots
    ):
        raise ProjectAssociationError(
            f"saved Desktop Codex Project {desktop_project_id!r} has invalid rootPaths"
        )
    roots = tuple(Path(item).expanduser().resolve() for item in raw_roots)
    target = target_root.expanduser().resolve()
    if not any(_contains(root, target) for root in roots):
        raise ProjectAssociationError(
            f"saved Desktop Codex Project {desktop_project_id!r} rootPaths do not "
            f"contain the target root {target}; configured rootPaths="
            + json.dumps([str(item) for item in roots], ensure_ascii=False)
        )
    return roots


def match_saved_project(
    root: Path,
    projects: list[dict[str, Any]],
    *,
    explicit_project_id: str | None = None,
) -> dict[str, Any] | None:
    """Return an explicit target project or the unique longest-root match."""
    resolved = root.expanduser().resolve()
    if explicit_project_id:
        explicit = [
            project
            for project in projects
            if str(project.get("id") or "") == explicit_project_id
        ]
        if len(explicit) != 1:
            raise ProjectAssociationError(
                f"explicit saved Codex Project {explicit_project_id!r} was not found uniquely"
            )
        if not _project_contains(explicit[0], resolved):
            raise ProjectAssociationError(
                f"explicit saved Codex Project {explicit_project_id!r} does not contain the target root"
            )
        return explicit[0]
    matches: list[tuple[int, dict[str, Any]]] = []
    for project in projects:
        for entry in project.get("roots") or []:
            raw = entry.get("path")
            if not isinstance(raw, str):
                continue
            project_root = Path(raw).expanduser().resolve()
            try:
                resolved.relative_to(project_root)
            except ValueError:
                continue
            matches.append((len(project_root.parts), project))
    if not matches:
        return None
    longest = max(size for size, _ in matches)
    best = {str(project["id"]): project for size, project in matches if size == longest}
    if len(best) != 1:
        raise ProjectAssociationError("multiple saved Codex Projects match this path")
    return next(iter(best.values()))


def resolve_preflight_project(
    target_root: Path,
    projects: list[dict[str, Any]],
    *,
    explicit_project_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve Desktop placement separately from the worker filesystem cwd.

    Only a saved project that actually contains ``target_root`` is a valid
    placement. The initiating session's own project is deliberately NOT a
    fallback: using it moves a canonical-target task into an unrelated
    project whose roots do not contain the work, which is invisible to the
    user as a misplacement and was observed in production (M10-REV-003).
    When nothing matches, the task stays unassigned in Recents with the
    canonical cwd and the limitation is reported exactly.
    """
    target = match_saved_project(
        target_root,
        projects,
        explicit_project_id=explicit_project_id,
    )
    if target:
        return target, "explicit target" if explicit_project_id else "target"
    return None, None


def _project_contains(project: dict[str, Any], root: Path) -> bool:
    for entry in project.get("roots") or []:
        raw = entry.get("path")
        if not isinstance(raw, str):
            continue
        try:
            root.relative_to(Path(raw).expanduser().resolve())
        except ValueError:
            continue
        return True
    return False


def _contains(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True
