"""Where a task's thread is filed and where it may write - two answers, not one.

Until this contract they were one: a staged task's thread got
``cwd = <root>/.codex-autopilot/staged-artifacts/<task>/workspace`` for both.
Desktop files a thread by its cwd, and only a cwd EQUAL to a project root
lands in the project (desktop_sidebar) - so every worker and verifier of a
staged task was invisible in her project, 65 threads of the beyondness run,
while the screener, the replanner and the on-call (cwd = root) were visible.

Contract 2 separates them: ``cwd = cfg.root`` - the thread is in the project
- and ``runtimeWorkspaceRoots = [workspace]`` - the only place it may write.
Every turn/start repeats both, because the server rewrites a thread's cwd on
each turn. It is used only when the isolation probe PASSed for this root,
profile and binary (isolation_probe); without that the thread keeps the old
placement (contract 1) and its placement is an R5 defect with that cause,
never a silent choice.

``descriptor.cwd`` keeps its meaning - the task's file workspace - so scope
baselines, staging and the descriptor's state dir are untouched.

Sessions created before this contract carry no ``placement_contract`` and a
cwd equal to their workspace; they are checked the old way to the end of
their life (a paused run's PREPARED session, a resume of its thread, the
on-call's relay repair).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

CONTRACT = 2


@dataclass(frozen=True, slots=True)
class Placement:
    cwd: Path
    workspace: Path
    # None: the thread carries no runtime roots (the plan verifier).
    workspace_roots: tuple[Path, ...] | None
    contract: int
    reason: str


def thread_placement(cfg: Any, workspace: Path, kind: str) -> Placement:
    """The contract for a thread whose file workspace is ``workspace``."""

    from .isolation_probe import isolation_proven

    root = Path(cfg.root)
    roots: tuple[Path, ...] | None = None if kind == "plan_verifier" else (workspace,)
    if workspace == root:
        return Placement(root, workspace, roots, CONTRACT, "the task works in the canonical root")
    if isolation_proven(cfg):
        return Placement(root, workspace, roots, CONTRACT, "filed at the root; writes only its staged workspace")
    return Placement(
        workspace, workspace, roots, 1,
        "isolation of the root is not proven (isolation-probe.json): the thread keeps its "
        "staged workspace as cwd and is outside the project in Desktop",
    )


def session_cwd(cfg: Any, session: Mapping[str, Any], workspace: Path) -> Path:
    """The cwd a created session's thread must have: its contract's, or the old one."""

    return Path(cfg.root) if session.get("placement_contract") == CONTRACT else workspace


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
    """The on-call's relay repair: the re-derived create contract has a known shape.

    control.py used to demand ``cwd == root`` and ``runtimeWorkspaceRoots ==
    [root]`` for every kind: under contract 2 a staged task's roots are its
    workspace, and the repair would be refused for every worker, verifier
    and revision (the independent check). Now: the roots are exactly the
    task's authenticated workspace (none for the plan verifier), and the cwd
    is the root - or, under contract 1, that staged workspace.
    """

    from .lifecycle_dispatch import _descriptor_workspace

    workspace = _descriptor_workspace(cfg, descriptor)
    cwd = Path(str(params.get("cwd") or "")).resolve()
    kind = str(getattr(descriptor, "kind", "") or "")
    expected_roots = None if kind == "plan_verifier" else [str(workspace)]
    return params.get("runtimeWorkspaceRoots") == expected_roots and cwd in {Path(cfg.root), workspace}
