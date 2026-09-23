"""Keep the run's wire traces from filling the disk.

Every dispatcher writes the whole App Server conversation, both directions,
to its own file under ``.codex-autopilot/logs``. Nothing removed them. On the
author's own run that was 167 files and 2.3 GB, individual traces between 70
and 108 MB, in a project directory of 2.4 GB - the run's actual memory
(journal, plan, project memory, handoffs) was a few megabytes of it.

What is NOT done here, deliberately:

- No file is truncated. A capped trace loses its tail, and the tail is where
  the failure is; the on-call engineer reads these to find out what the
  server actually said. Whole files are removed, oldest first, or nothing is.
- Only this runtime's own traces are touched, by exact name. Anything else a
  person put in that directory is not ours to delete.
- A file written recently is never removed, however large. Deciding whether
  another process holds a handle is not portable; age is, and a live
  dispatcher's own trace is by definition fresh.
"""

from __future__ import annotations

from pathlib import Path
import time


# One prefix per writer in the runtime: dispatcher, create, wait, production,
# and the relay's own stdout. Fixed names - thread-probe, placement,
# observe-workers - are small and stay.
_TRACE_PREFIXES = ("app-server-", "automatic-relay-")
# Files younger than this are left alone whatever the budget says: the
# dispatcher writing right now owns one of them.
MIN_AGE_SECONDS = 3600
# Kept regardless of age or budget, newest first. An incident is read from
# the last traces, and an engineer who opens a ticket an hour later must
# still find them.
KEEP_NEWEST = 5


def _traces(directory: Path) -> list[Path]:
    return [
        path
        for path in directory.iterdir()
        if path.is_file()
        and any(path.name.startswith(prefix) for prefix in _TRACE_PREFIXES)
    ]


def sweep_logs(
    directory: Path,
    *,
    budget_bytes: int,
    now: float | None = None,
) -> tuple[int, int]:
    """Remove the oldest traces until the directory fits its budget.

    Returns how many files were removed and how many bytes that freed.
    ``budget_bytes`` of zero or less keeps everything: that is the opt-out,
    not an instruction to delete all.
    """

    if budget_bytes <= 0 or not directory.is_dir():
        return (0, 0)
    try:
        traces = _traces(directory)
    except OSError:
        return (0, 0)

    stamped: list[tuple[float, int, Path]] = []
    for path in traces:
        try:
            info = path.stat()
        except OSError:
            continue
        stamped.append((info.st_mtime, info.st_size, path))
    total = sum(size for _mtime, size, _path in stamped)
    if total <= budget_bytes:
        return (0, 0)

    stamped.sort(key=lambda item: item[0])
    protected = {path for _mtime, _size, path in stamped[-KEEP_NEWEST:]}
    moment = time.time() if now is None else now

    removed = 0
    freed = 0
    for mtime, size, path in stamped:
        if total <= budget_bytes:
            break
        if path in protected or moment - mtime < MIN_AGE_SECONDS:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        removed += 1
        freed += size
    return (removed, freed)


# The launch agent's own log is a different shape from a wire trace, and it
# needs a different rule.
#
# launchd appends the sweep's stdout to one file at a fixed path, forever.
# Nothing rotates it, and the sweep ran every five minutes printing one line
# per registered project. Measured on the author's machine on 23 Sep 2026:
# 55 MB, 596,661 lines, almost all of them reporting that a temporary project
# from a test run was still gone.
#
# Deleting this file is not the same act as deleting a trace: launchd opened
# it before the sweep started, so an unlink leaves the current run writing to
# an inode nobody can find. Truncating to zero is safe with an append-mode
# handle - the next write lands at the new start - and it loses nothing that
# is worth reading, because a heartbeat's history is not evidence. The reset
# says so in the file itself rather than silently starting over.
WAKE_LOG_BUDGET_BYTES = 1_048_576


def reset_oversized_log(path: Path, *, budget_bytes: int = WAKE_LOG_BUDGET_BYTES) -> int:
    """Empty a launchd-owned append log that has outgrown its budget.

    Returns the number of bytes dropped, or 0 when nothing was done.
    """

    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size <= budget_bytes:
        return 0
    try:
        with path.open("r+") as handle:
            handle.truncate(0)
    except OSError:
        return 0
    return size
