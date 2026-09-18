"""Runtime archives are not charged to the worker as a foreign write.

The runtime itself places archives of earlier runs next to its state:
`.codex-autopilot.stuck-<time>` appears on --replace. The exclusion
compared the name exactly, so the archives did not fall under it.

Measured on a live run: task M0 was blocked under R7 for 37 paths, every
one inside .codex-autopilot.stuck-20260914T184420. It did no work there -
the runtime itself created the directory. The run stopped on a rule for
something the task did not do.
"""

from __future__ import annotations

import unittest

from codex_autopilot.config import STATE_DIR_NAME
from codex_autopilot.scope import _is_runtime_state


class RuntimeArchiveScopeTests(unittest.TestCase):
    def test_the_state_directory_itself_is_runtime(self) -> None:
        self.assertTrue(_is_runtime_state(f"{STATE_DIR_NAME}/run-state.json"))

    def test_an_archive_of_a_previous_run_is_runtime_too(self) -> None:
        self.assertTrue(
            _is_runtime_state(f"{STATE_DIR_NAME}.stuck-20260914T184420/config.toml")
        )
        self.assertTrue(
            _is_runtime_state(f"{STATE_DIR_NAME}.v080-done/plan.json")
        )

    def test_a_real_work_path_is_not_runtime(self) -> None:
        self.assertFalse(_is_runtime_state("src/codex_autopilot/plan.py"))
        self.assertFalse(_is_runtime_state("docs/README.md"))

    def test_a_name_that_merely_starts_alike_is_not_runtime(self) -> None:
        """Совпадение префикса без точки - чужой каталог, не наш архив."""

        self.assertFalse(_is_runtime_state(f"{STATE_DIR_NAME}-notes/plan.md"))


class SnapshotSiblingsAreRuntimeStateTests(unittest.TestCase):
    def test_replace_and_purge_snapshots_are_runtime_state(self) -> None:
        """R28-снимки лежат соседями состояния и не должны стать записью вне области (R7)."""

        from codex_autopilot.config import STATE_DIR_NAME
        from codex_autopilot.scope import _is_runtime_state

        self.assertTrue(_is_runtime_state(f"{STATE_DIR_NAME}.replaced-20260918T000000Z-0123abcd/plan.json"))
        self.assertTrue(_is_runtime_state(f"{STATE_DIR_NAME}.purged-20260918T000000Z-0123abcd/SNAPSHOT.md"))
