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
