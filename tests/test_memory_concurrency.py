from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_autopilot.memory import (
    MemoryBusyError,
    MemoryValidationError,
    ProjectMemory,
    SCHEMA_VERSION,
    utc_now,
)
from codex_autopilot.memory_mcp import MemoryMcpServer


def concurrent_project_memory() -> tuple[Path, ProjectMemory]:
    root = Path(tempfile.mkdtemp(prefix="codex-autopilot-memory-concurrency-"))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text(
        "Project Memory concurrency fixture.\n", encoding="utf-8"
    )
    memory = ProjectMemory(root)
    memory.initialize()
    return root, memory


def _await_file_barrier(root: Path, participant: str) -> None:
    barrier_dir = root / ".codex-autopilot" / "stress-barrier"
    barrier_dir.mkdir(parents=True, exist_ok=True)
    (barrier_dir / f"ready-{participant}").write_text("ready\n", encoding="utf-8")
    start = barrier_dir / "start"
    deadline = time.monotonic() + 10
    while not start.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("concurrent memory start barrier timed out")
        time.sleep(0.01)


def _mcp_writer(root: str, index: int, truth_id: str) -> dict[str, str]:
    _await_file_barrier(Path(root), f"writer-{index}")
    server = MemoryMcpServer(Path(root))
    actor = f"worker-{index}"
    evidence = server.call(
        "memory",
        {
            "operation": "record_evidence",
            "kind": "test",
            "summary": f"Concurrent check {index}",
            "milestone_id": "M9",
            "role": f"stress-check-{index}",
            "command": f"stress-check-{index}",
            "result": "PASS",
            "exit_code": 0,
            "created_by": actor,
            "provider": "stress-suite",
            "provider_thread_id": f"thread-{index}",
        },
    )
    observation = server.call(
        "memory",
        {
            "operation": "add_observation",
            "statement": f"Concurrent observation {index}",
            "created_by": actor,
            "confidence": "high",
            "provider": "stress-suite",
            "provider_thread_id": f"thread-{index}",
        },
    )
    decision = server.call(
        "memory",
        {
            "operation": "propose_decision",
            "statement": f"Concurrent decision {index}",
            "origin": "agent",
            "created_by": actor,
            "evidence_ids": [evidence["id"]],
            "provider": "stress-suite",
            "provider_thread_id": f"thread-{index}",
        },
    )
    verification = server.call(
        "memory",
        {
            "operation": "record_verification_result",
            "task_id": "M9",
            "check_id": f"stress-check-{index}",
            "policy": "deterministic",
            "verdict": "PASS",
            "summary": f"Concurrent verification {index} passed",
            "evidence_ids": [evidence["id"]],
            "created_by": actor,
            "provider": "stress-suite",
            "provider_thread_id": f"thread-{index}",
            "provider_turn_id": f"turn-{index}",
            "details": {"worker": index},
        },
    )
    attached = server.call(
        "memory",
        {
            "operation": "attach_evidence",
            "record_id": truth_id,
            "evidence_id": evidence["id"],
            "relation": "contradicts",
            "actor": actor,
        },
    )
    return {
        "evidence": str(evidence["id"]),
        "observation": str(observation["id"]),
        "decision": str(decision["id"]),
        "verification": str(verification["id"]),
        "conflict": str(attached["conflict"]["id"]),
    }


def _snapshotter(root: str, iterations: int) -> list[str]:
    _await_file_barrier(Path(root), "snapshotter")
    memory = ProjectMemory(Path(root))
    paths: list[str] = []
    for index in range(iterations):
        memory.render_views()
        target = memory.state_dir / "memory-backups" / f"stress-{index}.sqlite3"
        paths.append(str(memory.backup(target)))
    return paths


def _crash_mid_transaction(root: str) -> None:
    memory = ProjectMemory(Path(root))
    memory.initialize()
    with memory._connect(write=True) as db:
        evidence_id = memory._next_id(db, "evidence")
        db.execute(
            """INSERT INTO evidence(id,kind,summary,created_by,created_at)
               VALUES(?,?,?,?,?)""",
            (evidence_id, "test", "must roll back", "crashed-worker", utc_now()),
        )
        os._exit(91)


class ConcurrentMemoryTests(unittest.TestCase):
    def test_concurrent_mcp_writes_snapshots_and_generated_views_are_consistent(self):
        root, memory = concurrent_project_memory()
        seed = memory.record_evidence(
            kind="file",
            summary="Seed truth from the fixture file",
            path="README.md",
            milestone_id="M9",
            role="seed-check",
            created_by="stress-suite",
        )
        truth = memory.record_verified_fact(
            statement="The concurrency fixture exists.",
            evidence_ids=[seed["id"]],
            verification_method="file inspection",
            created_by="stress-suite",
        )

        worker_count = 8
        context = multiprocessing.get_context("spawn")
        barrier_dir = memory.state_dir / "stress-barrier"
        with ProcessPoolExecutor(max_workers=6, mp_context=context) as pool:
            snapshot_future = pool.submit(_snapshotter, str(root), 4)
            writer_futures = [
                pool.submit(_mcp_writer, str(root), index, truth["id"])
                for index in range(worker_count)
            ]
            deadline = time.monotonic() + 10
            while len(list(barrier_dir.glob("ready-*"))) < 6:
                if time.monotonic() >= deadline:
                    self.fail("concurrent workers did not reach the file barrier")
                time.sleep(0.01)
            (barrier_dir / "start").write_text("start\n", encoding="utf-8")
            rows = [future.result(timeout=30) for future in writer_futures]
            snapshot_paths = snapshot_future.result(timeout=30)

        for key in ("evidence", "observation", "decision", "verification", "conflict"):
            identifiers = [row[key] for row in rows]
            self.assertEqual(len(identifiers), len(set(identifiers)), key)

        summary = memory.export_summary()
        self.assertEqual(summary["evidence"], worker_count + 1)
        self.assertEqual(summary["records"].get("truth"), 1)
        self.assertEqual(summary["records"].get("observation"), worker_count)
        self.assertEqual(summary["records"].get("decision"), worker_count)
        self.assertEqual(summary["verification_results"], worker_count)
        self.assertEqual(summary["open_conflicts"], worker_count)
        self.assertEqual(memory.integrity_check(), "ok")

        loaded_truth = memory.get_record(truth["id"])
        self.assertEqual(loaded_truth["status"], "disputed")
        self.assertEqual(len(loaded_truth["conflicts"]), worker_count)
        for row in rows:
            verification = memory.get_verification_result(row["verification"])
            self.assertEqual(
                [item["id"] for item in verification["evidence"]], [row["evidence"]]
            )
            self.assertEqual(len(memory.get_conflict(row["conflict"])["history"]), 1)

        memory.render_views()
        state_view = (memory.state_dir / "PROJECT_STATE.md").read_text(encoding="utf-8")
        decisions_view = (memory.state_dir / "DECISIONS.md").read_text(encoding="utf-8")
        self.assertIn(f"- decision: {worker_count}", state_view)
        self.assertIn(f"- verification results: {worker_count}", state_view)
        self.assertEqual(decisions_view.count("\n- DEC-"), worker_count)

        for raw_path in snapshot_paths:
            snapshot = sqlite3.connect(raw_path)
            try:
                self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(snapshot.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                snapshot.close()
        self.assertEqual(list(memory.state_dir.rglob(".*.tmp-*")), [])
        self.assertEqual(list(memory.state_dir.rglob("*.restore-*")), [])

    def test_crashed_transaction_rolls_back_and_releases_writer_lock(self):
        root, memory = concurrent_project_memory()
        first = memory.record_evidence(
            kind="test",
            summary="Committed before crash",
            command="seed",
            result="PASS",
            exit_code=0,
            created_by="stress-suite",
        )
        self.assertEqual(first["id"], "EVID-001")

        context = multiprocessing.get_context("spawn")
        worker = context.Process(target=_crash_mid_transaction, args=(str(root),))
        worker.start()
        worker.join(10)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
            self.fail("crash worker failed to exit")
        self.assertEqual(worker.exitcode, 91)

        second = memory.record_evidence(
            kind="test",
            summary="Committed after crash",
            command="post-crash",
            result="PASS",
            exit_code=0,
            created_by="stress-suite",
        )
        self.assertEqual(second["id"], "EVID-002")
        self.assertEqual(memory.export_summary()["evidence"], 2)
        self.assertEqual(memory.integrity_check(), "ok")

    def test_schema_v1_upgrades_additively_and_backup_cannot_replace_live_db(self):
        root, memory = concurrent_project_memory()
        raw = sqlite3.connect(memory.path)
        try:
            raw.execute("DROP TABLE verification_result_evidence")
            raw.execute("DROP TABLE verification_results")
            raw.execute(
                "UPDATE schema_meta SET value='1' WHERE key='schema_version'"
            )
            raw.commit()
        finally:
            raw.close()

        migrated = ProjectMemory(root)
        migrated.initialize()
        raw = sqlite3.connect(memory.path)
        try:
            self.assertEqual(
                raw.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                str(SCHEMA_VERSION),
            )
            tables = {
                row[0]
                for row in raw.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            raw.close()
        self.assertTrue(
            {"verification_results", "verification_result_evidence"} <= tables
        )
        self.assertEqual(migrated.integrity_check(), "ok")
        with self.assertRaisesRegex(MemoryValidationError, "live database"):
            migrated.backup(migrated.path)

    def test_busy_wait_is_bounded_for_process_guard_and_sqlite_writer(self):
        root, memory = concurrent_project_memory()
        held = threading.Event()
        release = threading.Event()

        def hold_process_guard() -> None:
            with memory._project_lock(exclusive=True):
                held.set()
                release.wait(2)

        holder = threading.Thread(target=hold_process_guard)
        holder.start()
        self.assertTrue(held.wait(2))
        started = time.monotonic()
        with self.assertRaisesRegex(MemoryBusyError, "40 ms"):
            with memory._project_lock(exclusive=False, timeout_ms=40):
                self.fail("contended process guard unexpectedly opened")
        self.assertLess(time.monotonic() - started, 1)
        release.set()
        holder.join(2)
        self.assertFalse(holder.is_alive())

        contender = ProjectMemory(root)
        contender.initialize()
        raw = sqlite3.connect(memory.path, isolation_level=None)
        raw.execute("BEGIN IMMEDIATE")
        try:
            with patch("codex_autopilot.memory.MEMORY_BUSY_TIMEOUT_MS", 40):
                started = time.monotonic()
                with self.assertRaisesRegex(MemoryBusyError, "40 ms"):
                    contender.record_evidence(
                        kind="test",
                        summary="Must time out",
                        command="busy",
                        result="blocked",
                        exit_code=1,
                        created_by="stress-suite",
                    )
                self.assertLess(time.monotonic() - started, 1)
        finally:
            raw.rollback()
            raw.close()
        self.assertEqual(memory.integrity_check(), "ok")

    def test_verification_ledger_is_idempotent_bounded_and_never_truth(self):
        _root, memory = concurrent_project_memory()
        evidence = memory.record_evidence(
            kind="test",
            summary="Independent reproduction",
            command="verify",
            result="PASS",
            exit_code=0,
            created_by="verifier",
        )
        first = memory.record_verification_result(
            task_id="M9",
            check_id="acceptance",
            policy="independent",
            verdict="PASS",
            summary="Accepted independently",
            evidence_ids=[evidence["id"]],
            created_by="verifier",
            provider="codex-desktop",
            provider_thread_id="thread-0",
            provider_turn_id="turn-0",
            details={"round": 1},
        )
        replay = memory.record_verification_result(
            task_id="M9",
            check_id="acceptance",
            policy="independent",
            verdict="PASS",
            summary="Accepted independently",
            evidence_ids=[evidence["id"]],
            created_by="verifier",
            provider="codex-desktop",
            provider_thread_id="thread-0",
            provider_turn_id="turn-0",
            details={"round": 1},
        )
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaisesRegex(MemoryValidationError, "different payload"):
            memory.record_verification_result(
                task_id="M9",
                check_id="acceptance",
                policy="independent",
                verdict="REVISE",
                summary="Conflicting replay",
                evidence_ids=[evidence["id"]],
                created_by="verifier",
                provider="codex-desktop",
                provider_thread_id="thread-0",
                provider_turn_id="turn-0",
            )
        migrated = memory.record_evidence(
            kind="migration",
            summary="Legacy prose is advisory only",
            created_by="migration",
        )
        with self.assertRaisesRegex(MemoryValidationError, "cannot support verification"):
            memory.record_verification_result(
                task_id="M9",
                check_id="legacy-summary",
                policy="independent",
                verdict="PASS",
                summary="Must not accept advisory material",
                evidence_ids=[migrated["id"]],
                created_by="verifier",
                provider_thread_id="legacy-thread",
                provider_turn_id="legacy-turn",
            )

        for index in range(1, 23):
            memory.record_verification_result(
                task_id="M9",
                check_id="acceptance",
                policy="independent",
                verdict="PASS",
                summary=f"Independent agreement {index}",
                evidence_ids=[evidence["id"]],
                created_by="verifier",
                provider="codex-desktop",
                provider_thread_id=f"thread-{index}",
                provider_turn_id=f"turn-{index}",
            )
        first_page = memory.list_verification_results(task_id="M9", limit=20)
        second_page = memory.list_verification_results(
            task_id="M9", limit=20, cursor=first_page.next_cursor
        )
        self.assertEqual(len(first_page.records), 20)
        self.assertEqual(len(second_page.records), 3)
        self.assertTrue(
            {item["id"] for item in first_page.records}.isdisjoint(
                item["id"] for item in second_page.records
            )
        )
        with self.assertRaises(MemoryValidationError):
            memory.list_verification_results(task_id="M9", limit=21)
        self.assertEqual(memory.list_records(categories=["truth"], limit=8).records, [])
        with self.assertRaisesRegex(MemoryValidationError, "NO EVIDENCE"):
            memory.record_verified_fact(
                statement="Independent agreement alone is Truth",
                evidence_ids=[],
                verification_method="agreement",
                created_by="verifier",
            )
        self.assertEqual(
            memory.get_evidence(evidence["id"])["verification_results"][0], first["id"]
        )

    def test_recovery_serializes_a_waiting_writer_behind_atomic_restore(self):
        root, memory = concurrent_project_memory()
        memory.record_evidence(
            kind="test",
            summary="Verified backup seed",
            command="seed",
            result="PASS",
            exit_code=0,
            milestone_id="M9",
            role="recovery-seed",
            created_by="stress-suite",
        )
        memory.mark_milestone_complete(
            milestone_id="M9", run_id="run", worker_sequence=9
        )
        backup = memory.state_dir / "memory-backups" / "latest.sqlite3"
        memory.path.with_name(memory.path.name + "-wal").unlink(missing_ok=True)
        memory.path.with_name(memory.path.name + "-shm").unlink(missing_ok=True)
        memory.path.write_bytes(b"not a sqlite database")

        restore_copy_started = threading.Event()
        allow_restore = threading.Event()
        original_copy2 = shutil.copy2

        def controlled_copy2(source: object, destination: object, *args: object, **kwargs: object):
            result = original_copy2(source, destination, *args, **kwargs)
            if Path(source).resolve() == backup.resolve():
                restore_copy_started.set()
                if not allow_restore.wait(5):
                    raise RuntimeError("recovery race test timed out")
            return result

        writer_memory = ProjectMemory(root)
        with patch("codex_autopilot.memory.shutil.copy2", side_effect=controlled_copy2):
            with ThreadPoolExecutor(max_workers=2) as pool:
                recovery = pool.submit(memory.recover_latest)
                self.assertTrue(restore_copy_started.wait(2))
                writer = pool.submit(
                    writer_memory.record_evidence,
                    kind="test",
                    summary="Writer admitted after recovery",
                    command="post-recovery",
                    result="PASS",
                    exit_code=0,
                    created_by="waiting-worker",
                )
                time.sleep(0.05)
                self.assertFalse(writer.done())
                allow_restore.set()
                quarantine = recovery.result(timeout=5)
                written = writer.result(timeout=5)

        self.assertTrue(quarantine.is_file())
        self.assertEqual(quarantine.read_bytes(), b"not a sqlite database")
        self.assertEqual(memory.get_evidence(written["id"])["created_by"], "waiting-worker")
        self.assertEqual(memory.integrity_check(), "ok")
        self.assertEqual(list(memory.state_dir.glob("*.restore-*")), [])


if __name__ == "__main__":
    unittest.main()
