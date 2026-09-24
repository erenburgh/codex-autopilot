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
    """Require the run's root to BE a root of the Desktop project - as Desktop compares.

    App Server ``project/update`` and ``thread/metadata/update`` can succeed in
    the App Server project namespace without changing the Electron sidebar's
    local-project metadata.  When the Desktop state file is present, treating
    that success as UI placement would be a false positive.

    This check accepted a root anywhere below a project root (``_contains``)
    while Desktop files a thread only when its cwd EQUALS a root, normalized
    its way (desktop_sidebar): a run started in a subfolder passed preflight,
    and every thread it created - the on-call's too - was outside the
    project. The independent check named the contradiction; the rule is now
    the runtime's own. ``None`` means the state file does not exist - the
    caller reports that as a finding, never as a pass.
    """

    from .desktop_sidebar import INSIDE, UNOBSERVABLE, read_desktop_state, root_is_a_project_root

    state_path = codex_home.expanduser().resolve() / ".codex-global-state.json"
    if not state_path.is_file():
        return None
    payload, why = read_desktop_state(codex_home.expanduser().resolve())
    if payload is None:
        raise ProjectAssociationError(f"could not read Desktop project metadata from {state_path}: {why}")
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
    target = target_root.expanduser().resolve()
    placed = root_is_a_project_root(target, desktop_project_id, codex_home.expanduser().resolve())
    if placed.placement == UNOBSERVABLE:
        raise ProjectAssociationError(placed.reason)
    if placed.placement != INSIDE:
        raise ProjectAssociationError(
            f"saved Desktop Codex Project {desktop_project_id!r} rootPaths do not "
            f"contain the target root {target} as one of its roots: Desktop files a thread in "
            f"the project only when its cwd equals a root ({placed.reason}); configured rootPaths="
            + json.dumps(list(raw_roots), ensure_ascii=False)
            + " (resolved: "
            + json.dumps([str(Path(item).expanduser().resolve()) for item in raw_roots], ensure_ascii=False)
            + "; Desktop compares the spelling, not the resolved path)"
        )
    return tuple(Path(item) for item in raw_roots)


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


# --- R6: permission to mutate the saved project's roots --------------------

PROJECT_ROOT_AUTHORIZATION_PREFIX = "AUTOPILOT_PROJECT_ROOT_AUTHORIZATION"


def project_root_authorization_statement(project_id: str, root: Path) -> str:
    """The canonical text of the permission to add a root to the project.

    Text, not a flag, because it is stored in Project Memory as a user
    decision and must be recognized by exact match. It names a specific
    project and a specific root: a permission given to one project does not
    open another.
    """

    canonical = Path(str(root)).expanduser().resolve()
    return (
        f"{PROJECT_ROOT_AUTHORIZATION_PREFIX} "
        f"project_id={project_id} root={canonical}"
    )


def project_root_mutation_authorized(memory: Any, project_id: str, root: Path) -> bool:
    """Is there a recorded user decision for this mutation.

    R6 refuses by default. ``ensure_project_root`` used to silently append
    the canonical root to the saved project on every creation - the runtime
    changed the user's setting without asking or telling. A missing memory
    or a read error reads as "no permission": a closed refusal must not
    depend on the store being available.
    """

    if memory is None or not project_id:
        return False
    statement = project_root_authorization_statement(project_id, root)
    try:
        return memory.accepted_user_decision(statement) is not None
    except Exception:
        return False
