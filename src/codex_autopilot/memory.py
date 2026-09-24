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

from .trust import TRUST_POLICY


SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = {1, 2, SCHEMA_VERSION}
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
    "external",
}
TRUTH_EVIDENCE_KINDS = TRUST_POLICY.truth_evidence_kinds(EVIDENCE_KINDS)
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
# The prefix of every department's rubric scope (department_acceptance.rubric_scope).
RESERVED_RUBRIC_SCOPE = "department-acceptance-rubric:"
MAX_STATEMENT_CHARS = 8_000
MAX_FIELD_CHARS = 16_000
MAX_PAGE_SIZE = 20
MEMORY_BUSY_TIMEOUT_MS = 10_000
MEMORY_LOCK_POLL_SECONDS = 0.01
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _process_lock(path: Path) -> threading.RLock:
    """Return one process-local guard for every canonical Project Memory lock."""

    key = str(path)
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", -1)}:
                raise
    finally:
        os.close(descriptor)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class MemoryError(RuntimeError):
    pass


class MemoryValidationError(MemoryError):
    pass


class MemoryBusyError(MemoryError):
    """The bounded Project Memory lock wait expired."""

    pass


@dataclass(frozen=True, slots=True)
class SearchPage:
    records: list[dict[str, Any]]
    next_cursor: str | None


def probe_sqlite_fts5(parent: Path | None = None) -> dict[str, str]:
    """Exercise SQLite+FTS5 without leaving a project database behind."""
    kwargs: dict[str, Any] = {}
    if parent is not None:
        kwargs["dir"] = parent
    with tempfile.TemporaryDirectory(prefix=".codex-autopilot-memory-probe-", **kwargs) as raw:
        path = Path(raw) / "probe.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(value)")
            connection.execute("INSERT INTO probe(value) VALUES (?)", ("project memory ready",))
            row = connection.execute("SELECT value FROM probe WHERE probe MATCH ?", ('"memory"',)).fetchone()
            if not row or row[0] != "project memory ready":
                raise MemoryError("SQLite FTS5 probe returned an unexpected result")
            connection.commit()
        except sqlite3.Error as exc:
            raise MemoryError(f"SQLite FTS5 is unavailable: {exc}") from exc
        finally:
            connection.close()
    return {"sqlite": sqlite3.sqlite_version, "fts5": "available"}


class ProjectMemory:
    """Small project-scoped evidence store used by the dispatcher and MCP server."""

    def __init__(self, project_root: Path, database: Path | None = None) -> None:
        self.root = project_root.expanduser().resolve()
        if not self.root.is_dir():
            raise MemoryValidationError(f"project root does not exist: {self.root}")
        if not (self.root / ".git").exists():
            raise MemoryValidationError("Project Memory requires the target Git repository")
        self.state_dir = self.root / ".codex-autopilot"
        self.path = (database or self.state_dir / "memory.sqlite3").expanduser().resolve()
        if not self._is_within(self.path, self.root):
            raise MemoryValidationError("memory database must be inside the target project")
        self.lock_path = self.state_dir / "memory.lock"
        self._process_lock = _process_lock(self.lock_path)
        self._initialize_lock = threading.Lock()
        self._initialized = False

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def initialize(self) -> None:
        if self._initialized:
            return
        initialize_locked = self._initialize_lock.acquire(
            timeout=MEMORY_BUSY_TIMEOUT_MS / 1_000
        )
        if not initialize_locked:
            raise MemoryBusyError(
                "Project Memory initialization remained busy for "
                f"{MEMORY_BUSY_TIMEOUT_MS} ms"
            )
        try:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._project_lock(exclusive=True):
                db = self._open_connection()
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS schema_meta "
                        "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                    )
                    current = db.execute(
                        "SELECT value FROM schema_meta WHERE key='schema_version'"
                    ).fetchone()
                    if current and int(current[0]) not in SUPPORTED_SCHEMA_VERSIONS:
                        raise MemoryError(
                            f"unsupported Project Memory schema: {current[0]}"
                        )
                    stored_root = db.execute(
                        "SELECT value FROM schema_meta WHERE key='project_root'"
                    ).fetchone()
                    if stored_root and Path(stored_root[0]).resolve() != self.root:
                        raise MemoryValidationError(
                            "memory database belongs to a different project root"
                        )
                    db.executescript(
                        """BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sequences (
                    prefix TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL CHECK(category IN ('truth','decision','constraint','question','observation')),
                    statement TEXT NOT NULL,
                    origin TEXT NOT NULL CHECK(origin IN ('user','agent','project','environment')),
                    status TEXT NOT NULL,
                    reason TEXT,
                    scope TEXT NOT NULL DEFAULT 'project',
                    needed_for TEXT,
                    confidence TEXT,
                    verification_method TEXT,
                    created_by TEXT NOT NULL,
                    provider TEXT,
                    provider_thread_id TEXT,
                    supersedes_id TEXT REFERENCES records(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    provenance TEXT NOT NULL DEFAULT 'deterministic_tool_output'
                        CHECK(provenance IN ('external_text','legacy_migration','human_input','deterministic_tool_output','human_verified')),
                    trust_level TEXT NOT NULL DEFAULT 'deterministic'
                        CHECK(trust_level IN ('unverified','deterministic','human_verified')),
                    summary TEXT NOT NULL,
                    path TEXT,
                    line_start INTEGER,
                    line_end INTEGER,
                    content_sha256 TEXT,
                    command TEXT,
                    result TEXT,
                    exit_code INTEGER,
                    tool_name TEXT,
                    artifact_path TEXT,
                    user_instruction TEXT,
                    environment_probe TEXT,
                    created_by TEXT NOT NULL,
                    provider TEXT,
                    provider_thread_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS record_evidence (
                    record_id TEXT NOT NULL REFERENCES records(id),
                    evidence_id TEXT NOT NULL REFERENCES evidence(id),
                    relation TEXT NOT NULL CHECK(relation IN ('supports','contradicts')),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(record_id, evidence_id, relation)
                );
                CREATE TABLE IF NOT EXISTS milestone_evidence (
                    milestone_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL REFERENCES evidence(id),
                    role TEXT NOT NULL DEFAULT 'verification',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(milestone_id, evidence_id)
                );
                CREATE TABLE IF NOT EXISTS milestone_completions (
                    milestone_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    worker_sequence INTEGER,
                    evidence_count INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verification_results (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    check_id TEXT NOT NULL,
                    policy TEXT NOT NULL CHECK(policy IN ('self','deterministic','independent','auto')),
                    verdict TEXT NOT NULL CHECK(verdict IN ('PASS','REVISE')),
                    summary TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL,
                    provider TEXT,
                    provider_thread_id TEXT NOT NULL,
                    provider_turn_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, check_id, provider_thread_id, provider_turn_id)
                );
                CREATE TABLE IF NOT EXISTS verification_result_evidence (
                    verification_id TEXT NOT NULL REFERENCES verification_results(id),
                    evidence_id TEXT NOT NULL REFERENCES evidence(id),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(verification_id, evidence_id)
                );
                CREATE INDEX IF NOT EXISTS verification_results_task_idx
                    ON verification_results(task_id, created_at, id);
                CREATE TABLE IF NOT EXISTS conflicts (
                    id TEXT PRIMARY KEY,
                    existing_record_id TEXT NOT NULL REFERENCES records(id),
                    incoming_record_id TEXT REFERENCES records(id),
                    incoming_evidence_id TEXT REFERENCES evidence(id),
                    statement TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resolution TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conflict_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conflict_id TEXT NOT NULL REFERENCES conflicts(id),
                    action TEXT NOT NULL,
                    details TEXT,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
                    record_id UNINDEXED,
                    statement,
                    reason,
                    tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
                    INSERT INTO records_fts(record_id, statement, reason)
                    VALUES (new.id, new.statement, coalesce(new.reason, ''));
                END;
                CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
                    DELETE FROM records_fts WHERE record_id = old.id;
                    INSERT INTO records_fts(record_id, statement, reason)
                    VALUES (new.id, new.statement, coalesce(new.reason, ''));
                END;
                CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
                    DELETE FROM records_fts WHERE record_id = old.id;
                END;
                """
                    )
                    TRUST_POLICY.migrate_memory_schema(db)
                    db.execute(
                        "INSERT INTO schema_meta(key,value) VALUES('project_root',?) "
                        "ON CONFLICT(key) DO NOTHING",
                        (str(self.root),),
                    )
                    db.execute(
                        "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(SCHEMA_VERSION),),
                    )
                    db.commit()
                except Exception:
                    if db.in_transaction:
                        db.rollback()
                    raise
                finally:
                    db.close()
            self._initialized = True
        finally:
            self._initialize_lock.release()

    def _open_connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(
            self.path,
            timeout=MEMORY_BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(f"PRAGMA busy_timeout={MEMORY_BUSY_TIMEOUT_MS}")
            db.execute("PRAGMA synchronous=FULL")
            return db
        except Exception:
            db.close()
            raise

    @contextmanager
    def _project_lock(
        self,
        *,
        exclusive: bool,
        timeout_ms: int = MEMORY_BUSY_TIMEOUT_MS,
    ) -> Iterator[None]:
        """Bound cross-process file operations around SQLite's own transactions."""

        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 0:
            raise MemoryValidationError("memory lock timeout_ms must be a non-negative integer")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_ms / 1_000
        process_locked = self._process_lock.acquire(timeout=timeout_ms / 1_000)
        if not process_locked:
            mode = "exclusive" if exclusive else "shared"
            raise MemoryBusyError(
                f"Project Memory {mode} lock remained busy for {timeout_ms} ms"
            )
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            locked = False
            try:
                while True:
                    try:
                        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                        locked = True
                        break
                    except OSError as exc:
                        if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                            raise
                        if time.monotonic() >= deadline:
                            mode = "exclusive" if exclusive else "shared"
                            raise MemoryBusyError(
                                f"Project Memory {mode} lock remained busy for "
                                f"{timeout_ms} ms"
                            ) from exc
                        time.sleep(MEMORY_LOCK_POLL_SECONDS)
                yield
            finally:
                try:
                    if locked:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        finally:
            self._process_lock.release()

    @contextmanager
    def _connect(
        self,
        *,
        write: bool = False,
        acquire_lock: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        lock = self._project_lock(exclusive=write) if acquire_lock else nullcontext()
        with lock:
            db: sqlite3.Connection | None = None
            try:
                db = self._open_connection()
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
                db.commit()
            except Exception as exc:
                if db is not None and db.in_transaction:
                    db.rollback()
                error_code = getattr(exc, "sqlite_errorcode", None)
                if (
                    isinstance(exc, sqlite3.OperationalError)
                    and (
                        error_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                        or "locked" in str(exc).lower()
                        or "busy" in str(exc).lower()
                    )
                ):
                    operation = "write" if write else "read"
                    raise MemoryBusyError(
                        f"Project Memory {operation} remained busy for "
                        f"{MEMORY_BUSY_TIMEOUT_MS} ms"
                    ) from exc
                raise
            finally:
                if db is not None:
                    db.close()

    @staticmethod
    def _required(value: object, name: str, maximum: int = MAX_FIELD_CHARS) -> str:
        text = str(value or "").strip()
        if not text:
            raise MemoryValidationError(f"{name} is required")
        if len(text) > maximum:
            raise MemoryValidationError(f"{name} exceeds {maximum} characters")
        return text

    @staticmethod
    def _optional(value: object, name: str, maximum: int = MAX_FIELD_CHARS) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        if len(text) > maximum:
            raise MemoryValidationError(f"{name} exceeds {maximum} characters")
        return text

    def _next_id(self, db: sqlite3.Connection, kind: str) -> str:
        prefix = PREFIXES[kind]
        db.execute(
            "INSERT INTO sequences(prefix,value) VALUES(?,1) "
            "ON CONFLICT(prefix) DO UPDATE SET value=value+1",
            (prefix,),
        )
        value = db.execute("SELECT value FROM sequences WHERE prefix=?", (prefix,)).fetchone()[0]
        return f"{prefix}-{int(value):03d}"

    def _audit(self, db: sqlite3.Connection, action: str, entity_type: str, entity_id: str, actor: str, details: dict[str, Any] | None = None) -> None:
        db.execute(
            "INSERT INTO audit_log(action,entity_type,entity_id,actor,details_json,created_at) VALUES(?,?,?,?,?,?)",
            (action, entity_type, entity_id, actor, json.dumps(details or {}, ensure_ascii=False, sort_keys=True), utc_now()),
        )

    def _resolve_project_path(self, raw: str, *, must_exist: bool = True) -> tuple[str, Path]:
        value = self._required(raw, "path", 4_096)
        candidate = Path(value).expanduser()
        candidate = candidate if candidate.is_absolute() else self.root / candidate
        try:
            resolved = candidate.resolve(strict=must_exist)
        except OSError as exc:
            raise MemoryValidationError(f"evidence path cannot be resolved: {value}") from exc
        if not self._is_within(resolved, self.root):
            raise MemoryValidationError("evidence path escapes the target project")
        relative = str(resolved.relative_to(self.root))
        return relative, resolved

    def record_evidence(
        self,
        *,
        kind: str,
        summary: str,
        created_by: str,
        milestone_id: str | None = None,
        role: str = "verification",
        path: str | None = None,
        line_start: int | None = None,
        line_end: int | None = None,
        command: str | None = None,
        result: str | None = None,
        exit_code: int | None = None,
        tool_name: str | None = None,
        artifact_path: str | None = None,
        user_instruction: str | None = None,
        environment_probe: str | None = None,
        provider: str | None = None,
        provider_thread_id: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        kind = self._required(kind, "kind", 64)
        if kind not in EVIDENCE_KINDS:
            # A refusal must name what is accepted. Measured: a worker tried
            # filesystem_verification, command_output, test_result and
            # verification, getting only "unsupported" each time, then went
            # to read the plugin sources. Six minutes instead of forty
            # seconds.
            raise MemoryValidationError(
                f"unsupported evidence kind: {kind}. "
                f"allowed kinds: {', '.join(sorted(EVIDENCE_KINDS))}"
            )
        summary = self._required(summary, "summary")
        actor = self._required(created_by, "created_by", 256)
        if kind == "external" and not str(provider or "").strip():
            raise MemoryValidationError(
                "R18: external evidence requires a non-empty provider naming "
                "where the material came from"
            )
        evidence_trust = TRUST_POLICY.classify_evidence(kind, provider=provider, error_type=MemoryValidationError)
        if line_start is not None and (not isinstance(line_start, int) or line_start < 1):
            raise MemoryValidationError("line_start must be a positive integer")
        if line_end is not None and (line_start is None or not isinstance(line_end, int) or line_end < line_start):
            raise MemoryValidationError("line_end must be >= line_start")
        relative_path = None
        content_hash = None
        if path is not None:
            relative_path, resolved = self._resolve_project_path(path)
            if resolved.is_file():
                content_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
                if line_start is not None:
                    line_count = len(resolved.read_text(encoding="utf-8", errors="replace").splitlines())
                    if line_start > max(1, line_count) or (line_end is not None and line_end > max(1, line_count)):
                        raise MemoryValidationError("evidence line range exceeds the referenced file")
            elif kind == "file":
                raise MemoryValidationError("file evidence path must reference a regular file")
        if kind in {"file", "artifact", "screenshot"} and relative_path is None and artifact_path is None:
            raise MemoryValidationError(
                f"{kind} evidence requires a project path: pass path="
                "<repository-relative file> (or artifact_path for a produced artifact)"
            )
        normalized_artifact = None
        if artifact_path is not None:
            normalized_artifact, artifact_resolved = self._resolve_project_path(artifact_path)
            if content_hash is None and artifact_resolved.is_file():
                content_hash = hashlib.sha256(artifact_resolved.read_bytes()).hexdigest()
        if kind in {"test", "build"}:
            if not self._optional(command, "command") or exit_code is None or not self._optional(result, "result"):
                raise MemoryValidationError(f"{kind} evidence requires command, result, and exit_code")
        if kind == "tool" and not self._optional(tool_name, "tool_name"):
            raise MemoryValidationError("tool evidence requires tool_name")
        if kind == "user_instruction" and not self._optional(user_instruction, "user_instruction"):
            raise MemoryValidationError("user_instruction evidence requires the instruction text")
        if kind == "environment_probe" and not self._optional(environment_probe, "environment_probe"):
            raise MemoryValidationError("environment_probe evidence requires probe details")
        normalized_milestone_id = None
        normalized_role = None
        if milestone_id:
            normalized_milestone_id = self._required(
                milestone_id, "milestone_id", 128
            )
            normalized_role = self._required(role, "role", 64)
            self._require_allowed_skill_evidence_role(
                normalized_milestone_id, normalized_role
            )
        with self._connect(write=True) as db:
            evidence_id = self._next_id(db, "evidence")
            db.execute(
                """INSERT INTO evidence(
                    id,kind,provenance,trust_level,summary,path,line_start,line_end,content_sha256,command,result,exit_code,
                    tool_name,artifact_path,user_instruction,environment_probe,created_by,provider,
                    provider_thread_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evidence_id, kind, evidence_trust.provenance.value,
                    evidence_trust.level.value, summary, relative_path, line_start,
                    line_end, content_hash,
                    self._optional(command, "command"), self._optional(result, "result"), exit_code,
                    self._optional(tool_name, "tool_name", 256), normalized_artifact,
                    self._optional(user_instruction, "user_instruction"),
                    self._optional(environment_probe, "environment_probe"), actor,
                    self._optional(provider, "provider", 128),
                    self._optional(provider_thread_id, "provider_thread_id", 256), utc_now(),
                ),
            )
            if normalized_milestone_id is not None:
                db.execute(
                    "INSERT INTO milestone_evidence(milestone_id,evidence_id,role,created_at) VALUES(?,?,?,?)",
                    (normalized_milestone_id, evidence_id, normalized_role, utc_now()),
                )
            self._audit(
                db,
                "record",
                "evidence",
                evidence_id,
                actor,
                {"kind": kind, "milestone_id": normalized_milestone_id},
            )
        return self.get_evidence(evidence_id)

    def _require_allowed_skill_evidence_role(
        self, milestone_id: str, role: str
    ) -> None:
        from .skill_packs import require_allowed_skill_evidence_role

        require_allowed_skill_evidence_role(self, milestone_id, role)

    def get_evidence(self, evidence_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT * FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if not row:
                raise MemoryValidationError(f"unknown evidence: {evidence_id}")
            result = dict(row)
            links = db.execute(
                "SELECT record_id,relation FROM record_evidence WHERE evidence_id=? ORDER BY record_id",
                (evidence_id,),
            ).fetchall()
            result["records"] = [dict(item) for item in links]
            verifications = db.execute(
                """SELECT verification_id FROM verification_result_evidence
                   WHERE evidence_id=? ORDER BY verification_id""",
                (evidence_id,),
            ).fetchall()
            result["verification_results"] = [
                str(item["verification_id"]) for item in verifications
            ]
            return result

    def record_verification_result(
        self,
        *,
        task_id: str,
        check_id: str,
        policy: str,
        verdict: str,
        summary: str,
        evidence_ids: Sequence[str],
        created_by: str,
        provider_thread_id: str,
        provider_turn_id: str,
        details: Mapping[str, Any] | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Record a caller-reported outcome without runtime authority.

        This public API intentionally cannot mint the attestation required by
        trusted Skill Pack source, promotion, or qualification gates.  The
        deterministic runner and fresh-verifier lifecycle use the internal
        companion below after the runtime has observed the real completion.
        """

        from .memory_verification import record_verification_result

        return record_verification_result(
            self,
            task_id=task_id,
            check_id=check_id,
            policy=policy,
            verdict=verdict,
            summary=summary,
            evidence_ids=evidence_ids,
            created_by=created_by,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            details=details,
            provider=provider,
            runtime_attested=False,
        )

    def _record_runtime_verification_result(
        self,
        *,
        task_id: str,
        check_id: str,
        policy: str,
        verdict: str,
        summary: str,
        evidence_ids: Sequence[str],
        created_by: str,
        provider_thread_id: str,
        provider_turn_id: str,
        details: Mapping[str, Any] | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Runtime-only sink for observed verifier/runner outcomes."""

        from .memory_verification import record_verification_result

        return record_verification_result(
            self,
            task_id=task_id,
            check_id=check_id,
            policy=policy,
            verdict=verdict,
            summary=summary,
            evidence_ids=evidence_ids,
            created_by=created_by,
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            details=details,
            provider=provider,
            runtime_attested=True,
        )

    def get_verification_result(self, verification_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM verification_results WHERE id=?", (verification_id,)
            ).fetchone()
            if row is None:
                raise MemoryValidationError(
                    f"unknown verification result: {verification_id}"
                )
            result = dict(row)
            result["details"] = json.loads(str(result.pop("details_json")))
            result["evidence"] = [
                dict(item)
                for item in db.execute(
                    """SELECT e.* FROM verification_result_evidence vre
                       JOIN evidence e ON e.id=vre.evidence_id
                       WHERE vre.verification_id=? ORDER BY e.created_at,e.id""",
                    (verification_id,),
                ).fetchall()
            ]
            return result

    def list_verification_results(
        self,
        *,
        task_id: str,
        limit: int = 8,
        cursor: str | None = None,
    ) -> SearchPage:
        self.initialize()
        task = self._required(task_id, "task_id", 128)
        if not isinstance(limit, int) or limit < 1 or limit > MAX_PAGE_SIZE:
            raise MemoryValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        offset = self._decode_cursor(cursor)
        with self._connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    """SELECT id,task_id,check_id,policy,verdict,summary,created_by,
                              provider,provider_thread_id,provider_turn_id,created_at
                       FROM verification_results WHERE task_id=?
                       ORDER BY created_at,id LIMIT ? OFFSET ?""",
                    (task, limit + 1, offset),
                ).fetchall()
            ]
        has_more = len(rows) > limit
        page = rows[:limit]
        for row in page:
            row["summary"] = str(row["summary"])[:1_000]
        return SearchPage(
            page,
            self._encode_cursor(offset + limit) if has_more else None,
        )

    def _create_record(
        self,
        *,
        category: str,
        statement: str,
        origin: str,
        status: str,
        created_by: str,
        reason: str | None = None,
        scope: str = "project",
        needed_for: str | None = None,
        confidence: str | None = None,
        verification_method: str | None = None,
        evidence_ids: Sequence[str] = (),
        supersedes_id: str | None = None,
        provider: str | None = None,
        provider_thread_id: str | None = None,
        reserved_scope: bool = False,
    ) -> dict[str, Any]:
        self.initialize()
        if category not in CATEGORIES:
            raise MemoryValidationError(f"unsupported category: {category}")
        # R30: a department's rubric scope has one writer (department_acceptance,
        # under its lock). Any record of a model there - a verified fact, an
        # observation - used to be accepted and became the standard its own
        # work was judged by, or made the history ambiguous for good.
        if not reserved_scope and str(scope or "").strip().startswith(RESERVED_RUBRIC_SCOPE):
            raise MemoryValidationError(
                f"scope {scope!r} is reserved for department rubrics; a new version is "
                "proposed with memory_store_department_rubric"
            )
        statement = self._required(statement, "statement", MAX_STATEMENT_CHARS)
        if origin not in ORIGINS:
            raise MemoryValidationError(f"unsupported origin: {origin}")
        actor = self._required(created_by, "created_by", 256)
        with self._connect(write=True) as db:
            if supersedes_id and not db.execute("SELECT 1 FROM records WHERE id=?", (supersedes_id,)).fetchone():
                raise MemoryValidationError(f"unknown superseded record: {supersedes_id}")
            for evidence_id in evidence_ids:
                if not db.execute("SELECT 1 FROM evidence WHERE id=?", (evidence_id,)).fetchone():
                    raise MemoryValidationError(f"unknown evidence: {evidence_id}")
            record_id = self._next_id(db, category)
            now = utc_now()
            db.execute(
                """INSERT INTO records(
                    id,category,statement,origin,status,reason,scope,needed_for,confidence,
                    verification_method,created_by,provider,provider_thread_id,supersedes_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record_id, category, statement, origin, status,
                    self._optional(reason, "reason"), self._required(scope, "scope", 256),
                    self._optional(needed_for, "needed_for", 256),
                    self._optional(confidence, "confidence", 32),
                    self._optional(verification_method, "verification_method", 512), actor,
                    self._optional(provider, "provider", 128),
                    self._optional(provider_thread_id, "provider_thread_id", 256),
                    supersedes_id, now, now,
                ),
            )
            for evidence_id in evidence_ids:
                db.execute(
                    "INSERT INTO record_evidence(record_id,evidence_id,relation,created_at) VALUES(?,?,?,?)",
                    (record_id, evidence_id, "supports", now),
                )
            self._audit(db, "create", category, record_id, actor, {"origin": origin, "status": status})
        return self.get_record(record_id)

    def record_verified_fact(
        self,
        *,
        statement: str,
        evidence_ids: Sequence[str],
        verification_method: str,
        created_by: str,
        scope: str = "project",
        contradicts: Sequence[str] = (),
        provider: str | None = None,
        provider_thread_id: str | None = None,
        reserved_scope: bool = False,
    ) -> dict[str, Any]:
        if not evidence_ids:
            raise MemoryValidationError("NO EVIDENCE -> NO TRUTH: verified facts require evidence_ids")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise MemoryValidationError("Truth evidence IDs must be unique")
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id,kind,provenance,trust_level FROM evidence "
                f"WHERE id IN ({','.join('?' for _ in evidence_ids)})",
                tuple(evidence_ids),
            ).fetchall()
            found = {str(row["id"]): row for row in rows}
            missing = [item for item in evidence_ids if item not in found]
            if missing:
                raise MemoryValidationError(f"unknown evidence: {', '.join(missing)}")
            TRUST_POLICY.require_truth_rows(
                (found[item] for item in evidence_ids), error_type=MemoryValidationError,
                message_prefix="R18: evidence below the trust threshold cannot support Truth",
            )
        fact = self._create_record(
            category="truth", statement=statement, origin="project", status="verified",
            created_by=created_by, scope=scope, verification_method=verification_method,
            evidence_ids=evidence_ids, provider=provider, provider_thread_id=provider_thread_id,
            reserved_scope=reserved_scope,
        )
        conflict_ids = [self.open_conflict(existing_record_id=item, incoming_record_id=fact["id"], statement=f"New verified fact {fact['id']} conflicts with {item}.", created_by=created_by)["id"] for item in contradicts]
        fact["conflicts_created"] = conflict_ids
        return fact

    def add_observation(self, *, statement: str, created_by: str, confidence: str = "medium", scope: str = "project", reason: str | None = None, provider: str | None = None, provider_thread_id: str | None = None) -> dict[str, Any]:
        if confidence not in {"low", "medium", "high"}:
            raise MemoryValidationError("observation confidence must be low, medium, or high")
        return self._create_record(category="observation", statement=statement, origin="agent", status="unverified", created_by=created_by, confidence=confidence, scope=scope, reason=reason, provider=provider, provider_thread_id=provider_thread_id)

    def propose_decision(self, *, statement: str, origin: str, created_by: str, status: str = "proposed", reason: str | None = None, scope: str = "project", evidence_ids: Sequence[str] = (), provider: str | None = None, provider_thread_id: str | None = None) -> dict[str, Any]:
        if status not in {"proposed", "accepted", "superseded", "rejected"}:
            raise MemoryValidationError("invalid decision status")
        if origin == "agent" and status == "accepted":
            raise MemoryValidationError("agent-origin decisions must begin as proposed")
        if (
            status == "accepted"
            and origin != "user"
            and self._has_external_evidence(evidence_ids)
        ):
            # The user may make a decision citing external text: they decide.
            # The ban closes another path - an agent passing off an
            # instruction found outside as an accepted decision.
            raise MemoryValidationError(
                "R18: a non-user decision resting on external content must "
                "begin as proposed; external content does not decide"
            )
        return self._create_record(category="decision", statement=statement, origin=origin, status=status, created_by=created_by, reason=reason, scope=scope, evidence_ids=evidence_ids, provider=provider, provider_thread_id=provider_thread_id)

    def _has_external_evidence(self, evidence_ids: Sequence[str]) -> bool:
        """R18: does the decision rest on evidence below the Truth threshold."""

        if not evidence_ids:
            return False
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                f"SELECT kind,provenance,trust_level FROM evidence "
                f"WHERE id IN ({','.join('?' for _ in evidence_ids)})",
                tuple(evidence_ids),
            ).fetchall()
        return TRUST_POLICY.any_row_is_below_truth(
            rows, error_type=MemoryValidationError
        )

    def accepted_user_decision(self, statement: str) -> dict[str, Any] | None:
        """An accepted user decision with exactly this text, or None.

        An exact match, not a search: this is an authorization lookup, and
        it must not fire on a similar wording. The origin restriction is
        part of the check, not a convenience filter: a decision recorded by
        an agent is not an authorization.
        """

        text = self._required(statement, "statement")
        self.initialize()
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM records WHERE category='decision' AND status='accepted' "
                "AND origin='user' AND statement=? ORDER BY updated_at DESC, id ASC LIMIT 1",
                (text,),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_decision_status(self, decision_id: str, status: str, *, actor: str, reason: str | None = None) -> dict[str, Any]:
        if status not in {"proposed", "accepted", "superseded", "rejected"}:
            raise MemoryValidationError("invalid decision status")
        return self._set_record_status(decision_id, "decision", status, actor, reason)

    def add_constraint(self, *, statement: str, origin: str, created_by: str, scope: str = "project", reason: str | None = None, evidence_ids: Sequence[str] = ()) -> dict[str, Any]:
        if origin != "user" and self._has_external_evidence(evidence_ids):
            # R18. A Constraint has no "proposed" state: it is in force from
            # the moment it is recorded. So this is a refusal, not a
            # demotion to proposed as with a decision: nothing to demote.
            raise MemoryValidationError(
                "R18: a non-user constraint cannot rest on external content; "
                "record it as an observation and raise a Question instead"
            )
        return self._create_record(category="constraint", statement=statement, origin=origin, status="active", created_by=created_by, reason=reason, scope=scope, evidence_ids=evidence_ids)

    def open_question(self, *, question: str, created_by: str, needed_for: str | None = None, scope: str = "project") -> dict[str, Any]:
        return self._create_record(category="question", statement=question, origin="agent", status="open", created_by=created_by, needed_for=needed_for, scope=scope)

    def resolve_question(self, question_id: str, *, actor: str, reason: str, evidence_ids: Sequence[str] = ()) -> dict[str, Any]:
        self.initialize()
        normalized_actor = self._required(actor, "actor", 256)
        normalized_reason = self._required(reason, "reason")
        evidence = tuple(str(item).strip() for item in evidence_ids)
        if any(not item for item in evidence) or len(set(evidence)) != len(evidence):
            raise MemoryValidationError("question evidence IDs must be unique")
        with self._connect(write=True) as db:
            row = db.execute(
                "SELECT category FROM records WHERE id=?", (question_id,)
            ).fetchone()
            if not row or row["category"] != "question":
                raise MemoryValidationError(f"{question_id} is not a question")
            now = utc_now()
            for evidence_id in evidence:
                if not db.execute(
                    "SELECT 1 FROM evidence WHERE id=?", (evidence_id,)
                ).fetchone():
                    raise MemoryValidationError(f"unknown evidence: {evidence_id}")
                db.execute(
                    """INSERT OR IGNORE INTO record_evidence(
                        record_id,evidence_id,relation,created_at
                    ) VALUES(?,?,?,?)""",
                    (question_id, evidence_id, "supports", now),
                )
            db.execute(
                "UPDATE records SET status='resolved',reason=?,updated_at=? WHERE id=?",
                (normalized_reason, now, question_id),
            )
            self._audit(
                db,
                "status",
                "question",
                question_id,
                normalized_actor,
                {
                    "status": "resolved",
                    "reason": normalized_reason,
                    "evidence_ids": list(evidence),
                },
            )
        return self.get_record(question_id)

    # R18: states in which a record stops being a supposition and starts
    # governing the work. Entering them is the second moment when taint
    # must be re-checked: the intake check is bypassed in two calls by
    # first recording "proposed" and then simply changing the status.
    _BINDING_STATUSES = frozenset({"accepted", "active", "verified"})

    def _rests_on_external(self, db: sqlite3.Connection, record_id: str) -> bool:
        rows = db.execute(
            "SELECT e.kind,e.provenance,e.trust_level FROM record_evidence re "
            "JOIN evidence e ON e.id=re.evidence_id "
            "WHERE re.record_id=? AND re.relation='supports'",
            (record_id,),
        ).fetchall()
        return TRUST_POLICY.any_row_is_below_truth(
            rows, error_type=MemoryValidationError
        )

    def _set_record_status(self, record_id: str, category: str, status: str, actor: str, reason: str | None) -> dict[str, Any]:
        self.initialize()
        with self._connect(write=True) as db:
            row = db.execute("SELECT category,origin FROM records WHERE id=?", (record_id,)).fetchone()
            if not row or row["category"] != category:
                raise MemoryValidationError(f"{record_id} is not a {category}")
            if (
                status in self._BINDING_STATUSES
                and str(row["origin"] or "") != "user"
                and self._rests_on_external(db, record_id)
            ):
                raise MemoryValidationError(
                    f"R18: {record_id} rests on external content and cannot be "
                    f"promoted to {status} by anyone but the user; external "
                    "content does not decide"
                )
            db.execute("UPDATE records SET status=?,reason=coalesce(?,reason),updated_at=? WHERE id=?", (status, self._optional(reason, "reason"), utc_now(), record_id))
            self._audit(db, "status", category, record_id, actor, {"status": status, "reason": reason})
        return self.get_record(record_id)

    def attach_evidence(self, record_id: str, evidence_id: str, *, relation: str, actor: str) -> dict[str, Any]:
        if relation not in {"supports", "contradicts"}:
            raise MemoryValidationError("relation must be supports or contradicts")
        self.initialize()
        normalized_actor = self._required(actor, "actor", 256)
        with self._connect(write=True) as db:
            record = db.execute("SELECT category,status,origin FROM records WHERE id=?", (record_id,)).fetchone()
            evidence = db.execute(
                "SELECT kind,provenance,trust_level FROM evidence WHERE id=?",
                (evidence_id,),
            ).fetchone()
            if not record:
                raise MemoryValidationError(f"unknown record: {record_id}")
            if not evidence:
                raise MemoryValidationError(f"unknown evidence: {evidence_id}")
            evidence_below_truth = TRUST_POLICY.row_is_below_truth(
                evidence, error_type=MemoryValidationError
            )
            if (
                relation == "supports"
                and evidence_below_truth
                and record["status"] in self._BINDING_STATUSES
                and str(record["origin"] or "") != "user"
            ):
                raise MemoryValidationError(
                    f"R18: evidence below the trust threshold cannot be attached in support of "
                    f"{record_id} while it is {record['status']}; attach it as "
                    "contradicts, or let the user decide"
                )
            inserted = db.execute(
                "INSERT OR IGNORE INTO record_evidence(record_id,evidence_id,relation,created_at) VALUES(?,?,?,?)",
                (record_id, evidence_id, relation, utc_now()),
            ).rowcount
            self._audit(db, "attach_evidence", record["category"], record_id, normalized_actor, {"evidence_id": evidence_id, "relation": relation})
            conflict_id = None
            if inserted and relation == "contradicts" and record["category"] == "truth":
                conflict_id = self._open_conflict_in_transaction(
                    db,
                    existing_record_id=record_id,
                    incoming_evidence_id=evidence_id,
                    statement=(
                        f"Evidence {evidence_id} contradicts verified fact {record_id}."
                    ),
                    created_by=normalized_actor,
                )
        return {
            "record": self.get_record(record_id),
            "conflict": self.get_conflict(conflict_id) if conflict_id else None,
        }

    def get_record(self, record_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if not row:
                raise MemoryValidationError(f"unknown record: {record_id}")
            result = dict(row)
            links = db.execute(
                """SELECT e.*,re.relation FROM record_evidence re
                   JOIN evidence e ON e.id=re.evidence_id WHERE re.record_id=?
                   ORDER BY e.created_at,e.id""",
                (record_id,),
            ).fetchall()
            result["evidence"] = [dict(item) for item in links]
            conflicts = db.execute(
                "SELECT * FROM conflicts WHERE existing_record_id=? OR incoming_record_id=? ORDER BY created_at,id",
                (record_id, record_id),
            ).fetchall()
            result["conflicts"] = [dict(item) for item in conflicts]
            return result

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        if not tokens:
            raise MemoryValidationError("search query must contain a word or number")
        return " AND ".join('"' + token.replace('"', '""') + '"*' for token in tokens[:20])

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        return base64.urlsafe_b64encode(json.dumps({"offset": offset}, separators=(",", ":")).encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        if not cursor:
            return 0
        try:
            raw = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(raw.encode()).decode())
            offset = int(value["offset"])
            if offset < 0:
                raise ValueError
            return offset
        except Exception as exc:
            raise MemoryValidationError("invalid pagination cursor") from exc

    def search(self, *, query: str, categories: Sequence[str] | None = None, scope: str | None = None, limit: int = 8, cursor: str | None = None) -> SearchPage:
        self.initialize()
        if not isinstance(limit, int) or limit < 1 or limit > MAX_PAGE_SIZE:
            raise MemoryValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        selected = list(categories or sorted(CATEGORIES))
        if not selected or any(item not in CATEGORIES for item in selected):
            raise MemoryValidationError("categories contain an unsupported value")
        offset = self._decode_cursor(cursor)
        category_sql = ",".join("?" for _ in selected)
        params: list[Any] = [self._fts_query(query), *selected]
        scope_sql = ""
        if scope:
            scope_sql = " AND r.scope=?"
            params.append(scope)
        params.extend([limit + 1, offset])
        sql = f"""SELECT r.*, bm25(records_fts) AS relevance
            FROM records_fts JOIN records r ON r.id=records_fts.record_id
            WHERE records_fts MATCH ? AND r.category IN ({category_sql}){scope_sql}
            ORDER BY relevance ASC, r.updated_at DESC, r.id ASC LIMIT ? OFFSET ?"""
        with self._connect() as db:
            rows = [dict(row) for row in db.execute(sql, params).fetchall()]
        has_more = len(rows) > limit
        page = rows[:limit]
        for row in page:
            row.pop("reason", None)
            row.pop("provider_thread_id", None)
            row.pop("relevance", None)
            row["statement"] = str(row["statement"])[:1_000]
        return SearchPage(page, self._encode_cursor(offset + limit) if has_more else None)

    def list_records(self, *, categories: Sequence[str], statuses: Sequence[str] | None = None, scope: str | None = None, limit: int = 8, cursor: str | None = None, full_statements: bool = False) -> SearchPage:
        self.initialize()
        if not categories or any(item not in CATEGORIES for item in categories):
            raise MemoryValidationError("categories contain an unsupported value")
        if not isinstance(limit, int) or limit < 1 or limit > MAX_PAGE_SIZE:
            raise MemoryValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        offset = self._decode_cursor(cursor)
        clauses = [f"category IN ({','.join('?' for _ in categories)})"]
        params: list[Any] = list(categories)
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if scope:
            clauses.append("scope=?")
            params.append(scope)
        params.extend([limit + 1, offset])
        with self._connect() as db:
            rows = [dict(row) for row in db.execute(
                f"SELECT * FROM records WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC,id ASC LIMIT ? OFFSET ?",
                params,
            ).fetchall()]
        has_more = len(rows) > limit
        page = rows[:limit]
        for row in page:
            row.pop("reason", None)
            row.pop("provider_thread_id", None)
            if not full_statements:
                row["statement"] = str(row["statement"])[:1_000]
        return SearchPage(page, self._encode_cursor(offset + limit) if has_more else None)

    def milestone_evidence(self, milestone_id: str, *, after_audit_id: int = 0, limit: int = 20) -> list[dict[str, Any]]:
        self.initialize()
        if limit < 1 or limit > 100:
            raise MemoryValidationError("limit must be between 1 and 100")
        with self._connect() as db:
            rows = db.execute(
                """SELECT e.*,me.role FROM milestone_evidence me JOIN evidence e ON e.id=me.evidence_id
                   JOIN audit_log a ON a.entity_type='evidence' AND a.entity_id=e.id AND a.action='record'
                   WHERE me.milestone_id=? AND a.id>? ORDER BY a.id LIMIT ?""",
                (milestone_id, after_audit_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def audit_highwater(self) -> int:
        self.initialize()
        with self._connect() as db:
            return int(db.execute("SELECT coalesce(max(id),0) FROM audit_log").fetchone()[0])


    def mark_milestone_complete(self, *, milestone_id: str, run_id: str, worker_sequence: int, source: str = "worker") -> dict[str, Any]:
        self.initialize()
        normalized_milestone = self._required(milestone_id, "milestone_id", 128)
        normalized_run = self._required(run_id, "run_id", 256)
        normalized_source = self._required(source, "source", 128)
        if isinstance(worker_sequence, bool) or not isinstance(worker_sequence, int):
            raise MemoryValidationError("worker_sequence must be an integer")
        target = self.state_dir / "memory-backups" / "latest.sqlite3"
        with self._project_lock(exclusive=True):
            with self._connect(write=True, acquire_lock=False) as db:
                evidence_count = int(
                    db.execute(
                        "SELECT count(*) FROM milestone_evidence WHERE milestone_id=?",
                        (normalized_milestone,),
                    ).fetchone()[0]
                )
                if not evidence_count:
                    raise MemoryValidationError(
                        f"milestone {normalized_milestone} has no recorded evidence"
                    )
                db.execute(
                    """INSERT INTO milestone_completions(
                        milestone_id,run_id,worker_sequence,evidence_count,source,completed_at
                    ) VALUES(?,?,?,?,?,?) ON CONFLICT(milestone_id) DO UPDATE SET
                    run_id=excluded.run_id,worker_sequence=excluded.worker_sequence,
                    evidence_count=excluded.evidence_count,source=excluded.source,
                    completed_at=excluded.completed_at""",
                    (
                        normalized_milestone,
                        normalized_run,
                        worker_sequence,
                        evidence_count,
                        normalized_source,
                        utc_now(),
                    ),
                )
                self._audit(
                    db,
                    "complete",
                    "milestone",
                    normalized_milestone,
                    f"dispatcher:{normalized_run}",
                    {"evidence_count": evidence_count, "source": normalized_source},
                )
            self._backup_locked(target)
        return {
            "milestone_id": normalized_milestone,
            "evidence_count": evidence_count,
            "source": normalized_source,
        }






    def apply_user_correction(self, *, statement: str, related_ids: Sequence[str], actor: str = "user") -> dict[str, Any]:
        decision = self.propose_decision(statement=statement, origin="user", created_by=actor, status="accepted", reason="Explicit user correction")
        superseded: list[str] = []
        conflicts: list[str] = []
        for record_id in related_ids:
            record = self.get_record(record_id)
            if record["category"] == "decision" and record["origin"] == "agent" and record["status"] in {"proposed", "accepted"}:
                self.set_decision_status(record_id, "superseded", actor=actor, reason=f"Superseded by {decision['id']}")
                superseded.append(record_id)
            elif record["category"] == "truth" and record["status"] in {"verified", "disputed"}:
                conflict = self.open_conflict(existing_record_id=record_id, incoming_record_id=decision["id"], statement=f"User desired state {decision['id']} differs from actual verified state {record_id}; reverify project reality.", created_by=actor)
                conflicts.append(conflict["id"])
        return {"decision": decision, "superseded": superseded, "conflicts": conflicts}

    def critical_constraints(self, limit: int = 8) -> list[dict[str, Any]]:
        page = self.list_records(categories=["constraint"], statuses=["active"], limit=min(limit, MAX_PAGE_SIZE))
        records = page.records
        records.sort(key=lambda item: (0 if item["origin"] == "user" else 1, item["id"]))
        return records[:limit]

    def context_ids(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        try:
            page = self.search(query=query, categories=["truth", "decision", "question", "observation"], limit=min(limit, MAX_PAGE_SIZE))
        except MemoryValidationError:
            return []
        return [{"id": item["id"], "category": item["category"], "status": item["status"]} for item in page.records]

    # --- delegates to the extracted modules ----------------------------
    def open_conflict(self, *args, **kwargs):
        from .memory_conflicts import open_conflict
        return open_conflict(self, *args, **kwargs)

    def _open_conflict_in_transaction(self, *args, **kwargs):
        from .memory_conflicts import open_conflict_in_transaction
        return open_conflict_in_transaction(self, *args, **kwargs)

    def get_conflict(self, *args, **kwargs):
        from .memory_conflicts import get_conflict
        return get_conflict(self, *args, **kwargs)

    def resolve_conflict(self, *args, **kwargs):
        from .memory_conflicts import resolve_conflict
        return resolve_conflict(self, *args, **kwargs)

    def render_views(self) -> None:
        from .memory_views import render_views
        return render_views(self)

    def export_summary(self, *args, **kwargs):
        from .memory_views import export_summary
        return export_summary(self, *args, **kwargs)

    def backup(self, *args, **kwargs):
        from .memory_durability import backup
        return backup(self, *args, **kwargs)

    def _backup_locked(self, *args, **kwargs):
        from .memory_durability import _backup_locked
        return _backup_locked(self, *args, **kwargs)

    def recover_latest(self, *args, **kwargs):
        from .memory_durability import recover_latest
        return recover_latest(self, *args, **kwargs)

    def _assert_connection_integrity(self, *args, **kwargs):
        from .memory_durability import _assert_connection_integrity
        return _assert_connection_integrity(self, *args, **kwargs)

    def integrity_check(self, *args, **kwargs):
        from .memory_durability import integrity_check
        return integrity_check(self, *args, **kwargs)

    def ensure_healthy(self, *args, **kwargs):
        from .memory_durability import ensure_healthy
        return ensure_healthy(self, *args, **kwargs)
