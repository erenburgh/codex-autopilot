from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Iterator, Sequence


SCHEMA_VERSION = 1
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
}
MAX_STATEMENT_CHARS = 8_000
MAX_FIELD_CHARS = 16_000
MAX_PAGE_SIZE = 20


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryError(RuntimeError):
    pass


class MemoryValidationError(MemoryError):
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

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
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
            current = db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if current and int(current[0]) != SCHEMA_VERSION:
                raise MemoryError(f"unsupported Project Memory schema: {current[0]}")
            db.execute(
                "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            db.execute(
                "INSERT INTO schema_meta(key,value) VALUES('project_root',?) "
                "ON CONFLICT(key) DO NOTHING",
                (str(self.root),),
            )
            stored_root = db.execute("SELECT value FROM schema_meta WHERE key='project_root'").fetchone()[0]
            if Path(stored_root).resolve() != self.root:
                raise MemoryValidationError("memory database belongs to a different project root")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=10000")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
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
            raise MemoryValidationError(f"unsupported evidence kind: {kind}")
        summary = self._required(summary, "summary")
        actor = self._required(created_by, "created_by", 256)
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
            raise MemoryValidationError(f"{kind} evidence requires a project path")
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
        with self._connect() as db:
            evidence_id = self._next_id(db, "evidence")
            db.execute(
                """INSERT INTO evidence(
                    id,kind,summary,path,line_start,line_end,content_sha256,command,result,exit_code,
                    tool_name,artifact_path,user_instruction,environment_probe,created_by,provider,
                    provider_thread_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evidence_id, kind, summary, relative_path, line_start, line_end, content_hash,
                    self._optional(command, "command"), self._optional(result, "result"), exit_code,
                    self._optional(tool_name, "tool_name", 256), normalized_artifact,
                    self._optional(user_instruction, "user_instruction"),
                    self._optional(environment_probe, "environment_probe"), actor,
                    self._optional(provider, "provider", 128),
                    self._optional(provider_thread_id, "provider_thread_id", 256), utc_now(),
                ),
            )
            if milestone_id:
                db.execute(
                    "INSERT INTO milestone_evidence(milestone_id,evidence_id,role,created_at) VALUES(?,?,?,?)",
                    (self._required(milestone_id, "milestone_id", 128), evidence_id, self._required(role, "role", 64), utc_now()),
                )
            self._audit(db, "record", "evidence", evidence_id, actor, {"kind": kind, "milestone_id": milestone_id})
        return self.get_evidence(evidence_id)

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
            return result

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
    ) -> dict[str, Any]:
        self.initialize()
        if category not in CATEGORIES:
            raise MemoryValidationError(f"unsupported category: {category}")
        statement = self._required(statement, "statement", MAX_STATEMENT_CHARS)
        if origin not in ORIGINS:
            raise MemoryValidationError(f"unsupported origin: {origin}")
        actor = self._required(created_by, "created_by", 256)
        with self._connect() as db:
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
    ) -> dict[str, Any]:
        if not evidence_ids:
            raise MemoryValidationError("NO EVIDENCE -> NO TRUTH: verified facts require evidence_ids")
        self.initialize()
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id,kind FROM evidence WHERE id IN ({','.join('?' for _ in evidence_ids)})",
                tuple(evidence_ids),
            ).fetchall()
            found = {row["id"]: row["kind"] for row in rows}
            missing = [item for item in evidence_ids if item not in found]
            if missing:
                raise MemoryValidationError(f"unknown evidence: {', '.join(missing)}")
            weak = [item for item, kind in found.items() if kind not in TRUTH_EVIDENCE_KINDS]
            if weak:
                raise MemoryValidationError(f"migration/advisory material cannot support Truth: {', '.join(weak)}")
        fact = self._create_record(
            category="truth", statement=statement, origin="project", status="verified",
            created_by=created_by, scope=scope, verification_method=verification_method,
            evidence_ids=evidence_ids, provider=provider, provider_thread_id=provider_thread_id,
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
        return self._create_record(category="decision", statement=statement, origin=origin, status=status, created_by=created_by, reason=reason, scope=scope, evidence_ids=evidence_ids, provider=provider, provider_thread_id=provider_thread_id)

    def set_decision_status(self, decision_id: str, status: str, *, actor: str, reason: str | None = None) -> dict[str, Any]:
        if status not in {"proposed", "accepted", "superseded", "rejected"}:
            raise MemoryValidationError("invalid decision status")
        return self._set_record_status(decision_id, "decision", status, actor, reason)

    def add_constraint(self, *, statement: str, origin: str, created_by: str, scope: str = "project", reason: str | None = None, evidence_ids: Sequence[str] = ()) -> dict[str, Any]:
        return self._create_record(category="constraint", statement=statement, origin=origin, status="active", created_by=created_by, reason=reason, scope=scope, evidence_ids=evidence_ids)

    def open_question(self, *, question: str, created_by: str, needed_for: str | None = None, scope: str = "project") -> dict[str, Any]:
        return self._create_record(category="question", statement=question, origin="agent", status="open", created_by=created_by, needed_for=needed_for, scope=scope)

    def resolve_question(self, question_id: str, *, actor: str, reason: str, evidence_ids: Sequence[str] = ()) -> dict[str, Any]:
        for evidence_id in evidence_ids:
            self.attach_evidence(question_id, evidence_id, relation="supports", actor=actor)
        return self._set_record_status(question_id, "question", "resolved", actor, reason)

    def _set_record_status(self, record_id: str, category: str, status: str, actor: str, reason: str | None) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT category FROM records WHERE id=?", (record_id,)).fetchone()
            if not row or row["category"] != category:
                raise MemoryValidationError(f"{record_id} is not a {category}")
            db.execute("UPDATE records SET status=?,reason=coalesce(?,reason),updated_at=? WHERE id=?", (status, self._optional(reason, "reason"), utc_now(), record_id))
            self._audit(db, "status", category, record_id, actor, {"status": status, "reason": reason})
        return self.get_record(record_id)

    def attach_evidence(self, record_id: str, evidence_id: str, *, relation: str, actor: str) -> dict[str, Any]:
        if relation not in {"supports", "contradicts"}:
            raise MemoryValidationError("relation must be supports or contradicts")
        self.initialize()
        with self._connect() as db:
            record = db.execute("SELECT category FROM records WHERE id=?", (record_id,)).fetchone()
            evidence = db.execute("SELECT kind FROM evidence WHERE id=?", (evidence_id,)).fetchone()
            if not record:
                raise MemoryValidationError(f"unknown record: {record_id}")
            if not evidence:
                raise MemoryValidationError(f"unknown evidence: {evidence_id}")
            db.execute(
                "INSERT OR IGNORE INTO record_evidence(record_id,evidence_id,relation,created_at) VALUES(?,?,?,?)",
                (record_id, evidence_id, relation, utc_now()),
            )
            self._audit(db, "attach_evidence", record["category"], record_id, actor, {"evidence_id": evidence_id, "relation": relation})
        conflict = None
        if relation == "contradicts" and record["category"] == "truth":
            conflict = self.open_conflict(existing_record_id=record_id, incoming_evidence_id=evidence_id, statement=f"Evidence {evidence_id} contradicts verified fact {record_id}.", created_by=actor)
        return {"record": self.get_record(record_id), "conflict": conflict}

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

    def list_records(self, *, categories: Sequence[str], statuses: Sequence[str] | None = None, scope: str | None = None, limit: int = 8, cursor: str | None = None) -> SearchPage:
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

    def record_count(self) -> int:
        self.initialize()
        with self._connect() as db:
            return int(db.execute("SELECT count(*) FROM records").fetchone()[0])

    def mark_milestone_complete(self, *, milestone_id: str, run_id: str, worker_sequence: int, source: str = "worker") -> dict[str, Any]:
        evidence = self.milestone_evidence(milestone_id)
        if not evidence:
            raise MemoryValidationError(f"milestone {milestone_id} has no recorded evidence")
        with self._connect() as db:
            db.execute(
                """INSERT INTO milestone_completions(milestone_id,run_id,worker_sequence,evidence_count,source,completed_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(milestone_id) DO UPDATE SET
                   run_id=excluded.run_id,worker_sequence=excluded.worker_sequence,
                   evidence_count=excluded.evidence_count,source=excluded.source,completed_at=excluded.completed_at""",
                (milestone_id, run_id, worker_sequence, len(evidence), source, utc_now()),
            )
            self._audit(db, "complete", "milestone", milestone_id, f"dispatcher:{run_id}", {"evidence_count": len(evidence), "source": source})
        self.backup()
        return {"milestone_id": milestone_id, "evidence_count": len(evidence), "source": source}

    def open_conflict(self, *, existing_record_id: str, statement: str, created_by: str, incoming_record_id: str | None = None, incoming_evidence_id: str | None = None) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            existing = db.execute("SELECT category FROM records WHERE id=?", (existing_record_id,)).fetchone()
            if not existing or existing["category"] != "truth":
                raise MemoryValidationError("conflicts must reference an existing Truth record")
            if incoming_record_id and not db.execute("SELECT 1 FROM records WHERE id=?", (incoming_record_id,)).fetchone():
                raise MemoryValidationError(f"unknown incoming record: {incoming_record_id}")
            if incoming_evidence_id and not db.execute("SELECT 1 FROM evidence WHERE id=?", (incoming_evidence_id,)).fetchone():
                raise MemoryValidationError(f"unknown incoming evidence: {incoming_evidence_id}")
            conflict_id = self._next_id(db, "conflict")
            now = utc_now()
            db.execute(
                "INSERT INTO conflicts(id,existing_record_id,incoming_record_id,incoming_evidence_id,statement,status,created_by,created_at) VALUES(?,?,?,?,?,'needs_review',?,?)",
                (conflict_id, existing_record_id, incoming_record_id, incoming_evidence_id, self._required(statement, "statement"), self._required(created_by, "created_by", 256), now),
            )
            db.execute("UPDATE records SET status='disputed',updated_at=? WHERE id=?", (now, existing_record_id))
            db.execute("INSERT INTO conflict_history(conflict_id,action,details,actor,created_at) VALUES(?,?,?,?,?)", (conflict_id, "opened", statement, created_by, now))
            self._audit(db, "open", "conflict", conflict_id, created_by, {"existing_record_id": existing_record_id})
        return self.get_conflict(conflict_id)

    def get_conflict(self, conflict_id: str) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
            if not row:
                raise MemoryValidationError(f"unknown conflict: {conflict_id}")
            result = dict(row)
            result["history"] = [dict(item) for item in db.execute("SELECT * FROM conflict_history WHERE conflict_id=? ORDER BY id", (conflict_id,)).fetchall()]
            return result

    def resolve_conflict(self, conflict_id: str, *, outcome: str, resolution: str, actor: str) -> dict[str, Any]:
        if outcome not in {"supersede_existing", "reject_incoming", "reverified_existing"}:
            raise MemoryValidationError("invalid conflict outcome")
        self.initialize()
        with self._connect() as db:
            row = db.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
            if not row or row["status"] != "needs_review":
                raise MemoryValidationError("conflict is missing or already resolved")
            now = utc_now()
            existing_status = "superseded" if outcome == "supersede_existing" else "verified"
            db.execute("UPDATE records SET status=?,updated_at=? WHERE id=?", (existing_status, now, row["existing_record_id"]))
            db.execute("UPDATE conflicts SET status='resolved',resolution=?,resolved_at=? WHERE id=?", (self._required(resolution, "resolution"), now, conflict_id))
            db.execute("INSERT INTO conflict_history(conflict_id,action,details,actor,created_at) VALUES(?,?,?,?,?)", (conflict_id, outcome, resolution, actor, now))
            self._audit(db, "resolve", "conflict", conflict_id, actor, {"outcome": outcome})
        return self.get_conflict(conflict_id)

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

    def render_views(self) -> None:
        self.initialize()
        counts: dict[str, int] = {}
        with self._connect() as db:
            for row in db.execute("SELECT category,count(*) AS count FROM records GROUP BY category"):
                counts[row["category"]] = row["count"]
            open_conflicts = int(db.execute("SELECT count(*) FROM conflicts WHERE status='needs_review'").fetchone()[0])
            completions = [dict(row) for row in db.execute("SELECT * FROM milestone_completions ORDER BY completed_at,milestone_id").fetchall()]
            decisions = [dict(row) for row in db.execute("SELECT id,statement,origin,status,reason FROM records WHERE category='decision' ORDER BY created_at,id").fetchall()]
        state_lines = [
            "# Project state (generated view)", "",
            "Canonical project knowledge is stored in `memory.sqlite3`. This file is a cache, not evidence.", "",
            "## Record counts",
            *[f"- {name}: {counts.get(name, 0)}" for name in sorted(CATEGORIES)],
            f"- open conflicts: {open_conflicts}", "", "## Verified milestone completions",
            *([f"- {item['milestone_id']}: {item['evidence_count']} evidence record(s) ({item['source']})" for item in completions] or ["- None"]),
        ]
        (self.state_dir / "PROJECT_STATE.md").write_text("\n".join(state_lines) + "\n", encoding="utf-8")
        decision_lines = [
            "# Decisions (generated view)", "",
            "Canonical decisions and provenance are stored in `memory.sqlite3`.", "",
            *([f"- {item['id']} [{item['origin']}/{item['status']}]: {item['statement']}" + (f" — {item['reason']}" if item['reason'] else "") for item in decisions] or ["- None"]),
        ]
        (self.state_dir / "DECISIONS.md").write_text("\n".join(decision_lines) + "\n", encoding="utf-8")

    def integrity_check(self) -> str:
        self.initialize()
        with self._connect() as db:
            result = db.execute("PRAGMA integrity_check").fetchone()[0]
            truths_without_evidence = db.execute(
                """SELECT count(*) FROM records r WHERE r.category='truth' AND NOT EXISTS(
                    SELECT 1 FROM record_evidence re JOIN evidence e ON e.id=re.evidence_id
                    WHERE re.record_id=r.id AND re.relation='supports' AND e.kind!='migration')"""
            ).fetchone()[0]
            if result != "ok" or truths_without_evidence:
                raise MemoryError(f"memory integrity failure: sqlite={result}, truths_without_evidence={truths_without_evidence}")
            return "ok"

    def backup(self, destination: Path | None = None) -> Path:
        self.initialize()
        target = (destination or self.state_dir / "memory-backups" / "latest.sqlite3").expanduser().resolve()
        if not self._is_within(target, self.root):
            raise MemoryValidationError("memory backup must remain inside the target project")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
        temporary.unlink(missing_ok=True)
        source_db = sqlite3.connect(self.path, timeout=10)
        destination_db = sqlite3.connect(temporary)
        try:
            source_db.backup(destination_db)
            if destination_db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MemoryError("new Project Memory backup failed integrity_check")
            destination_db.commit()
        finally:
            destination_db.close()
            source_db.close()
        os.replace(temporary, target)
        return target

    def recover_latest(self) -> Path:
        backup = self.state_dir / "memory-backups" / "latest.sqlite3"
        if not backup.is_file():
            raise MemoryError("Project Memory is corrupt and no verified milestone backup exists")
        check = sqlite3.connect(backup)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MemoryError("latest Project Memory backup also failed integrity_check")
        finally:
            check.close()
        quarantine = self.state_dir / f"memory-corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.sqlite3"
        if self.path.exists():
            shutil.move(self.path, quarantine)
        self.path.with_name(self.path.name + "-wal").unlink(missing_ok=True)
        self.path.with_name(self.path.name + "-shm").unlink(missing_ok=True)
        shutil.copy2(backup, self.path)
        self.integrity_check()
        return quarantine

    def ensure_healthy(self, *, recover: bool = True) -> str:
        try:
            return self.integrity_check()
        except (sqlite3.DatabaseError, MemoryError, OSError):
            if not recover:
                raise
            self.recover_latest()
            return "recovered"

    def export_summary(self) -> dict[str, Any]:
        self.initialize()
        with self._connect() as db:
            counts = {row["category"]: row["count"] for row in db.execute("SELECT category,count(*) count FROM records GROUP BY category")}
            return {
                "schema_version": SCHEMA_VERSION,
                "database": str(self.path),
                "project_root": str(self.root),
                "records": counts,
                "evidence": int(db.execute("SELECT count(*) FROM evidence").fetchone()[0]),
                "open_conflicts": int(db.execute("SELECT count(*) FROM conflicts WHERE status='needs_review'").fetchone()[0]),
                "audit_highwater": int(db.execute("SELECT coalesce(max(id),0) FROM audit_log").fetchone()[0]),
            }
