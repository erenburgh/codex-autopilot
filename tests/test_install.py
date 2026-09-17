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
        # Посторонний каталог внутри дерева кэша Codex: именно из такого
        # "отложенного в сторонку" Codex восстановил копию 0.9.0 и снова
        # начал переписывать определение хуков.
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
        # Агент будильника пишется под подменённый HOME и зовёт стабильный
        # путь рантайма - тот, что переживает обновление версии.
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
        # Версия берётся из пакета: прибитая строка разошлась бы при
        # первом же подъёме версии - ровно так и случилось дважды.
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
        # Активный профиль теперь именно снимается и ставится заново, а его
        # кэш вычищается. Прежде плагин оставляли установленным, и Codex
        # продолжал грузить прежнюю копию: у пользователя стоял 0.9.7, а
        # работал 0.9.0 - с Interrupt на 30 секунд, который Codex зажимает
        # до 3 и переписывает файл. Хэш менялся, доверие Stop-хука слетало
        # на каждой загрузке, и выглядело это как "хуки слетают сами".
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
        # Прежние установки больше не лежат рядом с текущей: пока их было
        # тринадцать, любая могла стать источником чужой копии плагина, а
        # разница между "установлено" и "работает" стоила пользователю
        # целой ночи. Они не теряются - складываются в архив.
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


if __name__ == "__main__": unittest.main()
