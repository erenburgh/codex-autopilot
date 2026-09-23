"""The run's status is derived after the reservation - BLOCKED last of all.

BLOCKED used to be written by whoever stopped: the stop door wrote it before
the on-call had looked, the engineer's escalation wrote it for the whole run
over one task, and the wake-up skipped BLOCKED as "a human's decision". A
ticket in the engineer's lane with the run BLOCKED meant nobody ever came.

Now BLOCKED is what is left when nothing can be taken and what remains waits
for the owner - and it is derived in one place, run_status.
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from codex_autopilot.blocked_runs import escalate_to_owner, stop_run
from codex_autopilot.lifecycle_base import _finish_global_state
from codex_autopilot.run_state import RunState
from codex_autopilot.run_status import _finish_global_state as derived

AT = "2026-09-23T15:02:27+00:00"


class DerivedStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=Path(self._tmp.name))
        self.plan = types.SimpleNamespace(task_map={"A": object(), "B": object()})

    def _state(self, **states: str) -> RunState:
        return RunState(run_id="run-1", status="RUNNING", task_states=dict(states))

    def _ticket(self, state: RunState, task: str = "A") -> str:
        return str(
            stop_run(
                self.cfg,
                state,
                stop_kind="worker_blocked",
                phase="BLOCKED",
                reason="r",
                summary="s.",
                at=AT,
                task_ids=(task,),
            )
        )

    def derive(self, state: RunState, descriptors=(), paused=False) -> tuple[str, str]:
        _finish_global_state(self.plan, state, tuple(descriptors), paused=paused, cfg=self.cfg)
        return state.status, state.phase

    def test_lifecycle_base_re_exports_the_one_derivation(self) -> None:
        self.assertIs(_finish_global_state, derived)

    def test_any_pending_session_is_running_not_only_a_worker(self) -> None:
        """A screening next to a stopped task is work in progress.

        Only descriptors and active_task_ids used to count: a screening
        takes no work slot, so a run screening its next hire next to one
        BLOCKED task was declared BLOCKED.
        """

        state = self._state(A="BLOCKED", B="READY")
        state.worker_sessions = [{"kind": "screening", "task_id": "B", "status": "ACTIVE"}]
        self.assertEqual(self.derive(state), ("RUNNING", "DESKTOP_WORKERS_ACTIVE"))

    def test_an_engineer_at_work_is_running(self) -> None:
        state = self._state(A="BLOCKED", B="WAITING")
        self._ticket(state)
        state.worker_sessions = [{"kind": "pipeline_engineer", "task_id": "A", "status": "ACTIVE"}]
        self.assertEqual(self.derive(state), ("RUNNING", "PIPELINE_ENGINEER_ACTIVE"))

    def test_a_ticket_waiting_for_the_on_call_is_never_blocked(self) -> None:
        """Nobody reserved the engineer yet: the wake-up will. Not her call."""

        state = self._state(A="BLOCKED", B="WAITING")
        self._ticket(state)
        self.assertEqual(self.derive(state), ("WAITING", "PIPELINE_ENGINEER_PENDING"))

    def test_blocked_only_when_everything_left_waits_for_her(self) -> None:
        state = self._state(A="BLOCKED", B="WAITING")
        incident_id = self._ticket(state)
        escalate_to_owner(self.cfg, incident_id, code="PRODUCT_DECISION", detail="d", at=AT)
        self.assertEqual(self.derive(state), ("BLOCKED", "AWAITING_OWNER"))

    def test_work_that_could_still_be_taken_is_not_blocked(self) -> None:
        state = self._state(A="BLOCKED", B="READY")
        incident_id = self._ticket(state)
        escalate_to_owner(self.cfg, incident_id, code="PRODUCT_DECISION", detail="d", at=AT)
        self.assertEqual(self.derive(state), ("WAITING", "WAITING_DEPENDENCIES"))

    def test_her_pause_outranks_every_ticket(self) -> None:
        state = self._state(A="BLOCKED", B="WAITING")
        self._ticket(state)
        self.assertEqual(self.derive(state, paused=True), ("PAUSED", "PAUSED"))


class TheOnCallDoesNotDisplaceItsNeighboursTests(unittest.TestCase):
    """Small invariants the engineer's new place in the frontier rests on."""

    def test_a_replacement_reservation_does_not_retire_the_engineer_anchored_to_it(self) -> None:
        from codex_autopilot.lifecycle_base import fence_superseded_sessions

        state = RunState(run_id="run-1")
        common = {"task_id": "A", "status": "ACTIVE", "attempt": 1}
        state.worker_sessions = [
            {**common, "kind": "pipeline_engineer", "thread_id": "eng", "operation_id": "o1", "reservation_token": "t1"},
            {**common, "kind": "implementation", "thread_id": "old", "operation_id": "o2", "reservation_token": "t2"},
        ]
        fenced = fence_superseded_sessions(state, "A", at=AT, reason="replacement")
        self.assertEqual([item["thread_id"] for item in fenced], ["old"])
        self.assertEqual(state.worker_sessions[0]["status"], "ACTIVE")

    def test_the_prompt_is_built_for_the_sessions_own_ticket(self) -> None:
        """Another ticket may have become first in the lane since the reservation."""

        from codex_autopilot.engineer_reservation import pipeline_engineer_package

        with tempfile.TemporaryDirectory() as raw:
            cfg = types.SimpleNamespace(state_dir=Path(raw))
            state = RunState(run_id="run-1")
            first = stop_run(cfg, state, stop_kind="k1", phase="P1", reason="r", summary="s.", at=AT, task_ids=("A",))
            second = stop_run(cfg, state, stop_kind="k2", phase="P2", reason="r", summary="s.", at=AT, task_ids=("B",))
            self.assertNotEqual(first, second)
            package = pipeline_engineer_package(cfg, state, second)
            self.assertEqual(package["incident"]["incident_id"], second)
            self.assertEqual(pipeline_engineer_package(cfg, state)["incident"]["incident_id"], first)

    def test_a_reservation_its_own_dispatcher_is_creating_is_not_adopted(self) -> None:
        """With the engineer next to the work, a neighbour's fresh
        reservation is being created right now by its own dispatcher."""

        import os

        from codex_autopilot.engineer_escalation import _relayable_descriptors_without_a_thread

        state = types.SimpleNamespace(
            worker_sessions=[
                {
                    "status": "CREATE_REQUESTED",
                    "thread_id": None,
                    "descriptor": {"x": 1},
                    "automatic_dispatch_state": "RUNNING",
                    "automatic_dispatch_pid": os.getpid(),
                }
            ]
        )
        self.assertEqual(_relayable_descriptors_without_a_thread(state), ())


if __name__ == "__main__":
    unittest.main()
