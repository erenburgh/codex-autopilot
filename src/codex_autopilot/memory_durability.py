"""Долговечность Project Memory: снимки, восстановление, целостность.

Отделено от операций над записями. Здесь только то, что отвечает
за сохранность базы, а не за её содержимое. В классе ProjectMemory
оставлены тонкие делегирующие методы.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping, Sequence


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



from .memory import (  # noqa: F401
    ProjectMemory,
    CATEGORIES,
    EVIDENCE_KINDS,
    MAX_FIELD_CHARS,
    MAX_PAGE_SIZE,
    MAX_STATEMENT_CHARS,
    MEMORY_BUSY_TIMEOUT_MS,
    MEMORY_LOCK_POLL_SECONDS,
    MemoryBusyError,
    MemoryError,
    MemoryValidationError,
    ORIGINS,
    PREFIXES,
    SCHEMA_VERSION,
    TRUTH_EVIDENCE_KINDS,
    _PROCESS_LOCKS_GUARD,
    _fsync_directory,
)


def backup(memory, destination: Path | None = None) -> Path:
    memory.initialize()
    target = (destination or memory.state_dir / "memory-backups" / "latest.sqlite3").expanduser().resolve()
    if not memory._is_within(target, memory.root):
        raise MemoryValidationError("memory backup must remain inside the target project")
    reserved = {
        memory.path,
        memory.lock_path.resolve(),
        memory.path.with_name(memory.path.name + "-wal"),
        memory.path.with_name(memory.path.name + "-shm"),
    }
    if target in reserved:
        raise MemoryValidationError(
            "memory backup cannot overwrite the live database or lock files"
        )
    with memory._project_lock(exclusive=True):
        return memory._backup_locked(target)


def _backup_locked(memory, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{target.name}.tmp-", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(raw_temporary)
    try:
        source_db = memory._open_connection()
        destination_db = sqlite3.connect(temporary, isolation_level=None)
        destination_db.row_factory = sqlite3.Row
        try:
            source_db.backup(destination_db)
            memory._assert_connection_integrity(destination_db)
        finally:
            destination_db.close()
            source_db.close()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def recover_latest(memory) -> Path:
    backup = memory.state_dir / "memory-backups" / "latest.sqlite3"
    if not backup.is_file():
        raise MemoryError("Project Memory is corrupt and no verified milestone backup exists")
    if backup.is_symlink() or not memory._is_within(backup.resolve(), memory.root):
        raise MemoryValidationError(
            "latest Project Memory backup must be a regular project-local file"
        )
    with memory._project_lock(exclusive=True):
        check = sqlite3.connect(backup, isolation_level=None)
        check.row_factory = sqlite3.Row
        try:
            memory._assert_connection_integrity(check)
            stored_root = check.execute(
                "SELECT value FROM schema_meta WHERE key='project_root'"
            ).fetchone()
            if stored_root is None or Path(stored_root[0]).resolve() != memory.root:
                raise MemoryValidationError(
                    "latest Project Memory backup belongs to a different project"
                )
        finally:
            check.close()
        quarantine = memory.state_dir / (
            "memory-corrupt-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            + f"-{time.time_ns() % 1_000_000_000:09d}.sqlite3"
        )
        if memory.path.exists():
            shutil.copy2(memory.path, quarantine)
            with quarantine.open("rb") as handle:
                os.fsync(handle.fileno())
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{memory.path.name}.restore-", dir=memory.path.parent
        )
        os.close(descriptor)
        temporary = Path(raw_temporary)
        try:
            shutil.copy2(backup, temporary)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, memory.path)
            memory.path.with_name(memory.path.name + "-wal").unlink(missing_ok=True)
            memory.path.with_name(memory.path.name + "-shm").unlink(missing_ok=True)
            _fsync_directory(memory.path.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        restored = memory._open_connection()
        try:
            version = int(
                restored.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0]
            )
            if version == 1:
                restored.executescript(
                    """BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS verification_results (
                        id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        check_id TEXT NOT NULL,
                        policy TEXT NOT NULL CHECK(policy IN ('memory','deterministic','independent','auto')),
                        verdict TEXT NOT NULL CHECK(verdict IN ('PASS','REVISE')),
                        summary TEXT NOT NULL,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        created_by TEXT NOT NULL,
                        provider TEXT,
                        provider_thread_id TEXT NOT NULL,
                        provider_turn_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE(task_id,check_id,provider_thread_id,provider_turn_id)
                    );
                    CREATE TABLE IF NOT EXISTS verification_result_evidence (
                        verification_id TEXT NOT NULL REFERENCES verification_results(id),
                        evidence_id TEXT NOT NULL REFERENCES evidence(id),
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(verification_id,evidence_id)
                    );
                    CREATE INDEX IF NOT EXISTS verification_results_task_idx
                        ON verification_results(task_id,created_at,id);
                    UPDATE schema_meta SET value='2' WHERE key='schema_version';
                    COMMIT;"""
                )
            memory._assert_connection_integrity(restored)
        finally:
            restored.close()
        memory._initialized = True
        return quarantine


def _assert_connection_integrity(memory, db: sqlite3.Connection) -> None:
    result = str(db.execute("PRAGMA integrity_check").fetchone()[0])
    foreign_key_errors = len(db.execute("PRAGMA foreign_key_check").fetchall())
    tables = {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    }
    required_tables = {
        "schema_meta",
        "sequences",
        "records",
        "evidence",
        "record_evidence",
        "milestone_evidence",
        "milestone_completions",
        "conflicts",
        "conflict_history",
        "audit_log",
    }
    missing_tables = sorted(required_tables - tables)
    version_row = (
        db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if "schema_meta" in tables
        else None
    )
    schema_version = int(version_row[0]) if version_row is not None else 0
    if schema_version not in {1, SCHEMA_VERSION}:
        raise MemoryError(
            f"memory integrity failure: unsupported schema={schema_version}"
        )
    if schema_version >= 2:
        required_tables.update(
            {"verification_results", "verification_result_evidence"}
        )
        missing_tables = sorted(required_tables - tables)
    truths_without_evidence = int(
        db.execute(
            """SELECT count(*) FROM records r
               WHERE r.category='truth' AND NOT EXISTS(
                   SELECT 1 FROM record_evidence re
                   JOIN evidence e ON e.id=re.evidence_id
                   WHERE re.record_id=r.id AND re.relation='supports'
                     AND e.kind!='migration')"""
        ).fetchone()[0]
    )
    verification_tables = {
        "verification_results",
        "verification_result_evidence",
    }
    partial_verification_schema = bool(verification_tables & tables) and not (
        verification_tables <= tables
    )
    verifications_without_evidence = (
        int(
            db.execute(
                """SELECT count(*) FROM verification_results vr WHERE NOT EXISTS(
                       SELECT 1 FROM verification_result_evidence vre
                       WHERE vre.verification_id=vr.id)"""
            ).fetchone()[0]
        )
        if verification_tables <= tables
        else 0
    )
    invalid_verification_evidence = (
        int(
            db.execute(
                """SELECT count(*) FROM verification_result_evidence vre
                   JOIN evidence e ON e.id=vre.evidence_id
                   WHERE e.kind='migration'"""
            ).fetchone()[0]
        )
        if verification_tables <= tables
        else 0
    )
    if (
        result != "ok"
        or foreign_key_errors
        or missing_tables
        or partial_verification_schema
        or truths_without_evidence
        or verifications_without_evidence
        or invalid_verification_evidence
    ):
        raise MemoryError(
            "memory integrity failure: "
            f"sqlite={result}, foreign_keys={foreign_key_errors}, "
            f"missing_tables={missing_tables}, "
            f"partial_verification_schema={partial_verification_schema}, "
            f"truths_without_evidence={truths_without_evidence}, "
            f"verifications_without_evidence={verifications_without_evidence}, "
            f"invalid_verification_evidence={invalid_verification_evidence}"
        )


def integrity_check(memory) -> str:
    memory.initialize()
    with memory._connect() as db:
        memory._assert_connection_integrity(db)
        return "ok"


def ensure_healthy(memory, *, recover: bool = True) -> str:
    try:
        return memory.integrity_check()
    except MemoryBusyError:
        raise
    except (sqlite3.DatabaseError, MemoryError, OSError):
        if not recover:
            raise
        memory.recover_latest()
        return "recovered"
