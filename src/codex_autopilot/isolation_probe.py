"""Measure, before a thread is filed at the project root, that the root stays read-only.

Why this exists. A staged task's worker must not touch the canonical project
before an independent PASS; its thread used to run with cwd = its staged
workspace, and that cwd is what hid it from the project in Desktop (a
subfolder of a project root lands in no project, desktop_sidebar). The fix
files the thread at the root - ``cwd = cfg.root`` - and gives it the
workspace as its runtime root, ``runtimeWorkspaceRoots = [workspace]``.
Whether the root then stays read-only is not something to assume.

What the first probe measured, and why it was the wrong thing (the second
independent check). It ran ``command/exec`` in an ephemeral thread and read
the disk. But ``command/exec`` has no threadId and no runtime roots (codex
0.154.0 schema: command, processId, tty, streams, caps, timeouts, cwd, env,
size, sandboxPolicy, permissionProfile), so the thread's roots had no effect
on it; and it sent the thread/start answer's legacy ``sandbox``
(``workspaceWrite`` with no roots) as ``sandboxPolicy`` - under which the cwd,
the root, is writable - and then no profile at all, the two being exclusive.
A live run would have measured ROOT_WRITABLE whatever a turn may do. Its
workspace was a system temp directory, outside the root and writable to the
sandbox anyway, and its profile was hard-coded.

What was measured instead (``codex sandbox`` of codex 0.154.0, seatbelt, a
scratch tree R with a workspace W below it; no model, no App Server):

- the built-in ``:workspace`` profile grants writes through ``:workspace_roots``,
  and those default to the cwd: cwd R writes R and W; cwd W writes W, not R.
  So with runtime roots [W] the root is read-only - IF the turn materializes
  its roots from runtimeWorkspaceRoots and nothing widens them. Neither is
  observable through ``command/exec``, and Desktop rebuilds a thread's roots
  from its cwd when she opens it (isolation_guard);
- ``extends = <run profile>`` plus ``filesystem = {":workspace_roots" = "read",
  "<W>" = "write"}`` keeps R read-only and W writable whether
  ``:workspace_roots`` is [R] or [W] (an explicit ``R = "read"`` does not: the
  roots' write wins over it; ``":workspace_roots" = "read"`` with a write on
  W's parent makes W itself read-only - the grant must be W exactly).

So the root's protection is not left to how roots are materialized. Each
staged task gets that second profile - its own id, ``codex-autopilot-staged-
<hash of W>`` - defined for the App Server process that serves the task by
``-c`` overrides at its launch (no config.toml of hers is written). The
runtime decided it in advance; it is not a fork handed to her. The probe
measures exactly it:

- ``command/exec`` with ``cwd = cfg.root`` and that ``permissionProfile`` - the
  worst case, ``:workspace_roots`` materialized as the root itself - on the
  project's own config layers and trust;
- the workspace is a subfolder of the run's state directory, as every staged
  workspace is; the record says where it was, and a record of another shape
  does not count;
- an ephemeral thread started exactly as a worker's (cwd = root, runtime
  roots = [workspace], the same profile) must answer with no wider roots and
  with that profile active - so the profile measured is the one a turn runs;
- the ground truth is the disk: a probe file exists or it does not;
- a permission request is NEVER answered (her boundary): the command is
  terminated, and the request counts as "not proven";
- a server that answers with wider roots, or with another profile, fails.

Outcomes: PASS (workspace written, root not), ROOT_WRITABLE, NOT_PROVEN.
Neither of the last two stops anything or asks her: staged tasks keep their
workspace as cwd (contract 1, isolated but outside the project), and each
such thread is an R5 defect whose ticket reaches the on-call with the record
(placement_defects). Every outcome is written to
``<state_dir>/isolation-probe.json`` with the Codex binary identity, the
runtime code that measured and the Desktop version; the dispatcher measures
before it launches a task's App Server when there is no record of this
shape - which is how a run paused before this change is measured when it
resumes.

A failed measurement is measured again (the third independent check). The
record used to count by its shape alone - version, root, profile, binary,
workspace - never by its outcome, so one NOT_PROVEN (a probe timeout, an App
Server that did not start) kept every staged task of the run on contract 1
to its end, and the on-call's ticket sent it down a road that did not exist:
"repair it and let the dispatcher measure again" - a runtime patch changes
none of the four. Now:

- the record names the runtime code that measured it
  (``runtime_code_identity``: a digest of the package's modules as this
  process loaded them). A devops-repair-runtime patch, once installed,
  changes that digest, and the next staged thread is measured again on the
  repaired code - whatever the outcome was;
- NOT_PROVEN says only that nothing was proven; it stands for
  ``NOT_PROVEN_RETRY_SECONDS`` and is then measured again, so a transient
  failure costs one interval, not the run;
- ROOT_WRITABLE is a measurement, deterministic on one binary and one code:
  it stands until either changes (a new binary, an installed repair). The
  probe writes into her root to measure; repeating a known result every few
  minutes would only write there more often;
- PASS stands for the same binary and code.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timezone
import functools
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping
import uuid

from .appserver import ApprovalRequired

RECORD_FILE = "isolation-probe.json"
RECORD_VERSION = 2
PROBE_DIR = "isolation-probe"
PASS = "PASS"
ROOT_WRITABLE = "ROOT_WRITABLE"
NOT_PROVEN = "NOT_PROVEN"
STAGED_PROFILE_PREFIX = "codex-autopilot-staged-"
# How long a NOT_PROVEN record stands before the dispatcher measures again.
# Ten minutes: a dispatcher is launched per staged task, so a run with a
# transient probe failure retries at most once per interval, never per task.
NOT_PROVEN_RETRY_SECONDS = 600

# (overrides) -> a context manager yielding a connected App Server client
# launched with those ``-c`` overrides.
OpenClient = Callable[[tuple[str, ...]], AbstractContextManager[Any]]


def staged_profile_id(workspace: Path) -> str:
    """The permission profile of one staged workspace (ids match ^[A-Za-z0-9_-]+$)."""

    digest = hashlib.sha256(str(Path(workspace).expanduser().resolve(strict=False)).encode("utf-8"))
    return STAGED_PROFILE_PREFIX + digest.hexdigest()[:16]


def staged_profile_overrides(workspace: Path, base_profile: str) -> tuple[str, ...]:
    """``-c`` values that define the staged profile for one App Server process.

    Values are TOML; json.dumps gives a valid TOML basic string for any path.
    """

    profile = staged_profile_id(workspace)
    path = json.dumps(str(Path(workspace).expanduser().resolve(strict=False)))
    return (
        f"permissions.{profile}.extends={json.dumps(base_profile)}",
        f'permissions.{profile}.filesystem={{":workspace_roots" = "read", {path} = "write"}}',
    )


def probe_workspace(state_dir: Path) -> Path:
    return Path(state_dir) / PROBE_DIR / "workspace"


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


@functools.lru_cache(maxsize=1)
def runtime_code_identity() -> str:
    """The runtime code this process runs: a digest of the package's modules.

    Taken once per process, at first use - the code a process runs is the
    code it loaded, even if a repair is installed under it meanwhile. An
    installed devops-repair-runtime patch changes it for every process
    started after the install, and a record measured by other code is
    measured again (record_matches).
    """

    package = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for module in sorted(package.glob("*.py")):
        try:
            content = module.read_bytes()
        except OSError:
            continue
        digest.update(module.name.encode("utf-8") + b"\0" + content + b"\0")
    return digest.hexdigest()[:16]


def load_record(state_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(record_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _inside(path: Any, parent: Path) -> bool:
    try:
        candidate = Path(str(path)).expanduser().resolve(strict=False)
        base = Path(parent).expanduser().resolve(strict=False)
        return candidate != base and candidate.is_relative_to(base)
    except (TypeError, ValueError, OSError):
        return False


def record_matches(record: Mapping[str, Any] | None, cfg: Any) -> bool:
    """A measurement of this root, run profile, binary and runtime code - of the real shape.

    The real shape: this record version (the staged profile, measured on the
    root) and a workspace under this run's state directory, where every
    staged workspace lives. A record measured with a workspace elsewhere -
    the first probe's system temp directory - proves nothing about them.
    Whether the record still stands is ``record_stands``: this says only
    what it is a measurement of.
    """

    return bool(
        record
        and record.get("version") == RECORD_VERSION
        and record.get("root") == str(cfg.root)
        and record.get("base_profile") == cfg.desktop.permission_profile
        and record.get("codex_binary") == binary_identity(cfg.desktop.binary)
        and record.get("runtime_code") == runtime_code_identity()
        and _inside(record.get("workspace"), Path(cfg.state_dir))
    )


def _age_seconds(measured_at: Any, now: datetime) -> float | None:
    try:
        at = datetime.fromisoformat(str(measured_at))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (now - at).total_seconds()


def record_stands(record: Mapping[str, Any] | None, cfg: Any, *, now: datetime | None = None) -> bool:
    """A record the dispatcher takes as it is, without measuring again.

    Of this root, profile, binary and runtime code (record_matches), and
    not a NOT_PROVEN older than ``NOT_PROVEN_RETRY_SECONDS`` - or one whose
    time cannot be read.
    """

    if not record_matches(record, cfg):
        return False
    if (record or {}).get("outcome") != NOT_PROVEN:
        return True
    age = _age_seconds((record or {}).get("measured_at"), now or datetime.now(timezone.utc))
    return age is not None and 0 <= age < NOT_PROVEN_RETRY_SECONDS


def isolation_proven(cfg: Any) -> bool:
    record = load_record(cfg.state_dir)
    return record_matches(record, cfg) and (record or {}).get("outcome") == PASS


def write_record(state_dir: Path, record: Mapping[str, Any]) -> None:
    path = record_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(dict(record), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _wrote(client: Any, target: Path, *, cwd: Path, profile: str) -> tuple[bool | None, str]:
    """Try to create ``target``; True/False by the disk, None when not proven."""

    process_id = f"autopilot-isolation-{uuid.uuid4().hex[:12]}"
    try:
        client.exec_command(
            ["/usr/bin/touch", str(target)],
            cwd=cwd,
            process_id=process_id,
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


def _measure(client: Any, record: dict[str, Any], *, root: Path, workspace: Path, profile: str,
             inside: Path, outside: Path) -> None:
    allowed = {
        str(item.get("id"))
        for item in client.list_permission_profiles(root) or ()
        if item.get("allowed") is not False and item.get("id")
    }
    if profile not in allowed:
        record.update(outcome=NOT_PROVEN, reason=f"the server did not offer the staged profile {profile}")
        return
    started = client.start_thread(
        cwd=root,
        permission_profile=profile,
        project_id=None,
        model=None,
        ephemeral=True,
        project_memory=False,
        workspace_roots=[workspace],
    )
    returned = started.get("runtimeWorkspaceRoots")
    active = (started.get("activePermissionProfile") or {}).get("id")
    record.update(
        thread_id=str((started.get("thread") or {}).get("id") or ""),
        server_roots=returned,
        active_profile=active,
        # Recorded, never sent back: the legacy projection of the profile.
        legacy_sandbox=started.get("sandbox"),
    )
    if isinstance(returned, list) and [str(Path(str(item)).resolve()) for item in returned] != [str(workspace)]:
        record.update(outcome=ROOT_WRITABLE, reason=f"the server widened the runtime roots to {returned}")
        return
    if active is not None and active != profile:
        record.update(outcome=NOT_PROVEN, reason=f"the thread runs profile {active!r}, not the staged {profile!r}")
        return
    ws_ok, ws_why = _wrote(client, inside, cwd=root, profile=profile)
    root_ok, root_why = _wrote(client, outside, cwd=root, profile=profile)
    record.update(workspace_write=ws_ok, root_write=root_ok)
    if root_ok:
        record.update(outcome=ROOT_WRITABLE, reason="a file was written under the root outside the workspace")
    elif ws_ok and root_ok is False:
        record.update(outcome=PASS, reason="the workspace is writable, the root is not")
    else:
        why = root_why or ws_why or "the workspace itself was not writable: nothing was measured"
        record.update(outcome=NOT_PROVEN, reason=why)


def probe_isolation(
    open_client: OpenClient,
    *,
    root: Path,
    workspace: Path,
    base_profile: str,
    binary: str,
    codex_version: str | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Measure the staged profile on ``root``; the record goes to ``state_dir`` when given.

    The file that must not be writable sits directly under the root,
    outside the workspace: if the probe writes it, the root is writable,
    and the file - the probe's own - is removed at once.
    """

    from .desktop_sidebar import desktop_version
    from .run_state import utc_now

    root = Path(root).expanduser().resolve()
    workspace = Path(workspace).expanduser().resolve(strict=False)
    workspace.mkdir(parents=True, exist_ok=True)
    workspace = workspace.resolve()
    profile = staged_profile_id(workspace)
    overrides = staged_profile_overrides(workspace, base_profile)
    token = uuid.uuid4().hex[:12]
    inside, outside = workspace / f"probe-{token}", root / f".codex-autopilot-isolation-probe-{token}"
    record: dict[str, Any] = {
        "version": RECORD_VERSION,
        "method": (
            "command/exec on the root (cwd = root) under the task's staged profile; an ephemeral "
            "thread started as a worker's (cwd = root, runtimeWorkspaceRoots = [workspace]) must "
            "answer with those roots and that profile"
        ),
        "root": str(root),
        "workspace": str(workspace),
        "base_profile": base_profile,
        "profile": profile,
        "server_overrides": list(overrides),
        "codex_binary": binary_identity(binary),
        "runtime_code": runtime_code_identity(),
        "codex_version": codex_version,
        "desktop_version": desktop_version(),
        "measured_at": utc_now(),
    }
    try:
        with open_client(overrides) as client:
            _measure(client, record, root=root, workspace=workspace, profile=profile,
                     inside=inside, outside=outside)
    except Exception as exc:  # noqa: BLE001 - a probe that cannot run proves nothing
        record.setdefault("outcome", NOT_PROVEN)
        record.setdefault("reason", f"probe could not run: {exc}")
    finally:
        for path in (inside, outside):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    if state_dir is not None:
        write_record(state_dir, record)
    return record


def ensure_measured(cfg: Any, open_client: OpenClient, *, now: datetime | None = None) -> dict[str, Any]:
    """The record for this run, measured now when none stands (record_stands)."""

    record = load_record(cfg.state_dir)
    if record_stands(record, cfg, now=now):
        return dict(record or {})
    return probe_isolation(
        open_client,
        root=cfg.root,
        workspace=probe_workspace(cfg.state_dir),
        base_profile=cfg.desktop.permission_profile,
        binary=cfg.desktop.binary,
        state_dir=cfg.state_dir,
    )


def server_overrides(cfg: Any, session: Mapping[str, Any], open_client: OpenClient | None = None) -> tuple[str, ...]:
    """The ``-c`` values the App Server serving this session must be launched with.

    A thread created under its staged profile keeps needing it on every
    turn, whatever the record says now. A thread still to be created gets it
    when isolation is proven - measured first, through ``open_client``, if
    no record stands (record_stands: none of this root, profile, binary and
    runtime code, or a NOT_PROVEN past its interval; a run paused before
    contract 2 is measured here, when it resumes). Anything else: none.
    """

    descriptor = session.get("descriptor") or {}
    workspace = Path(str(descriptor.get("cwd") or cfg.root)).expanduser().resolve(strict=False)
    if workspace == Path(cfg.root):
        return ()
    overrides = staged_profile_overrides(workspace, cfg.desktop.permission_profile)
    if session.get("permission_profile") == staged_profile_id(workspace):
        return overrides
    if session.get("thread_id"):
        return ()
    if open_client is not None:
        ensure_measured(cfg, open_client)
    return overrides if isolation_proven(cfg) else ()


def dispatcher_overrides(cfg: Any, reservation_token: str, client_factory: Callable[..., Any] | None = None) -> tuple[str, ...]:
    """What the per-task dispatcher's App Server is launched with (cli relay loop).

    Measured, if needed, on a separate short-lived server of its own: the
    staged profile has to be defined at launch, so the connection that will
    create the thread cannot measure it for itself. Never the reason a
    dispatcher does not start - a failure here is contract 1 for this task,
    which is itself an R5 defect with a ticket.
    """

    from .appserver import AppServerClient
    from .run_state import StateStore

    factory = client_factory or AppServerClient
    try:
        state = StateStore(cfg.state_dir).load()
        session = next(
            item for item in state.worker_sessions if item.get("reservation_token") == reservation_token
        )
        log = Path(cfg.state_dir) / "logs" / "isolation-probe.jsonl"
        return server_overrides(
            cfg, session, lambda extra: factory(cfg.desktop.binary, log, config_overrides=extra)
        )
    except Exception:  # noqa: BLE001 - see the docstring
        return ()
