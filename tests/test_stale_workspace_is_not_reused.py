"""A staged workspace that the project has outgrown is replaced, not reused.

A staged workspace is a copy of the project taken at one moment. Reuse was
decided on nothing but "same run, and not finished", so a task returning
after a pause was handed its own old snapshot while the project had moved
on underneath it.

Measured on a live run: M11 staged its workspace on 20 Sep, came back on
22 Sep after its new prerequisite M11A had been verified and promoted, and
received the two-day-old copy - no `memory_lineage.py`, and `trust.py` at
the old hash `913fa1dc` instead of `a7ae5693`. Its worker compared the
prerequisite by hash, refused to copy the missing file in by hand because
that "would hide the dependency defect", and blocked. The run stopped.
That refusal is the only reason this surfaced as a stop rather than as
work built on a tree two days stale.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_autopilot.artifact_staging import (
    ArtifactStagingStore,
    StagingStatus,
)


class StalenessIsNoticedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = ArtifactStagingStore(self.root)

    def _record(self, task_id: str, status: str, promoted: list[str] | None = None) -> Path:
        task_root = self.store.root / task_id
        (task_root / "workspace").mkdir(parents=True)
        raw = {
            "schema_version": 1,
            "run_id": "run-1",
            "task_id": task_id,
            "workspace": str(
                (task_root / "workspace").relative_to(self.root)
            ),
            "status": status,
            "reservation_tokens": ["t1"],
            "created_at": "2026-09-20T00:00:00+00:00",
            "updated_at": "2026-09-20T00:00:00+00:00",
        }
        if promoted is not None:
            raw["promoted_when_staged"] = promoted
        (task_root / "record.json").write_text(json.dumps(raw), encoding="utf-8")
        return task_root

    def test_nothing_promoted_means_nothing_is_stale(self) -> None:
        self._record("M11", StagingStatus.PREPARED.value, promoted=[])
        raw = json.loads((self.store.root / "M11" / "record.json").read_text())
        self.assertEqual(self.store._workspace_is_stale(raw), "")

    def test_a_promotion_after_the_copy_makes_it_stale(self) -> None:
        self._record("M11A", StagingStatus.PROMOTED.value, promoted=[])
        self._record("M11", StagingStatus.PREPARED.value, promoted=[])
        raw = json.loads((self.store.root / "M11" / "record.json").read_text())
        reason = self.store._workspace_is_stale(raw)
        self.assertIn("M11A", reason)

    def test_a_promotion_the_copy_already_saw_is_not_stale(self) -> None:
        self._record("M11A", StagingStatus.PROMOTED.value, promoted=[])
        self._record("M11", StagingStatus.PREPARED.value, promoted=["M11A"])
        raw = json.loads((self.store.root / "M11" / "record.json").read_text())
        self.assertEqual(self.store._workspace_is_stale(raw), "")

    def test_a_record_from_before_the_field_is_not_trusted(self) -> None:
        """A copy of unknown age must not be used silently."""

        self._record("M11A", StagingStatus.PROMOTED.value, promoted=[])
        self._record("M11", StagingStatus.PREPARED.value, promoted=None)
        raw = json.loads((self.store.root / "M11" / "record.json").read_text())
        self.assertIn("predates", self.store._workspace_is_stale(raw))

    def test_a_set_aside_directory_is_not_counted_as_a_task(self) -> None:
        self._record("M11A", StagingStatus.PROMOTED.value, promoted=[])
        aside = self.store.root / "M11.superseded-20260922"
        (aside / "workspace").mkdir(parents=True)
        (aside / "record.json").write_text(
            json.dumps({"status": StagingStatus.PROMOTED.value}), encoding="utf-8"
        )
        self.assertEqual(self.store._promoted_task_ids(), ("M11A",))


class TheStaleCopyIsSetAsideNotDeletedTests(unittest.TestCase):
    """R28: state is set aside, never destroyed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = ArtifactStagingStore(self.root)
        task_root = self.store.root / "M11"
        (task_root / "workspace").mkdir(parents=True)
        (task_root / "workspace" / "keepme.txt").write_text("work", encoding="utf-8")
        self.raw = {
            "schema_version": 1,
            "run_id": "run-1",
            "task_id": "M11",
            "workspace": str((task_root / "workspace").relative_to(self.root)),
            "status": StagingStatus.PREPARED.value,
            "reservation_tokens": ["t1"],
            "created_at": "2026-09-20T00:00:00+00:00",
            "updated_at": "2026-09-20T00:00:00+00:00",
        }
        (task_root / "record.json").write_text(json.dumps(self.raw), encoding="utf-8")

    def test_the_work_survives_under_a_new_name(self) -> None:
        aside = self.store._set_aside_stale("M11", dict(self.raw), "because")
        self.assertTrue((aside / "workspace" / "keepme.txt").is_file())
        self.assertFalse((self.store.root / "M11").exists())

    def test_the_reason_is_recorded_on_the_fenced_record(self) -> None:
        aside = self.store._set_aside_stale("M11", dict(self.raw), "prerequisite landed")
        raw = json.loads((aside / "record.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["status"], StagingStatus.ABANDONED.value)
        self.assertEqual(raw["abandoned_reason"], "prerequisite landed")

    def test_the_task_root_is_free_for_a_fresh_copy(self) -> None:
        self.store._set_aside_stale("M11", dict(self.raw), "because")
        self.assertIsNone(self.store._load_raw("M11", missing_ok=True))


class PrepareTakesTheFreshPathTests(unittest.TestCase):
    def test_prepare_consults_staleness_before_reusing(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/artifact_staging.py"
        ).read_text(encoding="utf-8")
        prepare = source[source.index("    def prepare(") :]
        prepare = prepare[: prepare.index("\n    def ", 10)]
        self.assertIn("_workspace_is_stale(existing)", prepare)
        self.assertIn("_set_aside_stale(", prepare)
        stale_at = prepare.index("_workspace_is_stale(existing)")
        reuse_at = prepare.index("return self._from_raw(existing)")
        self.assertLess(stale_at, reuse_at, "staleness must be asked before reuse")

    def test_a_fresh_copy_records_what_it_saw(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/artifact_staging.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"promoted_when_staged": list(promoted_when_staged)', source)


if __name__ == "__main__":
    unittest.main()
