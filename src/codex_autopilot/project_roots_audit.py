"""The saved Codex project's roots, audited against the run - read-only (R6).

What happened. On the beyondness run its Codex project gained a second root:
the owner added ~/Developer/beyondness (the run's root) through Desktop
("Edit project -> Add folder"), prompted by the initiating agent, while
position 0 stayed ~/Documents/<game> - an old iCloud copy of the same game
(the same .uproject, a github remote against gitlab, git objects evicted:
``git log`` failed with ``bad object HEAD`` and a pack read timeout, no
.codex-autopilot). The same agent had also created an App Server project of
its own ("<game> - Developer", 01a0ce52-...) on the run's root: invisible in
Desktop, and it made every later project match ambiguous ("multiple saved
Codex Projects match this path"). Preflight printed "Desktop project
rootPaths OK: <both roots>" and said nothing of any of it, while her own
chats in the project opened in the broken copy. R6 asks for an explicit
record and a visible message on every divergence; there was neither.

What this module does. ``audit_project_roots`` compares the run's root with
the Desktop project (``local-projects`` in .codex-global-state.json), the App
Server projects and Desktop's legacy->server id map, and returns findings:

- SIBLING_ROOTS: another root of the project looks like a copy of the run's
  root. Two roots alone are not a finding (a monorepo plus an assets folder
  is legitimate); a copy is at least two of: the same folder name (case
  aside), the same top-level markers (*.uproject, AGENTS.md, .git), the same
  repository name in a remote;
- ACTIVE_ROOT_MISMATCH: the project's first root is not the run's root - new
  chats in the project open there. Measured from the project's own
  rootPaths[0]: ``active-workspace-roots`` is Desktop's global UI state (the
  independent check: it names whatever project she has open), so it only
  adds to the finding when ``selected-project`` is this project;
- DUPLICATE_APP_SERVER_PROJECTS: another App Server project holds the run's
  root, marked visible in Desktop or App Server only;
- ID_PAIR_MISMATCH: the App Server project Desktop links to this Desktop
  project is not the one the run uses;
- ROOTS_DIVERGED: Desktop's rootPaths and the linked App Server project's
  roots differ - the two spaces disagree;
- UNVERIFIED: a key of Desktop's undocumented format is missing, so a check
  could not be made. A WARN "could not check", never a pass and never a stop.

Nothing here stops a run. A finding is printed by preflight, recorded in the
run state, and becomes a proposed decision in Project Memory with a
recommendation and a ready command (``sync_roots_decisions``); the status
card carries one line while it is open. The runtime closes the proposal
itself when a later audit no longer finds it - most roots problems are fixed
by her own edit in Desktop, which writes both spaces (the diagnosis: App
Server updated_at 1790168578644, Desktop 1790168578645).

Reading a copy on iCloud must not download it. Only top-level names are
listed and only ``lstat`` is called on the rest; a file flagged SF_DATALESS
(evicted, st_flags 0x40000000 - measured on the copy's .git) is
never opened. The remote is parsed out of .git/config rather than asked of
git; git is run only for a .git pointer file, and always with a timeout -
the evicted copy answered ``Operation timed out``. The first design looked
for the com.apple.file-provider-domain-id xattr; the independent check
measured none on that directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from .desktop_sidebar import LOCAL_PROJECTS, normalize, read_desktop_state

SF_DATALESS = 0x40000000
GIT_TIMEOUT_SECONDS = 3.0
MAPPING_KEY = "app-server-project-id-by-legacy-project-id-by-host"
ACTIVE_ROOTS_KEY = "active-workspace-roots"
SELECTED_PROJECT_KEY = "selected-project"

SIBLING_ROOTS = "SIBLING_ROOTS"
ACTIVE_ROOT_MISMATCH = "ACTIVE_ROOT_MISMATCH"
DUPLICATE_APP_SERVER_PROJECTS = "DUPLICATE_APP_SERVER_PROJECTS"
ID_PAIR_MISMATCH = "ID_PAIR_MISMATCH"
ROOTS_DIVERGED = "ROOTS_DIVERGED"
UNVERIFIED = "UNVERIFIED"
# What asks for her decision: each becomes a proposed decision in memory.
ACTIONABLE = (
    SIBLING_ROOTS,
    ACTIVE_ROOT_MISMATCH,
    DUPLICATE_APP_SERVER_PROJECTS,
    ID_PAIR_MISMATCH,
    ROOTS_DIVERGED,
)
DESKTOP_CODES = (SIBLING_ROOTS, ACTIVE_ROOT_MISMATCH)
APP_SERVER_CODES = (DUPLICATE_APP_SERVER_PROJECTS, ID_PAIR_MISMATCH, ROOTS_DIVERGED)

FINDING_PREFIX = "AUTOPILOT_ROOTS_FINDING"
DECISION_SCOPE = "codex-project-roots"
CLI = "codex-autopilot"


@dataclass(frozen=True, slots=True)
class RootsFinding:
    code: str
    status: str
    project_id: str
    path: str
    detail: str
    recommendation: str = ""
    command: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        """The finding's identity: the first line of its proposed decision."""

        return f"{FINDING_PREFIX} {self.code} project_id={self.project_id} path={self.path}"

    def statement(self) -> str:
        """The proposed decision's text - deterministic, so it deduplicates."""

        lines = [self.key(), self.detail]
        if self.recommendation:
            lines.append(f"Recommendation: {self.recommendation}")
        if self.command:
            lines.append(f"To do it: {self.command}")
        return "\n".join(lines)

    def line(self) -> str:
        text = self.detail
        if self.recommendation:
            text += f" Recommendation: {self.recommendation}"
        if self.command:
            text += f" To do it: {self.command}"
        return text

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "project_id": self.project_id,
            "path": self.path,
            "detail": self.detail,
            "recommendation": self.recommendation,
            "command": self.command,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RootsFinding":
        return cls(
            code=str(data.get("code") or ""),
            status=str(data.get("status") or "WARN"),
            project_id=str(data.get("project_id") or ""),
            path=str(data.get("path") or ""),
            detail=str(data.get("detail") or ""),
            recommendation=str(data.get("recommendation") or ""),
            command=str(data.get("command") or ""),
            evidence=dict(data.get("evidence") or {}),
        )


@dataclass(slots=True)
class RootsAudit:
    target: str
    desktop_project_id: str | None
    selected_project_id: str | None
    linked_project_id: str | None
    codex_home: str | None
    # The codes this audit could actually check. A proposal is closed only
    # by an audit that checked its code: a Desktop-only audit (no App Server
    # list) says nothing about a duplicate project.
    checked: list[str] = field(default_factory=list)
    findings: list[RootsFinding] = field(default_factory=list)
    desktop_roots: list[str] = field(default_factory=list)

    def actionable(self) -> list[RootsFinding]:
        return [item for item in self.findings if item.code in ACTIONABLE]

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "desktop_project_id": self.desktop_project_id,
            "selected_project_id": self.selected_project_id,
            "linked_project_id": self.linked_project_id,
            "codex_home": self.codex_home,
            "checked": list(self.checked),
            "findings": [item.to_dict() for item in self.findings],
            "desktop_roots": list(self.desktop_roots),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RootsAudit":
        return cls(
            target=str(data.get("target") or ""),
            desktop_project_id=data.get("desktop_project_id"),
            selected_project_id=data.get("selected_project_id"),
            linked_project_id=data.get("linked_project_id"),
            codex_home=data.get("codex_home"),
            checked=[str(item) for item in data.get("checked") or ()],
            findings=[RootsFinding.from_dict(item) for item in data.get("findings") or () if isinstance(item, Mapping)],
            desktop_roots=[str(item) for item in data.get("desktop_roots") or ()],
        )


# --- Desktop's map and the pair of ids --------------------------------------


def linked_project_id(
    state: Mapping[str, Any] | None, codex_home: Path | None, desktop_project_id: str | None
) -> tuple[str | None, str]:
    """The App Server id Desktop links to ``desktop_project_id``, or why unknown.

    The map is a dictionary per host (``local:<codex home>``), so one Desktop
    project links to exactly one App Server project: "both linked" cannot
    happen. The host key is the Codex home as Desktop spells it; when that
    spelling differs, the single local host is taken.
    """

    if state is None or not desktop_project_id:
        return None, "Desktop state is not available"
    hosts = state.get(MAPPING_KEY)
    if not isinstance(hosts, Mapping):
        return None, f"{MAPPING_KEY} is missing from Desktop state"
    wanted = set()
    if codex_home is not None:
        wanted = {f"local:{codex_home}", f"local:{Path(codex_home).expanduser()}"}
        try:
            wanted.add(f"local:{Path(codex_home).expanduser().resolve()}")
        except OSError:
            pass
    mapping = next((hosts[key] for key in hosts if key in wanted), None)
    if mapping is None:
        local = [key for key in hosts if str(key).startswith("local:")]
        if len(local) == 1:
            mapping = hosts[local[0]]
    if not isinstance(mapping, Mapping):
        return None, f"{MAPPING_KEY} has no entry for this Codex home"
    linked = mapping.get(desktop_project_id)
    if not isinstance(linked, str) or not linked:
        return None, f"Desktop project {desktop_project_id} is not in {MAPPING_KEY}"
    return linked, ""


def desktop_visible_ids(state: Mapping[str, Any] | None) -> set[str] | None:
    """Every App Server id Desktop links to one of its projects, or None."""

    if state is None:
        return None
    hosts = state.get(MAPPING_KEY)
    if not isinstance(hosts, Mapping):
        return None
    visible: set[str] = set()
    for mapping in hosts.values():
        if isinstance(mapping, Mapping):
            visible.update(str(value) for value in mapping.values() if value)
    return visible


def _resolved(raw: Any) -> Path:
    return Path(str(raw)).expanduser().resolve(strict=False)


def _same_path(left: Any, right: Any) -> bool:
    return normalize(left) == normalize(right) or _resolved(left) == _resolved(right)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _project_roots(project: Mapping[str, Any]) -> list[str]:
    return [
        str(item.get("path"))
        for item in project.get("roots") or ()
        if isinstance(item, Mapping) and item.get("path")
    ]


def project_holds(project: Mapping[str, Any], target: Path) -> bool:
    return any(_within(target, _resolved(item)) for item in _project_roots(project))


# --- is a root a copy of the target -----------------------------------------


def _dataless(path: Path) -> bool:
    """Evicted by the File Provider: lstat only, the file is not downloaded."""

    try:
        return bool(getattr(os.lstat(path), "st_flags", 0) & SF_DATALESS)
    except OSError:
        return False


def _markers(names: Iterable[str]) -> set[str]:
    found = set()
    for name in names:
        if name.lower().endswith(".uproject") or name in {"AGENTS.md", ".git"}:
            found.add(name.lower())
    return found


def _repo_name(url: str) -> str:
    text = url.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    return re.split(r"[/:]", text)[-1].lower() if text else ""


def _remote_names_from_config(text: str) -> set[str]:
    names = set()
    in_remote = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            in_remote = line.lower().startswith("[remote ")
            continue
        if in_remote and line.lower().startswith("url"):
            _, _, value = line.partition("=")
            name = _repo_name(value)
            if name:
                names.add(name)
    return names


def _remote_names(root: Path, timeout: float) -> tuple[set[str], str | None]:
    """Repository names of the root's remotes, and why they are unknown."""

    git = root / ".git"
    try:
        if git.is_dir():
            config = git / "config"
            if _dataless(config):
                return set(), f"{config} is evicted to iCloud (dataless); not downloaded"
            if not config.is_file():
                return set(), None
            return _remote_names_from_config(config.read_text(encoding="utf-8", errors="replace")), None
        if not git.is_file():
            return set(), None
    except OSError as exc:
        return set(), f"{git} is unreadable: {exc}"
    # A .git pointer (a worktree or a submodule): git itself resolves it,
    # and git may wait on an evicted object store - so it is bounded.
    try:
        answer = subprocess.run(
            ["git", "-C", str(root), "config", "--get-regexp", r"^remote\..*\.url$"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return set(), f"git did not answer within {timeout:g}s in {root}"
    except OSError as exc:
        return set(), f"git could not run in {root}: {exc}"
    names = {_repo_name(line.split(None, 1)[1]) for line in answer.stdout.splitlines() if len(line.split(None, 1)) == 2}
    return {item for item in names if item}, None


def copy_signals(root: Path, target: Path, *, timeout: float = GIT_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Why ``root`` looks like a copy of ``target`` - read without downloading."""

    unavailable: list[str] = []
    try:
        names = os.listdir(root)
    except OSError as exc:
        return {"same_name": root.name.lower() == target.name.lower(), "same_markers": False,
                "same_remote": False, "unavailable": [f"{root} cannot be listed: {exc}"], "is_copy": False}
    try:
        target_names = os.listdir(target)
    except OSError:
        target_names = []
    probes = [root / name for name in sorted(names)[:20]]
    probes += [root / ".git" / name for name in ("HEAD", "config", "description", "index", "packed-refs")]
    dataless = [str(item) for item in probes if _dataless(item)]
    if dataless:
        unavailable.append(
            f"{len(dataless)} item(s) evicted to iCloud (dataless), e.g. {dataless[0]}"
        )
    markers = _markers(names)
    target_markers = _markers(target_names)
    remotes, why = _remote_names(root, timeout)
    if why:
        unavailable.append(why)
    target_remotes, _ = _remote_names(target, timeout)
    signals = {
        "same_name": root.name.lower() == target.name.lower(),
        "same_markers": bool(target_markers) and markers == target_markers,
        "same_remote": bool(remotes & target_remotes),
    }
    return {
        **signals,
        "markers": sorted(markers),
        "remotes": sorted(remotes),
        "unavailable": unavailable,
        "is_copy": sum(1 for value in signals.values() if value) >= 2,
    }


# --- the audit ---------------------------------------------------------------


def audit_project_roots(
    codex_home: Path | None,
    target_root: Path,
    desktop_project_id: str | None,
    app_server_projects: Sequence[Mapping[str, Any]] | None,
    selected_project_id: str | None,
    *,
    extra_findings: Sequence[RootsFinding] = (),
    git_timeout: float = GIT_TIMEOUT_SECONDS,
) -> RootsAudit:
    """Read-only. ``app_server_projects`` None means App Server was not asked."""

    target = _resolved(target_root)
    state, why = read_desktop_state(codex_home) if codex_home is not None else (None, "Codex home is unknown")
    linked, linked_why = linked_project_id(state, codex_home, desktop_project_id)
    audit = RootsAudit(
        target=str(target),
        desktop_project_id=desktop_project_id,
        selected_project_id=selected_project_id,
        linked_project_id=linked,
        codex_home=str(codex_home) if codex_home is not None else None,
    )
    audit.findings.extend(extra_findings)

    def unverified(what: str, reason: str) -> None:
        audit.findings.append(RootsFinding(
            UNVERIFIED, "WARN", desktop_project_id or "", what,
            f"Could not check {what}: {reason}.",
        ))

    projects = {str(item.get("id") or ""): item for item in app_server_projects or () if isinstance(item, Mapping)}
    desktop_roots: list[str] | None = None
    if state is None:
        unverified("Desktop project roots", why)
    else:
        local = state.get(LOCAL_PROJECTS)
        project = local.get(desktop_project_id) if isinstance(local, Mapping) and desktop_project_id else None
        raw = project.get("rootPaths") if isinstance(project, Mapping) else None
        if isinstance(raw, list) and all(isinstance(item, str) and item for item in raw) and raw:
            desktop_roots = list(raw)
        else:
            unverified("Desktop project roots", f"{LOCAL_PROJECTS}[{desktop_project_id!r}].rootPaths is missing")
    selected = projects.get(selected_project_id or "")
    roots = desktop_roots if desktop_roots is not None else (_project_roots(selected) if selected else None)
    project_label = desktop_project_id or selected_project_id or ""
    if roots is not None:
        audit.desktop_roots = list(desktop_roots or ())
        audit.checked.append(SIBLING_ROOTS)
        for raw in roots:
            candidate = _resolved(raw)
            if _within(target, candidate) or _within(candidate, target):
                continue
            signals = copy_signals(candidate, target, timeout=git_timeout)
            if not signals["is_copy"]:
                continue
            named = [key.replace("same_", "same ") for key in ("same_name", "same_markers", "same_remote") if signals[key]]
            detail = (
                f"Codex project {project_label} has {len(roots)} roots, and {raw} looks like an old "
                f"copy of the run's root {target} ({', '.join(named)})."
            )
            if signals["unavailable"]:
                detail += " It is not fully available: " + "; ".join(signals["unavailable"]) + "."
            audit.findings.append(RootsFinding(
                SIBLING_ROOTS, "WARN", project_label, str(raw), detail,
                recommendation=(
                    "remove the old copy from the project in Codex Desktop - Desktop writes both "
                    "its own list and App Server's; Autopilot re-checks and closes this record itself"
                ),
                command=f"Codex Desktop -> project menu -> Edit project -> remove {raw}",
                evidence=signals,
            ))
    if desktop_roots is not None:
        audit.checked.append(ACTIVE_ROOT_MISMATCH)
        first = desktop_roots[0]
        active = state.get(ACTIVE_ROOTS_KEY) if state is not None else None
        selected_here = state is not None and state.get(SELECTED_PROJECT_KEY) == desktop_project_id
        extra = ""
        if selected_here and isinstance(active, list) and not any(_same_path(item, target) for item in active):
            extra = f" Desktop's active workspace roots are {active}."
        if not _same_path(first, target) or extra:
            opens = first if not _same_path(first, target) else str(active[0] if active else first)
            audit.findings.append(RootsFinding(
                ACTIVE_ROOT_MISMATCH, "WARN", project_label, str(opens),
                f"New chats in the project will open in {opens}; the run lives in {target}.{extra}",
                recommendation=(
                    f"make {target} the project's first root in Codex Desktop, or remove {opens} "
                    "from the project if it is an old copy"
                ),
                command=f"Codex Desktop -> project menu -> Edit project -> remove {opens}",
                evidence={"root_paths": desktop_roots, "active_workspace_roots": active, "selected_here": selected_here},
            ))
    if app_server_projects is None:
        return audit
    visible = desktop_visible_ids(state)
    if linked is None:
        unverified("the Desktop/App Server id pair", linked_why)
    else:
        audit.checked.append(ID_PAIR_MISMATCH)
        if selected_project_id and linked != selected_project_id and not any(
            item.code == ID_PAIR_MISMATCH for item in audit.findings
        ):
            audit.findings.append(id_pair_finding(desktop_project_id or "", linked, selected_project_id, target, projects))
        linked_project = projects.get(linked)
        if desktop_roots is not None and linked_project is not None:
            audit.checked.append(ROOTS_DIVERGED)
            server_roots = _project_roots(linked_project)
            if [_resolved(item) for item in server_roots] != [_resolved(item) for item in desktop_roots]:
                audit.findings.append(diverged_finding(linked, desktop_roots, server_roots, target))
    audit.checked.append(DUPLICATE_APP_SERVER_PROJECTS)
    ours = {linked, selected_project_id} - {None}
    for project_id, project in sorted(projects.items()):
        if project_id in ours or not project_holds(project, target):
            continue
        seen = "unknown" if visible is None else ("visible in Desktop" if project_id in visible else "App Server only")
        finding = duplicate_finding(project, target, visibility=seen)
        # match_saved_project wrote the same duplicate without visibility.
        audit.findings = [item for item in audit.findings if item.key() != finding.key()]
        audit.findings.append(finding)
    return audit


def duplicate_finding(project: Mapping[str, Any], target: Path, *, visibility: str) -> RootsFinding:
    """Another App Server project holding the run's root."""

    project_id = str(project.get("id") or "")
    holder = next((item for item in _project_roots(project) if _within(target, _resolved(item))), str(target))
    if visibility == "visible in Desktop":
        recommendation = "delete it in Codex Desktop if it is not needed: Desktop shows it"
        command = f"Codex Desktop -> {project.get('name') or project_id} -> Delete project"
    else:
        # The proposal names the exact authorization the command records -
        # accepting it is typing the project id at her own terminal.
        from .project_roots_change import DELETE_PROJECT, change_statement

        recommendation = (
            "delete it: it is invisible in Desktop and only makes the project choice ambiguous; "
            f"the command records your decision {change_statement(DELETE_PROJECT, project_id, holder)!r}"
        )
        command = f"{CLI} authorize-project-root --project {target} --retire-duplicate {project_id}"
    return RootsFinding(
        DUPLICATE_APP_SERVER_PROJECTS, "WARN", project_id, holder,
        f"App Server project {project_id} ({project.get('name') or 'unnamed'}) also holds the run's root "
        f"{target} through {holder}; {visibility}.",
        recommendation=recommendation,
        command=command,
        evidence={"visibility": visibility, "roots": _project_roots(project)},
    )


def id_pair_finding(
    desktop_project_id: str,
    linked: str,
    other: str,
    target: Path,
    projects: Mapping[str, Mapping[str, Any]],
) -> RootsFinding:
    """Desktop links its project to ``linked``; the run was about to use ``other``.

    WARN when the linked project holds the target - the runtime takes the
    project she sees, deterministically. FAIL only when it does not: then the
    target is in neither space as one pair, and no choice is consistent.
    """

    linked_project = projects.get(linked)
    holds = linked_project is not None and project_holds(linked_project, target)
    name = f"Desktop project {desktop_project_id}" if desktop_project_id else "The Desktop project"
    if holds:
        detail = (
            f"{name} is linked to App Server project {linked}, not {other}; "
            f"the run uses {linked}, the one Desktop shows."
        )
        return RootsFinding(
            ID_PAIR_MISMATCH, "WARN", linked, str(target), detail,
            recommendation=f"pass --app-server-project-id {linked} next time",
            command=f"--app-server-project-id {linked}",
            evidence={"linked": linked, "requested": other},
        )
    detail = (
        f"{name} is linked to App Server project {linked}, which "
        + ("does not exist" if linked_project is None else f"does not hold the run's root {target}")
        + f"; {other} holds it but is not linked to the Desktop project, so a task would carry an "
        "App Server project Desktop does not show."
    )
    return RootsFinding(
        ID_PAIR_MISMATCH, "FAIL", linked, str(target), detail,
        recommendation=(
            "open the project in Codex Desktop and save its folders once (Edit project) so Desktop "
            f"writes {target} into its linked App Server project, then start again"
        ),
        command="Codex Desktop -> project menu -> Edit project -> Save",
        evidence={"linked": linked, "requested": other},
    )


def diverged_finding(linked: str, desktop_roots: Sequence[str], server_roots: Sequence[str], target: Path) -> RootsFinding:
    desktop = [_resolved(item) for item in desktop_roots]
    server = [_resolved(item) for item in server_roots]
    extra = [item for item in server_roots if _resolved(item) not in desktop]
    if extra:
        command = f"{CLI} authorize-project-root --project {target} --remove-root {extra[0]}"
        recommendation = f"bring App Server in line with Desktop: remove {extra[0]} from App Server project {linked}"
    elif sorted(map(str, desktop)) == sorted(map(str, server)):
        command = f"{CLI} authorize-project-root --project {target} --set-primary-root {desktop_roots[0]}"
        recommendation = f"bring App Server's order in line with Desktop: {desktop_roots[0]} first"
    else:
        missing = [item for item in desktop_roots if _resolved(item) not in server]
        command = f"Codex Desktop -> project menu -> Edit project -> Save (Desktop writes {missing[0]} to App Server)"
        recommendation = "save the project once in Codex Desktop so it writes its roots to App Server"
    return RootsFinding(
        ROOTS_DIVERGED, "WARN", linked, str(desktop_roots[0]),
        f"Desktop and App Server disagree on the roots of the project: Desktop {list(desktop_roots)}, "
        f"App Server project {linked} {list(server_roots)}.",
        recommendation=recommendation,
        command=command,
        evidence={"desktop": list(desktop_roots), "app_server": list(server_roots)},
    )


def preflight_project_pair(
    codex_home: Path, desktop_project_id: str | None
) -> tuple[str | None, set[str] | None]:
    """The App Server id Desktop links, and every id Desktop shows (None: unknown)."""

    state, _why = read_desktop_state(codex_home)
    linked, _ = linked_project_id(state, codex_home, desktop_project_id)
    return linked, desktop_visible_ids(state)


# --- R6 records: proposals in Project Memory ---------------------------------


def roots_decisions(memory: Any) -> list[dict[str, Any]]:
    """Every decision this audit ever proposed, with full statements."""

    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(50):
        page = memory.list_records(
            categories=["decision"], scope=DECISION_SCOPE, limit=20, cursor=cursor, full_statements=True
        )
        records.extend(page.records)
        cursor = page.next_cursor
        if not cursor:
            break
    return records


def _record_key(record: Mapping[str, Any]) -> str:
    return str(record.get("statement") or "").split("\n", 1)[0]


def sync_roots_decisions(memory: Any, audit: RootsAudit, *, actor: str = "runtime") -> dict[str, str]:
    """Propose a decision per open finding; close the ones that are gone.

    ``origin="environment"``: the runtime proposes from what it measured
    (``memory.ORIGINS`` has no "runtime" - the first design would have been
    refused with MemoryValidationError). One proposal per finding key, not
    one per audit: preflight, bootstrap and every creation audit again. A
    proposal she rejected is not proposed again. A proposal whose finding a
    later audit (of the same kind) no longer sees is superseded by the
    runtime with the evidence - her edit in Desktop fixes most of these, and
    nothing else would ever close them.
    """

    records = roots_decisions(memory)
    by_key: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_key.setdefault(_record_key(record), []).append(record)
    current = {item.key(): item for item in audit.actionable()}
    open_ids: dict[str, str] = {}
    for key, finding in current.items():
        existing = by_key.get(key, [])
        proposed = [item for item in existing if item.get("status") == "proposed"]
        if proposed:
            open_ids[key] = str(proposed[0]["id"])
            continue
        if any(item.get("status") == "rejected" for item in existing):
            continue
        record = memory.propose_decision(
            statement=finding.statement(),
            origin="environment",
            created_by=actor,
            status="proposed",
            scope=DECISION_SCOPE,
            reason="Codex project roots audit: " + "; ".join(
                f"{name}={value}" for name, value in sorted(dict(finding.evidence).items())
            )[:4000],
        )
        open_ids[key] = str(record["id"])
    for key, existing in by_key.items():
        if key in current:
            continue
        parts = key.split()
        code = parts[1] if len(parts) > 1 else ""
        if code not in audit.checked:
            continue
        for record in existing:
            if record.get("status") == "proposed":
                memory.set_decision_status(
                    str(record["id"]),
                    "superseded",
                    actor=actor,
                    reason=(
                        f"Closed by the runtime: a later audit of {audit.target} no longer finds it "
                        f"(Desktop roots {audit.desktop_roots or 'unread'})."
                    ),
                )
    return open_ids


def supersede_proposals(memory: Any, keys: Iterable[str], *, by_decision: str) -> list[str]:
    """Her authorization answers a proposal: it stops being open."""

    wanted = set(keys)
    closed = []
    for record in roots_decisions(memory):
        if record.get("status") == "proposed" and _record_key(record) in wanted:
            memory.set_decision_status(
                str(record["id"]), "superseded", actor="user",
                reason=f"Answered by the user's decision {by_decision}",
            )
            closed.append(str(record["id"]))
    return closed


def record_roots_audit(state: Any, audit: RootsAudit, decisions: Mapping[str, str], *, occasion: str) -> None:
    """The latest audit on the run state; a journal event when the findings change."""

    from .resilience import append_resilience_event
    from .run_state import utc_now

    previous = state.roots_audit if isinstance(getattr(state, "roots_audit", None), dict) else {}
    earlier = [RootsFinding.from_dict(item) for item in previous.get("findings") or () if isinstance(item, Mapping)]
    before = sorted(item.key() for item in earlier if item.code in ACTIONABLE)
    # What this audit could not check is carried over, not forgotten: a
    # wake-up reads Desktop only and says nothing of App Server projects.
    carried = [item for item in earlier if item.code in ACTIONABLE and item.code not in audit.checked]
    audit.findings.extend(carried)
    decisions = dict(decisions)
    for item in carried:
        known = (previous.get("decisions") or {}).get(item.key())
        if known:
            decisions.setdefault(item.key(), known)
    after = sorted(item.key() for item in audit.actionable())
    state.roots_audit = {
        **audit.to_dict(),
        "decisions": decisions,
        "occasion": occasion,
        "checked_at": utc_now(),
    }
    if before != after:
        append_resilience_event(
            state,
            "roots_audit",
            detail={
                "occasion": occasion,
                "findings": [{"code": item.code, "status": item.status, "path": item.path, "detail": item.detail}
                             for item in audit.actionable()],
                "decisions": decisions,
            },
        )


def refresh_run_roots_audit(cfg: Any, client: Any, *, occasion: str) -> RootsAudit | None:
    """Audit before a creation and at a wake-up (R6: "preflight and every creation").

    Never raises and never stops the creation: the audit is a record, and a
    failure to make it is printed, not escalated into the launch. The Codex
    home is the one App Server reported; a test double that names none is
    not audited against this machine's Desktop.
    """

    from .desktop_sidebar import codex_home_of
    from .memory import ProjectMemory
    from .resources import ResourceLockCoordinator
    from .run_state import StateStore

    try:
        desktop_id = getattr(cfg.desktop, "desktop_project_id", None)
        store = StateStore(cfg.state_dir)
        recorded = None
        if client is not None:
            codex_home = codex_home_of(client)
        else:
            # A wake-up has no App Server: Desktop is read at the home the
            # last audit was made with, else at this user's own Codex home -
            # a run paused before this audit existed is audited at its first
            # wake-up, as the independent check asked.
            from .preflight import default_codex_home

            recorded = store.load().roots_audit or {}
            codex_home = Path(recorded["codex_home"]) if recorded.get("codex_home") else default_codex_home()
        if not desktop_id or codex_home is None:
            return None
        projects = None
        if client is not None:
            try:
                projects = client.list_projects()
            except Exception:
                projects = None
        audit = audit_project_roots(codex_home, cfg.root, desktop_id, projects, cfg.desktop.project_id)
        if recorded == {} and not audit.checked:
            # Nothing could be checked at a guessed home: no record is made
            # of a Desktop that does not know this project.
            return None
        memory_file = cfg.root / ".codex-autopilot" / "memory.sqlite3"
        decisions: dict[str, str] = {}
        if audit.actionable() or memory_file.is_file():
            decisions = sync_roots_decisions(ProjectMemory(cfg.root), audit)
        with ResourceLockCoordinator(store, cfg.root).transaction():
            state = store.load()
            record_roots_audit(state, audit, decisions, occasion=occasion)
            store.save(state)
        return audit
    except Exception as exc:  # noqa: BLE001 - the audit is a record, never a stop
        print(f"codex-autopilot: the project roots audit was not made: {exc}", flush=True)
        return None


# --- the status card ---------------------------------------------------------


def roots_status_line(cfg: Any, state: Any, *, russian: bool = False) -> str:
    """One line while a roots finding waits for her; empty otherwise.

    Computed from a fresh Desktop-side audit (her Desktop edit shows here at
    once) plus the App Server findings of the last recorded audit, and the
    open proposal's id. A finding she rejected is not shown.
    """

    from .preflight import default_codex_home

    desktop_id = getattr(cfg.desktop, "desktop_project_id", None)
    if not desktop_id:
        return ""
    recorded = RootsAudit.from_dict(state.roots_audit) if isinstance(getattr(state, "roots_audit", None), dict) else None
    home = Path(recorded.codex_home) if recorded and recorded.codex_home else default_codex_home()
    try:
        fresh = audit_project_roots(home, cfg.root, desktop_id, None, cfg.desktop.project_id)
    except Exception:
        return ""
    findings = [item for item in fresh.findings if item.code in DESKTOP_CODES]
    if recorded is not None:
        findings += [item for item in recorded.findings if item.code in APP_SERVER_CODES]
    if not findings:
        return ""
    # The latest record per finding decides: open while proposed, gone once
    # she answered it (accepted, rejected) or the runtime closed it.
    latest: dict[str, tuple[str, str]] = {}
    memory_path = cfg.root / ".codex-autopilot" / "memory.sqlite3"
    if memory_path.is_file():
        try:
            from .memory import ProjectMemory

            for record in roots_decisions(ProjectMemory(cfg.root)):
                key = _record_key(record)
                if key not in latest or record.get("status") == "proposed":
                    latest[key] = (str(record.get("status") or ""), str(record.get("id") or ""))
        except Exception:
            latest = {}
    shown = [item for item in findings if latest.get(item.key(), ("proposed", ""))[0] == "proposed"]
    if not shown:
        return ""
    count = len(fresh.desktop_roots)
    parts = []
    copies = [item.path for item in shown if item.code == SIBLING_ROOTS]
    active = [item.path for item in shown if item.code == ACTIVE_ROOT_MISMATCH]
    if active:
        is_copy = active[0] in copies
        parts.append(
            (f"активный — копия {active[0]}" if is_copy else f"активный — {active[0]}, прогон в {fresh.target}")
            if russian
            else (f"the active one is a copy, {active[0]}" if is_copy else f"the active one is {active[0]}, the run is in {fresh.target}")
        )
    elif copies:
        parts.append(f"копия цели: {copies[0]}" if russian else f"a copy of the target: {copies[0]}")
    for item in shown:
        if item.code == DUPLICATE_APP_SERVER_PROJECTS:
            parts.append(f"лишний проект App Server {item.project_id}" if russian else f"extra App Server project {item.project_id}")
        elif item.code in {ID_PAIR_MISMATCH, ROOTS_DIVERGED}:
            parts.append(item.code)
    ids = [latest[item.key()][1] for item in shown if item.key() in latest]
    head = (f"Проект Codex: {count} корня" if russian else f"Codex project: {count} roots") if count > 1 else ("Проект Codex" if russian else "Codex project")
    tail = ""
    if ids:
        tail = ("; см. решение " if russian else "; see decision ") + ", ".join(dict.fromkeys(ids))
    else:
        tail = "; " + shown[0].command
    return f"{head}, " + ", ".join(parts) + tail if parts else head + tail


# --- the CLI's project when --project is not given ---------------------------

RUN_CREATING_COMMANDS = frozenset({"bootstrap", "start-skill", "preflight"})


class CliProjectRedirect(RuntimeError):
    """The cwd is a sibling root of a project whose run lives elsewhere."""


def sibling_runs(cwd: Path, codex_home: Path) -> list[Path]:
    """Other roots of the Desktop projects holding ``cwd`` that carry a run."""

    from .config import STATE_DIR_NAME

    state, _why = read_desktop_state(codex_home)
    local = state.get(LOCAL_PROJECTS) if state is not None else None
    if not isinstance(local, Mapping):
        return []
    here = _resolved(cwd)
    found: list[Path] = []
    for project in local.values():
        roots = project.get("rootPaths") if isinstance(project, Mapping) else None
        if not isinstance(roots, list):
            continue
        resolved = [_resolved(item) for item in roots if isinstance(item, str) and item]
        if not any(_within(here, item) for item in resolved):
            continue
        for item in resolved:
            if _within(here, item) or item in found:
                continue
            if (item / STATE_DIR_NAME / "config.toml").is_file():
                found.append(item)
    return found


def resolve_cli_project(command: str, explicit: Path | None, cwd: Path, codex_home: Path) -> tuple[Path, str]:
    """The project a CLI command acts on, and a note to print when it moved.

    Every subcommand defaulted to ``Path.cwd()``, so "no --project" could not
    be told from "--project <cwd>". A chat opened in a stale sibling root
    (the iCloud copy) then pointed bootstrap and start-skill at a new
    run in the copy. Now: an explicit --project is used as given; a cwd with
    a run is used; otherwise the other roots of the same Desktop project are
    looked at. For a command that creates a run, a sibling's run is a refusal
    naming it. For any other command a single sibling run is used, and said -
    the run goes on by default.
    """

    from .config import STATE_DIR_NAME

    if explicit is not None:
        return explicit, ""
    here = cwd.expanduser().resolve()
    if (here / STATE_DIR_NAME / "config.toml").is_file():
        return here, ""
    runs = sibling_runs(here, codex_home)
    if not runs:
        return here, ""
    if command in RUN_CREATING_COMMANDS:
        raise CliProjectRedirect(
            f"{here} has no Autopilot run, but the same Codex project has one at {runs[0]}"
            + (f" (and {len(runs) - 1} more)" if len(runs) > 1 else "")
            + f". Run the command there (--project {runs[0]}), or pass --project {here} to start a separate run here."
        )
    if len(runs) > 1:
        raise CliProjectRedirect(
            f"{here} has no Autopilot run; runs of the same Codex project are at "
            + ", ".join(str(item) for item in runs) + ". Pass --project with one of them."
        )
    return runs[0], f"codex-autopilot: {here} has no run; using the run at {runs[0]} (another root of the same Codex project)."
