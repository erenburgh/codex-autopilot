"""Поверхность одна, и устаревший конфиг обязан отвергаться при чтении.

В 0.8.1 снят headless_app_server вместе с путём, который не мог
выполниться. В 0.8.2 снят и параметр worker_surface у initialize_project:
аргумент с единственным допустимым значением - ложный выбор.

После этого единственное, что защищает от конфига, оставшегося от 0.8.0 и
называющего снятую поверхность, - проверка при чтении. Набор её стережёт.
"""

from __future__ import annotations

import unittest

from codex_autopilot.config import (
    DESKTOP_OWNED_SURFACE,
    WORKER_SURFACES,
    _worker_surface,
)


class SurfaceSetTests(unittest.TestCase):
    def test_there_is_exactly_one_surface(self) -> None:
        self.assertEqual(WORKER_SURFACES, {DESKTOP_OWNED_SURFACE})


class StaleConfigTests(unittest.TestCase):
    def test_the_live_surface_is_accepted(self) -> None:
        self.assertEqual(_worker_surface(DESKTOP_OWNED_SURFACE), DESKTOP_OWNED_SURFACE)

    def test_a_config_naming_the_removed_surface_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            _worker_surface("headless_app_server")
        self.assertIn("worker_surface", str(caught.exception))

    def test_an_unknown_surface_is_refused(self) -> None:
        for value in ("", "desktop", "DESKTOP_OWNED"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _worker_surface(value)


class WrittenConfigTests(unittest.TestCase):
    def test_bootstrap_writes_the_surface_explicitly(self) -> None:
        """Поле пишется явно, чтобы конфиг читался без знания умолчаний."""

        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/bootstrap.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'f"worker_surface = {_toml_string(DESKTOP_OWNED_SURFACE)}"', source
        )

    def test_bootstrap_takes_no_surface_argument(self) -> None:
        import inspect

        from codex_autopilot.bootstrap import initialize_project

        self.assertNotIn(
            "worker_surface", inspect.signature(initialize_project).parameters
        )


if __name__ == "__main__":
    unittest.main()


class FacadeBoundaryTests(unittest.TestCase):
    """Фасад реэкспортирует ровно то, что через него импортируют.

    Механическое разрезание монолита протащило в фасад 91 имя, из них 47
    приватных. Приватный помощник публичным API не был никогда, а его
    присутствие делало границу модуля неотличимой от его содержимого.
    """

    def test_the_facade_exports_no_private_names(self) -> None:
        from codex_autopilot import lifecycle

        private = sorted(n for n in lifecycle.__all__ if n.startswith("_"))
        self.assertEqual(private, [])

    def test_every_exported_name_resolves(self) -> None:
        from codex_autopilot import lifecycle

        missing = [n for n in lifecycle.__all__ if not hasattr(lifecycle, n)]
        self.assertEqual(missing, [])

    def test_the_facade_exports_nothing_nobody_imports(self) -> None:
        import ast
        from pathlib import Path

        from codex_autopilot import lifecycle

        root = Path(__file__).resolve().parents[1]
        wanted: set[str] = set()
        for path in list((root / "src/codex_autopilot").glob("*.py")) + list(
            (root / "tests").glob("*.py")
        ):
            if path.name == "lifecycle.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module in {
                    "lifecycle",
                    "codex_autopilot.lifecycle",
                }:
                    wanted.update(alias.name for alias in node.names)
        self.assertEqual(sorted(set(lifecycle.__all__)), sorted(wanted))
