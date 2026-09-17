"""A fresh install of the sources must pass without network.

The build required setuptools from the index, and in an empty offline
venv `pip install .` failed before it even got to the tests: a new user
could not install the product at all. Yet the package is pure Python on
the standard library and needs no external builder.

The test installs the tree the way a new user will: from the sources,
with the index closed.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parent.parent


class OfflineSourceInstallTests(unittest.TestCase):
    def test_the_build_system_requires_nothing_from_the_network(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            build_system = tomllib.load(handle)["build-system"]
        self.assertEqual(
            build_system.get("requires"),
            [],
            "внешнее требование сборки закрывает установку без сети",
        )
        backend_path = build_system.get("backend-path")
        self.assertTrue(backend_path, "backend обязан лежать в дереве")
        module = build_system["build-backend"].split(":", 1)[0].split(".", 1)[0]
        located = [
            ROOT / entry / f"{module}.py" for entry in backend_path
        ] + [ROOT / entry / module / "__init__.py" for entry in backend_path]
        self.assertTrue(
            any(path.is_file() for path in located),
            f"backend {module!r} не найден ни в одном из {backend_path}",
        )

    def test_a_fresh_offline_install_of_the_sources_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "site"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--disable-pip-version-check",
                    "--no-warn-script-location",
                    "--target",
                    str(target),
                    str(ROOT),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"установка без сети упала:\n{completed.stdout}\n{completed.stderr}",
            )
            self.assertTrue((target / "codex_autopilot" / "cli.py").is_file())
            dist_info = list(target.glob("codex_autopilot-*.dist-info"))
            self.assertEqual(len(dist_info), 1, "ровно один dist-info ожидается")
            entry_points = (dist_info[0] / "entry_points.txt").read_text(encoding="utf-8")
            self.assertIn("codex-autopilot = codex_autopilot.cli:main", entry_points)

            probe = subprocess.run(
                [sys.executable, "-c", "import codex_autopilot.cli as cli; print(cli.__file__)"],
                capture_output=True,
                text=True,
                cwd=raw,
                env={"PYTHONPATH": str(target), "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertTrue(probe.stdout.strip().startswith(str(target)))


if __name__ == "__main__":
    unittest.main()
