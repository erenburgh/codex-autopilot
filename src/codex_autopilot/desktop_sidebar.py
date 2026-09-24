"""Where Codex Desktop draws a thread: its own rule, read, never written (R5).

What was wrong. The placement check asked App Server for ``thread.projectId``
and called the thread INSIDE when it matched. Desktop does not draw its
sidebar from that field. Read out of its bundle (26.917.62051,
``webview/assets/app-initial-*.js``, the function the spec calls DXr and the
root map $Yr): (1) a thread with a record in ``thread-project-assignments``
goes to that project; (2) a thread in ``projectless-thread-ids`` goes to no
project; (3) otherwise its normalized cwd must EQUAL one of a local
project's roots - a subfolder of a root lands in no project. App Server's
projectId reaches the assignments only through Desktop's own observe ->
adopt migration, which on the owner's machine is stuck
(``threadAssignmentsMigrated=false``, 62 ids pending). Every worker and
verifier thread of the beyondness run - 65 of them, cwd
``<root>/.codex-autopilot/staged-artifacts/<task>/workspace`` - had the
right projectId and was invisible in the project, while the check said
INSIDE. R5 says the status never passes "projectId set" off as "visible in
the project"; it did exactly that.

The rule as Desktop has it, with the independent check's corrections to the
first copy of it:

- a path is compared as Desktop's ``Fz`` normalizes it: backslashes become
  '/', then lower case. A trailing '/' is NOT removed - Desktop does not
  remove it, so ``/a/b/`` and ``/a/b`` are different keys there and here.
  The first copy compared case-sensitively after ``rstrip('/')``: a false
  OUTSIDE for ``.../project`` against the root ``.../Project`` (such a
  pair is on the owner's machine), and a test that pinned a match Desktop
  does not make;
- a project's keys are its ``rootPaths`` plus its ``rootPathAliases`` and
  ``pathAlias`` (``rXr`` in $Yr);
- an assignment whose ``projectOrigin`` is ``chatgpt``, or whose
  ``workspaceKind`` is ``projectless``, is outside every local project; an
  assignment to a project that is not among the groups falls through to the
  cwd rule instead of deciding OUTSIDE;
- when several projects share a key, the last one written wins, as a JS
  ``Map.set`` over the projects in order.

Missing or unreadable state, or no such project, is UNOBSERVABLE - never
INSIDE. The rule was measured on one Desktop build; its version is written
next to every observation, so a later false OUTSIDE after a Desktop update
reads as what it is.

Read-only by construction: Desktop keeps this file in memory and rewrites it
from its own copy, and writing it behind the app is how an earlier version
masked a wrong diagnosis.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import plistlib
from typing import Any, Mapping

INSIDE = "INSIDE"
OUTSIDE = "OUTSIDE"
UNOBSERVABLE = "UNOBSERVABLE"

STATE_FILE = ".codex-global-state.json"
ASSIGNMENTS = "thread-project-assignments"
PROJECTLESS = "projectless-thread-ids"
LOCAL_PROJECTS = "local-projects"
# The Desktop app on macOS: bundle id com.openai.codex, shipped as ChatGPT.app.
DESKTOP_APP = Path("/Applications/ChatGPT.app")
MEASURED_ON = "26.917.62051"


@dataclass(frozen=True, slots=True)
class SidebarPlacement:
    """Where the sidebar puts a thread and by which branch of the rule.

    ``rule`` is ``assignment``, ``projectless``, ``exact_root`` or ``none``;
    ``project`` the local project the rule chose, if any.
    """

    placement: str
    rule: str
    reason: str
    project: str | None = None


def normalize(path: Any) -> str:
    """Desktop's key for a path: '\\' -> '/', lower case, nothing stripped."""

    return str(path).replace("\\", "/").lower()


def read_desktop_state(codex_home: Path | None) -> tuple[Mapping[str, Any] | None, str]:
    """The Desktop state, or None with the reason it cannot be read."""

    if codex_home is None:
        return None, "Codex home is unknown"
    path = Path(codex_home).expanduser() / STATE_FILE
    if not path.is_file():
        return None, f"{path} does not exist"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path} is unreadable: {exc}"
    if not isinstance(payload, Mapping):
        return None, f"{path} is not a JSON object"
    return payload, ""


def project_keys(project: Mapping[str, Any]) -> list[str]:
    """Every key Desktop files a project under: roots, then their aliases."""

    keys: list[str] = []
    for field in ("rootPaths", "rootPathAliases", "pathAlias"):
        value = project.get(field)
        values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
        keys.extend(normalize(item) for item in values if isinstance(item, str) and item)
    return keys


def _root_map(projects: Mapping[str, Any]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for project_id, project in projects.items():
        if isinstance(project, Mapping):
            for key in project_keys(project):
                mapped[key] = str(project_id)  # last written wins, as Map.set
    return mapped


def sidebar_placement(
    thread_id: str,
    cwd: Any,
    desktop_project_id: str | None,
    state: Mapping[str, Any] | None,
    *,
    unreadable: str = "",
) -> SidebarPlacement:
    """Repeat Desktop's rule for one thread against one wanted project."""

    if state is None:
        return SidebarPlacement(UNOBSERVABLE, "none", unreadable or "Desktop state not read")
    projects = state.get(LOCAL_PROJECTS)
    if not isinstance(projects, Mapping):
        return SidebarPlacement(UNOBSERVABLE, "none", f"{LOCAL_PROJECTS} is missing")
    if not desktop_project_id or not isinstance(projects.get(desktop_project_id), Mapping):
        return SidebarPlacement(
            UNOBSERVABLE, "none", f"Desktop project {desktop_project_id!r} is not among {LOCAL_PROJECTS}"
        )
    assignments = state.get(ASSIGNMENTS)
    assignment = assignments.get(thread_id) if isinstance(assignments, Mapping) else None
    if isinstance(assignment, Mapping):
        if assignment.get("projectOrigin") == "chatgpt":
            return SidebarPlacement(OUTSIDE, "assignment", "assigned to a ChatGPT project")
        if assignment.get("workspaceKind") == "projectless":
            return SidebarPlacement(OUTSIDE, "projectless", "assignment marks the thread projectless")
        assigned = str(assignment.get("projectId") or "")
        if assigned and isinstance(projects.get(assigned), Mapping):
            if assigned == desktop_project_id:
                return SidebarPlacement(INSIDE, "assignment", "assigned to the project", assigned)
            return SidebarPlacement(OUTSIDE, "assignment", f"assigned to project {assigned}", assigned)
        # An assignment to a project Desktop has no group for decides nothing.
    projectless = state.get(PROJECTLESS)
    if isinstance(projectless, list) and thread_id in projectless:
        return SidebarPlacement(OUTSIDE, "projectless", f"listed in {PROJECTLESS}")
    if not cwd:
        return SidebarPlacement(OUTSIDE, "none", "the thread has no cwd")
    owner = _root_map(projects).get(normalize(cwd))
    if owner == desktop_project_id:
        return SidebarPlacement(INSIDE, "exact_root", "cwd equals a root of the project", owner)
    if owner is not None:
        return SidebarPlacement(OUTSIDE, "exact_root", f"cwd equals a root of project {owner}", owner)
    return SidebarPlacement(
        OUTSIDE, "none", "cwd equals no root of any project (a subfolder of a root is not in it)"
    )


def observe(
    thread_id: str,
    cwd: Any,
    desktop_project_id: str | None,
    codex_home: Path | None,
) -> SidebarPlacement:
    state, why = read_desktop_state(codex_home)
    return sidebar_placement(thread_id, cwd, desktop_project_id, state, unreadable=why)


def root_is_a_project_root(
    root: Path, desktop_project_id: str | None, codex_home: Path | None
) -> SidebarPlacement:
    """Would a thread with cwd ``root`` land in the project - preflight's question."""

    return observe("", root, desktop_project_id, codex_home)


def desktop_version(app: Path = DESKTOP_APP) -> str | None:
    """CFBundleShortVersionString of the Desktop app, or None when unreadable."""

    try:
        with (app / "Contents" / "Info.plist").open("rb") as handle:
            value = plistlib.load(handle).get("CFBundleShortVersionString")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return str(value) if value else None


def codex_home_of(client: Any) -> Path | None:
    """Codex home as App Server reported it at initialize.

    A real client that was not told falls back to the default home, as
    preflight does. Anything else that does not say is unknown - UNOBSERVABLE
    - rather than this machine's own Desktop, which a test double must never
    read.
    """

    reported = getattr(client, "codex_home", None)
    if isinstance(reported, (str, Path)) and str(reported):
        return Path(reported).expanduser()
    from .appserver import AppServerClient
    from .preflight import default_codex_home

    return default_codex_home() if isinstance(client, AppServerClient) else None
