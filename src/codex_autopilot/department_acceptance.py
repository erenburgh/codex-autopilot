from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import re
import threading
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .memory import MemoryValidationError, ProjectMemory


RUBRIC_SCHEMA_VERSION = 1
RUBRIC_SCOPE_PREFIX = "department-acceptance-rubric"
DEPARTMENT_BINDING_RESOURCE_ID = "department-binding"
RUBRIC_BINDING_RESOURCE_ID = "rubric-binding"
DEPARTMENT_TARGET_PREFIX = "department-id:"
PROJECT_MEMORY_TARGET_PREFIX = "project-memory:"
OUTCOME_EVIDENCE_KINDS = frozenset(
    {"artifact", "build", "environment_probe", "screenshot", "test", "tool"}
)
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FACT_REFERENCE = re.compile(r"\bFACT-[0-9]+\b")
_SHA256_REFERENCE = re.compile(r"\b[0-9a-fA-F]{64}\b")
_RUBRIC_GUIDANCE = re.compile(
    r"rubric|рубри|record_id|sha256|project\s+memory\s+record",
    re.IGNORECASE,
)


class DepartmentAcceptanceError(ValueError):
    """A department, lead, rubric, or verifier attestation failed closed."""


# The nested field sets a plan may carry, named once so the prompt that
# states them and the parser that enforces them cannot drift. They were only
# enforced, never stated, and a real run lost its whole replan budget writing
# `lead_role` for `lead_role_id`.
#
# A department is derived by the runtime from the plan's leads
# (``department_runtime``); a planner declares one only to name it. `rubric`
# is accepted on an entry and kept verbatim - a 0.13 plan carried a pinned
# tuple there, and dropping it on load would change plan_to_dict, the plan
# digest, and with it the PLAN_VERIFIED receipt of a running run - but it is
# never the source of the pin: the department's current version in Project
# Memory is.
RUBRIC_REFERENCE_FIELDS = ("record_id", "version", "sha256")
DEPARTMENT_REQUIRED_FIELDS = ("id", "name", "lead_role_id")
DEPARTMENT_FIELDS = (*DEPARTMENT_REQUIRED_FIELDS, "rubric")
# The runtime's own writer of version 1. Project Memory refuses this tool
# name from a model (``memory_mcp``), so evidence carrying it is the
# runtime's, and it is never outcome evidence for a later version.
RUNTIME_RUBRIC_TOOL = "codex-autopilot/department-rubric"
RUNTIME_RUBRIC_AUTHOR = "codex-autopilot-runtime"
RESERVED_TOOL_PREFIX = "codex-autopilot/"


@dataclass(frozen=True, slots=True)
class RubricReference:
    record_id: str
    version: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "version": self.version,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class RubricCriterion:
    id: str
    requirement: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "requirement": self.requirement}


@dataclass(frozen=True, slots=True)
class DepartmentRubric:
    department_id: str
    version: int
    criteria: tuple[RubricCriterion, ...]
    standards: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RUBRIC_SCHEMA_VERSION,
            "department_id": self.department_id,
            "version": self.version,
            "criteria": [item.to_dict() for item in self.criteria],
            "standards": list(self.standards),
        }


@dataclass(frozen=True, slots=True)
class DepartmentContract:
    id: str
    name: str
    lead_role_id: str
    rubric: RubricReference | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "lead_role_id": self.lead_role_id,
            **({"rubric": self.rubric.to_dict()} if self.rubric is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class DepartmentDefinition:
    id: str
    name: str
    lead_role_id: str


@dataclass(frozen=True, slots=True)
class LoadedDepartmentRubric:
    reference: RubricReference
    rubric: DepartmentRubric

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference.to_dict(),
            "content": self.rubric.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class TaskDepartmentBinding:
    department_id: str
    rubric_scope: str

    def to_dict(self) -> dict[str, str]:
        return {
            "department_resource_id": DEPARTMENT_BINDING_RESOURCE_ID,
            "department_id": self.department_id,
            "rubric_resource_id": RUBRIC_BINDING_RESOURCE_ID,
            "rubric_scope": self.rubric_scope,
        }


@dataclass(frozen=True, slots=True)
class LoadedDepartmentAcceptance:
    """The department a task belongs to and the rubric its lead judges by.

    It used to carry a ``source``: the rubric tuple read from the evidence of
    a VERIFIED dependency. That made the first task of a department (M01, no
    dependencies) impossible to bind at all; the pin is now the department's
    current version in Project Memory (``department_runtime``).
    """

    department: DepartmentContract
    rubric: LoadedDepartmentRubric
    lead_profile_changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "department": self.department.to_dict(),
            "rubric": self.rubric.to_dict(),
            **(
                {
                    "lead_profile_changed_since_v1": True,
                    "note": (
                        "The Lead Role's profile changed after version 1 was written. "
                        "The rubric is not rewritten from the new profile: a new version "
                        "goes through outcome evidence (R30)."
                    ),
                }
                if self.lead_profile_changed
                else {}
            ),
        }


def rubric_reference_from_raw(raw: object, label: str = "rubric") -> RubricReference:
    data = _mapping(raw, label)
    _exact_keys(data, set(RUBRIC_REFERENCE_FIELDS), label)
    digest = _required_text(data.get("sha256"), f"{label}.sha256", 64)
    if not _SHA256.fullmatch(digest):
        raise DepartmentAcceptanceError(f"{label}.sha256 must be a lowercase SHA-256 digest")
    return RubricReference(
        record_id=_required_text(data.get("record_id"), f"{label}.record_id", 128),
        version=_positive_int(data.get("version"), f"{label}.version"),
        sha256=digest,
    )


def department_contract_from_raw(raw: object, label: str) -> DepartmentContract:
    data = _mapping(raw, label)
    # `rubric` is optional and kept verbatim: see DEPARTMENT_FIELDS.
    _exact_keys(
        data,
        set(DEPARTMENT_FIELDS),
        label,
        required=set(DEPARTMENT_REQUIRED_FIELDS),
    )
    return DepartmentContract(
        id=_identifier(data.get("id"), f"{label}.id"),
        name=_required_text(data.get("name"), f"{label}.name", 256),
        lead_role_id=_identifier(data.get("lead_role_id"), f"{label}.lead_role_id"),
        rubric=(
            rubric_reference_from_raw(data.get("rubric"), f"{label}.rubric")
            if "rubric" in data
            else None
        ),
    )


def department_contract_issues(
    departments: Sequence[DepartmentContract],
    *,
    role_ids: Iterable[str],
) -> list[str]:
    """Every duplicate id and every unknown lead, not just the first."""

    found: list[str] = []
    ids = [item.id for item in departments]
    duplicate_ids = sorted({item for item in ids if ids.count(item) > 1})
    if duplicate_ids:
        found.append(f"department mapping is ambiguous; duplicate department ids: {duplicate_ids}")
    leads = [item.lead_role_id for item in departments]
    shared = sorted({item for item in leads if leads.count(item) > 1})
    if shared:
        # One lead, one department, one rubric (R30): two entries naming the
        # same lead would give its tasks two rubrics.
        found.append(f"department mapping is ambiguous; one Lead Role heads several departments: {shared}")
    known_roles = set(role_ids)
    for department in departments:
        if department.lead_role_id not in known_roles:
            found.append(
                f"department {department.id!r} references unknown Lead Role "
                f"{department.lead_role_id!r}"
            )
    return found


def task_department_binding(task: object) -> TaskDepartmentBinding | None:
    """The 0.13 department claim a task may still carry in its resources.

    It was the only way into R30, and no planner ever wrote it: the rule was
    in force for nobody. The department is now derived from the plan's leads
    (``department_runtime.derive_task_department``); a binding that is
    present is a consistency claim checked against that derivation, never a
    second source. Tags are not consulted. The two resource claims form one
    binding and fail closed when only one is present or either has the wrong
    kind, access mode, or target.
    """

    resources = tuple(getattr(task, "resources", ()) or ())
    department_claims = tuple(
        item
        for item in resources
        if getattr(item, "id", None) == DEPARTMENT_BINDING_RESOURCE_ID
    )
    rubric_claims = tuple(
        item
        for item in resources
        if getattr(item, "id", None) == RUBRIC_BINDING_RESOURCE_ID
    )
    if not department_claims and not rubric_claims:
        return None
    if len(department_claims) != 1 or len(rubric_claims) != 1:
        raise DepartmentAcceptanceError(
            "department acceptance requires exactly one department-binding and "
            "one rubric-binding logical resource"
        )
    department_claim = department_claims[0]
    rubric_claim = rubric_claims[0]
    _require_logical_read_claim(department_claim, DEPARTMENT_BINDING_RESOURCE_ID)
    _require_logical_read_claim(rubric_claim, RUBRIC_BINDING_RESOURCE_ID)
    department_target = str(getattr(department_claim, "target", ""))
    if not department_target.startswith(DEPARTMENT_TARGET_PREFIX):
        raise DepartmentAcceptanceError(
            "department-binding target must use department-id:<department_id>"
        )
    department_id = _identifier(
        department_target[len(DEPARTMENT_TARGET_PREFIX) :],
        "department-binding department_id",
    )
    rubric_target = str(getattr(rubric_claim, "target", ""))
    if not rubric_target.startswith(PROJECT_MEMORY_TARGET_PREFIX):
        raise DepartmentAcceptanceError(
            "rubric-binding target must use project-memory:<scope>"
        )
    bound_scope = _required_text(
        rubric_target[len(PROJECT_MEMORY_TARGET_PREFIX) :],
        "rubric-binding scope",
        256,
    )
    expected_scope = rubric_scope(department_id)
    if bound_scope != expected_scope:
        raise DepartmentAcceptanceError(
            "rubric-binding scope conflicts with department-binding: "
            f"expected {expected_scope!r}, observed {bound_scope!r}"
        )
    return TaskDepartmentBinding(
        department_id=department_id,
        rubric_scope=bound_scope,
    )


def rubric_scope(department_id: str) -> str:
    return f"{RUBRIC_SCOPE_PREFIX}:{_identifier(department_id, 'department_id')}"


def rubric_digest(rubric: DepartmentRubric) -> str:
    return hashlib.sha256(_rubric_json(rubric).encode("utf-8")).hexdigest()


def store_department_rubric(
    memory: ProjectMemory,
    *,
    department_id: str,
    version: int,
    criteria: Sequence[Mapping[str, object] | RubricCriterion],
    standards: Sequence[str] = (),
    evidence_ids: Sequence[str],
    created_by: str,
    caller_thread_id: str = "",
) -> RubricReference:
    """Persist a later version of a department's rubric - never its first.

    Version 1 is the runtime's (``write_runtime_rubric``), derived from the
    Lead Role's profile before the department's first acceptance. A model
    used to write it, with any evidence, into a scope nobody reserved: the
    worker being judged could have written the rubric it is judged by, and a
    stray second v1 made the department "ambiguous" for good.

    A repeated identical write is idempotent. A new version advances by
    exactly one and cites outcome evidence: evidence an acceptance of this
    department rests on, as the runtime recorded it (``_require_outcome_evidence``) - an observation
    is not evidence and cannot change a department standard by itself (R30).
    Who may propose it (the department's lead or the on-call, never a worker
    of its tasks) is decided before this call, from the caller's thread
    (``department_runtime.authorize_rubric_proposal``).

    The check and the write run under one lock (``_rubric_write_lock``):
    they were two connections apart, and two writers could both see "no
    version 2" and both write it.
    """

    rubric = DepartmentRubric(
        department_id=_identifier(department_id, "department_id"),
        version=_positive_int(version, "version"),
        criteria=_criteria(criteria),
        standards=_strings(standards, "standards"),
    )
    digest = rubric_digest(rubric)
    with _rubric_write_lock(memory):
        existing = stored_rubric_versions(memory, rubric.department_id)
        same_version = [item for item in existing if item.rubric.version == rubric.version]
        if same_version:
            if same_version[0].reference.sha256 != digest:
                raise DepartmentAcceptanceError(
                    f"rubric version {rubric.version} is immutable and already has a different digest"
                )
            return same_version[0].reference
        if not existing:
            raise DepartmentAcceptanceError(
                f"version 1 of department {rubric.department_id!r} is written by the runtime "
                "from its Lead Role's profile before the first acceptance; a model proposes "
                "only a later version, with outcome evidence"
            )
        latest = existing[-1].rubric.version
        if rubric.version != latest + 1:
            raise DepartmentAcceptanceError(
                f"rubric version must advance exactly once ({latest} -> {latest + 1})"
            )
        _require_outcome_evidence(
            memory, evidence_ids, rubric.department_id, caller_thread_id=caller_thread_id
        )
        return _insert_rubric(memory, rubric, evidence_ids, created_by)


def write_runtime_rubric(
    memory: ProjectMemory,
    rubric: DepartmentRubric,
    *,
    evidence_result: Mapping[str, object],
) -> RubricReference:
    """Version 1, written by the runtime; an existing history is returned as is.

    Evidence is the runtime's own tool record (``RUNTIME_RUBRIC_TOOL``, a
    name Project Memory refuses from a model): what the rubric was derived
    from - the department, its lead and the lead's profile digest - so a
    later change of that profile is detectable without rewriting the rubric.
    """

    if rubric.version != 1:
        raise DepartmentAcceptanceError("the runtime writes only version 1 of a rubric")
    with _rubric_write_lock(memory):
        existing = stored_rubric_versions(memory, rubric.department_id)
        if existing:
            return existing[-1].reference
        try:
            evidence = memory.record_evidence(
                kind="tool",
                summary=(
                    f"Runtime derived version 1 of the {rubric.department_id} department "
                    "rubric from its Lead Role profile (R30)."
                ),
                tool_name=RUNTIME_RUBRIC_TOOL,
                result=json.dumps(dict(evidence_result), ensure_ascii=False, sort_keys=True),
                exit_code=0,
                created_by=RUNTIME_RUBRIC_AUTHOR,
            )
        except MemoryValidationError as exc:
            raise DepartmentAcceptanceError(str(exc)) from exc
        return _insert_rubric(memory, rubric, [str(evidence["id"])], RUNTIME_RUBRIC_AUTHOR)


def _insert_rubric(
    memory: ProjectMemory,
    rubric: DepartmentRubric,
    evidence_ids: Sequence[str],
    created_by: str,
) -> RubricReference:
    if not evidence_ids:
        raise DepartmentAcceptanceError(
            "NO EVIDENCE -> NO TRUTH: a department rubric requires evidence_ids"
        )
    try:
        record = memory.record_verified_fact(
            statement=_rubric_json(rubric),
            evidence_ids=evidence_ids,
            verification_method="department rubric outcome review",
            created_by=created_by,
            scope=rubric_scope(rubric.department_id),
            reserved_scope=True,
        )
    except MemoryValidationError as exc:
        raise DepartmentAcceptanceError(str(exc)) from exc
    return RubricReference(
        record_id=str(record["id"]),
        version=rubric.version,
        sha256=rubric_digest(rubric),
    )


_RUBRIC_LOCKS_GUARD = threading.Lock()
_RUBRIC_LOCKS: dict[str, threading.RLock] = {}


@contextmanager
def _rubric_write_lock(memory: ProjectMemory) -> Iterator[None]:
    """One writer of a department's rubric history at a time, across processes.

    A file of its own beside Project Memory's lock, not that lock: the check
    reads records and the write inserts them through Project Memory, whose
    own connections take its lock - held around them, a second flock on
    the same file from this process would wait on itself. Only rubric
    writers take this one, and the scope is reserved for them (``memory``
    refuses it from any other door), so check-then-write is atomic for
    everyone who can write there.
    """

    path = memory.state_dir / "department-rubric.lock"
    with _RUBRIC_LOCKS_GUARD:
        local = _RUBRIC_LOCKS.setdefault(str(path), threading.RLock())
    with local:
        memory.state_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def load_department_rubric(
    memory: ProjectMemory,
    department: DepartmentContract,
) -> LoadedDepartmentRubric:
    """Load and attest the exact pinned rubric before a verifier can launch."""

    reference = department.rubric
    try:
        record = memory.get_record(reference.record_id)
    except MemoryValidationError as exc:
        raise DepartmentAcceptanceError(
            f"department {department.id!r} rubric record {reference.record_id!r} is missing"
        ) from exc
    expected_scope = rubric_scope(department.id)
    if (
        record.get("category") != "truth"
        or record.get("status") != "verified"
        or record.get("scope") != expected_scope
    ):
        raise DepartmentAcceptanceError(
            f"rubric record {reference.record_id!r} is not verified department memory "
            f"in scope {expected_scope!r}"
        )
    rubric = department_rubric_from_raw(_parse_statement(record), "Project Memory rubric")
    actual_digest = rubric_digest(rubric)
    if rubric.department_id != department.id:
        raise DepartmentAcceptanceError(
            f"rubric record {reference.record_id!r} belongs to department "
            f"{rubric.department_id!r}, not {department.id!r}"
        )
    if rubric.version != reference.version:
        raise DepartmentAcceptanceError(
            f"rubric version changed: expected {reference.version}, observed {rubric.version}"
        )
    if actual_digest != reference.sha256:
        raise DepartmentAcceptanceError(
            f"rubric digest changed: expected {reference.sha256}, observed {actual_digest}"
        )
    return LoadedDepartmentRubric(reference=reference, rubric=rubric)


def require_rubric_attestation(
    expected: RubricReference,
    attested: RubricReference | None,
) -> None:
    if attested is None:
        raise DepartmentAcceptanceError(
            "department verifier verdict must attest the pinned rubric"
        )
    if attested != expected:
        raise DepartmentAcceptanceError(
            "department verifier verdict attested a different rubric: "
            f"expected {expected.to_dict()}, observed {attested.to_dict()}"
        )


def rubric_guidance_conflicts(text: str, expected: RubricReference) -> bool:
    """Return whether role/DoD prose names a superseded rubric identity.

    Department acceptance receives its authoritative identity from the
    department's current version in Project Memory.  Older plans may still
    carry a bootstrap record or digest in prose-oriented RoleProfile and DoD
    fields.  Those
    fields must not compete with the loaded structured contract in a verifier
    prompt.
    """

    if not _RUBRIC_GUIDANCE.search(text):
        return False
    record_ids = _FACT_REFERENCE.findall(text)
    digests = [item.lower() for item in _SHA256_REFERENCE.findall(text)]
    return any(item != expected.record_id for item in record_ids) or any(
        item != expected.sha256 for item in digests
    )


def omit_conflicting_rubric_guidance(
    values: Sequence[str],
    expected: RubricReference,
) -> tuple[str, ...]:
    """Drop obsolete rubric-bearing role entries before verifier launch."""

    return tuple(
        value for value in values if not rubric_guidance_conflicts(value, expected)
    )


def redact_conflicting_rubric_identity(
    text: str,
    expected: RubricReference,
) -> str:
    """Preserve a DoD requirement while removing superseded identity data."""

    if not rubric_guidance_conflicts(text, expected):
        return text
    redacted = _FACT_REFERENCE.sub(
        lambda match: (
            match.group(0)
            if match.group(0) == expected.record_id
            else "<superseded-department-rubric-record>"
        ),
        text,
    )
    return _SHA256_REFERENCE.sub(
        lambda match: (
            match.group(0)
            if match.group(0).lower() == expected.sha256
            else "<superseded-department-rubric-sha256>"
        ),
        redacted,
    )


def department_rubric_from_raw(raw: object, label: str = "rubric") -> DepartmentRubric:
    data = _mapping(raw, label)
    _exact_keys(
        data,
        {"schema_version", "department_id", "version", "criteria", "standards"},
        label,
    )
    if data.get("schema_version") != RUBRIC_SCHEMA_VERSION:
        raise DepartmentAcceptanceError(
            f"{label}.schema_version must be {RUBRIC_SCHEMA_VERSION}"
        )
    raw_criteria = data.get("criteria")
    if not isinstance(raw_criteria, list):
        raise DepartmentAcceptanceError(f"{label}.criteria must be an array")
    raw_standards = data.get("standards")
    if not isinstance(raw_standards, list):
        raise DepartmentAcceptanceError(f"{label}.standards must be an array")
    return DepartmentRubric(
        department_id=_identifier(data.get("department_id"), f"{label}.department_id"),
        version=_positive_int(data.get("version"), f"{label}.version"),
        criteria=_criteria(raw_criteria),
        standards=_strings(raw_standards, f"{label}.standards"),
    )


def stored_rubric_versions(
    memory: ProjectMemory,
    department_id: str,
) -> tuple[LoadedDepartmentRubric, ...]:
    """A department's verified rubric history, oldest first: exactly 1..n.

    A gap, a repeated version or a record that is not a rubric of this
    department is refused with the record ids, never guessed around: one of
    them would silently become the standard. The repair is the on-call's -
    it supersedes the stray record (``department_gate.supersede_rubric_record``,
    audited in Project Memory) and the history reads clean again.
    """

    loaded: list[LoadedDepartmentRubric] = []
    for record in _verified_rubric_records(memory, department_id):
        try:
            rubric = department_rubric_from_raw(_parse_statement(record), "Project Memory rubric")
        except DepartmentAcceptanceError as exc:
            raise DepartmentAcceptanceError(
                f"record {record['id']} in the rubric scope of {department_id!r} is not a "
                f"rubric ({exc}); the on-call supersedes it"
            ) from exc
        if rubric.department_id != department_id:
            raise DepartmentAcceptanceError(
                f"record {record['id']} in the rubric scope of {department_id!r} belongs to "
                f"department {rubric.department_id!r}; the on-call supersedes it"
            )
        reference = RubricReference(
            record_id=str(record["id"]),
            version=rubric.version,
            sha256=rubric_digest(rubric),
        )
        loaded.append(LoadedDepartmentRubric(reference=reference, rubric=rubric))
    loaded.sort(key=lambda item: (item.rubric.version, item.reference.record_id))
    versions = [item.rubric.version for item in loaded]
    if versions != list(range(1, len(versions) + 1)):
        raise DepartmentAcceptanceError(
            f"the rubric history of department {department_id!r} is ambiguous: versions "
            f"{versions} in records {[item.reference.record_id for item in loaded]}; "
            "it must be exactly 1..n - the on-call supersedes the stray record"
        )
    return tuple(loaded)


def _verified_rubric_records(memory: ProjectMemory, department_id: str) -> list[dict[str, Any]]:
    """Every verified record in the department's rubric scope, in the order written."""

    records: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = memory.list_records(
            categories=["truth"], statuses=["verified"], scope=rubric_scope(department_id),
            limit=20, cursor=cursor,
        )
        records.extend(memory.get_record(str(item["id"])) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return sorted(records, key=lambda item: (str(item.get("created_at") or ""), _ordinal(item["id"])))


def stray_rubric_records(memory: ProjectMemory, department_id: str) -> tuple[str, ...]:
    """The records an ambiguous history is repaired by retiring, oldest first.

    The history that stands is canonical: for version 1, 2, ... the FIRST
    verified record of that version, up to the first version missing. That
    is what earlier leads attested, whoever wrote it. Everything else in the
    scope is stray - a second record of a version, a version past a gap, a
    record that is not a rubric of this department. The on-call's repair
    used to refuse any record the runtime had written, and the measured
    failure was exactly two runtime v1s: nothing could be retired and the
    department stayed stopped for her.
    """

    by_version: dict[int, str] = {}
    strays: list[str] = []
    for record in _verified_rubric_records(memory, department_id):
        try:
            rubric = department_rubric_from_raw(_parse_statement(record), "Project Memory rubric")
        except DepartmentAcceptanceError:
            strays.append(str(record["id"]))
            continue
        if rubric.department_id != department_id or rubric.version in by_version:
            strays.append(str(record["id"]))
        else:
            by_version[rubric.version] = str(record["id"])
    version = 1
    while version in by_version:
        version += 1
    strays.extend(record_id for number, record_id in by_version.items() if number > version)
    return tuple(strays)


def _ordinal(record_id: object) -> int:
    digits = re.sub(r"[^0-9]", "", str(record_id))
    return int(digits) if digits else 0


def _require_outcome_evidence(
    memory: ProjectMemory,
    evidence_ids: Sequence[str],
    department_id: str,
    *,
    caller_thread_id: str = "",
) -> None:
    """Evidence of an outcome of this department's judging, not a claim.

    The kind alone used to decide: any `tool` record passed, including one
    a model wrote a minute before about nothing, and the runtime's own
    version-1 record. The next version asked for a verification result
    naming the department - and the independent check forged one in two MCP
    calls: evidence with provider_thread_id "someone-else", a result whose
    details named the department. Both fields are the model's to write.

    Now the outcome is what the RUNTIME recorded: a fresh-verifier result it
    attested at completion (``_record_runtime_verification_result``; the
    attestation key is refused from any other writer), of this department,
    judged in a thread other than the proposer's. And no item may be the
    runtime's rubric record or one the proposing thread wrote itself - by
    Project Memory's audit of the server that wrote it
    (``evidence_writer_thread``), not by a field in the record.
    """

    if not evidence_ids:
        raise DepartmentAcceptanceError(
            "a rubric change requires outcome evidence; one observation cannot change it"
        )
    caller = str(caller_thread_id or "").strip()
    outcome = False
    for evidence_id in evidence_ids:
        try:
            evidence = memory.get_evidence(evidence_id)
        except MemoryValidationError as exc:
            raise DepartmentAcceptanceError(str(exc)) from exc
        if str(evidence.get("tool_name") or "").startswith(RESERVED_TOOL_PREFIX):
            raise DepartmentAcceptanceError(
                f"{evidence_id} is the runtime's own record, not an outcome"
            )
        if caller and memory.evidence_writer_thread(evidence_id) == caller:
            raise DepartmentAcceptanceError(
                f"{evidence_id} was written by the proposing thread itself; outcome "
                "evidence comes from the department's recorded acceptances"
            )
        if str(evidence.get("kind") or "") not in OUTCOME_EVIDENCE_KINDS:
            continue
        for verification_id in evidence.get("verification_results") or ():
            try:
                result = memory.get_verification_result(str(verification_id))
            except MemoryValidationError:
                continue
            if _runtime_acceptance_of(result, department_id) and (
                not caller or str(result.get("provider_thread_id") or "") != caller
            ):
                outcome = True
    if not outcome:
        raise DepartmentAcceptanceError(
            "a rubric change requires outcome evidence: at least one item an acceptance of "
            f"department {department_id!r} rested on, as the runtime recorded it from another "
            "lead's thread (kinds: " + ", ".join(sorted(OUTCOME_EVIDENCE_KINDS)) + ")"
        )


def _runtime_acceptance_of(result: Mapping[str, Any], department_id: str) -> bool:
    from .memory_verification import RUNTIME_ATTESTATION_KEY

    details = result.get("details") or {}
    attestation = details.get(RUNTIME_ATTESTATION_KEY) or {}
    department = (details.get("department_acceptance") or {}).get("department") or {}
    return (
        attestation.get("authority_kind") == "fresh_verifier"
        and attestation.get("provider_thread_id") == result.get("provider_thread_id")
        and department.get("id") == department_id
    )


def _rubric_json(rubric: DepartmentRubric) -> str:
    return json.dumps(
        rubric.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse_statement(record: Mapping[str, object]) -> object:
    try:
        return json.loads(str(record.get("statement") or ""))
    except json.JSONDecodeError as exc:
        raise DepartmentAcceptanceError(
            f"rubric record {record.get('id')!r} does not contain canonical JSON"
        ) from exc


def _require_logical_read_claim(resource: object, label: str) -> None:
    if getattr(resource, "kind", None) != "logical":
        raise DepartmentAcceptanceError(f"{label} must be a logical resource")
    if getattr(resource, "access", None) != "read":
        raise DepartmentAcceptanceError(f"{label} must have read access")


def _department_name(department_id: str, lead_role_name: str) -> str:
    suffix = " Lead"
    if lead_role_name.endswith(suffix) and len(lead_role_name) > len(suffix):
        return lead_role_name[: -len(suffix)]
    return " ".join(part.capitalize() for part in department_id.split("-"))


def _criteria(
    values: Sequence[Mapping[str, object] | RubricCriterion],
) -> tuple[RubricCriterion, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or not values:
        raise DepartmentAcceptanceError("criteria must be a non-empty array")
    result: list[RubricCriterion] = []
    for index, value in enumerate(values, 1):
        if isinstance(value, RubricCriterion):
            criterion = value
        else:
            data = _mapping(value, f"criteria[{index}]")
            _exact_keys(data, {"id", "requirement"}, f"criteria[{index}]")
            criterion = RubricCriterion(
                id=_identifier(data.get("id"), f"criteria[{index}].id"),
                requirement=_required_text(
                    data.get("requirement"), f"criteria[{index}].requirement", 2_000
                ),
            )
        result.append(criterion)
    ids = [item.id for item in result]
    if len(ids) != len(set(ids)):
        raise DepartmentAcceptanceError("rubric criterion ids must be unique")
    return tuple(result)


def _strings(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise DepartmentAcceptanceError(f"{label} must be an array")
    return tuple(_required_text(value, f"{label}[{index}]", 2_000) for index, value in enumerate(values, 1))


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DepartmentAcceptanceError(f"{label} must be an object")
    return value


def _exact_keys(
    data: Mapping[str, object],
    allowed: set[str],
    label: str,
    *,
    required: set[str] | None = None,
) -> None:
    unknown = sorted(set(data) - allowed)
    missing = sorted((allowed if required is None else required) - set(data))
    if unknown:
        # R31: a refusal names what IS accepted. Naming only the rejected key
        # cost a real run its whole replan budget - the replanner wrote
        # `lead_role`, the field is `lead_role_id`, and nothing it could read
        # said so. And the missing ones in the same line: `lead_role` for
        # `lead_role_id` is one unknown and one missing, and the refusal
        # used to name only the first, leaving the second for a later round.
        raise DepartmentAcceptanceError(
            f"{label} has unknown fields: {unknown}; accepted fields are "
            f"{sorted(allowed)}"
            + (f"; missing required fields: {missing}" if missing else "")
        )
    if missing:
        raise DepartmentAcceptanceError(f"{label} is missing required fields: {missing}")


def _identifier(value: object, label: str) -> str:
    text = _required_text(value, label, 64)
    if not _IDENTIFIER.fullmatch(text):
        raise DepartmentAcceptanceError(
            f"{label} must match {_IDENTIFIER.pattern}"
        )
    return text


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DepartmentAcceptanceError(f"{label} must be a positive integer")
    return value


def _required_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise DepartmentAcceptanceError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise DepartmentAcceptanceError(f"{label} must be non-empty")
    if len(text) > maximum:
        raise DepartmentAcceptanceError(f"{label} exceeds {maximum} characters")
    return text
