from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import re
import unittest

from codex_autopilot import __version__


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_clean_install_reinstall_and_legacy_replacement(self):
        base = Path(tempfile.mkdtemp(prefix="codex-autopilot-install-"))
        home = base / "home"
        install_root = base / "runtime"
        preserved_v06 = install_root / "0.6.0-beta" / "preserved-marker"
        preserved_v06.parent.mkdir(parents=True)
        preserved_v06.write_text("keep v0.6", encoding="utf-8")
        preserved_v07 = install_root / "0.7.0-beta" / "preserved-marker"
        preserved_v07.parent.mkdir(parents=True)
        preserved_v07.write_text("keep v0.7", encoding="utf-8")
        # A stray directory inside the Codex cache tree: from exactly such a
        # copy "set aside" Codex restored 0.9.0 and again
        # started rewriting the hook definition.
        stray = home / ".codex/plugins/cache/codex-autopilot-local/.stale-backup-20260101/0.9.0-beta"
        stray.mkdir(parents=True)
        (stray / "marker").write_text("stale", encoding="utf-8")
        legacy = home / ".codex/skills/astra-autopilot-adaptive"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("old preview", encoding="utf-8")
        fake_codex = base / "codex"
        calls = base / "codex-calls"
        fake_codex.write_text(f'''#!/bin/sh\necho "$*" >> "{calls}"\ncase "$1 $2" in\n  "app-server --help"|"login status") exit 0 ;;\n  *) exit 0 ;;\nesac\n''', encoding="utf-8")
        fake_codex.chmod(0o755)
        env = os.environ.copy()
        env.update({"HOME": str(home), "CODEX_AUTOPILOT_INSTALL_ROOT": str(install_root), "CODEX_AUTOPILOT_CODEX_BIN": str(fake_codex), "CODEX_AUTOPILOT_PYTHON": os.environ.get("PYTHON", "python3"), "CODEX_AUTOPILOT_SKIP_LAUNCHD": "1"})
        hook_commands = []
        installed_versions = []
        for _ in range(2):
            result = subprocess.run([str(ROOT / "install.sh"), "--profile", "adaptive"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed_hook = json.loads(
                (install_root / "current/plugins/codex-autopilot-adaptive/hooks/hooks.json").read_text(encoding="utf-8")
            )
            hook_commands.append(installed_hook["hooks"]["Stop"][0]["hooks"][0]["command"])
            self.assertNotIn("PostToolUse", installed_hook["hooks"])
            installed_versions.append(json.loads(
                (install_root / "current/plugins/codex-autopilot-adaptive/.codex-plugin/plugin.json").read_text(encoding="utf-8")
            )["version"])
        self.assertTrue((install_root / "current/bin/codex-autopilot").is_file())
        # The runtime is installed as a repository-shaped tree: without it the
        # suite on the installed copy is red, and the engineer cannot prove
        # a single repair.
        for item in ("src", "tests", "scripts", "plugins", "docs", "pyproject.toml", "install.sh"):
            self.assertTrue((install_root / "current/runtime" / item).exists(), item)
        # The wake-up agent is written under the substituted HOME and calls the
        # stable runtime path - the one that survives a version upgrade.
        import plistlib

        plist = home / "Library/LaunchAgents/com.codex-autopilot.wake.plist"
        self.assertTrue(plist.is_file(), "агент будильника не установлен")
        agent = plistlib.loads(plist.read_bytes())
        self.assertEqual(agent["Label"], "com.codex-autopilot.wake")
        self.assertEqual(
            agent["ProgramArguments"],
            [str(install_root / "current/bin/codex-autopilot"), "_wake-sweep"],
        )
        self.assertEqual(agent["StartInterval"], 300)
        self.assertTrue(agent["RunAtLoad"])
        mcp = (install_root / "current/plugins/codex-autopilot-adaptive/.mcp.json").read_text(encoding="utf-8")
        self.assertNotIn("__CODEX_AUTOPILOT_RUNTIME__", mcp)
        stable_runtime = (
            (install_root / __version__).resolve().parent
            / "current/bin/codex-autopilot"
        )
        self.assertIn(str(stable_runtime), mcp)
        self.assertEqual(hook_commands[0], hook_commands[1])
        self.assertEqual(
            hook_commands[0],
            f'"{stable_runtime}" hook',
        )
        self.assertNotIn("/0.8.2-beta/bin/codex-autopilot", hook_commands[0])
        self.assertNotIn("plugins/cache", hook_commands[0])
        # The version comes from the package: a hard-coded string would diverge
        # at the first version bump - exactly what happened twice.
        self.assertTrue(
            all(value.startswith(f"{__version__}.local.") for value in installed_versions)
        )
        installed_manifest = json.loads(
            (install_root / "current/plugins/codex-autopilot-adaptive/.codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertRegex(
            installed_manifest["version"],
            rf"^{re.escape(__version__)}\.local\.\d{{8}}\.\d{{6}}$",
        )
        self.assertTrue((install_root / "legacy-backups/astra-autopilot-adaptive/SKILL.md").is_file())
        self.assertFalse(legacy.exists())
        command_text = calls.read_text()
        self.assertIn("plugin marketplace add", command_text)
        self.assertIn("plugin add codex-autopilot-adaptive@codex-autopilot-local", command_text)
        self.assertNotIn("plugin marketplace remove", command_text)
        # The active profile is now really removed and installed anew, and its
        # cache is cleared. The plugin used to be left installed, and Codex
        # kept loading the old copy: the user had 0.9.7 installed while
        # 0.9.0 ran - with the 30-second Interrupt that Codex clamps
        # to 3, rewriting the file. The hash changed, Stop-hook trust dropped
        # on every load, and it looked like "the hooks drop by themselves".
        self.assertIn(
            "plugin remove codex-autopilot-adaptive@codex-autopilot-local", command_text
        )
        self.assertLess(
            command_text.index("plugin remove codex-autopilot-adaptive@codex-autopilot-local"),
            command_text.index("plugin add codex-autopilot-adaptive@codex-autopilot-local"),
            "снятие обязано идти до установки, иначе кэш не обновится",
        )
        self.assertNotIn("config set", command_text)
        self.assertNotIn("danger", command_text)
        env["PATH"] = str(base) + os.pathsep + env.get("PATH", "")
        result = subprocess.run([str(install_root / "current/bin/codex-autopilot"), "uninstall", "--yes"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((install_root / __version__).exists())
        self.assertFalse((install_root / "current").exists())
        # Previous installations no longer lie next to the current one: while
        # there were thirteen, any could become the source of a foreign plugin
        # copy, and the gap between "installed" and "running" cost the user
        # a whole night. They are not lost - they go into an archive.
        self.assertFalse(preserved_v06.exists())
        self.assertFalse(preserved_v07.exists())
        self.assertFalse(
            stray.parent.exists(),
            "постороннее в дереве кэша Codex обязано быть убрано: оттуда возвращается чужая копия",
        )
        archives = sorted((install_root / "legacy-backups").glob("previous-installs-*.zip"))
        self.assertTrue(archives, "прежние установки обязаны сохраниться в архиве")
        import zipfile

        kept = {}
        for archive in archives:
            with zipfile.ZipFile(archive) as bundle:
                for name in bundle.namelist():
                    if name.endswith("preserved-marker"):
                        kept[name] = bundle.read(name).decode("utf-8")
        self.assertIn("0.6.0-beta/preserved-marker", kept)
        self.assertIn("0.7.0-beta/preserved-marker", kept)
        self.assertEqual(kept["0.6.0-beta/preserved-marker"], "keep v0.6")
        self.assertEqual(kept["0.7.0-beta/preserved-marker"], "keep v0.7")
        self.assertTrue((install_root / "legacy-backups/astra-autopilot-adaptive/SKILL.md").is_file())


class RepairedInstallationSurvivesReinstallTests(unittest.TestCase):
    """R28 for the installation itself: a repaired tree is moved aside.

    The runtime repairs its own code: the gateway writes accepted patches
    into ``runtime/patches`` and the repaired sources into ``runtime/src``,
    both inside the installed version directory. ``install.sh`` began with
    ``rm -rf "$target"``, and the archive loop at its end skips the current
    version by name - so reinstalling the SAME version deleted every
    accepted repair and left ``legacy-backups`` empty. Measured before this
    test existed; the self-repair the product promises did not survive an
    ordinary reinstall.
    """

    def _install(self, env, *, check=True):
        result = subprocess.run(
            [str(ROOT / "install.sh"), "--profile", "adaptive"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_a_reinstall_moves_an_accepted_repair_aside_instead_of_deleting_it(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="codex-autopilot-repaired-"))
        home = base / "home"
        install_root = base / "runtime"
        fake_codex = base / "codex"
        fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_codex.chmod(0o755)
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "CODEX_AUTOPILOT_INSTALL_ROOT": str(install_root),
            "CODEX_AUTOPILOT_CODEX_BIN": str(fake_codex),
            "CODEX_AUTOPILOT_PYTHON": os.environ.get("PYTHON", "python3"),
            "CODEX_AUTOPILOT_SKIP_LAUNCHD": "1",
        })
        self._install(env)
        target = install_root / __version__

        # What an accepted repair leaves behind: the catalogue entry with the
        # original it can be reverted to, and the repaired source in place.
        patch_dir = target / "runtime/patches/patch-e2e"
        patch_dir.mkdir(parents=True)
        (patch_dir / "patch.json").write_text(
            json.dumps({"patch_id": "patch-e2e", "test_name": "test_repro"}),
            encoding="utf-8",
        )
        (patch_dir / "status.py.orig").write_text("original\n", encoding="utf-8")
        repaired_source = target / "runtime/src/codex_autopilot/status.py"
        repaired_source.write_text(
            repaired_source.read_text(encoding="utf-8") + "\nREPAIRED = True\n",
            encoding="utf-8",
        )

        result = self._install(env)

        aside = sorted(install_root.glob(f"{__version__}.repaired-*"))
        self.assertEqual(len(aside), 1, f"repaired tree was not kept: {list(install_root.iterdir())}")
        kept = aside[0]
        self.assertIn(str(kept), result.stdout)
        self.assertTrue((kept / "runtime/patches/patch-e2e/patch.json").is_file())
        self.assertTrue((kept / "runtime/patches/patch-e2e/status.py.orig").is_file())
        self.assertIn(
            "REPAIRED = True",
            (kept / "runtime/src/codex_autopilot/status.py").read_text(encoding="utf-8"),
        )
        # The fresh installation is the shipped code, not the repaired one.
        self.assertNotIn(
            "REPAIRED = True",
            (target / "runtime/src/codex_autopilot/status.py").read_text(encoding="utf-8"),
        )
        self.assertFalse((target / "runtime/patches/patch-e2e").exists())

    def test_an_unrepaired_reinstall_leaves_nothing_aside(self) -> None:
        """No repairs, nothing irreplaceable: the tree is rebuilt in place."""

        base = Path(tempfile.mkdtemp(prefix="codex-autopilot-plain-"))
        home = base / "home"
        install_root = base / "runtime"
        fake_codex = base / "codex"
        fake_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_codex.chmod(0o755)
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "CODEX_AUTOPILOT_INSTALL_ROOT": str(install_root),
            "CODEX_AUTOPILOT_CODEX_BIN": str(fake_codex),
            "CODEX_AUTOPILOT_PYTHON": os.environ.get("PYTHON", "python3"),
            "CODEX_AUTOPILOT_SKIP_LAUNCHD": "1",
        })
        self._install(env)
        self._install(env)
        self.assertEqual(list(install_root.glob(f"{__version__}.repaired-*")), [])


if __name__ == "__main__": unittest.main()
