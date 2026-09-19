"""Repeated reconciliation is not a conflict.

`reconcile_running_work` clears hung sessions: a task whose turn no
longer runs it moves to RETRY_WAIT. But before that it required the task
to be in an active state - and refused if the task was **already** in
RETRY_WAIT, that is, exactly the state it sets on the next line.

Measured on 16 Sep 2026: the run stopped dead. The engineer's turn
completed, the session stayed hanging, M8 had been moved to RETRY_WAIT
by the previous reconciliation - and every resume attempt answered
«pending session for M8 is not in an active task state». Nothing could
resume the run: the only launch path ran into a refusal to do what was
already done.
"""

from __future__ import annotations

from pathlib import Path

import unittest

from codex_autopilot.task_state import ACTIVE_TASK_STATES, TaskState


class ReconcileIdempotenceTests(unittest.TestCase):
    def test_retry_wait_is_not_an_active_state(self) -> None:
        """The refusal condition rested on exactly this."""

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
        """The task is already in the target state - nothing to reconcile.

        This used to raise a conflict, and there was nothing left to
        resume the run with: the only launch path refused to do what had
        already been done.
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
        """The engineer repairs an incident, it does not run a task.

        The engineer's session is created deliberately without moving the
        task into an active state. Reconciliation used to demand activity
        from any pending session, and a hung engineer turn made resuming
        impossible: on 16.09.2026 M8 was READY, and every `Resume`
        answered "pending session for M8 is not in an active task
        state". Nothing could start the run.
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
        """Idempotence must not turn into forgiving everything."""

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
