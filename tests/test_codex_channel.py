"""The run talks to one Codex, and which one is a visible decision.

Desktop carries its own `codex` inside the application bundle; a
separately installed CLI is a different file on a different schedule.
They serve different model catalogs.

Measured 22-23 Sep 2026: Desktop offered a newly released model while
`model/list` through the CLI on PATH did not list it, because that CLI had
stayed at 0.154 while Desktop's was 0.155. A project pinned to that model
refused to start, and the refusal read as "your account does not have this
model" when the truth was "the client you are talking through cannot see
it".

So: pick the newest at project creation, write it where it can be read,
never switch it behind anyone's back, and make the refusal name the
binary that does serve the model.
"""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

from codex_autopilot import codex_binaries


class ChoosingAChannelTests(unittest.TestCase):
    def test_the_newest_wins(self) -> None:
        with mock.patch.object(
            codex_binaries,
            "discover_codex_binaries",
            return_value=(("/usr/bin/codex", (0, 154, 0)), ("/Apps/codex", (0, 155, 0))),
        ):
            self.assertEqual(codex_binaries.newest_codex_binary(), "/Apps/codex")

    def test_nothing_found_keeps_the_plain_name(self) -> None:
        """A machine we cannot inspect still gets a working config."""

        with mock.patch.object(
            codex_binaries, "discover_codex_binaries", return_value=()
        ):
            self.assertEqual(codex_binaries.newest_codex_binary(), "codex")

    def test_a_binary_that_will_not_say_its_version_is_not_chosen(self) -> None:
        """Silence is not newness."""

        with mock.patch.object(
            codex_binaries, "discover_codex_binaries", return_value=(("/weird/codex", ()),)
        ):
            self.assertEqual(codex_binaries.newest_codex_binary(), "codex")

    def test_discovery_survives_a_binary_that_cannot_be_run(self) -> None:
        with mock.patch.object(codex_binaries.shutil, "which", return_value=None):
            with mock.patch.object(
                codex_binaries, "DESKTOP_BUNDLED", Path("/nope/codex")
            ):
                self.assertEqual(codex_binaries.discover_codex_binaries(), ())

    def test_a_version_is_parsed_from_whatever_the_binary_prints(self) -> None:
        class _Done:
            stdout = "codex-cli 0.155.0-alpha.9.2\n"
            stderr = ""

        with mock.patch.object(codex_binaries.subprocess, "run", return_value=_Done()):
            self.assertEqual(codex_binaries._version("/x/codex"), (0, 155, 0))

    def test_an_unreadable_version_sorts_lowest_rather_than_raising(self) -> None:
        with mock.patch.object(
            codex_binaries.subprocess, "run", side_effect=OSError("boom")
        ):
            self.assertEqual(codex_binaries._version("/x/codex"), ())


class TheChoiceIsWrittenDownTests(unittest.TestCase):
    """Resolved once, visibly, not re-derived from PATH on every run."""

    def test_bootstrap_writes_the_chosen_binary(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/bootstrap.py"
        ).read_text(encoding="utf-8")
        self.assertIn("newest_codex_binary()", source)
        self.assertNotIn("'binary = \"codex\"'", source)

    def test_nothing_switches_a_live_project_silently(self) -> None:
        """A channel decides what you are served; it is not swapped for you."""

        for name in ("lifecycle_dispatch.py", "lifecycle_reservations.py", "control.py"):
            source = (
                Path(__file__).resolve().parents[1] / "src/codex_autopilot" / name
            ).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotIn("newest_codex_binary", source)


class TheRefusalNamesTheOtherClientTests(unittest.TestCase):
    def test_preflight_looks_for_a_binary_that_serves_the_model(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/preflight.py"
        ).read_text(encoding="utf-8")
        self.assertIn("binaries_serving(verdict.pinned)", source)
        self.assertIn("Set desktop.binary to that", source)

    def test_it_does_not_name_the_client_already_in_use(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/preflight.py"
        ).read_text(encoding="utf-8")
        self.assertIn("if item != str(codex_binary)", source)


class TheWrittenConfigExplainsItselfTests(unittest.TestCase):
    """The owner reads the config, not this module's source."""

    def _written(self, chosen: str) -> str:
        import tempfile
        import types

        from codex_autopilot import bootstrap

        plan = types.SimpleNamespace(
            execution_strategy="serial", max_parallel_workers=1, computer_use_slots=1
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".codex-autopilot").mkdir()
            with mock.patch.object(
                bootstrap, "newest_codex_binary", return_value=chosen
            ):
                bootstrap._write_config(
                    root,
                    "adaptive",
                    root / "skills",
                    plan=plan,
                    language="ru",
                    skill_screening="never",
                )
            return (root / ".codex-autopilot/config.toml").read_text(encoding="utf-8")

    def test_the_chosen_binary_is_what_lands_in_the_file(self) -> None:
        import tomllib

        written = self._written("/Applications/Whatever/codex")
        self.assertEqual(
            tomllib.loads(written)["desktop"]["binary"], "/Applications/Whatever/codex"
        )

    def test_the_file_says_why_this_line_exists_and_how_to_change_it(self) -> None:
        """A bare absolute path reads like something you must not touch."""

        written = self._written("/Applications/Whatever/codex")
        self.assertIn("# The Codex this project talks to", written)
        self.assertIn("Change this line", written)


if __name__ == "__main__":
    unittest.main()
