"""Measure, before a thread is filed at the project root, that the root stays read-only.

Why this exists. A staged task's worker must not touch the canonical project
before an independent PASS; its thread used to run with cwd = its staged
workspace, and that cwd is what hid it from the project in Desktop (a
subfolder of a project root lands in no project, desktop_sidebar). The fix
files the thread at the root - ``cwd = cfg.root`` - and gives it the
workspace as its only runtime root, ``runtimeWorkspaceRoots = [workspace]``.
Whether the root then stays read-only is not something to assume: the
thread/start answer still reports the sandbox in its old shape
(``{type: workspaceWrite, writableRoots: []}``), and in the old semantics
the cwd is writable. So the switch waits for this measurement, and without
a PASS the old placement stays - with an explicit finding, never silently.

How it measures, after the independent check of the first design:

- deterministically, through App Server's ``command/exec`` - no model turn,
  which may skip a command or ask for an escalation before the sandbox
  refuses anything;
- on ``cfg.root`` itself with the run's own permission profile - profiles,
  project trust and ``.codex/config.toml`` layers depend on the cwd, so a
  temporary directory would prove nothing about the project;
- in a thread started exactly as a worker's (cwd = root, runtime roots =
  [a workspace] - under the state directory at run time, a temporary
  directory at preflight, which creates no project state), ephemeral, so no
  preflight leaves a thread outside every project in her sidebar; each
  command runs under the sandbox that thread/start reports for it;
- the ground truth is the disk: a probe file exists or it does not;
- a permission request is NEVER answered (her boundary): the command is
  terminated, and the request counts as "not proven", not as "read-only" -
  a request proves nothing about what the sandbox would have refused;
- a server that answers with runtime roots wider than asked is a failure.

Outcomes: PASS (the workspace written, the root not), ROOT_WRITABLE (a
failure of isolation: preflight fails with an ISOLATION finding),
NOT_PROVEN (anything else: the old placement stays, with a finding). Every
outcome is written to ``<state_dir>/isolation-probe.json`` with the Codex
binary identity and the Desktop version; the dispatcher reads it before a
staged thread is created, and measures itself when there is no record for
this root, profile and binary - which is how a run paused before this
change is measured when it resumes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping
import uuid

from .appserver import ApprovalRequired

RECORD_FILE = "isolation-probe.json"
PROBE_DIR = "isolation-probe"
PASS = "PASS"
ROOT_WRITABLE = "ROOT_WRITABLE"
NOT_PROVEN = "NOT_PROVEN"


def record_path(state_dir: Path) -> Path:
    return Path(state_dir) / RECORD_FILE


def binary_identity(binary: str) -> str | None:
    """The Codex binary that was measured: its path, size and mtime."""

    found = binary if os.path.isabs(binary) else shutil.which(binary)
    if not found:
        return None
    try:
        resolved = Path(found).resolve()
        stat = resolved.stat()
    except OSError:
        return None
    return f"{resolved}:{stat.st_size}:{stat.st_mtime_ns}"


def load_record(state_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(record_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def record_matches(record: Mapping[str, Any] | None, cfg: Any) -> bool:
    """Is the record a measurement of this root, profile and Codex binary."""

    return bool(
        record
        and record.get("root") == str(cfg.root)
        and record.get("permission_profile") == cfg.desktop.permission_profile
        and record.get("codex_binary") == binary_identity(cfg.desktop.binary)
    )


def isolation_proven(cfg: Any) -> bool:
    record = load_record(cfg.state_dir)
    return record_matches(record, cfg) and (record or {}).get("outcome") == PASS


def write_record(state_dir: Path, record: Mapping[str, Any]) -> None:
    path = record_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(record), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _wrote(client: Any, target: Path, *, root: Path, sandbox: Any, profile: str) -> tuple[bool | None, str]:
    """Try to create ``target``; True/False by the disk, None when not proven."""

    process_id = f"autopilot-isolation-{uuid.uuid4().hex[:12]}"
    try:
        client.exec_command(
            ["/usr/bin/touch", str(target)],
            cwd=root,
            process_id=process_id,
            sandbox_policy=sandbox if isinstance(sandbox, Mapping) else None,
            permission_profile=profile,
        )
    except ApprovalRequired as exc:
        try:
            client.terminate_command(process_id)
        except Exception:  # noqa: BLE001 - the request stays unanswered either way
            pass
        method = str(exc.payload.get("method") or "a request")
        return None, f"{method} asked for a permission; never answered, so nothing is proven"
    except Exception as exc:  # noqa: BLE001 - a probe that cannot run proves nothing
        if target.exists():
            return True, f"wrote despite an error: {exc}"
        return None, f"command/exec failed: {exc}"
    return target.exists(), ""


def probe_isolation(
    client: Any,
    *,
    root: Path,
    workspace: Path,
    permission_profile: str,
    binary: str,
    codex_version: str | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Measure; the record is written to ``state_dir`` when one is given.

    The file that must not be writable sits directly under the root,
    outside the workspace: if the probe writes it, the root is writable,
    and the file - the probe's own - is removed at once.
    """

    from .desktop_sidebar import desktop_version
    from .run_state import utc_now

    root = Path(root).expanduser().resolve()
    workspace = Path(workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    inside, outside = workspace / f"probe-{token}", root / f".codex-autopilot-isolation-probe-{token}"
    record: dict[str, Any] = {
        "version": 1,
        "method": "command/exec in an ephemeral thread: cwd = root, runtimeWorkspaceRoots = [workspace]",
        "root": str(root),
        "workspace": str(workspace),
        "permission_profile": permission_profile,
        "codex_binary": binary_identity(binary),
        "codex_version": codex_version,
        "desktop_version": desktop_version(),
        "measured_at": utc_now(),
    }
    try:
        started = client.start_thread(
            cwd=root,
            permission_profile=permission_profile,
            project_id=None,
            model=None,
            ephemeral=True,
            project_memory=False,
            workspace_roots=[workspace],
        )
        record["thread_id"] = str((started.get("thread") or {}).get("id") or "")
        returned = started.get("runtimeWorkspaceRoots")
        record["server_roots"] = returned
        sandbox = started.get("sandbox")
        record["sandbox"] = sandbox
        if isinstance(returned, list) and [str(Path(str(item)).resolve()) for item in returned] != [str(workspace)]:
            record.update(outcome=ROOT_WRITABLE, reason=f"the server widened the runtime roots to {returned}")
        else:
            ws_ok, ws_why = _wrote(client, inside, root=root, sandbox=sandbox, profile=permission_profile)
            root_ok, root_why = _wrote(client, outside, root=root, sandbox=sandbox, profile=permission_profile)
            record.update(workspace_write=ws_ok, root_write=root_ok)
            if root_ok:
                record.update(outcome=ROOT_WRITABLE, reason="a file was written under the root outside the workspace")
            elif ws_ok and root_ok is False:
                record.update(outcome=PASS, reason="the workspace is writable, the root is not")
            else:
                why = root_why or ws_why or "the workspace itself was not writable: nothing was measured"
                record.update(outcome=NOT_PROVEN, reason=why)
    except Exception as exc:  # noqa: BLE001 - a probe that cannot run proves nothing
        record.update(outcome=NOT_PROVEN, reason=f"probe could not run: {exc}")
    finally:
        for path in (inside, outside):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    if state_dir is not None:
        write_record(state_dir, record)
    return record


def ensure_measured(cfg: Any, client: Any) -> dict[str, Any]:
    """The record for this run, measured now on ``client`` when there is none."""

    record = load_record(cfg.state_dir)
    if record_matches(record, cfg):
        return dict(record or {})
    return probe_isolation(
        client,
        root=cfg.root,
        workspace=Path(cfg.state_dir) / PROBE_DIR / "workspace",
        permission_profile=cfg.desktop.permission_profile,
        binary=cfg.desktop.binary,
        state_dir=cfg.state_dir,
    )
