from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping


class EvidenceProvenance(str, Enum):
    """Origin class retained with every evidence object.

    ``HUMAN_INPUT`` is deliberately distinct from ``HUMAN_VERIFIED``.  Text
    attributed to a person is not the same thing as a human judgment backed by
    verification, and callers cannot raise it to the latter by choosing a
    label.
    """

    EXTERNAL_TEXT = "external_text"
    LEGACY_MIGRATION = "legacy_migration"
    HUMAN_INPUT = "human_input"
    DETERMINISTIC_TOOL_OUTPUT = "deterministic_tool_output"
    HUMAN_VERIFIED = "human_verified"


class TrustLevel(str, Enum):
    UNVERIFIED = "unverified"
    DETERMINISTIC = "deterministic"
    HUMAN_VERIFIED = "human_verified"


class PromotionTarget(str, Enum):
    PROJECT_MEMORY_TRUTH = "project_memory_truth"
    TRUSTED_SKILL = "trusted_skill"


_TRUST_RANK = {
    TrustLevel.UNVERIFIED: 0,
    TrustLevel.DETERMINISTIC: 1,
    TrustLevel.HUMAN_VERIFIED: 2,
}


class TrustBoundaryViolation(ValueError):
    """A proposed promotion did not meet the configured trust threshold."""


@dataclass(frozen=True, slots=True)
class EvidenceTrust:
    provenance: EvidenceProvenance
    level: TrustLevel
    source_kind: str

    def __post_init__(self) -> None:
        expected = {
            EvidenceProvenance.EXTERNAL_TEXT: TrustLevel.UNVERIFIED,
            EvidenceProvenance.LEGACY_MIGRATION: TrustLevel.UNVERIFIED,
            EvidenceProvenance.HUMAN_INPUT: TrustLevel.UNVERIFIED,
            EvidenceProvenance.DETERMINISTIC_TOOL_OUTPUT: TrustLevel.DETERMINISTIC,
            EvidenceProvenance.HUMAN_VERIFIED: TrustLevel.HUMAN_VERIFIED,
        }[self.provenance]
        if self.level is not expected:
            raise TrustBoundaryViolation(
                "R18: evidence provenance and trust level disagree: "
                f"{self.provenance.value} requires {expected.value}, got "
                f"{self.level.value}"
            )
        if not str(self.source_kind).strip():
            raise TrustBoundaryViolation("R18: evidence source_kind is required")

    @classmethod
    def from_storage(cls, evidence: Mapping[str, Any]) -> EvidenceTrust:
        try:
            return cls(
                provenance=EvidenceProvenance(str(evidence["provenance"])),
                level=TrustLevel(str(evidence["trust_level"])),
                source_kind=str(evidence["kind"]),
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise TrustBoundaryViolation(
                "R18: evidence is missing a valid provenance/trust classification"
            ) from exc

    @classmethod
    def human_verified(cls, *, source_kind: str = "human_verification") -> EvidenceTrust:
        """Build the highest tier for an authoritative verification path.

        Project Memory ingestion does not expose a free-form switch for this
        tier.  A future human-verification ledger may construct it only after
        establishing that verification independently.
        """

        return cls(
            provenance=EvidenceProvenance.HUMAN_VERIFIED,
            level=TrustLevel.HUMAN_VERIFIED,
            source_kind=source_kind,
        )

    def to_storage(self) -> dict[str, str]:
        return {
            "provenance": self.provenance.value,
            "trust_level": self.level.value,
        }


@dataclass(frozen=True, slots=True)
class PromotionAssessment:
    target: PromotionTarget
    allowed: bool
    required_level: TrustLevel
    observed_level: TrustLevel | None
    reasons: tuple[str, ...]
    outcome_evidence_required: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target.value,
            "allowed": self.allowed,
            "required_level": self.required_level.value,
            "observed_level": (
                self.observed_level.value if self.observed_level is not None else None
            ),
            "reasons": list(self.reasons),
            "outcome_evidence_required": self.outcome_evidence_required,
        }


class TrustPolicy:
    """Central promotion policy for external-input trust boundaries.

    The policy is intentionally strict across a supporting evidence set: one
    untrusted item cannot hitchhike beside one deterministic result and become
    a supporting premise.  It may still be retained as advisory or
    contradictory material outside the promotion.
    """

    TRUTH_THRESHOLD = TrustLevel.DETERMINISTIC
    SKILL_THRESHOLD = TrustLevel.HUMAN_VERIFIED
    OUTCOME_THRESHOLD = TrustLevel.DETERMINISTIC

    _UNVERIFIED_KINDS = frozenset({"external", "migration", "user_instruction"})
    _DETERMINISTIC_KINDS = frozenset(
        {
            "artifact",
            "build",
            "environment_probe",
            "file",
            "git",
            "screenshot",
            "test",
            "tool",
        }
    )

    def classify_evidence(
        self,
        kind: str,
        *,
        provider: str | None = None,
        error_type: type[Exception] = TrustBoundaryViolation,
    ) -> EvidenceTrust:
        normalized = str(kind or "").strip()
        if normalized == "external":
            if not str(provider or "").strip():
                raise error_type(
                    "R18: external evidence requires provenance naming its provider"
                )
            return EvidenceTrust(
                EvidenceProvenance.EXTERNAL_TEXT,
                TrustLevel.UNVERIFIED,
                normalized,
            )
        if normalized == "migration":
            return EvidenceTrust(
                EvidenceProvenance.LEGACY_MIGRATION,
                TrustLevel.UNVERIFIED,
                normalized,
            )
        if normalized == "user_instruction":
            return EvidenceTrust(
                EvidenceProvenance.HUMAN_INPUT,
                TrustLevel.UNVERIFIED,
                normalized,
            )
        if normalized in self._DETERMINISTIC_KINDS:
            return EvidenceTrust(
                EvidenceProvenance.DETERMINISTIC_TOOL_OUTPUT,
                TrustLevel.DETERMINISTIC,
                normalized,
            )
        raise error_type(
            f"R18: evidence kind {normalized!r} has no trust classification"
        )

    def truth_evidence_kinds(self, kinds: Iterable[str]) -> frozenset[str]:
        admitted: set[str] = set()
        for kind in kinds:
            trust = self.classify_evidence(
                kind,
                provider="classification-only" if kind == "external" else None,
            )
            if self.level_meets(trust.level, self.TRUTH_THRESHOLD):
                admitted.add(kind)
        return frozenset(admitted)

    def level_meets(self, level: TrustLevel, threshold: TrustLevel) -> bool:
        return _TRUST_RANK[level] >= _TRUST_RANK[threshold]

    def assess_promotion(
        self,
        target: PromotionTarget,
        evidence: Iterable[EvidenceTrust],
        *,
        outcome_evidence: Iterable[EvidenceTrust] = (),
    ) -> PromotionAssessment:
        items = tuple(evidence)
        outcomes = tuple(outcome_evidence)
        required = (
            self.TRUTH_THRESHOLD
            if target is PromotionTarget.PROJECT_MEMORY_TRUTH
            else self.SKILL_THRESHOLD
        )
        reasons: list[str] = []
        observed = self._minimum_level(items)
        if not items:
            reasons.append("NO EVIDENCE -> NO TRUTH: promotion requires evidence")
        else:
            below = [item for item in items if not self.level_meets(item.level, required)]
            if below:
                labels = ", ".join(
                    f"{item.source_kind}:{item.provenance.value}/{item.level.value}"
                    for item in below
                )
                reasons.append(
                    f"supporting evidence is below {required.value}: {labels}"
                )

        outcome_required = target is PromotionTarget.TRUSTED_SKILL
        if outcome_required:
            if not outcomes:
                reasons.append(
                    "trusted Skill promotion requires evidence of improved outcome"
                )
            else:
                weak_outcomes = [
                    item
                    for item in outcomes
                    if not self.level_meets(item.level, self.OUTCOME_THRESHOLD)
                ]
                if weak_outcomes:
                    labels = ", ".join(
                        f"{item.source_kind}:{item.provenance.value}/{item.level.value}"
                        for item in weak_outcomes
                    )
                    reasons.append(
                        "outcome evidence is below deterministic: " + labels
                    )

        return PromotionAssessment(
            target=target,
            allowed=not reasons,
            required_level=required,
            observed_level=observed,
            reasons=tuple(reasons),
            outcome_evidence_required=outcome_required,
        )

    def require_promotion(
        self,
        target: PromotionTarget,
        evidence: Iterable[EvidenceTrust],
        *,
        outcome_evidence: Iterable[EvidenceTrust] = (),
        error_type: type[Exception] = TrustBoundaryViolation,
        message_prefix: str = "R18: promotion rejected",
    ) -> PromotionAssessment:
        assessment = self.assess_promotion(
            target,
            evidence,
            outcome_evidence=outcome_evidence,
        )
        if not assessment.allowed:
            raise error_type(
                f"{message_prefix} ({target.value}): "
                + "; ".join(assessment.reasons)
            )
        return assessment

    def require_truth_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        error_type: type[Exception] = TrustBoundaryViolation,
        message_prefix: str = "R18: evidence cannot support Truth",
    ) -> PromotionAssessment:
        try:
            evidence = tuple(EvidenceTrust.from_storage(row) for row in rows)
        except TrustBoundaryViolation as exc:
            raise error_type(str(exc)) from exc
        return self.require_promotion(
            PromotionTarget.PROJECT_MEMORY_TRUTH,
            evidence,
            error_type=error_type,
            message_prefix=message_prefix,
        )

    def row_is_below_truth(
        self,
        row: Mapping[str, Any],
        *,
        error_type: type[Exception] = TrustBoundaryViolation,
    ) -> bool:
        try:
            level = EvidenceTrust.from_storage(row).level
        except TrustBoundaryViolation as exc:
            raise error_type(str(exc)) from exc
        return not self.level_meets(level, self.TRUTH_THRESHOLD)

    def any_row_is_below_truth(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        error_type: type[Exception] = TrustBoundaryViolation,
    ) -> bool:
        return any(
            self.row_is_below_truth(row, error_type=error_type) for row in rows
        )

    def migrate_memory_schema(self, db: Any) -> None:
        """Add and conservatively backfill trust columns on schema 1/2."""

        columns = {
            str(row["name"])
            for row in db.execute("PRAGMA table_info(evidence)").fetchall()
        }
        added = False
        if "provenance" not in columns:
            db.execute(
                "ALTER TABLE evidence ADD COLUMN provenance TEXT NOT NULL "
                "DEFAULT 'deterministic_tool_output'"
            )
            added = True
        if "trust_level" not in columns:
            db.execute(
                "ALTER TABLE evidence ADD COLUMN trust_level TEXT NOT NULL "
                "DEFAULT 'deterministic'"
            )
            added = True
        if added:
            db.execute(
                """UPDATE evidence SET
                    provenance=CASE kind
                        WHEN 'external' THEN 'external_text'
                        WHEN 'migration' THEN 'legacy_migration'
                        WHEN 'user_instruction' THEN 'human_input'
                        ELSE 'deterministic_tool_output'
                    END,
                    trust_level=CASE
                        WHEN kind IN ('external','migration','user_instruction')
                            THEN 'unverified'
                        ELSE 'deterministic'
                    END"""
            )

    @staticmethod
    def _minimum_level(items: tuple[EvidenceTrust, ...]) -> TrustLevel | None:
        if not items:
            return None
        return min((item.level for item in items), key=_TRUST_RANK.__getitem__)


TRUST_POLICY = TrustPolicy()
