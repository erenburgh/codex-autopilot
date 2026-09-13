"""Версия объявляется в одном месте, остальные с ней сверяются.

За день одна и та же болезнь нашлась трижды, и каждый раз это была
отдельная прибитая строка, разошедшаяся с пакетом:

- версия MCP-сервера памяти (снято в 0.8.1);
- версия клиента в рукопожатии App Server: сервер слышал
  "codex-autopilot; 0.8.0-beta", когда установлена была 0.8.2;
- VERSION в scripts/build_release.py: сборка релиза назвала бы архив
  двумя версиями назад.

Тест не даёт появиться четвёртой.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from codex_autopilot import __version__

ROOT = Path(__file__).resolve().parents[1]


def _pep440(version: str) -> str:
    """X.Y.Z-beta -> X.Y.Zb0: та же версия в записи, которую требует pyproject."""

    return version.replace("-beta", "b0")


class VersionSingleSourceTests(unittest.TestCase):
    def test_pyproject_matches_the_package(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        declared = re.search(r'(?m)^version = "([^"]+)"', text)
        self.assertIsNotNone(declared)
        self.assertEqual(declared.group(1), _pep440(__version__))

    def test_the_installer_matches_the_package(self) -> None:
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        declared = re.search(r'(?m)^version="([^"]+)"', text)
        self.assertIsNotNone(declared)
        self.assertEqual(declared.group(1), __version__)

    def test_both_plugin_manifests_match_the_package(self) -> None:
        for manifest in sorted(ROOT.glob("plugins/*/.codex-plugin/plugin.json")):
            with self.subTest(manifest=manifest.parent.parent.name):
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                # Установщик дописывает cachebuster уже в установленную
                # копию; в исходнике версия голая.
                self.assertEqual(str(payload["version"]), __version__)

    def test_the_release_script_reads_the_package(self) -> None:
        text = (ROOT / "scripts/build_release.py").read_text(encoding="utf-8")
        self.assertIn("_package_version()", text)
        self.assertNotIn('VERSION = "0.8', text)

    def test_no_test_hardcodes_a_version_literal(self) -> None:
        """Тест с прибитой версией ломается при подъёме - и ломался дважды.

        test_install и test_mcp сверяли версию строкой и оба упали на
        переходе к 0.9.0. Это тот же дефект, что и в продакшене, только
        дороже: он срабатывает ровно в момент релиза.
        """

        # Ищется именно ТЕКУЩАЯ версия: старые номера в фикстурах
        # законны - они изображают прежние установки, которые
        # установщик обязан сохранить, и при подъёме не ломаются.
        current = {__version__, _pep440(__version__)}
        offenders = []
        for path in sorted((ROOT / "tests").glob("*.py")):
            if path.name == Path(__file__).name:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if any(f'"{value}"' in line or f"'{value}'" in line for value in current):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(
            offenders,
            [],
            "тест сверяется с текущей версией буквой и сломается при её "
            "подъёме: " + ", ".join(offenders),
        )

    def test_no_module_hardcodes_a_version_literal(self) -> None:
        """Единственное место, где версия записана буквой, - сам пакет."""

        pattern = re.compile(r'"\d+\.\d+\.\d+(-beta|b\d+)?"')
        offenders = []
        for path in sorted((ROOT / "src" / "codex_autopilot").glob("*.py")):
            if path.name == "__init__.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if pattern.search(line) and "version" in line.lower():
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [], "версия записана буквой: " + ", ".join(offenders))


if __name__ == "__main__":
    unittest.main()
