#!/usr/bin/env python3
"""Walk a thread through the visibility cycle and show where it breaks.

The cycle: created invisible -> became visible -> landed in the project.

The script does not reason; it takes steps and after each one checks
against Desktop's OWN records (~/.codex/.codex-global-state.json). A
successful call on the App Server side does not count as visibility:
project/update and thread/metadata/update pass in the App Server namespace
without changing the Electron sidebar metadata - recorded in the product
itself (project_association.py) and confirmed on a live run.

The states told apart by Desktop's records:

  ABSENT               Desktop does not know the thread - not visible at all
  VISIBLE OUTSIDE      the thread is in projectless-thread-ids: visible in Recents
  IN PROJECT           the thread is in thread-project-assignments and the sidebar order

Usage:
  python3 scripts/promote_thread_visibility.py                # the run's active thread
  python3 scripts/promote_thread_visibility.py --thread <id>
  python3 scripts/promote_thread_visibility.py --dry-run      # measure only, no calls
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_autopilot.appserver import AppServerClient  # noqa: E402
from codex_autopilot.config import load_config  # noqa: E402
from codex_autopilot.preflight import default_codex_home  # noqa: E402
from codex_autopilot.run_state import StateStore  # noqa: E402

PROJECT_KEYS = ("thread-project-assignments", "sidebar-project-thread-orders")
KNOWN_KEYS = PROJECT_KEYS + ("projectless-thread-ids", "electron-persisted-atom-state")

ABSENT = "ABSENT"
OUTSIDE = "VISIBLE OUTSIDE"
INSIDE = "IN PROJECT"


def desktop_state() -> dict:
    path = default_codex_home().expanduser().resolve() / ".codex-global-state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def placement(thread_id: str) -> tuple[str, list[str]]:
    """Where the thread is according to Desktop itself, and in which of its records."""

    state = desktop_state()
    hits = [
        key
        for key in KNOWN_KEYS
        if thread_id in json.dumps(state.get(key), ensure_ascii=False)
    ]
    if any(key in PROJECT_KEYS for key in hits):
        return INSIDE, hits
    if hits:
        return OUTSIDE, hits
    return ABSENT, hits


def active_thread(cfg) -> str:
    state = StateStore(cfg.state_dir).load()
    live = [
        item
        for item in state.worker_sessions
        if item.get("thread_id") and item.get("status") == "ACTIVE"
    ]
    if not live:
        raise SystemExit(
            "the run has no active thread — name it explicitly with --thread"
        )
    return str(live[-1]["thread_id"])


def report(stage: str, thread_id: str) -> str:
    where, hits = placement(thread_id)
    detail = ", ".join(hits) if hits else "in no record"
    print(f"  {stage:<22} {where:<18} ({detail})")
    return where


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", help="thread identifier; the active one by default")
    parser.add_argument("--project", help="App Server project; from the config by default")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only measure the current placement, calling nothing",
    )
    parser.add_argument("--settle", type=float, default=3.0,
                        help="how many seconds to wait for Desktop to rewrite its state")
    args = parser.parse_args()

    cfg = load_config(ROOT)
    thread_id = args.thread or active_thread(cfg)
    project_id = args.project or cfg.desktop.project_id
    if not project_id:
        raise SystemExit("the run config names no App Server project")

    print(f"thread:  {thread_id}")
    print(f"project: {project_id}\n")
    before = report("before intervention", thread_id)
    if args.dry_run:
        return 0 if before == INSIDE else 1
    if before == INSIDE:
        print("\nthe thread is already in the project — nothing to do")
        return 0

    print("\nstep 1: attach to the project through App Server")
    client = AppServerClient(
        cfg.desktop.binary, cfg.state_dir / "logs" / "promote-visibility.jsonl"
    )
    with client:
        thread = client.assign_thread_to_project(thread_id, project_id)
        print(f"  App Server returned projectId={thread.get('projectId')!r}")
        # Read back: a successful call and the actual state are different things.
        fresh = client.read_thread(thread_id)
        print(f"  thread/read shows projectId={fresh.get('projectId')!r}")

    # Desktop does not rewrite its state instantly.
    time.sleep(max(0.0, args.settle))
    after = report("after attaching", thread_id)

    print()
    if after == INSIDE:
        print("CYCLE COMPLETE: the thread is in the project.")
        return 0
    if after == OUTSIDE:
        print(
            "CYCLE BROKEN AT THE LAST STEP: the thread became visible but stayed\n"
            "outside the project. The App Server attach passed, but the Desktop\n"
            "sidebar records did not receive it — so this call cannot put the\n"
            "thread into the project, and a path where Desktop itself creates\n"
            "the thread is needed."
        )
        return 2
    print(
        "CYCLE NOT STARTED: Desktop does not know the thread even after attaching.\n"
        "Check that the application is running and the project is open, then retry."
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
