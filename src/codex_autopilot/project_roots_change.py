"""Her decisions on the saved Codex project, carried out - and nothing else (R6).

R6: "a mutation of a saved project without a recorded decision is refused".
This module is the one place the runtime changes her Codex projects, and
each change needs an accepted user decision whose text names the action, the
project and the path exactly - a permission for one project or one root does
not open another (``change_statement``).

What was learned before writing it (the independent check, from Desktop's
own bundle): Desktop keeps ``local-projects`` in .codex-global-state.json as
its source, sends ``project/update`` itself when she edits a project, and
never reads App Server's roots back. So the first design - remove a root
through App Server on her word - would always end in two spaces that
disagree: App Server with one root, Desktop still with both, her chats still
opening in the copy. Hence:

- the roots are fixed where she sees them, in Desktop ("Edit project"),
  which writes both spaces at once; that needs no record, it is her own
  edit, and the audit closes the proposal by itself afterwards;
- ``--remove-root`` and ``--set-primary-root`` change App Server only to
  FOLLOW Desktop - when Desktop already lists the wanted roots and App
  Server lags (ROOTS_DIVERGED). Asked to lead, they refuse with the Desktop
  instruction and change nothing. After the change the audit runs again and
  Desktop's rootPaths are compared; a disagreement is said and the change
  is not called done;
- ``--retire-duplicate`` deletes an App Server project that exists only
  there (invisible in Desktop, so she cannot delete it herself), holds the
  run's root, is not the run's project, has no threads - and only after a
  restorable snapshot of it is written (R28).

Whose word it is. ``authorize-project-root --yes`` records a user decision
for whoever runs it, and on beyondness the initiating agent already went
around the skill by escalating its sandbox. These decisions are therefore
confirmed by typing the project id at an interactive terminal, and refused
from inside a Codex task (CODEX_THREAD_ID set) - the confirmation must come
from her keyboard, not from an agent's --yes.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping

from .desktop_sidebar import LOCAL_PROJECTS, read_desktop_state
from .project_roots_audit import (
    DUPLICATE_APP_SERVER_PROJECTS,
    ROOTS_DIVERGED,
    _project_roots,
    _resolved,
    _same_path,
    _within,
    audit_project_roots,
    desktop_visible_ids,
    duplicate_finding,
    linked_project_id,
    project_holds,
    record_roots_audit,
    supersede_proposals,
    sync_roots_decisions,
)

REMOVE_ROOT = "AUTOPILOT_PROJECT_ROOT_REMOVE"
PRIMARY_ROOT = "AUTOPILOT_PROJECT_ROOT_PRIMARY"
DELETE_PROJECT = "AUTOPILOT_PROJECT_DELETE"


def change_statement(action: str, project_id: str, root: Path | str) -> str:
    """The exact text of her permission: one action, one project, one path."""

    return f"{action} project_id={project_id} root={_resolved(root)}"


def change_authorized(memory: Any, action: str, project_id: str, root: Path | str) -> bool:
    """An accepted user decision with exactly this text; any doubt is "no"."""

    if memory is None or not project_id:
        return False
    try:
        return memory.accepted_user_decision(change_statement(action, project_id, root)) is not None
    except Exception:
        return False


@dataclass(slots=True)
class ChangeOutcome:
    done: bool
    changed: bool
    message: str
    decision_id: str | None = None
    snapshot: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "done": self.done,
            "changed": self.changed,
            "message": self.message,
            "decision_id": self.decision_id,
            "snapshot": self.snapshot,
        }


def _record_decision(memory: Any, statement: str, confirm: Callable[[str], bool]) -> tuple[str | None, bool]:
    existing = memory.accepted_user_decision(statement)
    if existing is not None:
        return str(existing["id"]), False
    if not confirm(statement):
        return None, False
    record = memory.propose_decision(
        statement=statement,
        origin="user",
        created_by="user",
        status="accepted",
        reason="Confirmed by the user at an interactive terminal",
    )
    return str(record["id"]), True


def _desktop_roots(codex_home: Path | None, desktop_project_id: str | None) -> tuple[list[str] | None, Mapping[str, Any] | None]:
    state, _why = read_desktop_state(codex_home) if codex_home is not None else (None, "")
    local = state.get(LOCAL_PROJECTS) if state is not None else None
    project = local.get(desktop_project_id) if isinstance(local, Mapping) and desktop_project_id else None
    roots = project.get("rootPaths") if isinstance(project, Mapping) else None
    if isinstance(roots, list) and roots and all(isinstance(item, str) and item for item in roots):
        return list(roots), state
    return None, state


def _reaudit(cfg: Any, client: Any, memory: Any, codex_home: Path | None, *, occasion: str):
    from .resources import ResourceLockCoordinator
    from .run_state import StateStore

    audit = audit_project_roots(
        codex_home, cfg.root, cfg.desktop.desktop_project_id, client.list_projects(), cfg.desktop.project_id
    )
    decisions = sync_roots_decisions(memory, audit)
    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        record_roots_audit(state, audit, decisions, occasion=occasion)
        store.save(state)
    return audit


def change_project_roots(
    cfg: Any,
    client: Any,
    memory: Any,
    *,
    action: str,
    root: Path,
    codex_home: Path | None,
    confirm: Callable[[str], bool],
) -> ChangeOutcome:
    """Remove a root, or make it the first - bringing App Server in line with Desktop."""

    project_id = cfg.desktop.project_id
    if not project_id:
        return ChangeOutcome(False, False, "This run has no configured App Server project; there is nothing to change.")
    project = client.read_project(project_id)
    current = _project_roots(project)
    named = [item for item in current if _same_path(item, root)]
    if not named:
        return ChangeOutcome(False, False, f"{root} is not a root of App Server project {project_id} (its roots: {current}).")
    if action == REMOVE_ROOT:
        desired = [item for item in current if not _same_path(item, root)]
        ui = f"Codex Desktop -> project menu -> Edit project -> remove {root}"
    else:
        desired = [named[0], *(item for item in current if not _same_path(item, root))]
        ui = f"Codex Desktop -> project menu -> Edit project -> make {root} the first folder"
    if not any(_within(_resolved(cfg.root), _resolved(item)) for item in desired):
        return ChangeOutcome(False, False, f"Refused: the run's root {cfg.root} would leave project {project_id}.")
    desktop, _state = _desktop_roots(codex_home, cfg.desktop.desktop_project_id)
    if desktop is None:
        return ChangeOutcome(
            False, False,
            "Desktop's list of this project's roots cannot be read, so the two spaces cannot be compared; "
            "nothing was changed.",
        )
    if [_resolved(item) for item in desktop] != [_resolved(item) for item in desired]:
        return ChangeOutcome(
            False, False,
            f"Desktop lists {desktop} for this project. Make the change in Codex Desktop first: {ui}. "
            "Desktop writes App Server itself; Autopilot changes App Server only to follow Desktop, never ahead of it - "
            "a change on App Server alone is one Desktop never reads back. Nothing was changed.",
        )
    if [_resolved(item) for item in current] == [_resolved(item) for item in desired]:
        return ChangeOutcome(True, False, "Desktop and App Server already agree; nothing to change.")
    decision_id, _new = _record_decision(memory, change_statement(action, project_id, root), confirm)
    if decision_id is None:
        return ChangeOutcome(False, False, "Not confirmed; nothing was changed.")
    client.replace_project_roots(
        project_id,
        [Path(item) for item in desktop],
        keep=cfg.root,
        authorized=change_authorized(memory, action, project_id, root),
    )
    supersede_proposals(
        memory,
        [f"AUTOPILOT_ROOTS_FINDING {ROOTS_DIVERGED} project_id={project_id} path={desktop[0]}"],
        by_decision=decision_id,
    )
    audit = _reaudit(cfg, client, memory, codex_home, occasion="owner decision")
    diverged = [item for item in audit.findings if item.code == ROOTS_DIVERGED]
    if diverged:
        return ChangeOutcome(False, True, "App Server was changed, but the spaces still disagree: " + diverged[0].detail, decision_id)
    return ChangeOutcome(True, True, f"App Server project {project_id} now has Desktop's roots {desktop}.", decision_id)


def count_project_threads(codex_home: Path | None, project_id: str) -> int | None:
    """Threads App Server keeps under ``project_id`` - read-only; None if unknown."""

    if codex_home is None:
        return None
    candidates = sorted(
        Path(codex_home).expanduser().glob("state_*.sqlite"),
        key=lambda item: int(item.stem.split("_")[-1]) if item.stem.split("_")[-1].isdigit() else -1,
    )
    if not candidates:
        return None
    try:
        with closing(sqlite3.connect(f"file:{candidates[-1]}?mode=ro", uri=True, timeout=5)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(threads)")}
            if "project_id" not in columns:
                return None
            return int(db.execute("SELECT count(*) FROM threads WHERE project_id=?", (project_id,)).fetchone()[0])
    except sqlite3.Error:
        return None


def retire_duplicate_project(
    cfg: Any,
    client: Any,
    memory: Any,
    *,
    project_id: str,
    codex_home: Path | None,
    confirm: Callable[[str], bool],
) -> ChangeOutcome:
    """Delete an App Server-only project that duplicates the run's root."""

    from .resilience import append_resilience_event
    from .resources import ResourceLockCoordinator
    from .run_state import StateStore

    projects = {str(item.get("id") or ""): item for item in client.list_projects()}
    project = projects.get(project_id)
    if project is None:
        return ChangeOutcome(True, False, f"App Server project {project_id} does not exist; nothing to delete.")
    state, _why = read_desktop_state(codex_home) if codex_home is not None else (None, "")
    linked, _ = linked_project_id(state, codex_home, cfg.desktop.desktop_project_id)
    if project_id in {cfg.desktop.project_id, linked}:
        return ChangeOutcome(False, False, f"Refused: {project_id} is the run's own project.")
    visible = desktop_visible_ids(state)
    local = state.get(LOCAL_PROJECTS) if state is not None else None
    if visible is None or not isinstance(local, Mapping):
        return ChangeOutcome(False, False, "Refused: Desktop's project map cannot be read, so it cannot be shown the project is invisible there.")
    if project_id in visible or project_id in local:
        return ChangeOutcome(False, False, f"Refused: Desktop shows {project_id}; delete it in Codex Desktop if it is not needed.")
    if not project_holds(project, _resolved(cfg.root)):
        return ChangeOutcome(False, False, f"Refused: {project_id} does not hold the run's root {cfg.root}; it is not a duplicate of this run's project.")
    threads = count_project_threads(codex_home, project_id)
    if threads is None:
        return ChangeOutcome(False, False, "Refused: the number of threads in the project cannot be read.")
    if threads:
        return ChangeOutcome(False, False, f"Refused: {threads} thread(s) are filed in {project_id}; deleting it would orphan them.")
    finding = duplicate_finding(project, _resolved(cfg.root), visibility="App Server only")
    decision_id, _new = _record_decision(memory, change_statement(DELETE_PROJECT, project_id, finding.path), confirm)
    if decision_id is None:
        return ChangeOutcome(False, False, "Not confirmed; nothing was changed.")
    # R28: a restorable snapshot before the deletion, its path in the journal.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot = cfg.state_dir / "snapshots" / f"codex-project-{project_id}-{stamp}.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        json.dumps({"project": project, "restore": {"method": "project/create", "name": project.get("name"),
                                                     "roots": _project_roots(project)}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    client.delete_project(project_id, authorized=change_authorized(memory, DELETE_PROJECT, project_id, finding.path))
    supersede_proposals(memory, [finding.key()], by_decision=decision_id)
    remaining = {str(item.get("id") or "") for item in client.list_projects()}
    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        run = store.load()
        append_resilience_event(run, "codex_project_deleted", detail={
            "project_id": project_id, "decision_id": decision_id, "snapshot": str(snapshot),
            "gone": project_id not in remaining,
        })
        store.save(run)
    _reaudit(cfg, client, memory, codex_home, occasion="owner decision")
    if project_id in remaining:
        return ChangeOutcome(False, True, f"project/delete was sent, but {project_id} is still listed.", decision_id, str(snapshot))
    return ChangeOutcome(True, True, f"App Server project {project_id} was deleted; snapshot {snapshot}.", decision_id, str(snapshot))


def terminal_confirmation(project_id: str, *, environ: Mapping[str, str], stdin: Any, stdout: Any, ask: Callable[[str], str]) -> Callable[[str], bool]:
    """Her confirmation: typed at a terminal, never inside a Codex task."""

    def confirm(statement: str) -> bool:
        if str(environ.get("CODEX_THREAD_ID") or "").strip():
            print(
                "Refused: this runs inside a Codex task. Run the command in your own terminal - "
                "the decision must be typed by you, not answered by an agent.",
                file=stdout,
            )
            return False
        if not (getattr(stdin, "isatty", lambda: False)() and getattr(stdout, "isatty", lambda: False)()):
            print("Refused: an interactive terminal is required to confirm this decision.", file=stdout)
            return False
        print(f"This records your decision:\n  {statement}", file=stdout)
        return ask(f"Type the project id {project_id} to confirm: ").strip() == project_id

    return confirm
