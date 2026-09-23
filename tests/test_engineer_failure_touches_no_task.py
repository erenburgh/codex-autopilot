"""The on-call's own failed turn retires its session and nothing else.

Since the engineer works next to the run, its anchor task is either a task
its ticket holds (READY, IMPLEMENTED or BLOCKED) or only a context task -
which may be RUNNING under a worker of its own at that very moment. The
independent check drove ``record_desktop_failure`` with the engineer's
session through the definitive branch that every worker takes: it moved the
anchor to RETRY_WAIT. With a running neighbour as the anchor, that neighbour
was broken (RUNNING -> RETRY_WAIT, emptied active_task_ids while its worker
was still being created); with a held READY anchor, the record itself raised
IllegalTaskTransition, the dispatcher died, and the engineer's session stayed
ACTIVE until a sweep.

Calls are made in the dispatcher's own form (``lifecycle_dispatch``):
``reserve_other_ready=False`` and the relay owner as executor.
Only fakes: no live Codex and no App Server is started here.
"""

from __future__ import annotations

import unittest

from _relay import TEST_RELAY_OWNER, reserve_ready_frontier
from codex_autopilot.blocked_runs import stop_run
from codex_autopilot.lifecycle_failures import record_desktop_failure
from codex_autopilot.pipeline_engineer import IncidentPhase
from test_a_dead_dispatcher_strands_nothing import _Base


class TheEngineersFailedTurnTouchesNoTaskTests(_Base):
    def _fail(self, token: str, *, failure_code: str = "turn_ended_non_completed") -> None:
        state = self.store.load()
        session = next(i for i in state.worker_sessions if i["reservation_token"] == token)
        session["status"] = "ACTIVE"
        session["thread_id"] = f"thread-{token[:6]}"
        self.store.save(state)
        record_desktop_failure(
            self.cfg,
            token,
            reason="App Server production turn ended non-completed: failed",
            failure_code=failure_code,
            definitive=True,
            thread_id=session["thread_id"],
            turn_id="turn-1",
            reserve_other_ready=False,
            relay_executor_thread_id=TEST_RELAY_OWNER,
        )

    def test_a_running_context_anchor_keeps_its_worker(self) -> None:
        stop_run(
            self.cfg,
            self.store.load(),
            stop_kind="no_successor",
            phase="PIPELINE_ENGINEER_NO_SUCCESSOR",
            reason="nothing was reserved",
            summary="s.",
            at="2026-09-23T15:02:27+00:00",
            context_task_id="A",
        )
        reserved = reserve_ready_frontier(self.cfg)
        self.assertEqual(
            sorted((item.kind, item.task_id) for item in reserved),
            [("implementation", "A"), ("pipeline_engineer", "A")],
        )
        engineer = next(item for item in reserved if item.kind == "pipeline_engineer")

        self._fail(engineer.reservation_token)

        state = self.store.load()
        self.assertEqual(state.task_states["A"], "RUNNING")
        self.assertEqual(state.active_task_ids, ["A"])
        self.assertNotIn("A", state.task_retry_at)
        worker = next(i for i in state.worker_sessions if i["kind"] == "implementation")
        self.assertEqual(worker["status"], "CREATE_REQUESTED")
        self.assertEqual(self._session(engineer.reservation_token)["status"], "RETRY_WAIT")

    def test_a_held_anchor_does_not_raise_and_the_lane_is_free(self) -> None:
        incident_id = str(
            stop_run(
                self.cfg,
                self.store.load(),
                stop_kind="worker_blocked",
                phase="BLOCKED",
                reason="A waits for a repair",
                summary="s.",
                at="2026-09-23T15:02:27+00:00",
                task_ids=("A",),
            )
        )
        reserved = reserve_ready_frontier(self.cfg)
        engineer = next(item for item in reserved if item.kind == "pipeline_engineer")
        self.assertEqual(self.store.load().task_states["A"], "READY")

        self._fail(engineer.reservation_token)

        state = self.store.load()
        self.assertEqual(state.task_states["A"], "READY")
        self.assertEqual(state.task_states["B"], "RUNNING")
        # The lane is free: the next pass reserves a fresh engineer for the ticket.
        again = reserve_ready_frontier(self.cfg)
        self.assertEqual([item.kind for item in again], ["pipeline_engineer"])
        self.assertEqual(self._engineers()[-1]["incident_id"], incident_id)

    def test_two_failed_turns_send_the_ticket_to_her(self) -> None:
        incident_id = self._ticket_holding_everything()
        for _ in range(2):
            engineer = reserve_ready_frontier(self.cfg)[0]
            self.assertEqual(engineer.kind, "pipeline_engineer")
            self._fail(engineer.reservation_token)

        self.assertEqual(reserve_ready_frontier(self.cfg), ())
        ticket = next(
            item for item in self.incidents.load()["incidents"] if item["incident_id"] == incident_id
        )
        self.assertEqual(ticket["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        self.assertEqual(ticket["escalation_reason"], "RECOVERY_EXHAUSTED")

    def test_her_pause_is_not_a_lost_engineer(self) -> None:
        self._ticket_holding_everything()
        for _ in range(2):
            engineer = reserve_ready_frontier(self.cfg)[0]
            self._fail(engineer.reservation_token, failure_code="worker_paused")
        # Two pauses are not two failures: a third engineer still comes.
        self.assertEqual(
            [item.kind for item in reserve_ready_frontier(self.cfg)], ["pipeline_engineer"]
        )


if __name__ == "__main__":
    unittest.main()
