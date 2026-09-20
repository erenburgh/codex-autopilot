"""Wire traces stop filling the disk, and stop nothing else.

Measured on a real run before this existed: 167 files and 2.3 GB under
`.codex-autopilot/logs`, individual traces between 70 and 108 MB, in a
project directory of 2.4 GB whose actual memory - journal, plan, project
memory, handoffs - was a few megabytes. No code path anywhere removed,
rotated or truncated them.

The danger in the cure is deleting what someone still needs, so each rule
that holds it back is tested here rather than trusted.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.config import DEFAULT_LOG_RETENTION_MB
from codex_autopilot import log_retention as cli_log_retention
from codex_autopilot.log_retention import KEEP_NEWEST, MIN_AGE_SECONDS, sweep_logs


MB = 1024 * 1024


class SweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = Path(self.tmp.name)
        self.now = 1_000_000.0

    def _trace(self, name: str, *, mb: int, age_seconds: float) -> Path:
        path = self.logs / name
        path.write_bytes(b"x" * (mb * MB))
        stamp = self.now - age_seconds
        import os

        os.utime(path, (stamp, stamp))
        return path

    def test_it_removes_the_oldest_until_the_budget_is_met(self) -> None:
        old = [
            self._trace(f"app-server-dispatcher-{i}.jsonl", mb=10, age_seconds=90000 - i)
            for i in range(12)
        ]
        removed, freed = sweep_logs(self.logs, budget_bytes=50 * MB, now=self.now)
        self.assertGreater(removed, 0)
        self.assertEqual(freed, removed * 10 * MB)
        survivors = {path.name for path in self.logs.iterdir()}
        # What survives is the newest, and it fits.
        self.assertLessEqual(
            sum(path.stat().st_size for path in self.logs.iterdir()), 50 * MB
        )
        self.assertIn(old[-1].name, survivors)
        self.assertNotIn(old[0].name, survivors)

    def test_a_directory_within_its_budget_is_not_touched(self) -> None:
        self._trace("app-server-dispatcher-a.jsonl", mb=5, age_seconds=90000)
        removed, freed = sweep_logs(self.logs, budget_bytes=512 * MB, now=self.now)
        self.assertEqual((removed, freed), (0, 0))
        self.assertEqual(len(list(self.logs.iterdir())), 1)

    def test_the_newest_few_survive_however_far_over_budget(self) -> None:
        for i in range(KEEP_NEWEST):
            self._trace(f"app-server-dispatcher-{i}.jsonl", mb=100, age_seconds=90000 + i)
        sweep_logs(self.logs, budget_bytes=1 * MB, now=self.now)
        self.assertEqual(len(list(self.logs.iterdir())), KEEP_NEWEST)

    def test_a_trace_written_in_the_last_hour_is_never_removed(self) -> None:
        """The dispatcher writing right now owns one of these."""

        live = self._trace("app-server-dispatcher-live.jsonl", mb=200, age_seconds=60)
        for i in range(KEEP_NEWEST + 3):
            self._trace(
                f"app-server-dispatcher-old-{i}.jsonl", mb=1, age_seconds=MIN_AGE_SECONDS * 2
            )
        sweep_logs(self.logs, budget_bytes=1 * MB, now=self.now)
        self.assertTrue(live.is_file())

    def test_only_this_runtime_s_own_traces_are_removed(self) -> None:
        foreign = self.logs / "notes-from-the-user.txt"
        foreign.write_bytes(b"y" * (40 * MB))
        kept = self.logs / "dispatcher.log"
        kept.write_bytes(b"z" * (40 * MB))
        for i in range(KEEP_NEWEST + 2):
            self._trace(
                f"app-server-dispatcher-{i}.jsonl", mb=10, age_seconds=MIN_AGE_SECONDS * 2
            )
        sweep_logs(self.logs, budget_bytes=1 * MB, now=self.now)
        self.assertTrue(foreign.is_file())
        self.assertTrue(kept.is_file())

    def test_relay_logs_are_swept_too(self) -> None:
        for i in range(KEEP_NEWEST + 4):
            self._trace(
                f"automatic-relay-{i}.log", mb=10, age_seconds=MIN_AGE_SECONDS * 2 + i
            )
        removed, _freed = sweep_logs(self.logs, budget_bytes=10 * MB, now=self.now)
        self.assertGreater(removed, 0)

    def test_zero_is_the_opt_out_and_deletes_nothing(self) -> None:
        for i in range(KEEP_NEWEST + 4):
            self._trace(
                f"app-server-dispatcher-{i}.jsonl", mb=10, age_seconds=MIN_AGE_SECONDS * 2
            )
        before = len(list(self.logs.iterdir()))
        self.assertEqual(sweep_logs(self.logs, budget_bytes=0, now=self.now), (0, 0))
        self.assertEqual(len(list(self.logs.iterdir())), before)

    def test_a_missing_directory_is_not_an_error(self) -> None:
        self.assertEqual(
            sweep_logs(self.logs / "nope", budget_bytes=512 * MB, now=self.now), (0, 0)
        )

    def test_nothing_is_truncated_only_whole_files_go(self) -> None:
        """A capped trace loses its tail, and the tail is the failure."""

        survivor = self._trace(
            "app-server-dispatcher-keep.jsonl", mb=20, age_seconds=MIN_AGE_SECONDS * 2
        )
        for i in range(KEEP_NEWEST + 2):
            self._trace(
                f"app-server-dispatcher-old-{i}.jsonl",
                mb=20,
                age_seconds=MIN_AGE_SECONDS * 3 + i,
            )
        sweep_logs(self.logs, budget_bytes=60 * MB, now=self.now)
        if survivor.is_file():
            self.assertEqual(survivor.stat().st_size, 20 * MB)


class ItIsWiredIntoTheDispatcherTests(unittest.TestCase):
    """A sweep nobody calls is the disease this whole file is about."""

    def test_the_dispatcher_sweeps_before_it_opens_its_own_trace(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/cli.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_sweep_finished_traces(cfg, dispatcher_log.parent)", source)
        sweep = source.index("_sweep_finished_traces(cfg, dispatcher_log.parent)")
        opened = source.index("client = AppServerClient(")
        self.assertLess(sweep, opened, "the sweep must run before the trace is opened")

    def test_housekeeping_never_stops_a_run(self) -> None:
        """A dispatcher that cannot tidy up still dispatches.

        The first version read cfg.runtime.log_retention_mb directly and
        raised AttributeError before the first turn. A full disk is the
        state this feature improves; a run that will not start is not.
        """

        from types import SimpleNamespace
        from codex_autopilot import cli

        with tempfile.TemporaryDirectory() as tmp:
            # A config with no runtime section at all.
            cli._sweep_finished_traces(SimpleNamespace(), Path(tmp))
            # A budget that is not a number.
            cli._sweep_finished_traces(
                SimpleNamespace(runtime=SimpleNamespace(log_retention_mb="lots")),
                Path(tmp),
            )
            # A directory that is not there.
            cli._sweep_finished_traces(
                SimpleNamespace(runtime=SimpleNamespace(log_retention_mb=512)),
                Path(tmp) / "gone",
            )

    def test_a_failing_sweep_is_reported_and_swallowed(self) -> None:
        from types import SimpleNamespace
        from unittest import mock
        from codex_autopilot import cli

        cfg = SimpleNamespace(runtime=SimpleNamespace(log_retention_mb=512))
        with mock.patch.object(
            cli_log_retention, "sweep_logs", side_effect=OSError("disk went away")
        ):
            with mock.patch("builtins.print") as printed:
                cli._sweep_finished_traces(cfg, Path("."))
        self.assertTrue(
            any("skipped" in str(call) for call in printed.call_args_list),
            printed.call_args_list,
        )

    def test_the_budget_is_a_documented_setting_with_a_default(self) -> None:
        self.assertGreater(DEFAULT_LOG_RETENTION_MB, 0)
        for name in ("README.md", "GETTING_STARTED.md", "docs/INSTALL_FOOTPRINT.md"):
            with self.subTest(document=name):
                text = (
                    Path(__file__).resolve().parents[1] / name
                ).read_text(encoding="utf-8")
                self.assertIn("log_retention_mb", text)


if __name__ == "__main__":
    unittest.main()
