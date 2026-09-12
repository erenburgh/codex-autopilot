"""Исчезнувшая ветка - повод создать новую, а не повод встать навсегда.

v0.7 создавала ветку и тут же ею пользовалась, одним соединением. v0.8
создаёт ветку в одном процессе, требует его полного выхода и стартует ход
другим процессом позже. В этом промежутке ветка живёт без подписчика, и
после перезапуска её может уже не быть.

В живом прогоне так и вышло: резервация планировщика осталась привязанной
к ветке 01a0970c, а через два часа turn/start ответил
"thread not found". Резервация оказалась навечно привязана к мёртвому
идентификатору, и прогон стоял.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _gates import patch_hook_trust_gates
from _relay import reserve_ready_frontier
from codex_autopilot.appserver import AppServerRpcError
from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
from codex_autopilot.lifecycle_dispatch import (
    _reset_to_create_requested,
    _thread_is_gone,
)
from codex_autopilot.run_state import StateStore
from test_desktop_lifecycle import graph


class LostThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(graph()), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
            worker_surface=DESKTOP_OWNED_SURFACE,
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)
        self.token = reserve_ready_frontier(self.cfg)[0].reservation_token
        self.bind_thread("thread-gone")

    def bind_thread(self, thread_id: str) -> None:
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == self.token
        )
        session["thread_id"] = thread_id
        session["status"] = "PREPARED"
        self.store.save(state)

    def session(self) -> dict:
        return next(
            item
            for item in self.store.load().worker_sessions
            if item["reservation_token"] == self.token
        )

    def client_raising(self, error: Exception):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.read_thread.side_effect = error
        return client

    def test_a_missing_thread_is_detected(self) -> None:
        client = self.client_raising(
            AppServerRpcError(
                "thread/read",
                {"code": -32600, "message": "thread not found: thread-gone"},
            )
        )
        self.assertTrue(
            _thread_is_gone(self.cfg, self.token, connected_client=client)
        )

    def test_a_live_thread_is_not_treated_as_missing(self) -> None:
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.read_thread.return_value = {"id": "thread-gone"}
        self.assertFalse(
            _thread_is_gone(self.cfg, self.token, connected_client=client)
        )

    def test_a_transport_error_is_not_treated_as_missing(self) -> None:
        """Иначе временный сбой связи раздвоил бы работу живой ветки."""

        client = self.client_raising(OSError("connection reset"))
        self.assertFalse(
            _thread_is_gone(self.cfg, self.token, connected_client=client)
        )

    def test_an_unbound_reservation_is_not_missing(self) -> None:
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == self.token
        )
        session["thread_id"] = None
        self.store.save(state)
        self.assertFalse(_thread_is_gone(self.cfg, self.token))

    def test_reset_unbinds_the_dead_thread_and_asks_for_a_new_one(self) -> None:
        _reset_to_create_requested(self.cfg, self.token)
        session = self.session()
        self.assertEqual(session["status"], "CREATE_REQUESTED")
        self.assertIsNone(session["thread_id"])
        self.assertIsNone(session["creation_transport"])
        self.assertIsNone(session["app_server_create_exited_at"])

    def test_reset_keeps_the_reservation_and_its_causal_owner(self) -> None:
        """Пересоздаётся ветка, а не задача: владелец и слот те же."""

        before = self.session()
        _reset_to_create_requested(self.cfg, self.token)
        after = self.session()
        self.assertEqual(after["reservation_token"], before["reservation_token"])
        self.assertEqual(
            after["relay_owner_thread_id"], before["relay_owner_thread_id"]
        )
        self.assertEqual(after["task_id"], before["task_id"])

    def test_the_loss_is_recorded_in_the_journal(self) -> None:
        _reset_to_create_requested(self.cfg, self.token)
        events = [
            item
            for item in self.store.load().lifecycle_journal
            if item["event"] == "lost_thread_recreate_requested"
        ]
        self.assertEqual(len(events), 1)
        self.assertIn("thread-gone", events[0]["detail"])


if __name__ == "__main__":
    unittest.main()


class FinishedOwnerTurnTests(unittest.TestCase):
    """Барьер причинности ждёт конца хода владельца, а не его успеха.

    Stop-хук обязан вернуть decision "block", иначе Codex не покажет отчёт
    о запуске. Ход, чей Stop-хук ответил block, завершается со статусом
    "interrupted". Барьер, принимавший только "completed", ждал его до
    таймаута: показ лестницы и запуск исключали друг друга.
    """

    def test_an_interrupted_owner_turn_counts_as_finished(self) -> None:
        from codex_autopilot.lifecycle_dispatch import FINISHED_TURN_STATUSES

        self.assertIn("interrupted", FINISHED_TURN_STATUSES)

    def test_a_running_owner_turn_does_not(self) -> None:
        from codex_autopilot.lifecycle_dispatch import FINISHED_TURN_STATUSES

        for ongoing in ("in_progress", "queued", "pending", None):
            self.assertNotIn(ongoing, FINISHED_TURN_STATUSES)

    def test_the_worker_result_check_stays_strict(self) -> None:
        """Успех самой работы по-прежнему только "completed"."""

        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_dispatch.py"
        ).read_text(encoding="utf-8")
        self.assertIn('if completed_turn.get("status") != "completed":', source)
