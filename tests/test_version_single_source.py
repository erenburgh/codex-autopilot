"""The version is declared in one place; the rest are checked against it.

The same disease was found three times in one day, each time a separate
hard-coded string that had diverged from the package:

- the memory MCP server version (removed in 0.8.1);
- the client version in the App Server handshake: the server heard
  "codex-autopilot; 0.8.0-beta" while 0.8.2 was installed;
- VERSION in scripts/build_release.py: the release build would have
  named the archive two versions back.

The test does not let a fourth appear.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from codex_autopilot import __version__

ROOT = Path(__file__).resolve().parents[1]


def _pep440(version: str) -> str:
    """X.Y.Z-beta -> X.Y.Zb0: the same version in the spelling pyproject
    requires."""

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
                # The installer appends the cachebuster to the installed
                # copy; in the source the version is bare.
                self.assertEqual(str(payload["version"]), __version__)

    def test_the_release_script_reads_the_package(self) -> None:
        text = (ROOT / "scripts/build_release.py").read_text(encoding="utf-8")
        self.assertIn("_package_version()", text)
        self.assertNotIn('VERSION = "0.8', text)

    def test_no_test_hardcodes_a_version_literal(self) -> None:
        """A test with a hard-coded version breaks on a bump - and broke
        twice.

        test_install and test_mcp compared the version as a string and
        both failed on the move to 0.9.0. It is the same defect as in
        production, only more expensive: it fires exactly at the moment
        of release.
        """

        # The CURRENT version is what is searched for: old numbers in
        # fixtures are legitimate - they depict previous installations
        # the installer must keep, and they do not break on a bump.
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
        """The only place where the version is written out in letters is
        the package itself."""

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
