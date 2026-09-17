"""Пользователь видит, что рантайм чинил сам себя."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_autopilot.runtime_patch_log import applied_patches, render_applied_patches


class AppliedPatchesAreVisibleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def test_an_installation_without_repairs_says_so(self) -> None:
        self.assertEqual(render_applied_patches(self.root), "Runtime patches: none")

    def test_every_applied_repair_is_listed_with_its_modules(self) -> None:
        folder = self.root / "patches" / "patch-0001"
        folder.mkdir(parents=True)
        (folder / "patch.json").write_text(
            json.dumps(
                {
                    "patch_id": "patch-0001",
                    "at": "2026-09-17T00:00:00+00:00",
                    "test_name": "test_x",
                    "changes": [
                        {"module": "status.py", "sha256_before": "a", "sha256_after": "b"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(len(applied_patches(self.root)), 1)
        self.assertEqual(
            render_applied_patches(self.root), "Runtime patches: 1 (status.py)"
        )


class TheCardShowsThemTests(unittest.TestCase):
    def test_the_status_card_names_the_repairs(self) -> None:
        """Строка обязана быть в карточке, а не только в модуле."""

        import inspect

        from codex_autopilot import status

        source = inspect.getsource(status.render_project_status)
        self.assertIn("render_applied_patches", source)
