"""Versioned, composable skill packs and their fail-closed resolver.

Skill packs are procedure modules, not role personalities.  A canonical plan
stores the catalog and references exact versions; the runtime resolves only the
small stack selected for a task and exposes that stack to the worker prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import shlex
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .memory import MemoryValidationError
from .memory_verification import (
    RUNTIME_ATTESTATION_KEY,
    RUNTIME_VERIFICATION_AUTHORITY,
)
from .trust import EvidenceTrust, TRUST_POLICY


SKILL_SOURCES = frozenset({"vetted", "project_generated", "synthesized", "learned"})
SKILL_STATUSES = frozenset({"candidate", "trusted"})
SKILL_ATTESTATION_KINDS = frozenset({"source", "promotion", "qualification"})
PROMOTION_EVIDENCE_KINDS = frozenset(
    {
        "authoritative_documentation",
        "real_tool",
        "deterministic_test",
        "independent_verification",
        "verified_work_outcome",
    }
)
_PROMOTION_QUALIFIERS = PROMOTION_EVIDENCE_KINDS - {"independent_verification"}
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_MODEL_FIELDS = frozenset({"model", "model_id", "model_strategy", "reasoning"})
_SOURCE_ATTESTATION_REQUIRED = frozenset({"vetted", "project_generated"})

class SkillPackError(ValueError):
    """The skill catalog or requested stack is unsafe or inconsistent."""


@dataclass(frozen=True, slots=True)
class SkillReference:
    id: str
    version: str

    @property
    def key(self) -> tuple[str, str]:
        return self.id, self.version

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "version": self.version}


@dataclass(frozen=True, slots=True)
class SkillCheck:
    id: str
    description: str
    argv: tuple[str, ...]
    expected_exit_code: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "argv": list(self.argv),
            "expected_exit_code": self.expected_exit_code,
        }


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    id: str
    kind: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "verified": self.verified}


@dataclass(frozen=True, slots=True)
class SkillEvidenceRequirement:
    role: str
    kind: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class SkillAttestation:
    """A canonical task's contract for one exact Skill Pack revision."""

    kind: str
    skill: SkillReference
    evidence_roles: tuple[str, ...] = ()
    promotion_evidence: tuple[SkillEvidenceRequirement, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "skill": self.skill.to_dict(),
            **({"evidence_roles": list(self.evidence_roles)} if self.evidence_roles else {}),
            **(
                {"promotion_evidence": [item.to_dict() for item in self.promotion_evidence]}
                if self.promotion_evidence
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class SkillPack:
    id: str
    version: str
    capability: str
    source: str
    status: str
    procedures: tuple[str, ...]
    checklists: tuple[str, ...]
    failure_modes: tuple[str, ...]
    quality_criteria: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_mcp_servers: tuple[str, ...]
    deterministic_checks: tuple[SkillCheck, ...]
    evidence_roles: tuple[str, ...]
    conflicts_with: tuple[SkillReference, ...] = ()
    promotion_evidence: tuple[PromotionEvidence, ...] = ()
    source_verification_ids: tuple[str, ...] = ()
    qualification_verification_ids: tuple[str, ...] = ()

    @property
    def reference(self) -> SkillReference:
        return SkillReference(self.id, self.version)

    @property
    def behavioral_signature(self) -> tuple[tuple[str, ...], ...]:
        return (
            self.procedures,
            self.checklists,
            self.failure_modes,
            self.quality_criteria,
        )

    @property
    def revision_sha256(self) -> str:
        """Digest the immutable behavior being qualified or promoted.

        Evidence IDs and trust status are deliberately excluded: a candidate
        is tested first and then promoted without changing the revision it
        proved.  Every executable instruction, dependency and conflict is in
        the digest, so changing behavior invalidates the old verdict.
        """

        payload = {
            "id": self.id,
            "version": self.version,
            "capability": self.capability,
            "source": self.source,
            "procedures": list(self.procedures),
            "checklists": list(self.checklists),
            "failure_modes": list(self.failure_modes),
            "quality_criteria": list(self.quality_criteria),
            "required_tools": list(self.required_tools),
            "required_mcp_servers": list(self.required_mcp_servers),
            "deterministic_checks": [item.to_dict() for item in self.deterministic_checks],
            "evidence_roles": list(self.evidence_roles),
            "conflicts_with": [item.to_dict() for item in self.conflicts_with],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "capability": self.capability,
            "source": self.source,
            "status": self.status,
            "procedures": list(self.procedures),
            "checklists": list(self.checklists),
            "failure_modes": list(self.failure_modes),
            "quality_criteria": list(self.quality_criteria),
            "required_tools": list(self.required_tools),
            "required_mcp_servers": list(self.required_mcp_servers),
            "deterministic_checks": [item.to_dict() for item in self.deterministic_checks],
            "evidence_roles": list(self.evidence_roles),
            **(
                {"conflicts_with": [item.to_dict() for item in self.conflicts_with]}
                if self.conflicts_with
                else {}
            ),
            **(
                {"promotion_evidence": [item.to_dict() for item in self.promotion_evidence]}
                if self.promotion_evidence
                else {}
            ),
            **(
                {"source_verification_ids": list(self.source_verification_ids)}
                if self.source_verification_ids
                else {}
            ),
            **(
                {"qualification_verification_ids": list(self.qualification_verification_ids)}
                if self.qualification_verification_ids
                else {}
            ),
        }

    def to_prompt_dict(self) -> dict[str, Any]:
        """Return instructions and provenance, never routing/model controls."""

        return {
            "id": self.id,
            "version": self.version,
            "capability": self.capability,
            "provenance": {
                "source": self.source,
                "status": self.status,
                "revision_sha256": self.revision_sha256,
                "promotion_evidence_ids": [item.id for item in self.promotion_evidence],
                "source_verification_ids": list(self.source_verification_ids),
                "qualification_verification_ids": list(
                    self.qualification_verification_ids
                ),
            },
            "procedures": list(self.procedures),
            "checklists": list(self.checklists),
            "failure_modes": list(self.failure_modes),
            "quality_criteria": list(self.quality_criteria),
            "required_tools": list(self.required_tools),
            "required_mcp_servers": list(self.required_mcp_servers),
            "deterministic_checks": [item.to_dict() for item in self.deterministic_checks],
            "evidence_roles": list(self.evidence_roles),
        }


def skill_reference_from_raw(raw: Any, label: str = "skill reference") -> SkillReference:
    if not isinstance(raw, Mapping):
        raise SkillPackError(f"{label} must be an object")
    _reject_unknown(raw, {"id", "version"}, label)
    return SkillReference(
        id=_identifier(raw.get("id"), f"{label}.id"),
        version=_semantic_version(raw.get("version"), f"{label}.version"),
    )


def skill_references_from_raw(raw: Any, label: str) -> tuple[SkillReference, ...]:
    references = tuple(
        skill_reference_from_raw(item, f"{label}[{index}]")
        for index, item in enumerate(_array(raw, label))
    )
    _unique((item.key for item in references), label)
    return references


def skill_attestation_from_raw(
    raw: Any, label: str = "skill attestation"
) -> SkillAttestation | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SkillPackError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {"kind", "skill", "evidence_roles", "promotion_evidence"},
        label,
    )
    kind = _choice(raw.get("kind"), SKILL_ATTESTATION_KINDS, f"{label}.kind")
    skill = skill_reference_from_raw(raw.get("skill"), f"{label}.skill")
    evidence_roles = tuple(
        _identifier(item, f"{label}.evidence_roles item")
        for item in _array(raw.get("evidence_roles", []), f"{label}.evidence_roles")
    )
    _unique(evidence_roles, f"{label} evidence role")
    promotion_evidence = tuple(
        _skill_evidence_requirement_from_raw(
            item, f"{label}.promotion_evidence[{index}]"
        )
        for index, item in enumerate(
            _array(raw.get("promotion_evidence", []), f"{label}.promotion_evidence")
        )
    )
    _unique(
        (item.role for item in promotion_evidence),
        f"{label} promotion evidence role",
    )
    if kind == "source":
        if not evidence_roles:
            raise SkillPackError(f"{label}.evidence_roles must be non-empty for source review")
        if promotion_evidence:
            raise SkillPackError(f"{label} source review cannot declare promotion_evidence")
    elif kind == "promotion":
        if not promotion_evidence:
            raise SkillPackError(
                f"{label}.promotion_evidence must be non-empty for promotion review"
            )
        if evidence_roles:
            raise SkillPackError(f"{label} promotion review uses promotion_evidence roles")
    elif evidence_roles or promotion_evidence:
        raise SkillPackError(
            f"{label} qualification derives evidence from declared skill checks"
        )
    return SkillAttestation(
        kind=kind,
        skill=skill,
        evidence_roles=evidence_roles,
        promotion_evidence=promotion_evidence,
    )


def skill_packs_from_raw(raw: Any, label: str = "plan.skill_packs") -> tuple[SkillPack, ...]:
    return tuple(
        skill_pack_from_raw(item, f"skill pack {index}")
        for index, item in enumerate(_array(raw, label), 1)
    )


def skill_pack_from_raw(raw: Any, label: str = "skill pack") -> SkillPack:
    if not isinstance(raw, Mapping):
        raise SkillPackError(f"{label} must be an object")
    forbidden = sorted(set(raw) & _MODEL_FIELDS)
    if forbidden:
        raise SkillPackError(
            f"{label} cannot select a model; capability routing is the only model selector "
            f"(forbidden fields: {', '.join(forbidden)})"
        )
    allowed = {
        "id", "version", "capability", "source", "status", "procedures",
        "checklists", "failure_modes", "quality_criteria", "required_tools",
        "required_mcp_servers", "deterministic_checks", "evidence_roles",
        "conflicts_with", "promotion_evidence", "source_verification_ids",
        "qualification_verification_ids",
    }
    _reject_unknown(raw, allowed, label)
    source = _choice(raw.get("source"), SKILL_SOURCES, f"{label}.source")
    status = _choice(raw.get("status"), SKILL_STATUSES, f"{label}.status")
    checks = tuple(
        _check_from_raw(item, f"{label}.deterministic_checks[{index}]")
        for index, item in enumerate(
            _nonempty_array(raw.get("deterministic_checks"), f"{label}.deterministic_checks")
        )
    )
    _unique((item.id for item in checks), f"{label} deterministic check id")
    conflicts = tuple(
        skill_reference_from_raw(item, f"{label}.conflicts_with[{index}]")
        for index, item in enumerate(_array(raw.get("conflicts_with", []), f"{label}.conflicts_with"))
    )
    _unique((item.key for item in conflicts), f"{label} conflicting skill reference")
    promotion = tuple(
        _promotion_from_raw(item, f"{label}.promotion_evidence[{index}]")
        for index, item in enumerate(
            _array(raw.get("promotion_evidence", []), f"{label}.promotion_evidence")
        )
    )
    _unique((item.id for item in promotion), f"{label} promotion evidence id")
    if source == "synthesized" and status == "trusted":
        _validate_synthesized_promotion(promotion, label)
    if source == "learned" and status == "trusted":
        _validate_learned_promotion(promotion, label)
    pack = SkillPack(
        id=_identifier(raw.get("id"), f"{label}.id"),
        version=_semantic_version(raw.get("version"), f"{label}.version"),
        capability=_identifier(raw.get("capability"), f"{label}.capability"),
        source=source,
        status=status,
        procedures=_nonempty_strings(raw.get("procedures"), f"{label}.procedures"),
        checklists=_nonempty_strings(raw.get("checklists"), f"{label}.checklists"),
        failure_modes=_nonempty_strings(raw.get("failure_modes"), f"{label}.failure_modes"),
        quality_criteria=_nonempty_strings(
            raw.get("quality_criteria"), f"{label}.quality_criteria"
        ),
        required_tools=_strings(raw.get("required_tools", []), f"{label}.required_tools"),
        required_mcp_servers=_strings(
            raw.get("required_mcp_servers", []), f"{label}.required_mcp_servers"
        ),
        deterministic_checks=checks,
        evidence_roles=_nonempty_strings(raw.get("evidence_roles"), f"{label}.evidence_roles"),
        conflicts_with=conflicts,
        promotion_evidence=promotion,
        source_verification_ids=_strings(
            raw.get("source_verification_ids", []),
            f"{label}.source_verification_ids",
        ),
        qualification_verification_ids=_strings(
            raw.get("qualification_verification_ids", []),
            f"{label}.qualification_verification_ids",
        ),
    )
    if pack.reference in pack.conflicts_with:
        raise SkillPackError(f"{label} cannot conflict with itself")
    return pack


def resolve_skill_stack(
    catalog: Sequence[SkillPack],
    requested: Sequence[SkillReference],
    *,
    requirements: Sequence[SkillReference] | None = None,
    qualification_evidence_store: Any | None = None,
    require_qualification: bool = True,
) -> tuple[SkillPack, ...]:
    """Resolve exact versions and reject every unqualified trusted pack."""

    by_key: dict[tuple[str, str], SkillPack] = {}
    for pack in catalog:
        if pack.reference.key in by_key:
            raise SkillPackError(
                f"skill catalog duplicates {pack.id}@{pack.version}; resolution is ambiguous"
            )
        by_key[pack.reference.key] = pack
    _unique((item.id for item in requested), "loaded skill id")
    _unique((item.key for item in requested), "loaded skill reference")
    if requirements is not None:
        allowed = {item.key for item in requirements}
        missing = [f"{item.id}@{item.version}" for item in requested if item.key not in allowed]
        if missing:
            raise SkillPackError(
                "loaded skills are not declared by the role's Skill Requirements: "
                + ", ".join(missing)
            )
    resolved: list[SkillPack] = []
    for reference in requested:
        pack = by_key.get(reference.key)
        if pack is None:
            raise SkillPackError(
                f"skill {reference.id}@{reference.version} is not present in the catalog"
            )
        if pack.status != "trusted":
            raise SkillPackError(
                f"skill {pack.id}@{pack.version} is CANDIDATE and cannot be loaded as trusted"
            )
        resolved.append(pack)
    _reject_stack_duplicates(resolved)
    _reject_stack_conflicts(resolved)
    if require_qualification:
        # The prompt boundary is a second production gate.  A Plan object may
        # have been restored or assembled by a caller, so source provenance is
        # re-read from Project Memory immediately before instructions enter a
        # worker context instead of relying only on earlier plan validation.
        if qualification_evidence_store is not None:
            validate_trusted_skill_promotions(
                resolved, evidence_store=qualification_evidence_store
            )
        for pack in resolved:
            _validate_skill_qualification(pack, qualification_evidence_store)
    return tuple(resolved)


def validate_plan_skill_bindings(plan: Any) -> None:
    """Validate role requirements and task stacks without importing Plan here."""

    catalog = tuple(plan.skill_packs)
    # An empty resolution still validates duplicate catalog identities.
    resolve_skill_stack(catalog, (), require_qualification=False)
    for role in plan.roles:
        resolve_skill_stack(
            catalog, role.skill_requirements, require_qualification=False
        )
    for task in plan.tasks:
        role = plan.role_map[task.role]
        resolve_skill_stack(
            catalog,
            task.loaded_skills,
            requirements=role.skill_requirements,
            require_qualification=False,
        )
    validate_plan_skill_attestations(plan)


def validate_plan_skill_attestations(plan: Any) -> None:
    """Reject attestation work that cannot produce the resolver's exact records."""

    catalog = {pack.reference.key: pack for pack in plan.skill_packs}
    for task in plan.tasks:
        attestation = task.skill_attestation
        if attestation is None:
            continue
        pack = catalog.get(attestation.skill.key)
        if pack is None:
            raise SkillPackError(
                f"task {task.id} attests unknown skill "
                f"{attestation.skill.id}@{attestation.skill.version}"
            )
        if task.verification.policy != "independent" or not task.verification.required:
            raise SkillPackError(
                f"task {task.id} skill attestation requires independent verification"
            )
        if attestation.kind == "source":
            if pack.source not in _SOURCE_ATTESTATION_REQUIRED:
                raise SkillPackError(
                    f"task {task.id} source attestation is invalid for {pack.source!r} skills"
                )
            continue
        if attestation.kind == "promotion":
            if pack.source not in {"synthesized", "learned"}:
                raise SkillPackError(
                    f"task {task.id} promotion attestation is invalid for {pack.source!r} skills"
                )
            kinds = {item.kind for item in attestation.promotion_evidence}
            if pack.source == "learned" and "verified_work_outcome" not in kinds:
                raise SkillPackError(
                    f"task {task.id} learned skill promotion requires verified_work_outcome"
                )
            continue
        task_checks = {check.id: check for check in task.verification.deterministic_checks}
        for skill_check in pack.deterministic_checks:
            task_check = task_checks.get(skill_check.id)
            if task_check is None:
                raise SkillPackError(
                    f"task {task.id} qualification does not execute skill check "
                    f"{skill_check.id}"
                )
            if (
                task_check.kind != "command"
                or task_check.argv != skill_check.argv
                or task_check.expected_exit_code != skill_check.expected_exit_code
            ):
                raise SkillPackError(
                    f"task {task.id} qualification check {skill_check.id} must exactly "
                    "match the Skill Pack argv and expected exit code"
                )


def record_runtime_skill_attestation(
    memory: Any,
    plan: Any,
    task: Any,
    *,
    evidence: Sequence[Mapping[str, Any]],
    check_results: Sequence[Mapping[str, Any]],
    created_by: str,
    provider_thread_id: str,
    provider_turn_id: str,
) -> Mapping[str, Any] | None:
    """Write a revision-bound attestation from a fresh verifier completion.

    This is deliberately invoked by the canonical lifecycle only after PASS.
    Worker-authored evidence supplies the material; the runtime supplies the
    unforgeable attestation and exact causal verifier identity.
    """

    attestation = task.skill_attestation
    if attestation is None:
        return None
    pack = {item.reference.key: item for item in plan.skill_packs}.get(
        attestation.skill.key
    )
    if pack is None:
        raise SkillPackError(
            f"task {task.id} attests an absent skill {attestation.skill.id}@"
            f"{attestation.skill.version}"
        )
    revision = {"id": pack.id, "version": pack.version, "sha256": pack.revision_sha256}
    special_task_id = (
        f"SKILL-{attestation.kind.upper()}:{pack.id}@{pack.version}:"
        f"{pack.revision_sha256}"
    )
    if attestation.kind == "source":
        selected = _evidence_for_roles(
            evidence,
            attestation.evidence_roles,
            f"task {task.id} source attestation",
        )
        details = {
            "skill_pack_revision": revision,
            "skill_source": {
                "source": pack.source,
                "skill_pack_sha256": pack.revision_sha256,
                "evidence_ids": [str(item["id"]) for item in selected],
            },
        }
        check_id = "skill-source"
        summary = "Independent Skill Pack source review passed."
    elif attestation.kind == "promotion":
        selected_rows: list[Mapping[str, Any]] = []
        for requirement in attestation.promotion_evidence:
            rows = _evidence_for_roles(
                evidence,
                (requirement.role,),
                f"task {task.id} promotion attestation",
            )
            for row in rows:
                _validate_qualifying_evidence(
                    PromotionEvidence(str(row["id"]), requirement.kind, True),
                    row,
                    f"skill {pack.id}@{pack.version}",
                )
            selected_rows.extend(rows)
        _unique(
            (str(item["id"]) for item in selected_rows),
            f"task {task.id} promotion evidence",
        )
        selected = tuple(selected_rows)
        details = {
            "skill_pack_revision": revision,
            "promotion_evidence": [
                {"id": str(item["id"]), "skill_pack_sha256": pack.revision_sha256}
                for item in selected
            ],
        }
        check_id = "skill-promotion"
        summary = "Independent Skill Pack promotion review passed."
    else:
        selected, skill_checks = _qualification_results(
            pack,
            evidence,
            check_results,
            label=f"task {task.id} qualification attestation",
        )
        details = {"skill_pack_revision": revision, "skill_checks": skill_checks}
        check_id = "skill-qualification"
        summary = "Independent review confirmed every Skill Pack qualification check."
    return memory._record_runtime_verification_result(
        task_id=special_task_id,
        check_id=check_id,
        policy="independent",
        verdict="PASS",
        summary=summary,
        evidence_ids=[str(item["id"]) for item in selected],
        created_by=created_by,
        provider="codex-desktop",
        provider_thread_id=provider_thread_id,
        provider_turn_id=provider_turn_id,
        details=details,
    )


def validate_plan_skill_qualifications(
    plan: Any,
    *,
    evidence_store: Any | None = None,
    project_root: Path | None = None,
) -> None:
    """Require authoritative, revision-bound PASS records for loaded packs."""

    loaded_tasks = tuple(task for task in plan.tasks if task.loaded_skills)
    if not loaded_tasks:
        return
    if evidence_store is None and project_root is not None:
        from .memory import ProjectMemory

        evidence_store = ProjectMemory(project_root)
    catalog = tuple(plan.skill_packs)
    for task in loaded_tasks:
        role = plan.role_map[task.role]
        resolve_skill_stack(
            catalog,
            task.loaded_skills,
            requirements=role.skill_requirements,
            qualification_evidence_store=evidence_store,
        )


def require_allowed_skill_evidence_role(
    memory: Any, milestone_id: str, role: str
) -> None:
    """Enforce loaded pack roles at the Project Memory write boundary."""

    if not (memory.state_dir / "plan.json").is_file():
        return
    from .plan import load_plan

    plan = load_plan(memory.state_dir, "adaptive")
    task = plan.task_map.get(milestone_id)
    if task is None or not task.loaded_skills:
        return
    catalog = {item.reference.key: item for item in plan.skill_packs}
    allowed = {
        evidence_role
        for reference in task.loaded_skills
        for evidence_role in catalog[reference.key].evidence_roles
    }
    allowed.update(item.id for item in task.verification.deterministic_checks)
    if role not in allowed:
        raise MemoryValidationError(
            f"evidence role {role!r} is not allowed for loaded Skill Packs "
            f"on task {milestone_id}; allowed roles: {', '.join(sorted(allowed))}"
        )


def validate_trusted_skill_promotions(
    packs: Sequence[SkillPack],
    *,
    evidence_store: Any | None = None,
    project_root: Path | None = None,
) -> None:
    """Resolve every trusted source claim and promotion against Project Memory.

    The serialized ``verified`` flag is a request, not authority.  A trusted
    vetted/project-generated pack needs an independent source attestation for
    its exact revision.  A generated or learned pack is admitted only when its
    promotion IDs resolve to the expected Project Memory record types, the
    records retain sufficient trust, and one independent PASS actually cites
    every qualifying evidence item.
    """

    source_attested = tuple(
        pack
        for pack in packs
        if pack.status == "trusted" and pack.source in _SOURCE_ATTESTATION_REQUIRED
    )
    promoted = tuple(
        pack
        for pack in packs
        if pack.status == "trusted" and pack.source in {"synthesized", "learned"}
    )
    if not source_attested and not promoted:
        return
    if evidence_store is None and project_root is not None:
        from .memory import ProjectMemory

        evidence_store = ProjectMemory(project_root)
    if evidence_store is None:
        names = ", ".join(
            f"{pack.id}@{pack.version}" for pack in (*source_attested, *promoted)
        )
        raise SkillPackError(
            "trusted skill provenance requires Project Memory validation; "
            f"no evidence store was supplied for {names}"
        )

    for pack in source_attested:
        _validate_skill_source_attestation(pack, evidence_store)

    for pack in promoted:
        label = f"skill {pack.id}@{pack.version}"
        verifications: list[Mapping[str, Any]] = []
        qualifiers: list[tuple[PromotionEvidence, Mapping[str, Any]]] = []
        for item in pack.promotion_evidence:
            if item.kind == "independent_verification":
                record = _memory_record(evidence_store, item.id, verification=True)
                if record is None:
                    raise SkillPackError(
                        f"{label} references unknown Project Memory verification {item.id}"
                    )
                _validate_independent_verification(record, item.id, label, pack)
                verifications.append(record)
            else:
                record = _memory_record(evidence_store, item.id, verification=False)
                if record is None:
                    raise SkillPackError(
                        f"{label} references unknown Project Memory evidence {item.id}"
                    )
                _validate_qualifying_evidence(item, record, label)
                qualifiers.append((item, record))

        cited_ids = {
            str(row.get("id"))
            for verification in verifications
            for row in _record_array(verification.get("evidence"), "verification.evidence")
        }
        uncited = [item.id for item, _record in qualifiers if item.id not in cited_ids]
        if uncited:
            raise SkillPackError(
                f"{label} promotion evidence is not cited by its independent PASS: "
                + ", ".join(uncited)
            )
        bound_ids = {
            str(item_id)
            for verification in verifications
            for item_id in _promotion_evidence_bindings(verification, pack, label)
        }
        unbound = [item.id for item, _record in qualifiers if item.id not in bound_ids]
        if unbound:
            raise SkillPackError(
                f"{label} promotion evidence is not bound to revision "
                f"{pack.revision_sha256}: " + ", ".join(unbound)
            )


def _validate_skill_source_attestation(pack: SkillPack, evidence_store: Any) -> None:
    """Require an independent, revision-bound origin verdict for library sources."""

    label = f"skill {pack.id}@{pack.version}"
    if pack.promotion_evidence:
        raise SkillPackError(
            f"{label} declares source {pack.source!r} and cannot claim "
            "promotion_evidence; use an authoritative source verification"
        )
    if not pack.source_verification_ids:
        raise SkillPackError(
            f"{label} declares source {pack.source!r} but has no authoritative "
            "source verification"
        )
    for verification_id in pack.source_verification_ids:
        record = _memory_record(evidence_store, verification_id, verification=True)
        if record is None:
            raise SkillPackError(
                f"{label} references unknown source verification {verification_id}"
            )
        if str(record.get("id")) != verification_id:
            raise SkillPackError(f"{label} source verification lookup returned a different ID")
        expected_task_id = f"SKILL-SOURCE:{pack.id}@{pack.version}:{pack.revision_sha256}"
        if (
            record.get("task_id") != expected_task_id
            or record.get("check_id") != "skill-source"
            or record.get("policy") != "independent"
            or record.get("verdict") != "PASS"
        ):
            raise SkillPackError(
                f"{label} source verification {verification_id} is not an independent "
                "PASS for this exact revision"
            )
        _require_runtime_attestation(
            record,
            f"source verification {verification_id}",
            authority_kind="fresh_verifier",
        )
        _require_revision_binding(record, pack, f"source verification {verification_id}")
        rows = _record_array(
            record.get("evidence"), f"source verification {verification_id}.evidence"
        )
        if not rows:
            raise SkillPackError(
                f"{label} source verification {verification_id} has no evidence"
            )
        for row in rows:
            _require_deterministic_trust(
                row, f"source verification {verification_id} evidence"
            )
        details = _record_mapping(
            record.get("details"), f"source verification {verification_id}.details"
        )
        binding = details.get("skill_source")
        expected_binding = {
            "source": pack.source,
            "skill_pack_sha256": pack.revision_sha256,
            "evidence_ids": [str(row.get("id")) for row in rows],
        }
        if not isinstance(binding, Mapping) or dict(binding) != expected_binding:
            raise SkillPackError(
                f"{label} source verification {verification_id} must bind source, "
                "revision sha256, and its exact evidence IDs"
            )


def _validate_skill_qualification(pack: SkillPack, evidence_store: Any | None) -> None:
    label = f"skill {pack.id}@{pack.version}"
    if evidence_store is None:
        raise SkillPackError(
            f"{label} qualification requires Project Memory validation before loading"
        )
    if not pack.qualification_verification_ids:
        raise SkillPackError(
            f"{label} has no authoritative qualification PASS before loading"
        )
    expected_checks = {item.id: item for item in pack.deterministic_checks}
    admitted: set[str] = set()
    for verification_id in pack.qualification_verification_ids:
        record = _memory_record(evidence_store, verification_id, verification=True)
        if record is None:
            raise SkillPackError(
                f"{label} references unknown qualification verification {verification_id}"
            )
        if str(record.get("id")) != verification_id:
            raise SkillPackError(f"{label} qualification lookup returned a different ID")
        expected_task_id = (
            f"SKILL-QUALIFICATION:{pack.id}@{pack.version}:{pack.revision_sha256}"
        )
        if (
            record.get("task_id") != expected_task_id
            or record.get("check_id") != "skill-qualification"
            or record.get("policy") not in {"deterministic", "independent"}
            or record.get("verdict") != "PASS"
        ):
            raise SkillPackError(
                f"{label} qualification {verification_id} is not an authoritative PASS "
                "for this exact revision"
            )
        _require_runtime_attestation(
            record,
            f"qualification {verification_id}",
            authority_kind=(
                "deterministic_runner"
                if record.get("policy") == "deterministic"
                else "fresh_verifier"
            ),
        )
        _require_revision_binding(record, pack, f"qualification {verification_id}")
        evidence_rows = _record_array(
            record.get("evidence"), f"qualification {verification_id}.evidence"
        )
        evidence_by_id = {str(row.get("id")): row for row in evidence_rows}
        for row in evidence_rows:
            _require_deterministic_trust(row, f"qualification {verification_id} evidence")
        details = _record_mapping(
            record.get("details"), f"qualification {verification_id}.details"
        )
        results = _record_array(
            details.get("skill_checks"),
            f"qualification {verification_id}.details.skill_checks",
        )
        for result in results:
            check_id = str(result.get("id") or "")
            check = expected_checks.get(check_id)
            if check is None:
                raise SkillPackError(
                    f"{label} qualification {verification_id} reports unknown check {check_id!r}"
                )
            if check_id in admitted:
                raise SkillPackError(f"{label} qualification duplicates check {check_id}")
            evidence_id = str(result.get("evidence_id") or "")
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                raise SkillPackError(
                    f"{label} qualification check {check_id} references uncited evidence "
                    f"{evidence_id!r}"
                )
            expected_result = {
                "id": check.id,
                "argv": list(check.argv),
                "expected_exit_code": check.expected_exit_code,
                "exit_code": check.expected_exit_code,
                "evidence_id": evidence_id,
            }
            if dict(result) != expected_result:
                raise SkillPackError(
                    f"{label} qualification check {check_id} does not match its declared "
                    "argv and expected exit code"
                )
            if evidence.get("kind") not in {"test", "build"} or (
                evidence.get("exit_code") != check.expected_exit_code
            ) or (
                evidence.get("command") != shlex.join(check.argv)
            ):
                raise SkillPackError(
                    f"{label} qualification check {check_id} has no passing deterministic "
                    "test evidence for its declared argv"
                )
            admitted.add(check_id)
    missing = sorted(set(expected_checks) - admitted)
    if missing:
        raise SkillPackError(
            f"{label} qualification has no authoritative PASS for checks: "
            + ", ".join(missing)
        )


def _memory_record(store: Any, record_id: str, *, verification: bool) -> Mapping[str, Any] | None:
    if not store.path.is_file():
        return None
    try:
        return (
            store.get_verification_result(record_id)
            if verification
            else store.get_evidence(record_id)
        )
    except MemoryValidationError as exc:
        expected = "unknown verification result" if verification else "unknown evidence"
        if expected in str(exc):
            return None
        raise


def _evidence_for_roles(
    evidence: Sequence[Mapping[str, Any]],
    roles: Sequence[str],
    label: str,
) -> tuple[Mapping[str, Any], ...]:
    selected: list[Mapping[str, Any]] = []
    for role in roles:
        matches = [item for item in evidence if item.get("role") == role]
        if not matches:
            raise SkillPackError(f"{label} has no implementation evidence role {role!r}")
        for item in matches:
            _required_string(item.get("id"), f"{label} evidence id")
            _require_deterministic_trust(item, f"{label} evidence {item.get('id')}")
        selected.extend(matches)
    _unique((str(item["id"]) for item in selected), f"{label} evidence")
    return tuple(selected)


def _qualification_results(
    pack: SkillPack,
    evidence: Sequence[Mapping[str, Any]],
    check_results: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> tuple[tuple[Mapping[str, Any], ...], list[dict[str, Any]]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    for result in check_results:
        check_id = str(result.get("check_id") or "")
        if check_id in by_id:
            raise SkillPackError(f"{label} duplicates deterministic result {check_id!r}")
        by_id[check_id] = result
    selected: list[Mapping[str, Any]] = []
    skill_results: list[dict[str, Any]] = []
    for check in pack.deterministic_checks:
        result = by_id.get(check.id)
        if result is None:
            raise SkillPackError(f"{label} has no executed result for {check.id}")
        if (
            result.get("kind") != "command"
            or result.get("passed") is not True
            or result.get("exit_code") != check.expected_exit_code
        ):
            raise SkillPackError(f"{label} check {check.id} did not pass as declared")
        matches = [
            item
            for item in evidence
            if item.get("role") == check.id
            and item.get("provider") == "deterministic-runtime"
            and item.get("kind") == "test"
            and item.get("command") == shlex.join(check.argv)
            and item.get("exit_code") == check.expected_exit_code
        ]
        if len(matches) != 1:
            raise SkillPackError(
                f"{label} check {check.id} requires exactly one runtime test evidence record"
            )
        row = matches[0]
        _require_deterministic_trust(row, f"{label} evidence {row.get('id')}")
        evidence_id = _required_string(row.get("id"), f"{label} evidence id")
        selected.append(row)
        skill_results.append(
            {
                "id": check.id,
                "argv": list(check.argv),
                "expected_exit_code": check.expected_exit_code,
                "exit_code": check.expected_exit_code,
                "evidence_id": evidence_id,
            }
        )
    return tuple(selected), skill_results


def _reject_stack_duplicates(packs: Sequence[SkillPack]) -> None:
    capabilities: dict[str, SkillPack] = {}
    signatures: dict[tuple[tuple[str, ...], ...], SkillPack] = {}
    for pack in packs:
        previous = capabilities.get(pack.capability)
        if previous is not None:
            raise SkillPackError(
                "loaded skill packs duplicate capability "
                f"{pack.capability!r}: {previous.id}@{previous.version} and "
                f"{pack.id}@{pack.version}"
            )
        capabilities[pack.capability] = pack
        duplicate = signatures.get(pack.behavioral_signature)
        if duplicate is not None:
            raise SkillPackError(
                "loaded skill packs duplicate the same procedures and quality contract: "
                f"{duplicate.id}@{duplicate.version} and {pack.id}@{pack.version}"
            )
        signatures[pack.behavioral_signature] = pack


def _reject_stack_conflicts(packs: Sequence[SkillPack]) -> None:
    loaded = {pack.reference.key: pack for pack in packs}
    for pack in packs:
        for conflict in pack.conflicts_with:
            other = loaded.get(conflict.key)
            if other is not None:
                raise SkillPackError(
                    "loaded skill packs conflict: "
                    f"{pack.id}@{pack.version} conflicts with {other.id}@{other.version}"
                )


def _check_from_raw(raw: Any, label: str) -> SkillCheck:
    if not isinstance(raw, Mapping):
        raise SkillPackError(f"{label} must be an object")
    _reject_unknown(raw, {"id", "description", "argv", "expected_exit_code"}, label)
    expected = raw.get("expected_exit_code", 0)
    if isinstance(expected, bool) or not isinstance(expected, int):
        raise SkillPackError(f"{label}.expected_exit_code must be an integer")
    return SkillCheck(
        id=_identifier(raw.get("id"), f"{label}.id"),
        description=_required_string(raw.get("description"), f"{label}.description"),
        argv=_nonempty_strings(raw.get("argv"), f"{label}.argv"),
        expected_exit_code=expected,
    )


def _promotion_from_raw(raw: Any, label: str) -> PromotionEvidence:
    if not isinstance(raw, Mapping):
        raise SkillPackError(f"{label} must be an object")
    _reject_unknown(raw, {"id", "kind", "verified"}, label)
    verified = raw.get("verified")
    if verified is not True:
        raise SkillPackError(
            f"{label}.verified must be true; Project Memory still validates the claim"
        )
    return PromotionEvidence(
        id=_required_string(raw.get("id"), f"{label}.id"),
        kind=_choice(raw.get("kind"), PROMOTION_EVIDENCE_KINDS, f"{label}.kind"),
        verified=verified,
    )


def _skill_evidence_requirement_from_raw(
    raw: Any, label: str
) -> SkillEvidenceRequirement:
    if not isinstance(raw, Mapping):
        raise SkillPackError(f"{label} must be an object")
    _reject_unknown(raw, {"role", "kind"}, label)
    return SkillEvidenceRequirement(
        role=_identifier(raw.get("role"), f"{label}.role"),
        kind=_choice(raw.get("kind"), _PROMOTION_QUALIFIERS, f"{label}.kind"),
    )


def _validate_synthesized_promotion(evidence: Sequence[PromotionEvidence], label: str) -> None:
    verified_kinds = {item.kind for item in evidence if item.verified}
    if "independent_verification" not in verified_kinds or not (
        verified_kinds & _PROMOTION_QUALIFIERS
    ):
        raise SkillPackError(
            f"{label} is synthesized and cannot be declared trusted without verified "
            "independent_verification plus authoritative documentation, a real tool, "
            "a deterministic test, or a verified work outcome"
        )


def _validate_learned_promotion(evidence: Sequence[PromotionEvidence], label: str) -> None:
    verified_kinds = {item.kind for item in evidence if item.verified}
    if not {"independent_verification", "verified_work_outcome"} <= verified_kinds:
        raise SkillPackError(
            f"{label} is learned and cannot be declared trusted without verified "
            "independent_verification and verified_work_outcome evidence"
        )


def _validate_independent_verification(
    record: Mapping[str, Any], evidence_id: str, label: str, pack: SkillPack
) -> None:
    if str(record.get("id")) != evidence_id:
        raise SkillPackError(f"{label} verification lookup returned a different ID")
    if record.get("policy") != "independent" or record.get("verdict") != "PASS":
        raise SkillPackError(
            f"{label} promotion verification {evidence_id} must be an independent PASS"
        )
    expected_task_id = f"SKILL-PROMOTION:{pack.id}@{pack.version}:{pack.revision_sha256}"
    if record.get("task_id") != expected_task_id or record.get("check_id") != "skill-promotion":
        raise SkillPackError(
            f"{label} promotion verification {evidence_id} is not for this exact revision"
        )
    _require_runtime_attestation(
        record,
        f"promotion verification {evidence_id}",
        authority_kind="fresh_verifier",
    )
    _require_revision_binding(record, pack, f"promotion verification {evidence_id}")
    rows = _record_array(record.get("evidence"), f"verification {evidence_id}.evidence")
    if not rows:
        raise SkillPackError(f"{label} promotion verification {evidence_id} has no evidence")
    for row in rows:
        _require_deterministic_trust(row, f"verification {evidence_id} evidence")


def _require_revision_binding(
    record: Mapping[str, Any], pack: SkillPack, label: str
) -> None:
    details = _record_mapping(record.get("details"), f"{label}.details")
    binding = details.get("skill_pack_revision")
    expected = {
        "id": pack.id,
        "version": pack.version,
        "sha256": pack.revision_sha256,
    }
    if not isinstance(binding, Mapping) or dict(binding) != expected:
        raise SkillPackError(
            f"{label} must bind id, version, and sha256 for the exact skill revision"
        )


def _promotion_evidence_bindings(
    record: Mapping[str, Any], pack: SkillPack, label: str
) -> tuple[str, ...]:
    details = _record_mapping(record.get("details"), f"{label} promotion details")
    raw = details.get("promotion_evidence")
    bindings = _record_array(raw, f"{label} promotion_evidence bindings")
    cited_ids = {
        str(row.get("id"))
        for row in _record_array(record.get("evidence"), f"{label} verification evidence")
    }
    result: list[str] = []
    for binding in bindings:
        evidence_id = _required_string(
            binding.get("id"), f"{label} promotion evidence binding id"
        )
        expected = {"id": evidence_id, "skill_pack_sha256": pack.revision_sha256}
        if dict(binding) != expected:
            raise SkillPackError(
                f"{label} promotion evidence {evidence_id} is not bound to exact revision "
                f"{pack.revision_sha256}"
            )
        if evidence_id not in cited_ids:
            raise SkillPackError(
                f"{label} promotion evidence binding {evidence_id} is not cited by "
                "the same independent PASS"
            )
        result.append(evidence_id)
    _unique(result, f"{label} promotion evidence binding")
    return tuple(result)


def _validate_qualifying_evidence(
    item: PromotionEvidence, record: Mapping[str, Any], label: str
) -> None:
    if str(record.get("id")) != item.id:
        raise SkillPackError(f"{label} evidence lookup returned a different ID")
    expected_kinds = {
        "authoritative_documentation": frozenset({"file", "external"}),
        "real_tool": frozenset({"tool"}),
        "deterministic_test": frozenset({"test", "build"}),
        "verified_work_outcome": frozenset({"artifact", "build", "test", "tool"}),
    }[item.kind]
    stored_kind = str(record.get("kind") or "")
    if stored_kind not in expected_kinds:
        raise SkillPackError(
            f"{label} promotion evidence {item.id} kind mismatch: "
            f"{item.kind} requires Project Memory kind {sorted(expected_kinds)}, got {stored_kind!r}"
        )
    if item.kind == "deterministic_test" and record.get("exit_code") != 0:
        raise SkillPackError(
            f"{label} deterministic test evidence {item.id} did not pass"
        )
    _require_deterministic_trust(record, f"promotion evidence {item.id}")


def _require_deterministic_trust(record: Mapping[str, Any], label: str) -> None:
    try:
        trust = EvidenceTrust.from_storage(record)
    except ValueError as exc:
        raise SkillPackError(str(exc)) from exc
    if not TRUST_POLICY.level_meets(trust.level, TRUST_POLICY.TRUTH_THRESHOLD):
        raise SkillPackError(
            f"{label} is below deterministic trust: "
            f"{trust.provenance.value}/{trust.level.value}"
        )


def _require_runtime_attestation(
    record: Mapping[str, Any], label: str, *, authority_kind: str
) -> None:
    details = _record_mapping(record.get("details"), f"{label}.details")
    attestation = details.get(RUNTIME_ATTESTATION_KEY)
    expected = {
        "schema_version": 1,
        "authority": RUNTIME_VERIFICATION_AUTHORITY,
        "authority_kind": authority_kind,
        "task_id": record.get("task_id"),
        "check_id": record.get("check_id"),
        "policy": record.get("policy"),
        "provider": record.get("provider"),
        "provider_thread_id": record.get("provider_thread_id"),
        "provider_turn_id": record.get("provider_turn_id"),
    }
    if not isinstance(attestation, Mapping) or dict(attestation) != expected:
        raise SkillPackError(
            f"{label} is not runtime-attested by the observed "
            f"{authority_kind.replace('_', ' ')}"
        )


def _record_array(value: Any, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise SkillPackError(f"{label} must be an array of Project Memory records")
    return tuple(value)


def _record_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SkillPackError(f"{label} must be a Project Memory object")
    return value


def _semantic_version(value: Any, label: str) -> str:
    text = _required_string(value, label)
    match = _SEMVER.fullmatch(text)
    if match is None:
        raise SkillPackError(f"{label} must be an exact semantic version (MAJOR.MINOR.PATCH)")
    prerelease = match.group(4)
    if prerelease is not None and any(
        part.isdigit() and len(part) > 1 and part.startswith("0")
        for part in prerelease.split(".")
    ):
        raise SkillPackError(
            f"{label} must be valid Semantic Versioning; numeric prerelease "
            "identifiers cannot contain leading zeroes"
        )
    return text


def _identifier(value: Any, label: str) -> str:
    text = _required_string(value, label)
    if _IDENTIFIER.fullmatch(text) is None:
        raise SkillPackError(f"{label} must be a stable identifier")
    return text


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillPackError(f"{label} must be a non-empty string")
    return value.strip()


def _choice(value: Any, allowed: Iterable[str], label: str) -> str:
    text = _required_string(value, label)
    choices = frozenset(allowed)
    if text not in choices:
        raise SkillPackError(f"{label} must be one of {sorted(choices)}")
    return text


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise SkillPackError(f"{label} must be an array")
    return value


def _nonempty_array(value: Any, label: str) -> list[Any]:
    items = _array(value, label)
    if not items:
        raise SkillPackError(f"{label} must not be empty")
    return items


def _strings(value: Any, label: str) -> tuple[str, ...]:
    result = tuple(
        _required_string(item, f"{label}[{index}]")
        for index, item in enumerate(_array(value, label))
    )
    _unique(result, label)
    return result


def _nonempty_strings(value: Any, label: str) -> tuple[str, ...]:
    result = _strings(value, label)
    if not result:
        raise SkillPackError(f"{label} must not be empty")
    return result


def _unique(values: Iterable[Any], label: str) -> None:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise SkillPackError(f"duplicate {label}: {value!r}")
        seen.add(value)


def _reject_unknown(raw: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SkillPackError(f"{label} has unknown fields: {', '.join(unknown)}")
