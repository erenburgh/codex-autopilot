"""ChatGPT.app moved its Codex, and a run follows it - only it.

Until the ChatGPT update of 3 Oct 2026 the app kept its Codex as one
executable, Contents/Resources/codex, and codex_binaries named that path
outright. The update replaced it with a package directory described by a
manifest - Contents/Resources/codex-cli/codex-package.json, entrypoint
"bin/codex" - and the old path stopped existing. Measured the next day:

- discovery pointed at nothing, so a machine without a separate CLI found
  no Codex at all, and a machine with an older Homebrew CLI picked that one,
  which did not serve the pinned model;
- a project created earlier carried the old path in its config: App Server
  could not be started (ENOENT), and the hook-trust gate refused with
  "hooks/list: [Errno 2] No such file or directory" - no run could start;
- doctor fell through to the Homebrew CLI on PATH and reported the pinned
  model "no longer served".

Every test below builds a fake ChatGPT.app in a temporary directory - a
manifest and a script that answers `--version`, `login status` and the App
Server handshake - and drives the production entry points: discovery,
``load_config``, ``bootstrap._write_config``, the hook-trust gate with a real
``AppServerClient``, ``cli.doctor`` and ``status_text``. Nothing reads the
real /Applications or the real PATH.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

from codex_autopilot import bootstrap, cli, codex_binaries
from codex_autopilot.appserver import AppServerClient
from codex_autopilot.config import load_config
from codex_autopilot.models import MODEL_IDS, PUBLIC_REASONING
from codex_autopilot.hook_trust import (
    HookPreflightError,
    autopilot_plugin_id,
    require_trusted_stop_hook_for_config,
    runtime_hook_command,
)

# The manifest as the 3 Oct 2026 update shipped it.
MANIFEST = {
    "layoutVersion": 1,
    "version": "0.160.0",
    "target": "aarch64-apple-darwin",
    "variant": "codex",
    "entrypoint": "bin/codex",
    "resourcesDir": "codex-resources",
    "pathDir": "codex-path",
}

FAKE_CODEX = '''#!{python}
import json, os, sys

STATE = {state!r}
with open(os.path.join(STATE, "calls.jsonl"), "a", encoding="utf-8") as log:
    log.write(json.dumps([os.path.abspath(sys.argv[0]), *sys.argv[1:]]) + "\\n")
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("codex-cli {version}")
    sys.exit(0)
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
    sys.exit(0)
if args[:1] != ["app-server"]:
    sys.exit(2)
answers_path = os.path.join(STATE, "answers.json")
answers = {{}}
if os.path.isfile(answers_path):
    with open(answers_path, encoding="utf-8") as handle:
        answers = json.load(handle)
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    params = message.get("params") or {{}}
    if method == "initialize":
        result = {{"userAgent": "fake-codex/{version}", "codexHome": os.environ.get("CODEX_HOME", "")}}
    elif method == "hooks/list":
        result = {{"data": [
            {{"cwd": cwd, "hooks": answers.get("hooks", []), "warnings": [], "errors": []}}
            for cwd in params.get("cwds", [])
        ]}}
    elif method == "permissionProfile/list":
        result = {{"data": [{{"id": ":workspace"}}]}}
    elif method == "model/list":
        result = {{"data": answers.get("models", [])}}
    else:
        result = {{}}
    print(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": result}}), flush=True)
'''


def write_codex(path: Path, version: str, state: Path) -> Path:
    """An executable that is a Codex as far as the runtime can tell."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        FAKE_CODEX.format(python=sys.executable, state=str(state), version=version),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class FakeMachine:
    """A ChatGPT.app and, optionally, a separate CLI, in one temp directory."""

    def __init__(self, case: unittest.TestCase) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="codex-autopilot-bundle-")).resolve()
        case.addCleanup(shutil.rmtree, self.base, True)
        self.state = self.base / "state"
        self.state.mkdir()
        self.app = self.base / "ChatGPT.app"
        (self.app / "Contents/Resources").mkdir(parents=True)
        self.package = self.app / "Contents/Resources/codex-cli"
        self.legacy = self.app / "Contents/Resources/codex"
        self.cli: Path | None = None

    def packaged(self, manifest: object = None, *, entry: str = "bin/codex", version: str = "0.160.0") -> Path:
        """The 3 Oct 2026 layout: a manifest and the executable it names."""

        self.package.mkdir(parents=True, exist_ok=True)
        payload = dict(MANIFEST, entrypoint=entry) if manifest is None else manifest
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (self.package / "codex-package.json").write_text(text, encoding="utf-8")
        return write_codex(self.package / entry, version, self.state)

    def old_layout(self, version: str = "0.155.0") -> Path:
        return write_codex(self.legacy, version, self.state)

    def separate_cli(self, version: str = "0.154.0") -> Path:
        self.cli = write_codex(self.base / "homebrew/bin/codex", version, self.state)
        return self.cli

    def which(self, name: str, *args, **kwargs) -> str | None:
        """PATH holds only this machine's separate CLI, if it has one."""

        if os.sep in str(name):
            return str(name) if os.path.isfile(name) and os.access(name, os.X_OK) else None
        if name == "codex" and self.cli is not None:
            return str(self.cli)
        return None

    @contextlib.contextmanager
    def installed(self):
        with mock.patch.object(codex_binaries, "DESKTOP_APP", self.app), mock.patch.dict(
            os.environ, {codex_binaries.DESKTOP_APP_ENV: str(self.app)}
        ), mock.patch.object(codex_binaries.shutil, "which", side_effect=self.which):
            yield

    def calls(self) -> list[list[str]]:
        log = self.state / "calls.jsonl"
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def answer(self, **answers) -> None:
        """What the fake App Server returns for hooks/list and model/list."""

        (self.state / "answers.json").write_text(json.dumps(answers), encoding="utf-8")


def write_project(machine: FakeMachine, binary: str, *, name: str = "project") -> Path:
    """A project config exactly as bootstrap writes it, naming ``binary``."""

    root = machine.base / name
    (root / ".codex-autopilot").mkdir(parents=True)
    plan = types.SimpleNamespace(
        execution_strategy="serial", max_parallel_workers=1, computer_use_slots=1
    )
    with mock.patch.object(bootstrap, "newest_codex_binary", return_value=binary):
        bootstrap._write_config(
            root,
            "adaptive",
            root / "skills/SKILL.md",
            plan=plan,
            language="en",
            skill_screening="never",
        )
    return root


class TheAppsCodexIsFoundThroughItsManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = FakeMachine(self)

    def test_the_packaged_entrypoint_is_the_apps_codex(self) -> None:
        entry = self.m.packaged()
        with self.m.installed():
            self.assertEqual(codex_binaries.desktop_codex(), entry)
            self.assertEqual(
                codex_binaries.discover_codex_binaries(), ((str(entry), (0, 160, 0)),)
            )
            self.assertEqual(codex_binaries.newest_codex_binary(), str(entry))

    def test_the_entrypoint_is_whatever_the_manifest_names(self) -> None:
        """Read through the manifest, not one more hard-coded path."""

        entry = self.m.packaged(entry="libexec/next/codex")
        with self.m.installed():
            self.assertEqual(codex_binaries.desktop_codex(), entry)
            self.assertEqual(codex_binaries.newest_codex_binary(), str(entry))

    def test_the_newer_app_wins_over_an_older_cli_on_path(self) -> None:
        """The machine of 4 Oct 2026: Homebrew 0.154 on PATH, the app at 0.160."""

        self.m.separate_cli("0.154.0")
        entry = self.m.packaged(version="0.160.0")
        with self.m.installed():
            self.assertEqual(
                codex_binaries.discover_codex_binaries(),
                ((str(self.m.cli), (0, 154, 0)), (str(entry), (0, 160, 0))),
            )
            self.assertEqual(codex_binaries.newest_codex_binary(), str(entry))

    def test_the_old_executable_is_still_a_candidate(self) -> None:
        legacy = self.m.old_layout("0.155.0")
        with self.m.installed():
            self.assertEqual(codex_binaries.desktop_codex_candidates(), (legacy,))
            self.assertEqual(codex_binaries.newest_codex_binary(), str(legacy))

    def test_both_addresses_present_the_package_comes_first(self) -> None:
        entry = self.m.packaged(version="0.160.0")
        legacy = self.m.old_layout("0.155.0")
        with self.m.installed():
            self.assertEqual(codex_binaries.desktop_codex_candidates(), (entry, legacy))
            self.assertEqual(codex_binaries.desktop_codex(), entry)
            self.assertEqual(codex_binaries.newest_codex_binary(), str(entry))

    def test_no_app_and_no_cli_keeps_the_plain_name(self) -> None:
        with mock.patch.object(
            codex_binaries, "DESKTOP_APP", self.m.base / "Missing.app"
        ), mock.patch.object(codex_binaries.shutil, "which", side_effect=self.m.which):
            self.assertEqual(codex_binaries.desktop_codex_candidates(), ())
            self.assertEqual(codex_binaries.newest_codex_binary(), "codex")


class ABadManifestIsNotACandidateTests(unittest.TestCase):
    """Worst cases: no candidate, no exception, and the old file still counts."""

    def setUp(self) -> None:
        self.m = FakeMachine(self)
        self.outside = write_codex(self.m.base / "elsewhere/codex", "0.999.0", self.m.state)

    def _cases(self):
        package = self.m.package
        yield "no manifest", lambda: write_codex(package / "bin/codex", "0.160.0", self.m.state)
        yield "broken json", lambda: self.m.packaged('{"entrypoint": "bin/codex",')
        yield "not utf-8", lambda: (
            self.m.packaged(),
            (package / "codex-package.json").write_bytes(b"\xff\xfe{\x00"),
        )
        yield "a list", lambda: self.m.packaged(["bin/codex"])
        yield "no entrypoint", lambda: self.m.packaged(
            {key: value for key, value in MANIFEST.items() if key != "entrypoint"}
        )
        yield "empty entrypoint", lambda: self.m.packaged(dict(MANIFEST, entrypoint=""))
        yield "entrypoint not a string", lambda: self.m.packaged(dict(MANIFEST, entrypoint=7))
        yield "entrypoint is a directory", lambda: (
            self.m.packaged(),
            (package / "codex-package.json").write_text(
                json.dumps(dict(MANIFEST, entrypoint="bin")), encoding="utf-8"
            ),
        )
        yield "entrypoint missing", lambda: self.m.packaged(dict(MANIFEST, entrypoint="bin/gone"))
        yield "absolute entrypoint", lambda: self.m.packaged(
            dict(MANIFEST, entrypoint=str(self.outside))
        )
        yield "climbs out with ..", lambda: self.m.packaged(
            dict(MANIFEST, entrypoint="../../../../elsewhere/codex")
        )
        yield "climbs out by symlink", lambda: (
            self.m.packaged(dict(MANIFEST, entrypoint="bin/link")),
            (package / "bin").mkdir(parents=True, exist_ok=True),
            (package / "bin/link").symlink_to(self.outside),
        )
        yield "nul in entrypoint", lambda: self.m.packaged(dict(MANIFEST, entrypoint="bin/co\x00dex"))

    def test_each_bad_manifest_yields_no_candidate(self) -> None:
        for name, build in self._cases():
            with self.subTest(case=name):
                if self.m.package.exists():
                    shutil.rmtree(self.m.package)
                build()
                with self.m.installed():
                    self.assertEqual(codex_binaries.desktop_codex_candidates(), ())
                    self.assertEqual(codex_binaries.discover_codex_binaries(), ())
                    self.assertEqual(codex_binaries.newest_codex_binary(), "codex")

    def test_a_bad_manifest_leaves_the_old_executable_standing(self) -> None:
        legacy = self.m.old_layout()
        for name, build in self._cases():
            with self.subTest(case=name):
                if self.m.package.exists():
                    shutil.rmtree(self.m.package)
                build()
                with self.m.installed():
                    self.assertEqual(codex_binaries.desktop_codex_candidates(), (legacy,))


class BootstrapWritesTheNewAddressTests(unittest.TestCase):
    def test_a_new_project_names_the_packaged_entrypoint(self) -> None:
        import tomllib

        m = FakeMachine(self)
        m.separate_cli("0.154.0")
        entry = m.packaged(version="0.160.0")
        root = m.base / "fresh"
        (root / ".codex-autopilot").mkdir(parents=True)
        plan = types.SimpleNamespace(
            execution_strategy="serial", max_parallel_workers=1, computer_use_slots=1
        )
        with m.installed():
            bootstrap._write_config(
                root, "adaptive", root / "skills/SKILL.md",
                plan=plan, language="en", skill_screening="never",
            )
        written = tomllib.loads((root / ".codex-autopilot/config.toml").read_text(encoding="utf-8"))
        self.assertEqual(written["desktop"]["binary"], str(entry))


class AnOldAddressInTheConfigFollowsTheAppTests(unittest.TestCase):
    """The same app at a new address is followed; nothing else is replaced."""

    def setUp(self) -> None:
        self.m = FakeMachine(self)

    def test_the_old_executable_path_runs_the_packaged_codex(self) -> None:
        entry = self.m.packaged()
        root = write_project(self.m, str(self.m.legacy))
        config = root / ".codex-autopilot/config.toml"
        before = config.read_bytes()
        with self.m.installed():
            cfg = load_config(root)
        self.assertEqual(cfg.desktop.binary, str(entry))
        self.assertEqual(cfg.desktop.binary_moved_from, str(self.m.legacy))
        self.assertEqual(config.read_bytes(), before, "her config is not rewritten")

    def test_the_layout_one_entrypoint_follows_the_next_move_too(self) -> None:
        """0.14.1 writes bin/codex; when the app moves it again, it follows."""

        moved = self.m.packaged(entry="libexec/codex")
        written_by_this_release = self.m.package / "bin/codex"
        self.assertFalse(written_by_this_release.exists())
        root = write_project(self.m, str(written_by_this_release))
        with self.m.installed():
            cfg = load_config(root)
        self.assertEqual(cfg.desktop.binary, str(moved))
        self.assertEqual(cfg.desktop.binary_moved_from, str(written_by_this_release))

    def test_an_old_address_that_still_exists_is_used_as_written(self) -> None:
        self.m.packaged()
        legacy = self.m.old_layout()
        root = write_project(self.m, str(legacy))
        with self.m.installed():
            cfg = load_config(root)
        self.assertEqual(cfg.desktop.binary, str(legacy))
        self.assertIsNone(cfg.desktop.binary_moved_from)

    def test_the_current_address_is_used_as_written(self) -> None:
        entry = self.m.packaged()
        root = write_project(self.m, str(entry))
        with self.m.installed():
            cfg = load_config(root)
        self.assertEqual(cfg.desktop.binary, str(entry))
        self.assertIsNone(cfg.desktop.binary_moved_from)

    def test_a_missing_binary_anywhere_else_is_not_replaced(self) -> None:
        """Another Codex is another model catalog: that is her decision."""

        self.m.packaged()
        self.m.separate_cli()
        for name, missing in (
            ("removed cli", self.m.base / "homebrew/Cellar/codex/0.150.0/bin/codex"),
            ("typo", self.m.base / "Applications/ChatGTP.app/Contents/Resources/codex"),
            ("unknown place in the same app", self.m.app / "Contents/MacOS/codex"),
            ("plain name", Path("codex-nightly")),
            ("nul byte", Path("/nowhere/co\x00dex")),
        ):
            with self.subTest(case=name):
                root = write_project(self.m, str(missing), name=f"project-{name.replace(' ', '-')}")
                with self.m.installed():
                    cfg = load_config(root)
                self.assertEqual(cfg.desktop.binary, str(missing))
                self.assertIsNone(cfg.desktop.binary_moved_from)

    def test_a_plain_name_is_a_path_lookup_even_inside_the_bundle(self) -> None:
        """"codex" means PATH, whatever directory the runtime happens to be in."""

        self.m.packaged()
        root = write_project(self.m, "codex")
        with contextlib.chdir(self.m.app / "Contents/Resources"), self.m.installed():
            cfg = load_config(root)
        self.assertEqual(cfg.desktop.binary, "codex")
        self.assertIsNone(cfg.desktop.binary_moved_from)

    def test_an_app_that_carries_no_codex_any_more_changes_nothing(self) -> None:
        for name, build in (
            ("no package", lambda: None),
            ("broken manifest", lambda: self.m.packaged("{")),
        ):
            with self.subTest(case=name):
                if self.m.package.exists():
                    shutil.rmtree(self.m.package)
                build()
                root = write_project(self.m, str(self.m.legacy), name=f"project-{name.replace(' ', '-')}")
                with self.m.installed():
                    cfg = load_config(root)
                self.assertEqual(cfg.desktop.binary, str(self.m.legacy))
                self.assertIsNone(cfg.desktop.binary_moved_from)


class TheRunStartsThroughTheMovedCodexTests(unittest.TestCase):
    """The gate that refused on 4 Oct 2026, with a real AppServerClient."""

    def setUp(self) -> None:
        self.m = FakeMachine(self)

    def _trusted(self, cfg) -> None:
        self.m.answer(hooks=[
            {
                "eventName": "stop",
                "handlerType": "command",
                "command": runtime_hook_command(),
                "pluginId": autopilot_plugin_id(cfg.skill_name),
                "enabled": True,
                "trustStatus": "trusted",
                "currentHash": "sha256:trusted",
            }
        ])

    def test_the_hook_gate_reaches_the_packaged_codex_and_sees_the_hook_trusted(self) -> None:
        entry = self.m.packaged()
        root = write_project(self.m, str(self.m.legacy))
        with self.m.installed():
            cfg = load_config(root)
        self._trusted(cfg)
        snapshot = require_trusted_stop_hook_for_config(cfg)
        self.assertEqual(snapshot.trust_status, "trusted")
        self.assertIn([str(entry), "app-server", "--listen", "stdio://"], self.m.calls())

    def test_any_other_missing_binary_is_the_same_clear_error_as_before(self) -> None:
        self.m.packaged()
        missing = self.m.base / "homebrew/Cellar/codex/0.150.0/bin/codex"
        root = write_project(self.m, str(missing))
        with self.m.installed():
            cfg = load_config(root)
        self._trusted(cfg)
        with self.assertRaises(HookPreflightError) as caught:
            require_trusted_stop_hook_for_config(cfg)
        self.assertIn("No such file or directory", str(caught.exception))
        self.assertEqual(self.m.calls(), [], "no other Codex was started in its place")


class DoctorSaysWhatItUsesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = FakeMachine(self)
        self.m.separate_cli("0.154.0")

    def _doctor(self, root: Path) -> tuple[int, str]:
        log = self.m.state / "doctor.jsonl"

        class Client(AppServerClient):
            """The real client; only its wire log stays in the temp dir."""

            def __init__(self, binary, _log_path, **kwargs):
                super().__init__(binary, log, **kwargs)

        out = io.StringIO()
        with self.m.installed(), mock.patch.object(cli, "AppServerClient", Client), contextlib.redirect_stdout(out):
            code = cli.doctor(root)
        return code, out.getvalue()

    def _app_servers(self) -> list[str]:
        return [call[0] for call in self.m.calls() if call[1:2] == ["app-server"]]

    def test_an_old_app_address_is_followed_and_said_in_one_line(self) -> None:
        """With the app's new address doctor passes whole; the note is no failure."""

        entry = self.m.packaged()
        self.m.answer(models=[
            {
                "id": model,
                "model": model,
                "supportedReasoningEfforts": [{"reasoningEffort": item} for item in PUBLIC_REASONING],
            }
            for model in MODEL_IDS.values()
        ])
        root = write_project(self.m, str(self.m.legacy))
        with mock.patch.object(sys, "platform", "darwin"):
            code, output = self._doctor(root)
        self.assertEqual([line for line in output.splitlines() if line.startswith("FAIL")], [])
        self.assertEqual(code, 0)
        self.assertIn(f"PASS  Codex CLI: {entry}", output)
        (note,) = [line for line in output.splitlines() if line.startswith("NOTE  ")]
        self.assertIn(str(self.m.legacy), note)
        self.assertIn(f"using {entry}", note)
        self.assertIn(f'binary = "{entry}"', note)
        self.assertEqual(self._app_servers(), [str(entry)])

    def test_any_other_missing_binary_fails_instead_of_asking_path(self) -> None:
        """The 4 Oct 2026 doctor fell through to Homebrew and blamed the model."""

        self.m.packaged()
        missing = self.m.base / "homebrew/Cellar/codex/0.150.0/bin/codex"
        root = write_project(self.m, str(missing))
        code, output = self._doctor(root)
        self.assertEqual(code, 1)
        self.assertIn(f"FAIL  Codex CLI: {missing} (desktop.binary) not found", output)
        self.assertNotIn("NOTE  ", output)
        self.assertEqual(self._app_servers(), [], "PATH's Codex was not asked in its place")

    def test_a_directory_without_a_project_still_asks_path(self) -> None:
        self.m.packaged()
        bare = self.m.base / "bare"
        bare.mkdir()
        _code, output = self._doctor(bare)
        self.assertIn(f"PASS  Codex CLI: {self.m.cli}", output)
        self.assertEqual(self._app_servers(), [str(self.m.cli)])


class TheStatusCardSaysWhatItUsesTests(unittest.TestCase):
    def _initialized(self, language: str) -> tuple[FakeMachine, Path, Path]:
        from _gates import patch_hook_trust_gates
        from _plan_contract import initialize_verified_project
        from test_desktop_lifecycle import graph

        patch_hook_trust_gates(self)
        m = FakeMachine(self)
        entry = m.packaged()
        root = m.base / "project"
        (root / ".git").mkdir(parents=True)
        skill = m.base / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = m.base / "plan.json"
        plan_file.write_text(json.dumps(graph()), encoding="utf-8")
        # What 0.12.2-0.14.0 wrote for the app's Codex.
        with mock.patch.object(bootstrap, "newest_codex_binary", return_value=str(m.legacy)):
            initialize_verified_project(
                root, plan_file, profile="adaptive", skill_path=skill, language=language
            )
        return m, root, entry

    def test_the_card_and_the_report_carry_one_line(self) -> None:
        from codex_autopilot.control import status_text

        m, root, entry = self._initialized("en")
        with m.installed():
            card = status_text(root, detailed=False)
            report = status_text(root)
        for text in (card, report):
            (line,) = [item for item in text.splitlines() if item.startswith("Codex: ")]
            self.assertIn(str(m.legacy), line)
            self.assertIn(f"using {entry}", line)
            self.assertIn(f'binary = "{entry}"', line)

    def test_the_card_speaks_the_run_language(self) -> None:
        from codex_autopilot.control import status_text

        m, root, entry = self._initialized("ru")
        with m.installed():
            card = status_text(root, detailed=False)
        (line,) = [item for item in card.splitlines() if item.startswith("Codex: ")]
        self.assertIn(f"используется {entry}", line)

    def test_nothing_is_said_while_the_config_is_current(self) -> None:
        from codex_autopilot.control import status_text

        m, root, _entry = self._initialized("en")
        m.old_layout()
        with m.installed():
            for text in (status_text(root, detailed=False), status_text(root)):
                self.assertEqual(
                    [item for item in text.splitlines() if item.startswith("Codex: ")], []
                )


if __name__ == "__main__":
    unittest.main()
