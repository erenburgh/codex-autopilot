"""After a staged thread is visible in her project: did anything widen it, and did it leak.

Filing a staged task's thread at the project root (placement_contract 2) is
what makes it visible - and visible means she can open it and take a turn in
it, which is what R5 is for. The independent check read Desktop's bundle:
on resume Desktop rebuilds a thread's runtime roots from its cwd and its own
``thread-writable-roots`` (DCt/bCt; ``runtimeWorkspaceRoots =
dm(dm(currentPermissions.runtimeWorkspaceRoots, ae), L)``), and a thread of
hers in this project already carries both of the project's roots there. A turn
Desktop runs is served by Desktop's own App Server, which does not define
the task's staged profile (isolation_probe). So once she opens the thread,
the canonical root may become writable for it, and nothing Autopilot sends
can prevent that turn. The probe measures headless App Server only; this
path it cannot see.

What is closed, and how:

- the staged profile makes ``:workspace_roots`` read-only, so widened roots
  do not open the root on any turn that carries that profile - every turn
  Autopilot runs does;
- before each such turn the thread's own report is read (``environments[].
  runtimeWorkspaceRoots`` of thread/read, and the resume answer's roots):
  roots wider than the workspace are recorded on the session, in the
  journal, and signalled to the on-call (one ticket while it is open, a
  new one if the roots widen again after it was closed). Nothing is held: the
  turn/start that follows replaces the roots ("for this turn and subsequent
  turns", codex 0.154.0 schema) and names the staged profile again;
- after a task's promotion the canonical root is compared with the
  manifest: every path that changed since the task was staged must be one
  this promotion or a later promotion of another task wrote. Anything else -
  her own edit, or a thread that wrote the root - goes to the on-call with
  the paths and whether roots were seen widened on this task's threads.

Nothing here writes Desktop's state or answers an approval.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .placement_contract import roots_within

# More paths than this in one ticket say nothing more.
_PATHS_SHOWN = 50


def observed_roots(thread: Mapping[str, Any], response: Mapping[str, Any] | None = None) -> list[str]:
    """Every runtime root the server reports for a thread, in order, once each."""

    found: list[str] = []
    for environment in thread.get("environments") or ():
        if isinstance(environment, Mapping):
            found.extend(str(item) for item in environment.get("runtimeWorkspaceRoots") or ())
    if response:
        found.extend(str(item) for item in response.get("runtimeWorkspaceRoots") or ())
    return list(dict.fromkeys(found))


def check_thread_roots(
    cfg: Any,
    reservation_token: str,
    *,
    thread: Mapping[str, Any],
    response: Mapping[str, Any] | None,
    workspace: Path,
    codex_home: Any = None,
    at: str | None = None,
) -> list[str]:
    """Before a turn: roots wider than the workspace are recorded and signalled. Returns them."""

    observed = observed_roots(thread, response)
    if roots_within(observed, [workspace]):
        return []
    widened = [item for item in observed if not roots_within([item], [workspace])]

    from .lifecycle_base import _append_event, _session_by_token
    from .placement_defects import _file_once
    from .resources import ResourceLockCoordinator
    from .run_state import StateStore, utc_now

    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        task_id = str(session.get("task_id") or "")
        defect = {
            "thread_id": session.get("thread_id"),
            "task_id": task_id,
            "workspace": str(workspace),
            "observed_roots": observed,
            "widened": widened,
            "at": timestamp,
        }
        session.setdefault("runtime_roots_widened", []).append(defect)
        _append_event(state, "runtime_roots_widened", session, timestamp, detail=", ".join(widened))
        _file_once(
            cfg, state, cause="runtime_roots_widened", task_id=task_id,
            kind=str(session.get("kind") or ""), after="WIDENED",
            observation={"codex_home": codex_home}, at=timestamp, defect=defect, list_outside=False,
        )
        store.save(state)
    return widened


def canonical_outside_manifest(store: Any, task_id: str) -> list[str]:
    """Canonical paths changed since ``task_id`` was staged that no promotion explains.

    The task's baseline is the canonical manifest at staging (the copy
    skips only what the manifest skips). Explained: this task's changes,
    and the changes of every other task promoted after this copy was taken.
    """

    from .artifact_staging import (
        _changes_from_raw,
        _diff_manifests,
        _manifest,
        _manifest_from_dict,
    )

    raw = store._load_raw(task_id)
    baseline = _manifest_from_dict(raw.get("baseline"), "baseline")
    changed = {item.path for item in _diff_manifests(baseline, _manifest(store.project_root))}
    explained = {item.path for item in _changes_from_raw((raw.get("proposal") or {}).get("changes"))}
    merged_before = {str(item) for item in raw.get("promoted_when_staged") or ()}
    for other in store._promoted_task_ids():
        if other == task_id or other in merged_before:
            continue
        other_raw = store._load_raw(other)
        explained |= {item.path for item in _changes_from_raw((other_raw.get("proposal") or {}).get("changes"))}
    return sorted(changed - explained)


def record_outside_manifest(
    cfg: Any, state: Any, session: dict[str, Any], task_id: str, paths: Sequence[str], at: str
) -> str | None:
    """Record unexplained canonical changes on the session; one ticket per task. Returns it."""

    from .lifecycle_base import _append_event
    from .placement_defects import _file_once

    if not paths:
        return None
    shown = list(paths[:_PATHS_SHOWN])
    widened = any(
        item.get("runtime_roots_widened")
        for item in getattr(state, "worker_sessions", None) or ()
        if str(item.get("task_id") or "") == task_id
    )
    session["canonical_outside_manifest"] = {"paths": shown, "count": len(paths), "roots_widened": widened, "at": at}
    _append_event(state, "canonical_changed_outside_manifest", session, at, detail=", ".join(shown))
    cause = "canonical_changed_outside_manifest"
    return _file_once(
        cfg, state, cause=cause, task_id=task_id, kind=str(session.get("kind") or ""),
        after="PROMOTED", observation={}, at=at, key=f"{cause}:{task_id}", list_outside=False,
        defect={"task_id": task_id, "paths": shown, "count": len(paths), "roots_widened": widened},
    )
