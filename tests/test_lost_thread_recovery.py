"""A vanished thread is a reason to create a new one, not to stand forever.

v0.7 created a thread and used it at once, on one connection. v0.8
creates the thread in one process, requires that process to exit fully,
and starts the turn from another process later. In that gap the thread
lives without a subscriber, and after a restart it may no longer exist.

On the live run that is what happened: the planner's reservation stayed
bound to thread 01a0970c, and two hours later turn/start answered
"thread not found". The reservation was forever bound to a dead
identifier, and the run stood.
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
from _plan_contract import initialize_verified_project as initialize_project
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


class OwnerTurnBarrierTests(unittest.TestCase):
    """Ворота открывает ход, который действительно кончился.

    Пока синхронный Stop-хук работает, второй App Server наблюдает тот же
    ход как "interrupted". Замерено в рабочем прогоне 0.7: ход
    01a097aa-4832 виден сначала interrupted, затем completed. Принимать
    одно лишь interrupted значило бы открывать ворота ровно в тот момент,
    от которого барьер и защищает.

    Исключение ровно одно и оно доказуемо: прерывание, записанное в наш
    собственный журнал для этого же хода. Такой ход не станет completed
    никогда, и ждать его - значит ждать вечно.
    """

    @staticmethod
    def _state(journal):
        from codex_autopilot.run_state import RunState

        state = RunState()
        state.lifecycle_journal = list(journal)
        return state

    def test_completed_opens_the_gate(self) -> None:
        from codex_autopilot.lifecycle_dispatch import causal_gate_open

        self.assertTrue(
            causal_gate_open(
                {"id": "T", "status": "completed"},
                self._state([]),
                thread_id="TH",
                turn_id="T",
            )
        )

    def test_a_bare_interrupt_keeps_the_gate_shut(self) -> None:
        """Тот самый миг перед completed, ради которого барьер и написан."""

        from codex_autopilot.lifecycle_dispatch import causal_gate_open

        self.assertFalse(
            causal_gate_open(
                {"id": "T", "status": "interrupted"},
                self._state([]),
                thread_id="TH",
                turn_id="T",
            )
        )

    def test_a_journalled_interrupt_opens_the_gate(self) -> None:
        """Прерывание записано нами - ход кончился и completed не станет."""

        from codex_autopilot.lifecycle_dispatch import causal_gate_open

        journal = [
            {"event": "interrupt_observed", "thread_id": "TH", "turn_id": "T"}
        ]
        self.assertTrue(
            causal_gate_open(
                {"id": "T", "status": "interrupted"},
                self._state(journal),
                thread_id="TH",
                turn_id="T",
            )
        )

    def test_an_interrupt_of_another_turn_proves_nothing(self) -> None:
        from codex_autopilot.lifecycle_dispatch import causal_gate_open

        journal = [
            {"event": "interrupt_observed", "thread_id": "TH", "turn_id": "OTHER"}
        ]
        self.assertFalse(
            causal_gate_open(
                {"id": "T", "status": "interrupted"},
                self._state(journal),
                thread_id="TH",
                turn_id="T",
            )
        )

    def test_a_journalled_interrupt_also_counts_as_a_finished_turn(self) -> None:
        """Тот же вывод на втором барьере - при выборе предшественника.

        Оба гейта ждали `turn_completed`. Прерванный ход его не пишет, и
        преемника было некому поднять ни здесь, ни в диспетчере.
        """

        from codex_autopilot.control import _turn_is_completed

        state = self._state(
            [{"event": "interrupt_observed", "thread_id": "TH", "turn_id": "T"}]
        )
        self.assertTrue(_turn_is_completed(state, "TH", "T"))
        self.assertFalse(_turn_is_completed(state, "TH", "OTHER"))

    def test_a_missing_turn_keeps_the_gate_shut(self) -> None:
        from codex_autopilot.lifecycle_dispatch import causal_gate_open

        self.assertFalse(
            causal_gate_open(None, self._state([]), thread_id="TH", turn_id="T")
        )

    def test_a_proceeding_launch_never_blocks_the_owner_turn(self) -> None:
        from codex_autopilot.control import _launch_report

        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/control.py"
        ).read_text(encoding="utf-8")
        head = source[source.index("def _launch_report") :]
        body = head[: head.index("\ndef ", 1)]
        self.assertIn('return {"continue": True, "systemMessage": report}', body)

    def test_the_worker_result_check_stays_strict(self) -> None:
        """Успех самой работы по-прежнему только "completed"."""

        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_dispatch.py"
        ).read_text(encoding="utf-8")
        self.assertIn('if completed_turn.get("status") != "completed":', source)
