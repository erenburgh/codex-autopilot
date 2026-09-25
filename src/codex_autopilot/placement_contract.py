"""Where a task's thread is filed and where it may write - two answers, not one.

Until this contract they were one: a staged task's thread got
``cwd = <root>/.codex-autopilot/staged-artifacts/<task>/workspace`` for both.
Desktop files a thread by its cwd, and only a cwd EQUAL to a project root
lands in the project (desktop_sidebar) - so every worker and verifier of a
staged task was invisible in her project, 65 threads of the art run,
while the screener, the replanner and the on-call (cwd = root) were visible.

Contract 2 separates them: ``cwd = cfg.root`` - the thread is in the project
- and ``runtimeWorkspaceRoots = [workspace]``, under the task's own staged
permission profile, which keeps the root read-only whatever the roots are
(isolation_probe: measured, not assumed). Every turn/start repeats cwd,
roots and profile, because the server rewrites a thread's cwd on each turn.
It is used only when the isolation probe PASSed for this root, profile and
binary, and only on an App Server launched with that profile's definition;
without either the thread keeps the old placement (contract 1) and its
placement is an R5 defect with that cause, never a silent choice.

``descriptor.cwd`` keeps its meaning - the task's file workspace - so scope
baselines, staging and the descriptor's state dir are untouched.

Sessions created before this contract carry no ``placement_contract`` and a
cwd equal to their workspace; they are checked the old way to the end of
their life (a paused run's PREPARED session, a resume of its thread, the
on-call's relay repair). An ACTIVE one - the paused art run's M01
verifier, its dispatcher gone - never reaches these checks: resume
reconciliation (control._reconcile_before_resume) reads its finished thread
and retires the attempt to RETRY_WAIT, and the task's next attempt is a new
thread under this contract. Both roads are exercised in
test_placement_contract.SessionsFromBeforeTheContractTests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Collection, Mapping, Sequence

CONTRACT = 2


@dataclass(frozen=True, slots=True)
class Placement:
    cwd: Path
    workspace: Path
    # None: the thread carries no runtime roots (the plan verifier).
    workspace_roots: tuple[Path, ...] | None
    contract: int
    reason: str
    # The profile thread/start and every turn/start name: the run's own, or
    # under contract 2 with a staged workspace the task's staged profile.
    permission_profile: str = ""


def thread_placement(
    cfg: Any, workspace: Path, kind: str, *, available_profiles: Collection[str] | None = None
) -> Placement:
    """The contract for a thread whose file workspace is ``workspace``.

    ``available_profiles`` - what the connected server offers: a server not
    launched with the task's staged profile cannot run contract 2.
    """

    from .isolation_probe import isolation_proven, staged_profile_id

    root = Path(cfg.root)
    base = cfg.desktop.permission_profile
    roots: tuple[Path, ...] | None = None if kind == "plan_verifier" else (workspace,)
    if workspace == root:
        return Placement(root, workspace, roots, CONTRACT, "the task works in the canonical root", base)
    staged = staged_profile_id(workspace)
    if isolation_proven(cfg):
        if available_profiles is None or staged in available_profiles:
            return Placement(
                root, workspace, roots, CONTRACT,
                "filed at the root; its staged profile writes only its workspace", staged,
            )
        why = (
            f"the App Server serving this task was not launched with its staged profile {staged}: "
            "the thread keeps its staged workspace as cwd and is outside the project in Desktop"
        )
    else:
        why = (
            "isolation of the root is not proven (isolation-probe.json): the thread keeps its "
            "staged workspace as cwd and is outside the project in Desktop"
        )
    return Placement(workspace, workspace, roots, 1, why, base)


def session_cwd(cfg: Any, session: Mapping[str, Any], workspace: Path) -> Path:
    """The cwd a created session's thread must have: its contract's, or the old one."""

    return Path(cfg.root) if session.get("placement_contract") == CONTRACT else workspace


def session_profile(cfg: Any, session: Mapping[str, Any]) -> str:
    """The profile every turn of a created session names: the one it was created with."""

    return str(session.get("permission_profile") or cfg.desktop.permission_profile)


def roots_within(returned: Any, requested: Sequence[Path] | None) -> bool:
    """A server answer whose runtime roots are no wider than asked.

    Absent roots are no widening; any root not asked for is.
    """

    if returned is None or requested is None:
        return True
    if not isinstance(returned, list):
        return False
    asked = {str(Path(item).expanduser().resolve(strict=False)) for item in requested}
    return all(str(Path(str(item)).expanduser().resolve(strict=False)) in asked for item in returned)


def repair_contract_ok(cfg: Any, descriptor: Any, params: Mapping[str, Any]) -> bool:
    """The on-call's relay repair: the re-derived create contract is one thread_placement makes.

    control.py used to demand ``cwd == root`` and ``runtimeWorkspaceRoots ==
    [root]`` for every kind: under contract 2 a staged task's roots are its
    workspace, and the repair would be refused for every worker, verifier
    and revision (the independent check). The first replacement checked
    roots, cwd and profile each on its own, and the fourth check found it
    loose: cwd = root with the run's base profile - a thread filed at the
    root that may write the root - passed, and a mutation dropping the
    roots check left every test green. Now the roots are exactly the task's
    authenticated workspace (none for the plan verifier), and cwd and
    profile are one of the pairs thread_placement produces: the root with
    the base profile when the workspace IS the root; for a staged workspace
    the root with its staged profile (contract 2) or the workspace with the
    base profile (contract 1).
    """

    from .isolation_probe import staged_profile_id
    from .lifecycle_dispatch import _descriptor_workspace

    root = Path(cfg.root)
    base = cfg.desktop.permission_profile
    workspace = _descriptor_workspace(cfg, descriptor)
    cwd = Path(str(params.get("cwd") or "")).resolve()
    kind = str(getattr(descriptor, "kind", "") or "")
    expected_roots = None if kind == "plan_verifier" else [str(workspace)]
    if workspace == root:
        shapes = {(root, base)}
    else:
        shapes = {(root, staged_profile_id(workspace)), (workspace, base)}
    return (
        params.get("runtimeWorkspaceRoots") == expected_roots
        and (cwd, params.get("permissions")) in shapes
    )
