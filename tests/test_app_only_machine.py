"""A machine with only ChatGPT.app: every command finds the app's Codex.

0.14.1 taught discovery and the run itself to find the Codex ChatGPT moved
on 3 Oct 2026 (test_bundled_codex_package). The review of 9 Oct 2026 found
the doors around the run still asking PATH for ``codex``, reproduced on a
fake machine:

- preflight looked the binary up on PATH while bootstrap wrote the newest
  on the machine. With the app alone, ``preflight`` and ``start-skill``
  stopped at "Runtime: FAIL - official Codex CLI not found", so a new
  project could not start; with an older Homebrew CLI beside the app,
  preflight checked the model catalog and the hook trust on Homebrew and
  bootstrap then wrote the app's path;
- doctor outside a project said "FAIL Codex CLI: not found";
- uninstall skipped removing the plugin and the marketplace from Codex;
- the placement probe started ``codex`` from PATH.

Every test here builds the fake machine of test_bundled_codex_package - a
ChatGPT.app with a manifest and a script that answers like Codex - and
drives the production entry points as the product calls them.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from codex_autopilot import cli, codex_binaries
from codex_autopilot.preflight import PreflightResult

from test_bundled_codex_package import FakeMachine


def _plan_file(machine: FakeMachine) -> Path:
    from test_desktop_lifecycle import graph

    path = machine.base / "plan.json"
    path.write_text(json.dumps(graph()), encoding="utf-8")
    return path


def _project(machine: FakeMachine) -> tuple[Path, Path]:
    root = machine.base / "project"
    (root / ".git").mkdir(parents=True)
    skill = machine.base / "skill/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# test skill\n", encoding="utf-8")
    return root, skill


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class PreflightAsksTheCodexBootstrapWritesTests(unittest.TestCase):
    """``codex-autopilot preflight`` with the real run_preflight and client.

    Preflight stops at "Desktop project" - no Desktop project is named -
    which is after the two checks this is about: which Codex it found
    (Runtime) and which one answered (App Server).
    """

    def setUp(self) -> None:
        self.m = FakeMachine(self)
        self.root, self.skill = _project(self.m)
        self.plan = _plan_file(self.m)

    def _preflight(self) -> tuple[int, str]:
        with self.m.installed():
            code, out, _err = _run([
                "preflight", "--project", str(self.root), "--plan-file", str(self.plan),
                "--skill-path", str(self.skill),
            ])
        return code, out

    def _app_servers(self) -> list[str]:
        return [call[0] for call in self.m.calls() if call[1:2] == ["app-server"]]

    def test_with_only_the_app_preflight_finds_and_starts_its_codex(self) -> None:
        entry = self.m.packaged()
        code, out = self._preflight()
        self.assertIn(f"Runtime: OK — {entry};", out)
        self.assertIn("App Server: OK — fake-codex/0.160.0", out)
        self.assertNotIn("Runtime: FAIL", out)
        self.assertEqual(self._app_servers(), [str(entry)])
        # It goes on to the next check; the stop there is no Desktop project.
        self.assertIn("Desktop project: FAIL", out)
        self.assertEqual(code, 2)

    def test_beside_an_older_homebrew_it_checks_the_app_not_homebrew(self) -> None:
        entry = self.m.packaged(version="0.160.0")
        self.m.separate_cli("0.154.0")
        _code, out = self._preflight()
        self.assertIn(f"Runtime: OK — {entry};", out)
        self.assertIn("App Server: OK — fake-codex/0.160.0", out)
        self.assertEqual(self._app_servers(), [str(entry)], "Homebrew was asked instead")

    def test_a_newer_cli_on_path_is_what_it_checks_then(self) -> None:
        """The rule is the newest, not the app: it is the rule bootstrap uses."""

        self.m.packaged(version="0.160.0")
        newer = self.m.separate_cli("0.161.0")
        _code, out = self._preflight()
        self.assertIn(f"Runtime: OK — {newer};", out)
        self.assertEqual(self._app_servers(), [str(newer)])

    def test_with_no_codex_at_all_it_says_where_it_looked(self) -> None:
        code, out = self._preflight()
        self.assertEqual(code, 2)
        self.assertIn("Runtime: FAIL — no Codex found: neither ChatGPT.app's own nor `codex` on PATH", out)
        self.assertEqual(self.m.calls(), [])
        self.assertFalse((self.root / ".codex-autopilot/config.toml").exists())


class BootstrapWritesTheCodexPreflightCheckedTests(unittest.TestCase):
    """``codex-autopilot bootstrap``: one choice reaches both halves.

    Preflight is replaced by a recorder of what it was handed; bootstrap
    runs for real and writes the config.
    """

    def setUp(self) -> None:
        from _gates import patch_hook_trust_gates

        patch_hook_trust_gates(self)
        self.m = FakeMachine(self)
        self.root, self.skill = _project(self.m)
        self.plan = _plan_file(self.m)
        self.preflighted: list[str] = []

    def _bootstrap(self) -> dict:
        import tomllib

        from _plan_contract import canonical_plan_verification

        def preflight(root, *, plan, binary="codex", **_kwargs):
            self.preflighted.append(binary)
            return PreflightResult(
                project=Path(root).resolve(),
                codex_binary=binary,
                plan_verification=canonical_plan_verification(plan),
            )

        discover = mock.patch.object(
            codex_binaries,
            "discover_codex_binaries",
            wraps=codex_binaries.discover_codex_binaries,
        )
        with self.m.installed(), mock.patch.object(cli, "run_preflight", side_effect=preflight), discover as looked:
            code, _out, err = _run([
                "bootstrap", "--project", str(self.root), "--plan-file", str(self.plan),
                "--skill-path", str(self.skill),
            ])
        self.assertEqual(code, 0, err)
        self.looked = looked.call_count
        config = self.root / ".codex-autopilot/config.toml"
        return tomllib.loads(config.read_text(encoding="utf-8"))

    def test_the_app_only_machine_writes_the_codex_preflight_checked(self) -> None:
        entry = self.m.packaged()
        written = self._bootstrap()
        self.assertEqual(self.preflighted, [str(entry)])
        self.assertEqual(written["desktop"]["binary"], str(entry))

    def test_the_machine_is_looked_at_once_for_both(self) -> None:
        """Two looks can give two answers; one look gives one."""

        self.m.packaged(version="0.160.0")
        self.m.separate_cli("0.154.0")
        written = self._bootstrap()
        self.assertEqual(self.preflighted, [written["desktop"]["binary"]])
        self.assertEqual(self.looked, 1)


class AnotherProcessIsToldWhereTheAppIsTests(unittest.TestCase):
    """install.sh and the installed CLI run as processes of their own.

    A test cannot patch DESKTOP_APP inside them, so the bundle can be named
    through the environment as well - and only a test does that.
    """

    def setUp(self) -> None:
        self.m = FakeMachine(self)
        self.entry = self.m.packaged()

    def test_the_environment_names_the_bundle(self) -> None:
        absent = self.m.base / "absent/ChatGPT.app"
        with mock.patch.object(codex_binaries, "DESKTOP_APP", absent), mock.patch.dict(
            os.environ, {codex_binaries.DESKTOP_APP_ENV: str(self.m.app)}
        ):
            self.assertEqual(codex_binaries.desktop_codex(), self.entry)

    def test_unset_the_bundle_is_the_attribute(self) -> None:
        with mock.patch.object(codex_binaries, "DESKTOP_APP", self.m.app), mock.patch.dict(os.environ):
            os.environ.pop(codex_binaries.DESKTOP_APP_ENV, None)
            self.assertEqual(codex_binaries.desktop_codex(), self.entry)


if __name__ == "__main__":
    unittest.main()
