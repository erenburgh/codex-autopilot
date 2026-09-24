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
    desktop_linked_project_id: str | None = None,
    desktop_visible_project_ids: set[str] | None = None,
    findings: list[Any] | None = None,
) -> dict[str, Any] | None:
    """Return the target project: the one Desktop links, else explicit, else longest root.

    Two App Server projects on one root used to be a stop: "multiple saved
    Codex Projects match this path". Measured on beyondness: the initiating
    agent created "<game> - Developer" (01a0ce52) on the run's root, and
    every later start without --app-server-project-id failed. Desktop's map
    (``app-server-project-id-by-legacy-project-id-by-host``) is a dictionary:
    one Desktop project links exactly one App Server project, the one she
    sees. When it holds the root it is taken and the others are written as
    DUPLICATE_APP_SERVER_PROJECTS - not a stop, not silence. An explicit id
    that differs from the linked holder is the same case (ID_PAIR_MISMATCH,
    WARN); so is a tie with no link at all, broken deterministically below.

    A link to a project WITHOUT the root was still a FAIL here, and the
    independent check named it: preflight has just seen the root in
    Desktop's rootPaths, and another App Server project holds it - the
    target is in both spaces, only the pair is off. Now the holder is taken
    (the explicit one, else the tie-break) and ID_PAIR_MISMATCH is written as
    a WARN whose fix is one save of the project in Desktop, which writes the
    root into its linked App Server project. Nothing here refuses - neither
    the link nor an explicit flag naming a project without the root; FAIL
    is the audit's word for a target in neither space, and a record too.
    """
    from .project_roots_audit import duplicate_finding, explicit_id_finding, id_pair_finding

    resolved = root.expanduser().resolve()
    by_id = {str(project.get("id") or ""): project for project in projects}
    linked = by_id.get(desktop_linked_project_id or "")
    linked_holds = linked is not None and _project_contains(linked, resolved)

    def mismatch(other: str) -> None:
        if findings is not None:
            findings.append(id_pair_finding("", desktop_linked_project_id or "", other, resolved, by_id))

    if explicit_project_id:
        explicit = [
            project
            for project in projects
            if str(project.get("id") or "") == explicit_project_id
        ]
        pair_off = bool(desktop_linked_project_id) and desktop_linked_project_id != explicit_project_id
        if pair_off and linked_holds:
            # Desktop's pair wins, whatever the flag named.
            mismatch(explicit_project_id)
            return linked
        if len(explicit) == 1 and _project_contains(explicit[0], resolved):
            if pair_off:
                mismatch(explicit_project_id)
            return explicit[0]
        # A flag naming a project without the root used to refuse even when
        # another project held it - the same stop as the link above, one
        # argument earlier. The holder is chosen as if no flag were given
        # and the flag is written down. With no holder at all it refused
        # too, although preflight has just found the root in Desktop's
        # rootPaths: the run goes on exactly as without the flag (no App
        # Server project, Desktop files the thread by its cwd) and the
        # flag's mistake is written with its fix.
        chosen = match_saved_project(
            root, projects,
            desktop_linked_project_id=desktop_linked_project_id,
            desktop_visible_project_ids=desktop_visible_project_ids,
            findings=findings,
        )
        if findings is not None:
            findings.append(explicit_id_finding(
                explicit_project_id, str(chosen["id"]) if chosen is not None else None, resolved, by_id
            ))
        return chosen
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
    if desktop_linked_project_id and desktop_linked_project_id not in best and linked_holds:
        # Desktop links a project that holds the root through a shorter
        # one: the linked project is taken, the other written.
        mismatch(sorted(best)[0])
        return linked
    if desktop_linked_project_id in best:
        if findings is not None:
            findings.extend(
                duplicate_finding(project, resolved, visibility="unknown")
                for project_id, project in sorted(best.items())
                if project_id != desktop_linked_project_id
            )
        return best[desktop_linked_project_id]
    # No usable link (Desktop's map missing, no Desktop project, or a link
    # to a project without the root): still not a stop. The choice is
    # deterministic - a project Desktop shows first, then the lowest id (App
    # Server ids are UUIDv7, so the oldest: 01a049a3 before the agent's
    # 01a0ce52) - and every other candidate is written down.
    shown = desktop_visible_project_ids or set()
    chosen = min(best, key=lambda project_id: (project_id not in shown, project_id))
    if findings is not None:
        findings.extend(
            duplicate_finding(
                project,
                resolved,
                visibility=(
                    "unknown" if desktop_visible_project_ids is None
                    else "visible in Desktop" if project_id in shown else "App Server only"
                ),
            )
            for project_id, project in sorted(best.items())
            if project_id != chosen
        )
    if desktop_linked_project_id:
        mismatch(chosen)
    return best[chosen]


def resolve_preflight_project(
    target_root: Path,
    projects: list[dict[str, Any]],
    *,
    explicit_project_id: str | None = None,
    desktop_linked_project_id: str | None = None,
    desktop_visible_project_ids: set[str] | None = None,
    findings: list[Any] | None = None,
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
        desktop_linked_project_id=desktop_linked_project_id,
        desktop_visible_project_ids=desktop_visible_project_ids,
        findings=findings,
    )
    if target:
        # "explicit target" only when the flag's project is the one used; a
        # flag overruled by Desktop's link or by the holder is not its source.
        named = bool(explicit_project_id) and str(target.get("id") or "") == explicit_project_id
        return target, "explicit target" if named else "target"
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
