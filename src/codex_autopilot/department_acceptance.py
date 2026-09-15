from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

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
    rubric: RubricReference

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "lead_role_id": self.lead_role_id,
            "rubric": self.rubric.to_dict(),
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
class DependencyRubricReference:
    dependency_task_id: str
    output_id: str
    evidence_id: str
    department_id: str
    scope: str
    reference: RubricReference

    def to_dict(self) -> dict[str, Any]:
        return {
            "dependency_task_id": self.dependency_task_id,
            "output_id": self.output_id,
            "evidence_id": self.evidence_id,
            "department_id": self.department_id,
            "scope": self.scope,
            "reference": self.reference.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LoadedDepartmentAcceptance:
    binding: TaskDepartmentBinding
    department: DepartmentContract
    rubric: LoadedDepartmentRubric
    source: DependencyRubricReference

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding.to_dict(),
            "department": self.department.to_dict(),
            "rubric": self.rubric.to_dict(),
            "source": self.source.to_dict(),
        }


def rubric_reference_from_raw(raw: object, label: str = "rubric") -> RubricReference:
    data = _mapping(raw, label)
    _exact_keys(data, {"record_id", "version", "sha256"}, label)
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
    _exact_keys(data, {"id", "name", "lead_role_id", "rubric"}, label)
    return DepartmentContract(
        id=_identifier(data.get("id"), f"{label}.id"),
        name=_required_text(data.get("name"), f"{label}.name", 256),
        lead_role_id=_identifier(data.get("lead_role_id"), f"{label}.lead_role_id"),
        rubric=rubric_reference_from_raw(data.get("rubric"), f"{label}.rubric"),
    )


def validate_department_contracts(
    departments: Sequence[DepartmentContract],
    *,
    role_ids: Iterable[str],
) -> None:
    ids = [item.id for item in departments]
    duplicate_ids = sorted({item for item in ids if ids.count(item) > 1})
    if duplicate_ids:
        raise DepartmentAcceptanceError(
            f"department mapping is ambiguous; duplicate department ids: {duplicate_ids}"
        )
    known_roles = set(role_ids)
    for department in departments:
        if department.lead_role_id not in known_roles:
            raise DepartmentAcceptanceError(
                f"department {department.id!r} references unknown Lead Role "
                f"{department.lead_role_id!r}"
            )


def resolve_department(
    departments: Sequence[DepartmentContract],
    department_id: str | None,
) -> DepartmentContract:
    if not department_id:
        raise DepartmentAcceptanceError(
            "department acceptance requires an explicit department binding; "
            "missing mappings are rejected"
        )
    matches = [item for item in departments if item.id == department_id]
    if not matches:
        raise DepartmentAcceptanceError(
            f"task references unknown department {department_id!r}"
        )
    if len(matches) != 1:
        raise DepartmentAcceptanceError(
            f"department mapping for {department_id!r} is ambiguous"
        )
    return matches[0]


def resolve_task_department(
    departments: Sequence[DepartmentContract],
    task: object,
    *,
    role_names: Mapping[str, str],
) -> DepartmentDefinition | None:
    """Resolve a task's Lead Role from its department, never from tags.

    A plan with an explicit department registry must contain exactly one
    matching entry.  Bootstrap plans created before the registry field existed
    use the stable ``<department-id>-lead`` RoleProfile convention; that
    convention is accepted only when the registry is empty and the resulting
    role exists.  A task-level verifier_role may attest the same mapping but
    cannot override it.
    """

    binding = task_department_binding(task)
    if binding is None:
        return None
    declared_verifier = getattr(
        getattr(task, "verification", None),
        "verifier_role",
        None,
    )
    if departments:
        contract = resolve_department(departments, binding.department_id)
        definition = DepartmentDefinition(
            id=contract.id,
            name=contract.name,
            lead_role_id=contract.lead_role_id,
        )
    else:
        lead_role_id = f"{binding.department_id}-lead"
        role_name = role_names.get(lead_role_id)
        if role_name is None:
            raise DepartmentAcceptanceError(
                f"department {binding.department_id!r} derives missing Lead Role "
                f"{lead_role_id!r}"
            )
        definition = DepartmentDefinition(
            id=binding.department_id,
            name=_department_name(binding.department_id, role_name),
            lead_role_id=lead_role_id,
        )
    if declared_verifier is not None and declared_verifier != definition.lead_role_id:
        raise DepartmentAcceptanceError(
            f"task verifier role {declared_verifier!r} conflicts with department "
            f"{definition.id!r} Lead Role {definition.lead_role_id!r}"
        )
    if definition.lead_role_id not in role_names:
        raise DepartmentAcceptanceError(
            f"department {definition.id!r} references unknown Lead Role "
            f"{definition.lead_role_id!r}"
        )
    return definition


def task_department_binding(task: object) -> TaskDepartmentBinding | None:
    """Resolve the task's R30 binding only from its logical resources.

    Tags are deliberately not consulted: they are descriptive labels, not a
    task-level authority contract.  The two resource claims form one binding
    and therefore fail closed when only one is present or either claim has the
    wrong kind, access mode, or target.
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


def load_task_department_acceptance(
    memory: ProjectMemory,
    *,
    departments: Sequence[DepartmentContract],
    task: object,
    role_names: Mapping[str, str],
    dependency_outputs: Sequence[Mapping[str, object]],
) -> LoadedDepartmentAcceptance:
    """Materialize the exact department contract from a VERIFIED dependency.

    The plan-level department entry owns the stable department and Lead Role
    mapping.  Its rubric field is not trusted for a task binding: the pinned
    reference comes from the selected VERIFIED dependency output evidence so a
    stale bootstrap tuple cannot silently become the acceptance standard.
    """

    binding = task_department_binding(task)
    if binding is None:
        raise DepartmentAcceptanceError(
            "department acceptance requires task logical resources"
        )
    base = resolve_task_department(
        departments,
        task,
        role_names=role_names,
    )
    if base is None:  # guarded by the binding check above
        raise DepartmentAcceptanceError("department acceptance binding disappeared")
    source = rubric_reference_from_dependency_outputs(
        memory,
        binding=binding,
        dependency_outputs=dependency_outputs,
    )
    department = DepartmentContract(
        id=base.id,
        name=base.name,
        lead_role_id=base.lead_role_id,
        rubric=source.reference,
    )
    loaded = load_department_rubric(memory, department)
    return LoadedDepartmentAcceptance(
        binding=binding,
        department=department,
        rubric=loaded,
        source=source,
    )


def rubric_reference_from_dependency_outputs(
    memory: ProjectMemory,
    *,
    binding: TaskDepartmentBinding,
    dependency_outputs: Sequence[Mapping[str, object]],
) -> DependencyRubricReference:
    candidates: list[DependencyRubricReference] = []
    expected_output_id = f"{binding.department_id}-rubric-reference"
    for output in dependency_outputs:
        if str(output.get("output_id") or "") != expected_output_id:
            continue
        if str(output.get("dependency_state") or "") != "VERIFIED":
            raise DepartmentAcceptanceError(
                "department rubric reference requires a VERIFIED dependency output"
            )
        dependency_task_id = _required_text(
            output.get("dependency_task_id"),
            "dependency output task id",
            128,
        )
        output_id = _required_text(
            output.get("output_id"),
            "dependency output id",
            128,
        )
        evidence_ids = output.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            raise DepartmentAcceptanceError(
                f"dependency output {output_id!r} evidence_ids must be an array"
            )
        for raw_evidence_id in evidence_ids:
            evidence_id = _required_text(
                raw_evidence_id,
                f"dependency output {output_id!r} evidence id",
                128,
            )
            try:
                evidence = memory.get_evidence(evidence_id)
            except MemoryValidationError as exc:
                raise DepartmentAcceptanceError(str(exc)) from exc
            raw_tuple = _dependency_rubric_tuple(evidence)
            if raw_tuple is None:
                continue
            if evidence.get("exit_code") != 0:
                raise DepartmentAcceptanceError(
                    f"rubric reference evidence {evidence_id!r} did not pass"
                )
            department_id = _identifier(
                raw_tuple.get("department_id"),
                f"rubric reference evidence {evidence_id}.department_id",
            )
            scope = _required_text(
                raw_tuple.get("scope"),
                f"rubric reference evidence {evidence_id}.scope",
                256,
            )
            reference = rubric_reference_from_raw(
                {
                    "record_id": raw_tuple.get("record_id"),
                    "version": raw_tuple.get("version"),
                    "sha256": raw_tuple.get("sha256"),
                },
                f"rubric reference evidence {evidence_id}",
            )
            if department_id != binding.department_id or scope != binding.rubric_scope:
                raise DepartmentAcceptanceError(
                    f"rubric reference evidence {evidence_id!r} conflicts with task binding"
                )
            candidates.append(
                DependencyRubricReference(
                    dependency_task_id=dependency_task_id,
                    output_id=output_id,
                    evidence_id=evidence_id,
                    department_id=department_id,
                    scope=scope,
                    reference=reference,
                )
            )
    identities = {
        (
            item.department_id,
            item.scope,
            item.reference.record_id,
            item.reference.version,
            item.reference.sha256,
        )
        for item in candidates
    }
    sources = {
        (item.dependency_task_id, item.output_id)
        for item in candidates
    }
    if not candidates:
        raise DepartmentAcceptanceError(
            "no rubric reference tuple was found in VERIFIED dependency output evidence"
        )
    if len(identities) != 1 or len(sources) != 1:
        raise DepartmentAcceptanceError(
            "rubric reference is ambiguous across VERIFIED dependency output evidence"
        )
    return candidates[0]


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
) -> RubricReference:
    """Persist one immutable rubric version as a verified Project Memory record.

    A repeated identical write is idempotent. A changed version must advance by
    exactly one and cite outcome evidence; an observation is not evidence and
    therefore cannot mutate a department standard by itself.
    """

    rubric = DepartmentRubric(
        department_id=_identifier(department_id, "department_id"),
        version=_positive_int(version, "version"),
        criteria=_criteria(criteria),
        standards=_strings(standards, "standards"),
    )
    statement = _rubric_json(rubric)
    digest = rubric_digest(rubric)
    existing = _stored_rubrics(memory, rubric.department_id)
    same_version = [item for item in existing if item.rubric.version == rubric.version]
    if len(same_version) > 1:
        raise DepartmentAcceptanceError(
            f"rubric version {rubric.version} for department {rubric.department_id!r} "
            "is ambiguous in Project Memory"
        )
    if same_version:
        current = same_version[0]
        if current.reference.sha256 != digest:
            raise DepartmentAcceptanceError(
                f"rubric version {rubric.version} is immutable and already has a different digest"
            )
        return current.reference
    if existing:
        latest = max(item.rubric.version for item in existing)
        if rubric.version != latest + 1:
            raise DepartmentAcceptanceError(
                f"rubric version must advance exactly once ({latest} -> {latest + 1})"
            )
        _require_outcome_evidence(memory, evidence_ids)
    if not evidence_ids:
        raise DepartmentAcceptanceError(
            "NO EVIDENCE -> NO TRUTH: a department rubric requires evidence_ids"
        )
    try:
        record = memory.record_verified_fact(
            statement=statement,
            evidence_ids=evidence_ids,
            verification_method="department rubric outcome review",
            created_by=created_by,
            scope=rubric_scope(rubric.department_id),
        )
    except MemoryValidationError as exc:
        raise DepartmentAcceptanceError(str(exc)) from exc
    return RubricReference(
        record_id=str(record["id"]),
        version=rubric.version,
        sha256=digest,
    )


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
    VERIFIED dependency output.  Older plans may still carry a bootstrap
    record or digest in prose-oriented RoleProfile and DoD fields.  Those
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


def _stored_rubrics(
    memory: ProjectMemory,
    department_id: str,
) -> tuple[LoadedDepartmentRubric, ...]:
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = memory.list_records(
            categories=["truth"],
            statuses=["verified"],
            scope=rubric_scope(department_id),
            limit=20,
            cursor=cursor,
        )
        records.extend(page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    loaded: list[LoadedDepartmentRubric] = []
    for selector in records:
        record = memory.get_record(str(selector["id"]))
        rubric = department_rubric_from_raw(_parse_statement(record), "Project Memory rubric")
        reference = RubricReference(
            record_id=str(record["id"]),
            version=rubric.version,
            sha256=rubric_digest(rubric),
        )
        loaded.append(LoadedDepartmentRubric(reference=reference, rubric=rubric))
    return tuple(loaded)


def _require_outcome_evidence(
    memory: ProjectMemory,
    evidence_ids: Sequence[str],
) -> None:
    if not evidence_ids:
        raise DepartmentAcceptanceError(
            "a rubric change requires outcome evidence; one observation cannot change it"
        )
    kinds: list[str] = []
    for evidence_id in evidence_ids:
        try:
            evidence = memory.get_evidence(evidence_id)
        except MemoryValidationError as exc:
            raise DepartmentAcceptanceError(str(exc)) from exc
        kinds.append(str(evidence.get("kind") or ""))
    if not any(kind in OUTCOME_EVIDENCE_KINDS for kind in kinds):
        raise DepartmentAcceptanceError(
            "a rubric change requires outcome evidence; allowed kinds: "
            + ", ".join(sorted(OUTCOME_EVIDENCE_KINDS))
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


def _dependency_rubric_tuple(
    evidence: Mapping[str, object],
) -> Mapping[str, object] | None:
    raw_result = evidence.get("result")
    if isinstance(raw_result, Mapping):
        parsed: object = raw_result
    elif isinstance(raw_result, str):
        try:
            parsed = json.loads(raw_result)
        except json.JSONDecodeError:
            return None
    else:
        return None
    if not isinstance(parsed, Mapping):
        return None
    required = {"department_id", "record_id", "version", "sha256", "scope"}
    if set(parsed) != required:
        return None
    return parsed


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


def _exact_keys(data: Mapping[str, object], allowed: set[str], label: str) -> None:
    unknown = sorted(set(data) - allowed)
    missing = sorted(allowed - set(data))
    if unknown:
        raise DepartmentAcceptanceError(f"{label} has unknown fields: {unknown}")
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
