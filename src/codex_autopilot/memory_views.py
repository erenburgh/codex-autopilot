"""Views over Project Memory.

Rendering PROJECT_STATE.md and DECISIONS.md is presentation, not storage.
Moved out of the ProjectMemory class: these are operations OVER memory,
not its internals. Thin delegating methods stay on the class, so the API
and every call site are unchanged.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any


SCHEMA_VERSION = 2
CATEGORIES = {"truth", "decision", "constraint", "question", "observation"}
ORIGINS = {"user", "agent", "project", "environment"}
EVIDENCE_KINDS = {
    "user_instruction",
    "file",
    "git",
    "test",
    "build",
    "tool",
    "screenshot",
    "artifact",
    "environment_probe",
    "migration",
}
TRUTH_EVIDENCE_KINDS = EVIDENCE_KINDS - {"migration"}
PREFIXES = {
    "truth": "FACT",
    "decision": "DEC",
    "constraint": "CON",
    "question": "Q",
    "observation": "OBS",
    "evidence": "EVID",
    "conflict": "CONFLICT",
    "verification": "VERIFY",
}
MAX_STATEMENT_CHARS = 8_000
MAX_FIELD_CHARS = 16_000
MAX_PAGE_SIZE = 20
MEMORY_BUSY_TIMEOUT_MS = 10_000
MEMORY_LOCK_POLL_SECONDS = 0.01
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}



from .memory import ProjectMemory, CATEGORIES, EVIDENCE_KINDS, SCHEMA_VERSION, _atomic_write_text


def render_views(memory) -> None:
    memory.initialize()
    counts: dict[str, int] = {}
    with memory._project_lock(exclusive=True):
        with memory._connect(acquire_lock=False) as db:
            for row in db.execute(
                "SELECT category,count(*) AS count FROM records GROUP BY category"
            ):
                counts[row["category"]] = row["count"]
            open_conflicts = int(
                db.execute(
                    "SELECT count(*) FROM conflicts WHERE status='needs_review'"
                ).fetchone()[0]
            )
            verification_count = int(
                db.execute("SELECT count(*) FROM verification_results").fetchone()[0]
            )
            completions = [
                dict(row)
                for row in db.execute(
                    """SELECT * FROM milestone_completions
                       ORDER BY completed_at,milestone_id"""
                ).fetchall()
            ]
            decisions = [
                dict(row)
                for row in db.execute(
                    """SELECT id,statement,origin,status,reason FROM records
                       WHERE category='decision' ORDER BY created_at,id"""
                ).fetchall()
            ]
        state_lines = [
            "# Project state (generated view)",
            "",
            "Canonical project knowledge is stored in `memory.sqlite3`. This file is a cache, not evidence.",
            "",
            "## Record counts",
            *[f"- {name}: {counts.get(name, 0)}" for name in sorted(CATEGORIES)],
            f"- verification results: {verification_count}",
            f"- open conflicts: {open_conflicts}",
            "",
            "## Verified milestone completions",
            *(
                [
                    f"- {item['milestone_id']}: {item['evidence_count']} evidence record(s) ({item['source']})"
                    for item in completions
                ]
                or ["- None"]
            ),
        ]
        decision_lines = [
            "# Decisions (generated view)",
            "",
            "Canonical decisions and provenance are stored in `memory.sqlite3`.",
            "",
            *(
                [
                    f"- {item['id']} [{item['origin']}/{item['status']}]: {item['statement']}"
                    + (f" — {item['reason']}" if item["reason"] else "")
                    for item in decisions
                ]
                or ["- None"]
            ),
        ]
        _atomic_write_text(
            memory.state_dir / "PROJECT_STATE.md", "\n".join(state_lines) + "\n"
        )
        _atomic_write_text(
            memory.state_dir / "DECISIONS.md", "\n".join(decision_lines) + "\n"
        )


def export_summary(memory) -> dict[str, Any]:
    memory.initialize()
    with memory._connect() as db:
        counts = {row["category"]: row["count"] for row in db.execute("SELECT category,count(*) count FROM records GROUP BY category")}
        return {
            "schema_version": SCHEMA_VERSION,
            "database": str(memory.path),
            "project_root": str(memory.root),
            "records": counts,
            "evidence": int(db.execute("SELECT count(*) FROM evidence").fetchone()[0]),
            "verification_results": int(
                db.execute("SELECT count(*) FROM verification_results").fetchone()[0]
            ),
            "open_conflicts": int(db.execute("SELECT count(*) FROM conflicts WHERE status='needs_review'").fetchone()[0]),
            "audit_highwater": int(db.execute("SELECT coalesce(max(id),0) FROM audit_log").fetchone()[0]),
        }
