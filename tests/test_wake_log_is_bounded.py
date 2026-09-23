"""The heartbeat must not fill the disk, and must be worth reading.

The launch agent runs every five minutes and launchd appends its stdout to
one file forever. The sweep printed a line per registered project, and the
registry keeps every project ever created, including the temporary ones
test runs leave behind. Measured on the author's machine 23 Sep 2026: 55 MB
and 596,661 lines, nearly all of them "gone".

Two rules come out of that: say what happened rather than what was looked
at, and bound the file.
"""

from __future__ import annotations

from pathlib import Path
import io
import unittest
from unittest import mock

from codex_autopilot.log_retention import reset_oversized_log


class BoundingTheLogTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "wake-sweep.log"

    def test_a_log_within_budget_is_left_alone(self) -> None:
        self.path.write_text("a heartbeat\n", encoding="utf-8")
        self.assertEqual(reset_oversized_log(self.path, budget_bytes=1024), 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "a heartbeat\n")

    def test_an_oversized_log_is_emptied_and_reports_what_it_dropped(self) -> None:
        self.path.write_text("x" * 5000, encoding="utf-8")
        self.assertEqual(reset_oversized_log(self.path, budget_bytes=1024), 5000)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")

    def test_it_empties_rather_than_unlinks(self) -> None:
        """launchd opened this file before the sweep started.

        Unlinking would leave the running sweep writing into an inode with
        no name, so the run that did the cleanup would lose its own output.
        """

        self.path.write_text("x" * 5000, encoding="utf-8")
        before = self.path.stat().st_ino
        reset_oversized_log(self.path, budget_bytes=1024)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.stat().st_ino, before)

    def test_a_missing_log_is_not_an_error(self) -> None:
        self.assertEqual(reset_oversized_log(self.path, budget_bytes=1024), 0)

    def test_an_unwritable_log_does_not_stop_the_sweep(self) -> None:
        self.path.write_text("x" * 5000, encoding="utf-8")
        with mock.patch.object(Path, "open", side_effect=OSError("read-only")):
            self.assertEqual(reset_oversized_log(self.path, budget_bytes=1024), 0)


class WhatTheSweepSaysTests(unittest.TestCase):
    def _run_sweep(self, outcome: dict[str, str], env: dict[str, str] | None = None) -> str:
        import os

        from codex_autopilot import cli

        captured = io.StringIO()
        argv = ["codex-autopilot", "_wake-sweep"]
        with mock.patch.dict(os.environ, env or {}, clear=False):
            with mock.patch("codex_autopilot.wake.sweep", return_value=outcome):
                with mock.patch("sys.stdout", captured):
                    cli.main(argv[1:])
        return captured.getvalue()

    def test_a_wake_is_named(self) -> None:
        written = self._run_sweep({"/p/one": "wake 4211"})
        self.assertIn("/p/one: wake 4211", written)

    def test_a_fault_is_named(self) -> None:
        written = self._run_sweep({"/p/two": "unreadable: bad toml"})
        self.assertIn("/p/two: unreadable: bad toml", written)

    def test_the_quiet_majority_is_counted_not_listed(self) -> None:
        outcome = {f"/tmp/gone-{i}": "gone" for i in range(400)}
        outcome["/p/live"] = "nothing due"
        written = self._run_sweep(outcome)
        self.assertNotIn("/tmp/gone-0", written)
        self.assertIn("quiet: 400 gone, 1 nothing due", written)
        self.assertLess(len(written), 200)

    def test_a_sweep_with_nothing_to_report_says_nothing(self) -> None:
        self.assertEqual(self._run_sweep({}), "")

    def test_the_reset_is_announced_in_the_file_it_reset(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "wake-sweep.log"
            log.write_text("x" * (2 * 1024 * 1024), encoding="utf-8")
            written = self._run_sweep({}, env={"CODEX_AUTOPILOT_WAKE_LOG": str(log)})
        self.assertIn("log reset:", written)
        self.assertIn("bytes of earlier sweeps dropped", written)


class TheAgentKnowsWhereItsLogIsTests(unittest.TestCase):
    def test_the_installer_passes_the_path_to_the_agent(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "install.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("CODEX_AUTOPILOT_WAKE_LOG", source)
        self.assertIn("<key>EnvironmentVariables</key>", source)


if __name__ == "__main__":
    unittest.main()
