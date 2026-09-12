"""Внесение ветки в проект записью, которую делает сам Desktop.

Замерено на живом прогоне: привязка через App Server проходит и
возвращает полные метаданные, а Desktop о ветке не узнаёт - размещение
остаётся ABSENT. Его собственная очередь переноса застревает, потому что
обход падает на первой же сбойной ветке и флаг завершения не пишется.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class DesktopAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        patcher = mock.patch(
            "codex_autopilot.preflight.default_codex_home", return_value=self.home
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.home / ".codex-global-state.json"
        self.write(
            **{
                "app-server-project-id-by-legacy-project-id-by-host": {
                    "host": {"legacy-1": "server-1"}
                },
                "projectless-thread-ids": ["t1"],
            }
        )

    def write(self, **keys) -> None:
        self.path.write_text(json.dumps(keys), encoding="utf-8")

    def read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_adoption_writes_the_same_record_desktop_writes(self) -> None:
        from codex_autopilot.launch_gate import (
            INSIDE,
            adopt_into_desktop_project,
            desktop_placement,
        )

        self.assertTrue(adopt_into_desktop_project("t1", "server-1"))
        state = self.read()
        self.assertEqual(
            state["thread-project-assignments"]["t1"],
            {"projectKind": "local", "projectId": "legacy-1"},
        )
        self.assertEqual(
            state["sidebar-project-thread-orders"]["legacy-1"]["threadIds"], ["t1"]
        )
        self.assertEqual(state["projectless-thread-ids"], [])
        self.assertEqual(desktop_placement("t1"), INSIDE)

    def test_an_unmapped_project_is_never_guessed(self) -> None:
        from codex_autopilot.launch_gate import adopt_into_desktop_project

        self.assertFalse(adopt_into_desktop_project("t1", "server-unknown"))
        self.assertNotIn("thread-project-assignments", self.read())

    def test_adoption_is_idempotent(self) -> None:
        from codex_autopilot.launch_gate import adopt_into_desktop_project

        self.assertTrue(adopt_into_desktop_project("t1", "server-1"))
        self.assertFalse(adopt_into_desktop_project("t1", "server-1"))
        self.assertEqual(
            self.read()["sidebar-project-thread-orders"]["legacy-1"]["threadIds"], ["t1"]
        )

    def test_other_keys_are_left_untouched(self) -> None:
        """Трогаются ровно три ключа: состояние Desktop не наше."""

        from codex_autopilot.launch_gate import adopt_into_desktop_project

        self.write(
            **{
                "app-server-project-id-by-legacy-project-id-by-host": {
                    "host": {"legacy-1": "server-1"}
                },
                "local-projects": {"legacy-1": {"name": "P", "rootPaths": ["/x"]}},
                "pinned-thread-ids": ["keep-me"],
            }
        )
        adopt_into_desktop_project("t1", "server-1")
        state = self.read()
        self.assertEqual(state["local-projects"]["legacy-1"]["name"], "P")
        self.assertEqual(state["pinned-thread-ids"], ["keep-me"])

    def test_unreadable_state_is_not_an_exception(self) -> None:
        from codex_autopilot.launch_gate import adopt_into_desktop_project

        self.path.write_text("{ это не json", encoding="utf-8")
        self.assertFalse(adopt_into_desktop_project("t1", "server-1"))

    def test_promotion_falls_back_to_adoption_when_app_server_is_not_enough(self) -> None:
        from codex_autopilot.launch_gate import INSIDE, promote_into_project

        client = mock.Mock()
        client.assign_thread_to_project.return_value = {"projectId": "server-1"}
        before, after = promote_into_project(
            "t1", "server-1", client=client, sleep=lambda _s: None
        )
        client.assign_thread_to_project.assert_called_once()
        self.assertNotEqual(before, INSIDE)
        self.assertEqual(after, INSIDE)

    def test_promotion_from_absent_also_reaches_the_project(self) -> None:
        """Живой случай: Desktop о ветке не знает вовсе."""

        from codex_autopilot.launch_gate import ABSENT, INSIDE, promote_into_project

        self.write(
            **{
                "app-server-project-id-by-legacy-project-id-by-host": {
                    "host": {"legacy-1": "server-1"}
                }
            }
        )
        client = mock.Mock()
        before, after = promote_into_project(
            "t1", "server-1", client=client, sleep=lambda _s: None
        )
        self.assertEqual((before, after), (ABSENT, INSIDE))


if __name__ == "__main__":
    unittest.main()
