"""Повторная сверка - не конфликт.

`reconcile_running_work` снимает зависшие сессии: задачу, чей ход уже
не идёт, она переводит в RETRY_WAIT. Но перед этим требовала, чтобы
задача была в активном состоянии, - и отказывала, если задача **уже**
в RETRY_WAIT, то есть ровно в том состоянии, которое сама же ставит
следующей строкой.

Замерено 16.09.2026: прогон встал намертво. Ход инженера завершился,
сессия осталась висеть, M8 был переведён в RETRY_WAIT предыдущей
сверкой - и каждая попытка возобновления отвечала «pending session for
M8 is not in an active task state». Возобновить прогон стало нельзя
ничем: единственный путь запуска упирался в отказ сделать уже сделанное.
"""

from __future__ import annotations

from pathlib import Path

import unittest

from codex_autopilot.task_state import ACTIVE_TASK_STATES, TaskState


class ReconcileIdempotenceTests(unittest.TestCase):
    def test_retry_wait_is_not_an_active_state(self) -> None:
        """Условие отказа опиралось именно на это."""

        self.assertNotIn(TaskState.RETRY_WAIT, ACTIVE_TASK_STATES)

    def _state_with_pending_session(self, task_state: str):
        from codex_autopilot.run_state import RunState

        return RunState(
            status="RUNNING",
            phase="DESKTOP_WORKERS_ACTIVE",
            graph_version=1,
            task_states={"A": task_state},
            active_task_ids=[],
            worker_sessions=[
                {
                    "reservation_token": "token-a",
                    "resource_ownership_token": "token-a",
                    "task_id": "A",
                    "kind": "implementation",
                    "status": "ACTIVE",
                    "attempt": 1,
                    "thread_id": "thread-a",
                    "turn_id": "turn-a",
                }
            ],
        )

    def _plan(self):
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _plan_contract import canonical_verification, canonicalize_plan
        from codex_autopilot.plan import validate_plan

        return validate_plan(
            canonicalize_plan(
                {
                    "schema_version": 3,
                    "graph_version": 1,
                    "goal": "Reconcile a stuck session.",
                    "user_request": "Reconcile a stuck session exactly as specified.",
                    "model_strategy": "auto",
                    "roles": [
                        {"id": "b", "name": "B", "responsibilities": ["Do work."]}
                    ],
                    "tasks": [
                        {
                            "id": "A",
                            "title": "A",
                            "objective": "Do A.",
                            "definition_of_done": ["A done"],
                            "execution_mode": "code",
                            "execution_mode_reason": "Files suffice.",
                            "reasoning": "medium",
                            "role": "b",
                            "depends_on": [],
                            "priority": 0,
                            "verification": canonical_verification(),
                            "resources": [
                                {
                                    "id": "t",
                                    "kind": "directory",
                                    "target": "src",
                                    "access": "write",
                                }
                            ],
                        }
                    ],
                }
            ),
            "adaptive",
        )

    def test_a_pending_session_on_an_already_reconciled_task_is_accepted(self) -> None:
        """Задача уже в целевом состоянии - сверке нечего делать.

        Прежде здесь поднимался конфликт, и возобновить прогон было
        нечем: единственный путь запуска отказывался сделать уже
        сделанное.
        """

        from codex_autopilot.resilience import reconcile_running_work

        result = reconcile_running_work(
            self._plan(),
            self._state_with_pending_session(TaskState.RETRY_WAIT.value),
            {"token-a": "terminal"},
            now_epoch=0,
            retry_delay_seconds=15,
        )
        self.assertIsNotNone(result)

    def test_a_stuck_engineer_session_never_demands_an_active_task(self) -> None:
        """Инженер чинит инцидент, а не выполняет задачу.

        Его сессия намеренно создаётся без перевода задачи в активное
        состояние. Прежде сверка требовала активности от любой
        pending-сессии, и зависший ход инженера делал возобновление
        невозможным: 16.09.2026 M8 был READY, а каждый `Resume`
        отвечал «pending session for M8 is not in an active task
        state». Прогон нельзя было запустить ничем.
        """

        from codex_autopilot.resilience import reconcile_running_work

        state = self._state_with_pending_session(TaskState.READY.value)
        state.worker_sessions[0]["kind"] = "pipeline_engineer"

        result = reconcile_running_work(
            self._plan(), state, {"token-a": "terminal"},
            now_epoch=0, retry_delay_seconds=15,
        )
        self.assertIsNotNone(result)
        self.assertEqual(state.task_states["A"], TaskState.READY.value)
        self.assertEqual(state.worker_sessions[0]["status"], "RETRY_WAIT")

    def test_a_genuinely_wrong_state_is_still_a_conflict(self) -> None:
        """Идемпотентность не должна превратиться в всепрощение."""

        from codex_autopilot.resilience import (
            PlanChangeConflictError,
            reconcile_running_work,
        )

        with self.assertRaises(PlanChangeConflictError):
            reconcile_running_work(
                self._plan(),
                self._state_with_pending_session(TaskState.VERIFIED.value),
                {"token-a": "terminal"},
                now_epoch=0,
                retry_delay_seconds=15,
            )


if __name__ == "__main__":
    unittest.main()
