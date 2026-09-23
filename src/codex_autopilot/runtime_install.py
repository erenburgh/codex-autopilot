"""A proven runtime patch is staged in the project and installed atomically.

The independent check found two defects in how a repair reached the runtime,
and neither was a matter of care:

- the on-call's command wrote into the installed runtime directly
  (``~/Library/Application Support/CodexAutopilot/current/runtime``). That
  tree is outside the project; the engineer's thread runs with the
  ``:workspace`` permission profile and the project root as its only
  writable root. A write there raises a permission request, the turn stops
  on it, and answering approvals is her boundary - never ours. The live run
  journal holds not one ``runtime_patched`` event;
- the write went file by file into the tree every dispatcher of every run
  imports from. The runtime imports modules lazily inside functions, so a
  neighbour's dispatcher could load half an old set and half a new one.

So the repair is split at the sandbox line:

1. inside the engineer's turn, the gateway proves the patch (reproduction
   test red then green, whole suite green, guarded definitions unchanged)
   in a copy under the project's state directory, and the proven sources
   are staged there too (``stage_proven_patch``) - all within the cwd;
2. while a patch is staged, the run drains: the frontier reserves nothing
   new (``runtime_patch_pending``), so the sessions in flight finish on the
   code they started with;
3. a process outside the sandbox - the wake-up, which launchd runs from the
   installation's own launcher - installs it only when no run registered
   for the sweep has a live automatic dispatcher (``install_when_quiet``):
   it copies the current version to a new ``<version>.repaired-<stamp>``
   directory (the name the installer already preserves), checks each module
   still matches the text the patch was proven against, writes the set and
   its backup there, and switches ``current`` with one ``rename`` of a fresh
   symlink. A process that started before the switch keeps the old tree
   (dispatchers put their own resolved source root first on PYTHONPATH);
   every process started after it reads the new one. No process ever sees
   half a set.

A patch that no longer fits the tree (the installation changed under it) is
refused, not forced, and the refusal files a ticket so the on-call proves it
again: a staged patch never waits in silence.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterable

PATCH_DIR = "runtime-patches"
PENDING = "pending"
INSTALLED = "installed"
REFUSED = "refused"
WITHDRAWN = "withdrawn"
STAGING = ".staging"


class RuntimeInstallError(RuntimeError):
    """The staged patch could not be installed; the live tree is untouched."""


def patch_root(state_dir: Path) -> Path:
    return Path(state_dir) / PATCH_DIR


def staging_parent(state_dir: Path) -> Path:
    """Where the gateway copies the runtime to prove a patch: inside the project."""

    return patch_root(state_dir) / STAGING


def stage_proven_patch(state_dir: Path, proven: Any) -> Path:
    """Stage a proven patch in the project. Atomic: a rename of a finished directory."""

    from .runtime_repair import _sha256

    record = proven.record
    root = patch_root(state_dir)
    target = root / PENDING / record.patch_id
    if target.exists():
        raise RuntimeInstallError(f"{record.patch_id} is already staged")
    scratch = root / f".{record.patch_id}.{os.getpid()}"
    shutil.rmtree(scratch, ignore_errors=True)
    (scratch / "modules").mkdir(parents=True)
    for module, source in proven.sources.items():
        (scratch / "modules" / module).write_text(source, encoding="utf-8")
    # The text each module was proven against, kept next to the new one:
    # what a patch changed on the acceptance path is read from the pair
    # (``ladder_grants``), never from what the ticket says about it.
    (scratch / "originals").mkdir()
    for module, source in proven.originals.items():
        if source is not None:
            (scratch / "originals" / module).write_text(source, encoding="utf-8")
    (scratch / "test.py").write_text(proven.test_source, encoding="utf-8")
    manifest = {
        "kind": "patch",
        "record": record.to_dict(),
        "test_name": proven.test_name,
        "originals_sha256": {
            module: (None if text is None else _sha256(text))
            for module, text in proven.originals.items()
        },
    }
    (scratch / "patch.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(scratch, target)
    return target


def stage_revert(state_dir: Path, patch_id: str, *, at: str) -> Path:
    """Stage taking back an installed patch; installed the same way as a patch."""

    root = patch_root(state_dir)
    target = root / PENDING / f"revert-{patch_id}"
    scratch = root / f".revert-{patch_id}.{os.getpid()}"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    (scratch / "patch.json").write_text(
        json.dumps({"kind": "revert", "patch_id": patch_id, "at": at}, sort_keys=True),
        encoding="utf-8",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(scratch, target)
    return target


def withdraw_staged(state_dir: Path, patch_id: str) -> bool:
    """Take back a patch that was staged and never installed. Kept, not deleted."""

    source = patch_root(state_dir) / PENDING / patch_id
    if not source.is_dir():
        return False
    target = patch_root(state_dir) / WITHDRAWN / patch_id
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    return True


def patch_status(state_dir: Path, patch_id: str) -> str:
    """Where a staged patch is now: pending, installed, reverted, withdrawn, refused or absent.

    A revert staged or installed for it makes it "reverted": the change is
    being taken back, and nothing that rested on it still does.
    """

    root = patch_root(state_dir)

    def there(where: str, name: str) -> bool:
        return (root / where / name / "patch.json").is_file()

    if there(PENDING, f"revert-{patch_id}") or there(INSTALLED, f"revert-{patch_id}"):
        return "reverted"
    for where in (PENDING, INSTALLED, WITHDRAWN, REFUSED):
        if there(where, patch_id):
            return where
    return "absent"


def pending_entries(state_dir: Path) -> list[Path]:
    folder = patch_root(state_dir) / PENDING
    if not folder.is_dir():
        return []
    return sorted(item for item in folder.iterdir() if (item / "patch.json").is_file())


def runtime_patch_pending(cfg: Any) -> bool:
    """A proven patch waits to be installed: the run drains so it can be.

    Never raises: an unreadable directory drains nothing.
    """

    try:
        return bool(pending_entries(Path(cfg.state_dir)))
    except Exception:  # noqa: BLE001 - housekeeping may never stop a reservation
        return False


def install_root_from_env() -> Path | None:
    configured = os.environ.get("CODEX_AUTOPILOT_INSTALL_ROOT")
    return Path(configured).expanduser() if configured else None


def install_pending(
    install_root: Path,
    state_dirs: Iterable[Path],
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Install every staged patch of these projects into a new tree, then switch.

    The caller has already made sure no dispatcher is alive. Returns what was
    installed and refused; installs nothing when nothing fits.
    """

    from .runtime_repair import (
        PatchRecord,
        ProvenPatch,
        RuntimeRepairError,
        RuntimeTree,
        install_proven_patch,
        revert_runtime_patch,
    )

    current = Path(install_root) / "current"
    if not current.is_symlink():
        raise RuntimeInstallError(
            f"{current} is not the installation's version symlink; a staged patch is "
            "installed only into an installation"
        )
    live = current.resolve()
    entries = [
        (Path(state_dir), entry)
        for state_dir in state_dirs
        for entry in pending_entries(Path(state_dir))
    ]
    if not entries:
        return {"installed": [], "refused": []}
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now()))
    base = live.name.split(".repaired-")[0]
    tree = Path(install_root) / f"{base}.repaired-{stamp}"
    suffix = 1
    while tree.exists():
        suffix += 1
        tree = Path(install_root) / f"{base}.repaired-{stamp}-{suffix}"
    shutil.copytree(live, tree, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
    runtime = RuntimeTree(src=tree / "runtime" / "src", tests=tree / "runtime" / "tests")
    installed: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for state_dir, entry in entries:
        try:
            manifest = json.loads((entry / "patch.json").read_text(encoding="utf-8"))
            if manifest.get("kind") == "revert":
                revert_runtime_patch(str(manifest["patch_id"]), tree=runtime)
                installed.append({"state_dir": str(state_dir), "entry": entry.name, "kind": "revert"})
                continue
            record = PatchRecord.from_dict(manifest["record"])
            install_proven_patch(
                runtime,
                ProvenPatch(
                    record=record,
                    sources={
                        change.module: (entry / "modules" / change.module).read_text(
                            encoding="utf-8"
                        )
                        for change in record.changes
                    },
                    originals={},
                    test_name=str(manifest["test_name"]),
                    test_source=(entry / "test.py").read_text(encoding="utf-8"),
                ),
            )
            installed.append({"state_dir": str(state_dir), "entry": entry.name, "kind": "patch"})
        except (RuntimeRepairError, RuntimeInstallError, OSError, KeyError, ValueError) as exc:
            refused.append({"state_dir": str(state_dir), "entry": entry.name, "reason": str(exc)})
    if not installed:
        shutil.rmtree(tree, ignore_errors=True)
    else:
        link = Path(install_root) / f".current-{os.getpid()}"
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(tree, link)
        os.replace(link, current)
    for item in installed:
        _file(Path(item["state_dir"]), item["entry"], INSTALLED, {"tree": str(tree)})
    for item in refused:
        _file(Path(item["state_dir"]), item["entry"], REFUSED, {"reason": item["reason"]})
    return {"installed": installed, "refused": refused, "tree": str(tree) if installed else None}


def _file(state_dir: Path, name: str, where: str, note: dict[str, Any]) -> None:
    source = patch_root(state_dir) / PENDING / name
    target = patch_root(state_dir) / where / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    os.replace(source, target)
    (target / f"{where}.json").write_text(json.dumps(note, sort_keys=True), encoding="utf-8")


def install_when_quiet(
    cfg: Any,
    *,
    install_root: Path | None = None,
    roots: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Install this project's staged patches if no registered run is alive.

    Called by the wake-up (outside the sandbox). Returns None when nothing
    is staged; otherwise what happened, including "deferred" with the run
    that is still alive. The installation is shared by every registered
    project, so every one of them is asked.
    """

    from .run_state import StateStore
    from .wake import _dispatcher_alive, registered_projects

    if not runtime_patch_pending(cfg):
        return None
    root = install_root or install_root_from_env()
    current = None if root is None else Path(root) / "current"
    if current is None or not current.is_symlink():
        # Not an installation - a working copy, a test tree. Waiting would
        # drain the run forever, so the staged patches are refused and the
        # caller files a ticket: the on-call applies nothing here.
        return {"installed": [], "refused": _refuse_all(
            Path(cfg.state_dir),
            "this runtime is not an installation with a `current` version symlink; "
            "a staged patch has nowhere to be installed",
        )}
    state_dirs = [Path(cfg.state_dir)]
    for raw in roots if roots is not None else registered_projects():
        candidate = Path(raw) / ".codex-autopilot"
        if candidate.resolve() != Path(cfg.state_dir).resolve() and candidate.is_dir():
            state_dirs.append(candidate)
    for state_dir in state_dirs:
        try:
            if _dispatcher_alive(StateStore(state_dir).load()):
                return {"deferred": f"a dispatcher is alive in {state_dir.parent}"}
        except Exception:  # noqa: BLE001 - an unreadable run is not proof of a quiet one
            return {"deferred": f"the run in {state_dir.parent} could not be read"}
    try:
        return install_pending(root, [Path(cfg.state_dir)])
    except RuntimeInstallError as exc:
        return {"deferred": str(exc)}


def _refuse_all(state_dir: Path, reason: str) -> list[dict[str, Any]]:
    refused = []
    for entry in pending_entries(state_dir):
        _file(state_dir, entry.name, REFUSED, {"reason": reason})
        refused.append({"state_dir": str(state_dir), "entry": entry.name, "reason": reason})
    return refused
