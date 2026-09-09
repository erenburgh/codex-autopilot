from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from codex_autopilot.memory import MemoryError, MemoryValidationError, ProjectMemory, probe_sqlite_fts5
from codex_autopilot.memory_mcp import TOOLS


def project_memory() -> tuple[Path, ProjectMemory]:
    root = Path(tempfile.mkdtemp(prefix="codex-autopilot-memory-test-"))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text("Inventory persistence uses SQLite.\n", encoding="utf-8")
    memory = ProjectMemory(root)
    memory.initialize()
    return root, memory


def file_evidence(memory: ProjectMemory, milestone: str = "M1", summary: str = "Inspected implementation") -> dict:
    return memory.record_evidence(kind="file", summary=summary, path="README.md", milestone_id=milestone, created_by="worker-1")


class MemoryTests(unittest.TestCase):
    def test_sqlite_initialization_and_fts5(self):
        root, memory = project_memory()
        self.assertTrue(memory.path.is_file())
        self.assertEqual(memory.integrity_check(), "ok")
        self.assertEqual(probe_sqlite_fts5(root)["fts5"], "available")

    def test_truth_requires_existing_non_migration_evidence(self):
        _root, memory = project_memory()
        with self.assertRaisesRegex(MemoryValidationError, "NO EVIDENCE"):
            memory.record_verified_fact(statement="Uses SQLite", evidence_ids=[], verification_method="inspection", created_by="worker-1")
        migrated = memory.record_evidence(kind="migration", summary="Old handoff said SQLite", created_by="migration")
        with self.assertRaisesRegex(MemoryValidationError, "cannot support Truth"):
            memory.record_verified_fact(statement="Uses SQLite", evidence_ids=[migrated["id"]], verification_method="old prose", created_by="migration")

    def test_verified_fact_has_evidence_trail_and_content_hash(self):
        _root, memory = project_memory()
        evidence = file_evidence(memory)
        fact = memory.record_verified_fact(statement="Inventory persistence uses SQLite.", evidence_ids=[evidence["id"]], verification_method="code inspection", created_by="worker-1")
        loaded = memory.get_record(fact["id"])
        self.assertEqual(loaded["category"], "truth")
        self.assertEqual(loaded["status"], "verified")
        self.assertEqual(loaded["evidence"][0]["id"], evidence["id"])
        self.assertEqual(len(loaded["evidence"][0]["content_sha256"]), 64)

    def test_observation_never_promotes_itself(self):
        _root, memory = project_memory()
        observation = memory.add_observation(statement="Component X probably uses Y.", created_by="worker-3", confidence="medium")
        loaded = memory.get_record(observation["id"])
        self.assertEqual((loaded["category"], loaded["status"], loaded["origin"]), ("observation", "unverified", "agent"))
        self.assertEqual(memory.list_records(categories=["truth"], limit=8).records, [])

    def test_decision_provenance_separates_user_and_agent(self):
        _root, memory = project_memory()
        agent = memory.propose_decision(statement="Use SQLite", origin="agent", created_by="worker-2")
        user = memory.propose_decision(statement="Run without a backend", origin="user", status="accepted", created_by="user")
        self.assertEqual(memory.get_record(agent["id"])["status"], "proposed")
        self.assertEqual(memory.get_record(user["id"])["origin"], "user")
        with self.assertRaisesRegex(MemoryValidationError, "must begin as proposed"):
            memory.propose_decision(statement="Agent accepted itself", origin="agent", status="accepted", created_by="worker")

    def test_constraints_and_questions_have_distinct_lifecycles(self):
        _root, memory = project_memory()
        constraint = memory.add_constraint(statement="No backend", origin="user", created_by="user")
        question = memory.open_question(question="Does replication require authority?", needed_for="M23", created_by="worker")
        resolved = memory.resolve_question(question["id"], actor="worker-23", reason="Verified separately")
        self.assertEqual(memory.get_record(constraint["id"])["status"], "active")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(memory.list_records(categories=["truth"], limit=8).records, [])

    def test_contradictory_evidence_opens_conflict_without_overwrite(self):
        _root, memory = project_memory()
        first = file_evidence(memory)
        fact = memory.record_verified_fact(statement="Persistence uses SQLite", evidence_ids=[first["id"]], verification_method="inspection", created_by="worker-1")
        second = memory.record_evidence(kind="test", summary="Runtime probe reported Postgres", command="python probe.py", result="postgres", exit_code=0, milestone_id="M15", created_by="worker-15")
        attached = memory.attach_evidence(fact["id"], second["id"], relation="contradicts", actor="worker-15")
        conflict = attached["conflict"]
        self.assertEqual(memory.get_record(fact["id"])["status"], "disputed")
        self.assertEqual(conflict["status"], "needs_review")
        self.assertEqual(memory.get_record(fact["id"])["statement"], "Persistence uses SQLite")
        resolved = memory.resolve_conflict(conflict["id"], outcome="reject_incoming", resolution="The probe used a stale fixture.", actor="worker-16")
        self.assertEqual(resolved["status"], "resolved")
        self.assertGreaterEqual(len(resolved["history"]), 2)
        self.assertEqual(memory.get_record(fact["id"])["status"], "verified")

    def test_user_correction_preserves_actual_truth_and_supersedes_agent_decision(self):
        _root, memory = project_memory()
        evidence = file_evidence(memory)
        fact = memory.record_verified_fact(statement="Current code uses SQLite", evidence_ids=[evidence["id"]], verification_method="inspection", created_by="worker")
        old_decision = memory.propose_decision(statement="Continue with SQLite", origin="agent", created_by="worker")
        correction = memory.apply_user_correction(statement="Desired state: do not use SQLite", related_ids=[fact["id"], old_decision["id"]])
        self.assertEqual(correction["decision"]["origin"], "user")
        self.assertEqual(memory.get_record(old_decision["id"])["status"], "superseded")
        self.assertEqual(memory.get_record(fact["id"])["status"], "disputed")
        self.assertEqual(len(correction["conflicts"]), 1)

    def test_fts_search_is_bounded_and_paginated(self):
        _root, memory = project_memory()
        for index in range(7):
            memory.add_observation(statement=f"Inventory replication observation {index}", created_by="worker")
        first = memory.search(query="inventory", categories=["observation"], limit=3)
        second = memory.search(query="inventory", categories=["observation"], limit=3, cursor=first.next_cursor)
        self.assertEqual(len(first.records), 3)
        self.assertEqual(len(second.records), 3)
        self.assertIsNotNone(first.next_cursor)
        self.assertTrue(set(item["id"] for item in first.records).isdisjoint(item["id"] for item in second.records))
        with self.assertRaises(MemoryValidationError):
            memory.search(query="inventory", limit=21)

    def test_path_traversal_and_symlink_escape_are_rejected(self):
        root, memory = project_memory()
        fd, raw = tempfile.mkstemp(prefix="outside-evidence-")
        os.close(fd)
        outside = Path(raw)
        with self.assertRaisesRegex(MemoryValidationError, "escapes"):
            memory.record_evidence(kind="file", summary="outside", path=str(outside), created_by="worker")
        link = root / "outside-link"
        link.symlink_to(outside)
        with self.assertRaisesRegex(MemoryValidationError, "escapes"):
            memory.record_evidence(kind="file", summary="symlink", path="outside-link", created_by="worker")

    def test_database_is_bound_to_one_project_root(self):
        root, memory = project_memory()
        memory.backup()
        other = Path(tempfile.mkdtemp(prefix="codex-autopilot-memory-other-"))
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        (other / ".codex-autopilot").mkdir()
        shutil.copy2(root / ".codex-autopilot/memory-backups/latest.sqlite3", other / ".codex-autopilot/memory.sqlite3")
        with self.assertRaisesRegex(MemoryValidationError, "different project"):
            ProjectMemory(other).initialize()

    def test_memory_recovers_from_last_verified_milestone_backup(self):
        _root, memory = project_memory()
        file_evidence(memory, "M1")
        memory.mark_milestone_complete(milestone_id="M1", run_id="run", worker_sequence=1)
        memory.path.with_name(memory.path.name + "-wal").unlink(missing_ok=True)
        memory.path.with_name(memory.path.name + "-shm").unlink(missing_ok=True)
        memory.path.write_bytes(b"not a sqlite database")
        self.assertEqual(memory.ensure_healthy(recover=True), "recovered")
        self.assertEqual(memory.integrity_check(), "ok")
        self.assertTrue(list(memory.state_dir.glob("memory-corrupt-*.sqlite3")))

    def test_twenty_milestone_observation_to_truth_boundary(self):
        _root, memory = project_memory()
        observation = None
        fact = None
        for milestone in range(1, 21):
            if milestone == 3:
                observation = memory.add_observation(statement="Component X probably uses Y.", created_by="worker-3")
            if milestone == 12:
                seen = memory.search(query="Component X uses Y", categories=["observation", "truth"], limit=8).records
                self.assertEqual([(item["category"], item["status"]) for item in seen], [("observation", "unverified")])
            if milestone == 15:
                evidence = file_evidence(memory, "M15", "M15 inspected actual implementation")
                fact = memory.record_verified_fact(statement="Component X uses Y.", evidence_ids=[evidence["id"]], verification_method="file inspection", created_by="worker-15")
        self.assertIsNotNone(observation)
        self.assertEqual(memory.get_record(observation["id"])["status"], "unverified")
        self.assertEqual(memory.get_record(fact["id"])["status"], "verified")

    def test_summary_of_summary_drift_does_not_mutate_canonical_fact(self):
        root, memory = project_memory()
        evidence = file_evidence(memory)
        fact = memory.record_verified_fact(statement="Canonical value is alpha.", evidence_ids=[evidence["id"]], verification_method="inspection", created_by="worker-1")
        handoff = root / ".codex-autopilot/HANDOFF.md"
        for generation in range(1, 21):
            handoff.write_text(f"Generation {generation} paraphrase says value might be omega.\n", encoding="utf-8")
        self.assertEqual(memory.get_record(fact["id"])["statement"], "Canonical value is alpha.")

    def test_mcp_contract_is_allowlisted_and_has_no_raw_sql_or_shell_tool(self):
        names = {item["name"] for item in TOOLS}
        self.assertLessEqual(len(names), 14)
        self.assertFalse(any("sql" in name or "shell" in name or "exec" in name for name in names))
        self.assertEqual(names, {"memory"})
        actions = {
            branch["properties"]["operation"]["const"]
            for branch in TOOLS[0]["inputSchema"]["oneOf"]
        }
        self.assertTrue({"search", "record_evidence", "record_verified_fact", "conflict"}.issubset(actions))


if __name__ == "__main__":
    unittest.main()
