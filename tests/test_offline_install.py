"""Свежая установка исходников обязана проходить без сети.

Сборка требовала setuptools из индекса, и в пустом venv без сети
`pip install .` падал ещё до того, как доходило до тестов: новый
пользователь не мог поставить продукт вообще. Пакет при этом - чистый
Python на стандартной библиотеке, и внешнего сборщика ему не нужно.

Тест ставит дерево так, как его поставит новый пользователь: из
исходников, с закрытым индексом.
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
