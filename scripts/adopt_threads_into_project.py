#!/usr/bin/env python3
"""Bring the run's threads into the project the way Desktop itself does.

Why. Desktop can adopt threads created through App Server: the thread/list
sweep calls observe -> thread/read -> adopt, and adopt writes a record into
thread-project-assignments. But the whole sweep lives inside migrate(),
where a failure on any thread of the batch raises for the whole loop, and
the threadAssignmentsMigrated flag is written only at the very end. One
failing thread blocks the migration entirely: the queue never drains.

The script writes the same record adopt writes, and nothing beyond it.

SAFETY. The application keeps its state in memory and overwrites the file
with its own copy, so this may run ONLY while Codex is closed. The script
refuses to run by itself if the application is alive. A backup is made next
to the file before writing.

Usage:
  python3 scripts/adopt_threads_into_project.py            # show what would be done
  python3 scripts/adopt_threads_into_project.py --apply    # write
  python3 scripts/adopt_threads_into_project.py --thread <id> --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_autopilot.config import load_config  # noqa: E402
from codex_autopilot.preflight import default_codex_home  # noqa: E402
from codex_autopilot.run_state import StateStore  # noqa: E402

ASSIGNMENTS = "thread-project-assignments"
ORDERS = "sidebar-project-thread-orders"
PROJECTLESS = "projectless-thread-ids"
MAPPING = "app-server-project-id-by-legacy-project-id-by-host"


def codex_is_running() -> bool:
    try:
        out = subprocess.run(
            ("pgrep", "-f", "ChatGPT.app/Contents/MacOS"),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return bool(out.stdout.strip())


def legacy_project_id(state: dict, app_server_project_id: str) -> str | None:
    """The reverse mapping from an App Server project to a Desktop project."""

    for mapping in (state.get(MAPPING) or {}).values():
        for legacy, server in (mapping or {}).items():
            if server == app_server_project_id:
                return legacy
    return None


def run_threads(cfg) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for session in StateStore(cfg.state_dir).load().worker_sessions:
        thread_id = session.get("thread_id")
        if thread_id and thread_id not in seen:
            seen[str(thread_id)] = str(session.get("task_id") or "?")
    return list(seen.items())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", action="append", help="a specific thread; may repeat")
    parser.add_argument("--apply", action="store_true", help="write; without it only show")
    args = parser.parse_args()

    cfg = load_config(ROOT)
    path = default_codex_home().expanduser().resolve() / ".codex-global-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))

    project = legacy_project_id(state, cfg.desktop.project_id or "")
    if project is None:
        raise SystemExit(
            f"App Server project {cfg.desktop.project_id!r} maps to no "
            "Desktop project — nowhere to adopt into"
        )
    name = ((state.get("local-projects") or {}).get(project) or {}).get("name")
    print(f"Desktop project: {name!r} ({project})\n")

    targets = [(t, "?") for t in (args.thread or [])] or run_threads(cfg)
    assignments = state.get(ASSIGNMENTS) or {}
    projectless = list(state.get(PROJECTLESS) or [])
    orders = state.get(ORDERS) or {}
    order = list((orders.get(project) or {}).get("threadIds") or [])

    planned: list[tuple[str, str]] = []
    for thread_id, task in targets:
        if assignments.get(thread_id, {}).get("projectId") == project:
            print(f"  {task:4s} {thread_id}  already in the project")
            continue
        planned.append((thread_id, task))
        print(f"  {task:4s} {thread_id}  WILL BE ADOPTED")

    if not planned:
        print("\nnothing to adopt")
        return 0
    if not args.apply:
        print(f"\ndry run: {len(planned)} threads. Add --apply")
        return 0
    if codex_is_running():
        raise SystemExit(
            "\nCodex is running. It keeps its state in memory and will overwrite the "
            "file with its own copy — close the application and retry."
        )

    backup = path.with_name(path.name + f".backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)

    for thread_id, _task in planned:
        # Exactly the record adopt writes inside the application.
        assignments[thread_id] = {"projectKind": "local", "projectId": project}
        if thread_id in projectless:
            projectless.remove(thread_id)
        if thread_id not in order:
            order.append(thread_id)

    state[ASSIGNMENTS] = assignments
    state[PROJECTLESS] = projectless
    orders[project] = {"threadIds": order}
    state[ORDERS] = orders
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nthreads adopted: {len(planned)}")
    print(f"backup: {backup}")
    print("open Codex and check the project")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
