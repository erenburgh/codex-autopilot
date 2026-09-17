"""Rule R7: work stays inside the declared scope.

A task's scope is declared through its ResourceClaims with file kinds
(path/directory/glob) and access mode write or exclusive. A claim with
read access is not a write scope: the file may be read, not changed.

A task that declared no file write claim may change nothing. That is not
a formality: on the live v0.9 run ALL tasks had empty resources, so
leaving the scope was impossible by construction, and workers edited
anything. An empty claim must yield a loud defect, not a silent permit.

The paths actually changed come from git. If the observation is
unavailable (the project is not under git, git is not installed), the
audit honestly reports that the check was not performed instead of
returning "no violations".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import STATE_DIR_NAME
from .plan import Task
from .resources import (
    FILESYSTEM_RESOURCE_KINDS,
    NormalizedResourceClaim,
    _glob_matches,
    _is_within,
    normalize_task_claims,
)

WRITE_ACCESS_MODES = frozenset({"write", "exclusive"})

__all__ = [
    "WRITE_ACCESS_MODES",
    "ScopeNotObservable",
    "audit_declared_scope",
    "observe_changed_paths",
    "scope_baseline",
]


class ScopeNotObservable(Exception):
    """No way to observe the changed paths; the scope was not checked."""


def scope_baseline(root: Path) -> str | None:
    """The revision at task start, against which the diff is computed."""

    head = _git(root, "rev-parse", "HEAD")
    return head.strip() if head else None


def observe_changed_paths(root: Path, baseline: str | None) -> tuple[str, ...]:
    """Absolute paths changed since the baseline, uncommitted ones included.

    The runtime's own state (.codex-autopilot) is excluded: those files are
    written not by the worker but by Autopilot itself - the journal, the
    reservations, the handoff files. Without the exclusion every task would
    always violate R7, and the rule would turn into noise nobody reads.

    Raises ScopeNotObservable if observation is impossible - the caller
    must record that as unchecked, not as a clean result.
    """

    root = root.resolve(strict=False)
    if _git(root, "rev-parse", "--show-toplevel") is None:
        raise ScopeNotObservable(f"{root} is not a git work tree")
    # Names are taken without parsing status prefixes: git returns them as
    # is. --no-renames keeps both sides of a rename, because for the scope
    # those are two different paths, not one.
    against = baseline or "HEAD"
    changed = _git(root, "diff", "--name-only", "--no-renames", against)
    if changed is None:
        raise ScopeNotObservable(f"cannot diff against {against}")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard")
    if untracked is None:
        raise ScopeNotObservable("cannot list untracked files")
    names = set(changed.splitlines()) | set(untracked.splitlines())
    return tuple(
        sorted(
            str((root / name).resolve(strict=False))
            for name in names
            if name and not _is_runtime_state(name)
        )
    )


def _is_runtime_state(name: str) -> bool:
    head = Path(name).parts[:1]
    if not head:
        return False
    # The runtime places its own archives next door: `.codex-autopilot.stuck-<time>`
    # from --replace, snapshots of earlier runs. Their name differs, so the
    # exact comparison missed them - and the runtime's own litter was
    # charged to the worker as a write outside the declared scope.
    #
    # Measured: task M0 was blocked under R7 for 37 paths, every one inside
    # .codex-autopilot.stuck-20260914T184420. It did no work there; the
    # run's installer created the directory.
    return head[0] == STATE_DIR_NAME or head[0].startswith(STATE_DIR_NAME + ".")


def audit_declared_scope(
    task: Task,
    changed_paths: tuple[str, ...],
    *,
    project_root: Path,
) -> list[str]:
    """Paths outside the declared scope - a defect citing R7 with the list."""

    writable = [
        claim
        for claim in normalize_task_claims(task, project_root)
        if claim.kind in FILESYSTEM_RESOURCE_KINDS and claim.access in WRITE_ACCESS_MODES
    ]
    offenders = [
        path
        for path in changed_paths
        if not any(_covers(claim, path) for claim in writable)
    ]
    if not offenders:
        return []
    listed = ", ".join(offenders[:10])
    if len(offenders) > 10:
        listed += f" (and {len(offenders) - 10} more)"
    if not writable:
        return [
            f"R7: task {task.id} declared no file write claim, "
            f"yet changed {len(offenders)} paths: {listed}. "
            "The scope is declared with a ResourceClaim of kind path/directory/glob and access write"
        ]
    return [
        f"R7: task {task.id} changed {len(offenders)} paths outside the declared "
        f"scope: {listed}. Work outside the scope needs a PLAN_CHANGE_REQUEST"
    ]


def _covers(claim: NormalizedResourceClaim, path: str) -> bool:
    if claim.kind == "path":
        return claim.target == path
    if claim.kind == "directory":
        return _is_within(path, claim.target)
    return _glob_matches(claim.target, path)


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *args),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout
