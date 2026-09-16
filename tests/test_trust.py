from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_autopilot.memory import MemoryValidationError, ProjectMemory, SCHEMA_VERSION
from codex_autopilot.memory_mcp import MemoryMcpServer
from codex_autopilot.trust import (
    EvidenceProvenance,
    EvidenceTrust,
    PromotionTarget,
    TRUST_POLICY,
    TrustBoundaryViolation,
    TrustLevel,
)


class TrustPolicyTests(unittest.TestCase):
    def test_section_12_hierarchy_is_strict(self) -> None:
        external = TRUST_POLICY.classify_evidence(
            "external", provider="github.example/issue/12"
        )
        deterministic = TRUST_POLICY.classify_evidence("test")
        human_verified = EvidenceTrust.human_verified()

        self.assertFalse(
            TRUST_POLICY.level_meets(external.level, deterministic.level)
        )
        self.assertFalse(
            TRUST_POLICY.level_meets(deterministic.level, human_verified.level)
        )
        self.assertTrue(
            TRUST_POLICY.level_meets(human_verified.level, deterministic.level)
        )

    def test_external_provenance_cannot_be_self_upgraded(self) -> None:
        with self.assertRaisesRegex(TrustBoundaryViolation, "provenance and trust"):
            EvidenceTrust(
                provenance=EvidenceProvenance.EXTERNAL_TEXT,
                level=TrustLevel.HUMAN_VERIFIED,
                source_kind="external",
            )

    def test_truth_requires_deterministic_evidence(self) -> None:
        external = TRUST_POLICY.classify_evidence(
            "external", provider="figma.example/comment/7"
        )
        deterministic = TRUST_POLICY.classify_evidence("test")

        denied = TRUST_POLICY.assess_promotion(
            PromotionTarget.PROJECT_MEMORY_TRUTH,
            [external],
        )
        allowed = TRUST_POLICY.assess_promotion(
            PromotionTarget.PROJECT_MEMORY_TRUTH,
            [deterministic],
        )
        self.assertFalse(denied.allowed)
        self.assertTrue(allowed.allowed)

    def test_weak_evidence_cannot_hitchhike_beside_a_strong_item(self) -> None:
        external = TRUST_POLICY.classify_evidence(
            "external", provider="wiki.example/page"
        )
        deterministic = TRUST_POLICY.classify_evidence("file")
        decision = TRUST_POLICY.assess_promotion(
            PromotionTarget.PROJECT_MEMORY_TRUTH,
            [external, deterministic],
        )
        self.assertFalse(decision.allowed)
        self.assertIn("external_text", " ".join(decision.reasons))

    def test_skill_threshold_is_higher_and_requires_outcome_evidence(self) -> None:
        deterministic = TRUST_POLICY.classify_evidence("test")
        human_verified = EvidenceTrust.human_verified()

        self.assertFalse(
            TRUST_POLICY.assess_promotion(
                PromotionTarget.TRUSTED_SKILL,
                [deterministic],
                outcome_evidence=[deterministic],
            ).allowed
        )
        self.assertFalse(
            TRUST_POLICY.assess_promotion(
                PromotionTarget.TRUSTED_SKILL,
                [human_verified],
            ).allowed
        )
        accepted = TRUST_POLICY.assess_promotion(
            PromotionTarget.TRUSTED_SKILL,
            [human_verified],
            outcome_evidence=[deterministic],
        )
        self.assertTrue(accepted.allowed)
        self.assertTrue(accepted.outcome_evidence_required)


class ProjectMemoryTrustIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        (self.root / "verified.txt").write_text("verified\n", encoding="utf-8")
        self.memory = ProjectMemory(self.root)

    def test_each_evidence_object_stores_provenance_and_trust_level(self) -> None:
        external = self.memory.record_evidence(
            kind="external",
            summary="Text copied from an external issue.",
            provider="github.example/issue/12",
            created_by="worker",
        )
        deterministic = self.memory.record_evidence(
            kind="file",
            summary="Inspected repository file.",
            path="verified.txt",
            created_by="worker",
        )

        self.assertEqual(external["provenance"], "external_text")
        self.assertEqual(external["trust_level"], "unverified")
        self.assertEqual(
            deterministic["provenance"], "deterministic_tool_output"
        )
        self.assertEqual(deterministic["trust_level"], "deterministic")

    def test_external_plus_deterministic_still_cannot_support_truth(self) -> None:
        external = self.memory.record_evidence(
            kind="external",
            summary="External claim.",
            provider="docs.example/claim",
            created_by="worker",
        )
        deterministic = self.memory.record_evidence(
            kind="file",
            summary="Repository observation.",
            path="verified.txt",
            created_by="worker",
        )
        with self.assertRaisesRegex(MemoryValidationError, "R18"):
            self.memory.record_verified_fact(
                statement="The external claim is now a fact.",
                evidence_ids=[external["id"], deterministic["id"]],
                verification_method="mixed support",
                created_by="worker",
            )

    def test_human_text_without_verification_is_not_truth_evidence(self) -> None:
        instruction = self.memory.record_evidence(
            kind="user_instruction",
            summary="A requested desired state, not an observed fact.",
            user_instruction="Prefer blue.",
            created_by="worker",
        )
        self.assertEqual(instruction["provenance"], "human_input")
        self.assertEqual(instruction["trust_level"], "unverified")
        with self.assertRaisesRegex(MemoryValidationError, "trust threshold"):
            self.memory.record_verified_fact(
                statement="The product is blue.",
                evidence_ids=[instruction["id"]],
                verification_method="quoted request",
                created_by="worker",
            )

    def test_mcp_caller_cannot_supply_its_own_trust_level(self) -> None:
        server = MemoryMcpServer(self.root)
        with self.assertRaisesRegex(MemoryValidationError, "unknown argument"):
            server.call(
                "memory",
                {
                    "operation": "record_evidence",
                    "kind": "external",
                    "summary": "Attempted relabel.",
                    "provider": "external.example",
                    "created_by": "worker",
                    "provenance": "human_verified",
                    "trust_level": "human_verified",
                },
            )

    def test_schema_two_is_additively_backfilled_without_elevation(self) -> None:
        state = self.root / ".codex-autopilot"
        state.mkdir()
        database = state / "memory.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_meta(key,value) VALUES('schema_version','2');
                INSERT INTO schema_meta(key,value) VALUES('project_root','REPLACE_ROOT');
                CREATE TABLE evidence (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    provider TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO evidence(id,kind,summary,created_by,provider,created_at)
                VALUES('EVID-001','external','legacy external','worker','wiki','now');
                """.replace("REPLACE_ROOT", str(self.root).replace("'", "''"))
            )
            connection.commit()
        finally:
            connection.close()

        migrated = ProjectMemory(self.root)
        migrated.initialize()
        evidence = migrated.get_evidence("EVID-001")
        self.assertEqual(SCHEMA_VERSION, 3)
        self.assertEqual(evidence["provenance"], "external_text")
        self.assertEqual(evidence["trust_level"], "unverified")
        with sqlite3.connect(database) as check:
            version = check.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
        self.assertEqual(version, "3")


if __name__ == "__main__":
    unittest.main()

