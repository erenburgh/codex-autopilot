"""R4: the run's durable authorization in run-state, and what a request falls under.

Her rule R4 (RULES.md): the user gave durable authorization for the run;
run-state holds it with the list of covered operations, fixed and versioned,
and a confirmation request for an operation on that list is refused, citing
R4.

What there was before: the on-call compared a permission request with
``stop_diagnosis.RUN_AUTHORIZATION`` - three sentences in a module it may
patch, which its own comment called "the fixed statement", because the run
carried no list of its own. The independent check found the consequence:
the brief's branch "the runtime asked for more than the run needs - a
runtime defect" had no machine-checkable basis. Two engineers could read
the same request against the same prose and go opposite ways, and nothing
in the runtime could refuse the one that sent a covered operation to her.

So now:

- ``arm`` (her start of the run) writes ``state.durable_authorization``:
  the version and the operations of ``engineer_authority`` (out of the
  engineer's reach), the project root, the permission profile, when. A run
  armed before this list existed gets it at its first permission request,
  marked as a backfill - R4 was in force for it all along, only unrecorded;
- ``covering_operation`` decides, from the request itself, which recorded
  operation covers it - or None when that cannot be proven. Conservative by
  construction: a path it cannot see, a command it cannot read as the
  plugin's own CLI, is not covered, and the request goes to the on-call and
  on to her as before;
- ``approval_stops`` records a covered request as an R4 violation (the
  runtime asked for what the run already holds - a runtime defect), and
  ``engineer_escalation.read_engineer_outcome`` refuses an escalation that
  would send it to her as DANGEROUS_PERMISSION: that is exactly the
  confirmation request R4 forbids.

Nobody answers the request here or anywhere: this module only reads.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Mapping

# Shell syntax that could chain a second command behind the plugin's CLI.
_SHELL_SYNTAX = frozenset(";|&<>`$\n")
# Request fields that name where an operation reaches.
_PATH_FIELDS = ("cwd", "grantRoot", "path", "paths", "root")


def authorization_record(cfg: Any, *, at: str, granted_by: str) -> dict[str, Any]:
    """The durable authorization as run-state keeps it."""

    from .engineer_authority import (
        RUN_AUTHORIZATION_VERSION,
        RUN_AUTHORIZED_CLI_SUBCOMMANDS,
        RUN_AUTHORIZED_OPERATIONS,
    )

    return {
        "version": RUN_AUTHORIZATION_VERSION,
        "operations": [
            {
                "id": op_id,
                "methods": list(methods),
                "means": means,
                **(
                    {"subcommands": list(RUN_AUTHORIZED_CLI_SUBCOMMANDS)}
                    if op_id == "autopilot_cli_in_project"
                    else {}
                ),
            }
            for op_id, methods, means in RUN_AUTHORIZED_OPERATIONS
        ],
        "project_root": str(Path(cfg.root).resolve()),
        "permission_profile": str(getattr(getattr(cfg, "desktop", None), "permission_profile", "") or ""),
        "granted_at": at,
        "granted_by": granted_by,
    }


def ensure_recorded(cfg: Any, state: Any, *, at: str, granted_by: str) -> dict[str, Any]:
    """Write the durable authorization into ``state`` when it is missing or older.

    The caller holds the run's transaction and saves the state.
    """

    from .engineer_authority import RUN_AUTHORIZATION_VERSION

    current = getattr(state, "durable_authorization", None)
    if isinstance(current, Mapping) and int(current.get("version") or 0) >= RUN_AUTHORIZATION_VERSION:
        return dict(current)
    record = authorization_record(cfg, at=at, granted_by=granted_by)
    if isinstance(current, Mapping):
        record["supersedes_version"] = current.get("version")
    state.durable_authorization = record
    return record


def _inside(root: Path, value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _paths(params: Mapping[str, Any]) -> list[Any]:
    found: list[Any] = []
    for key in _PATH_FIELDS:
        value = params.get(key)
        if value is None:
            continue
        found.extend(value if isinstance(value, (list, tuple)) else [value])
    changes = params.get("changes")
    if isinstance(changes, Mapping):
        found.extend(changes.keys())
    return found


def _cli_words(command: Any) -> list[str] | None:
    """The words of a plain ``codex-autopilot`` command, or None."""

    if isinstance(command, (list, tuple)):
        words = [str(item) for item in command]
    elif isinstance(command, str):
        if any(char in _SHELL_SYNTAX for char in command):
            return None
        try:
            words = shlex.split(command)
        except ValueError:
            return None
    else:
        return None
    if not words or any(char in _SHELL_SYNTAX for word in words for char in word):
        return None
    return words if Path(words[0]).name == "codex-autopilot" else None


def _authorized_cli(root: Path, operation: Mapping[str, Any], params: Mapping[str, Any]) -> bool:
    """A plain ``codex-autopilot`` command the run is authorized for.

    The subcommand must be on the list the run recorded (a record without
    one - version 1 - covers none: what is not proven is not covered), the
    turn must stand inside the project (a request that names no cwd proves
    nothing about where it runs), and a ``--project`` it names must be this
    one.
    """

    from .engineer_authority import RUN_AUTHORIZED_CLI_SUBCOMMANDS

    words = _cli_words(params.get("command"))
    if not words or len(words) < 2 or not _inside(root, params.get("cwd")):
        return False
    allowed = operation.get("subcommands")
    if not isinstance(allowed, (list, tuple)):
        return False
    # Never wider than the list of the code that reads it, whatever a
    # record says.
    if words[1] not in allowed or words[1] not in RUN_AUTHORIZED_CLI_SUBCOMMANDS:
        return False
    cwd = Path(str(params.get("cwd"))).expanduser()
    for at, word in enumerate(words):
        if word == "--project":
            named = words[at + 1] if at + 1 < len(words) else ""
        elif word.startswith("--project="):
            named = word.split("=", 1)[1]
        else:
            continue
        # A relative --project is read from where the command runs.
        if not named or not _inside(root, cwd / Path(named).expanduser()):
            return False
    return True


def covering_operation(
    authorization: Mapping[str, Any] | None, payload: Mapping[str, Any]
) -> str | None:
    """The recorded operation that covers this approval request, or None.

    Only what the request itself proves: its method is one the operation
    answers for, every path it names lies inside the project root the
    authorization was recorded for, and - for a command - it is the plugin's
    own CLI with no shell around it, run from inside the project, and a
    subcommand the run is authorized for (not one of hers). A file change that names no target of
    its own (only the turn's cwd, or nothing) proves nothing and is not
    covered.
    """

    if not isinstance(authorization, Mapping):
        return None
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    if not method or not isinstance(params, Mapping):
        return None
    root_text = str(authorization.get("project_root") or "")
    if not root_text:
        return None
    root = Path(root_text).resolve()
    paths = _paths(params)
    if not all(_inside(root, item) for item in paths):
        return None
    for operation in authorization.get("operations") or ():
        if not isinstance(operation, Mapping) or method not in (operation.get("methods") or ()):
            continue
        op_id = str(operation.get("id") or "")
        # Where the change lands, not where the turn stands: a turn inside
        # the project may still ask to write outside it.
        if op_id == "file_change_in_project" and [item for item in paths if item != params.get("cwd")]:
            return op_id
        if op_id == "autopilot_cli_in_project" and _authorized_cli(root, operation, params):
            return op_id
    return None
