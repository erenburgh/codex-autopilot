"""A run nobody is left to raise must not wait for a human to type Resume.

The normal cycle leaves no dispatcher between turns: it launches a worker
and exits, and the worker's own Stop hook raises the next one. That is most
of a run, and it is not a fault.

It becomes one when the hook never fires. Measured 23 Sep 2026: a detached
dispatch failed, the incident went to the on-call, and the run sat there -
state saying RUNNING, a verifier marked ACTIVE, no process anywhere, and
nothing that would ever raise one. The owner had to type "Resume" for
something the runtime knew how to do. Exactly the hole this module was
written to close for rate-limit retries, in a different place.

Nothing here bypasses anything: the wake-up raises the dispatcher through
the same hook-trust and ownership gate as a hook-driven launch.
"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from codex_autopilot import wake
from codex_autopilot.engineer_authority import IncidentClass, SideEffectOutcome
from codex_autopilot.pipeline_engineer import IncidentSignal, PipelineIncidentStore


def _state(**fields):
    base = {
        "status": "RUNNING",
        "dispatcher_pid": None,
        "task_states": {},
        "task_retry_at": {},
    }
    base.update(fields)
    return types.SimpleNamespace(**base)


class StrandedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=Path(self._tmp.name))

    def _ticket_with_the_on_call(self) -> None:
        store = PipelineIncidentStore(self.cfg.state_dir)
        incident = store.open_incident(
            IncidentSignal(
                signal_id="run-1:stranded",
                code="run_stopped:BLOCKED",
                surface=IncidentClass.RUNTIME,
                summary="s",
                affected_task_ids=("M01",),
                operation="",
                side_effect_outcome=SideEffectOutcome.NONE,
                system_state={},
                recent_events=(),
            ),
            at="2026-09-23T16:14:56+00:00",
        )
        store.ensure_pipeline_engineer(
            str(incident["incident_id"]), at="2026-09-23T16:14:56+00:00"
        )

    def test_a_ticket_with_the_on_call_and_no_dispatcher_is_stranded(self) -> None:
        self._ticket_with_the_on_call()
        self.assertTrue(wake.is_stranded(self.cfg, _state()))

    def test_no_ticket_is_not_stranded(self) -> None:
        """Most of a run has no dispatcher and is perfectly healthy."""

        self.assertFalse(wake.is_stranded(self.cfg, _state()))

    def test_a_live_dispatcher_is_never_stranded(self) -> None:
        """Do not race a dispatcher that is waiting for a worker to think."""

        self._ticket_with_the_on_call()
        with mock.patch.object(wake, "_pid_alive", return_value=True):
            self.assertFalse(wake.is_stranded(self.cfg, _state(dispatcher_pid=4242)))

    def test_a_run_a_human_stopped_is_left_alone(self) -> None:
        self._ticket_with_the_on_call()
        for status in ("BLOCKED", "DONE"):
            with self.subTest(status=status):
                self.assertFalse(wake.is_stranded(self.cfg, _state(status=status)))

    def test_an_unreadable_journal_wakes_nothing(self) -> None:
        (self.cfg.state_dir / "pipeline-incidents.json").write_text("{", encoding="utf-8")
        self.assertFalse(wake.is_stranded(self.cfg, _state()))

    def test_a_paused_run_is_stopped_not_stranded(self) -> None:
        """The owner's pause outranks every reason to raise a run."""

        self._ticket_with_the_on_call()
        self.assertFalse(wake.is_stranded(self.cfg, _state(status="PAUSED")))

    def test_a_pause_marker_alone_is_enough_to_leave_it_alone(self) -> None:
        """The marker is the authority; the status may not have caught up."""

        self._ticket_with_the_on_call()
        (self.cfg.state_dir / "pause-requested").write_text(
            "2026-09-23T16:29:55.848920+00:00", encoding="utf-8"
        )
        self.assertFalse(wake.is_stranded(self.cfg, _state(status="RUNNING")))


class WhenTheRunIsRaisedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = types.SimpleNamespace(state_dir=Path(self._tmp.name))

    def test_a_stranded_run_is_due_now(self) -> None:
        with mock.patch.object(wake, "is_stranded", return_value=True):
            with mock.patch.object(wake.time, "time", return_value=1790180555.0):
                self.assertEqual(
                    wake.due_wake_epoch(_state(), self.cfg), 1790180555
                )

    def test_a_retry_still_wins_when_it_comes_first(self) -> None:
        state = _state(
            task_states={"M02": "RETRY_WAIT"}, task_retry_at={"M02": 1790180000}
        )
        with mock.patch.object(wake, "is_stranded", return_value=True):
            with mock.patch.object(wake.time, "time", return_value=1790180555.0):
                self.assertEqual(wake.due_wake_epoch(state, self.cfg), 1790180000)

    def test_without_a_project_only_retries_are_seen(self) -> None:
        """The old signature keeps its old meaning for callers that have no cfg."""

        self.assertIsNone(wake.due_wake_epoch(_state()))

    def test_the_sweep_asks_about_stranding_too(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/wake.py"
        ).read_text(encoding="utf-8")
        self.assertIn("due_wake_epoch(state, cfg)", source)
        self.assertNotIn("due_wake_epoch(state)\n", source)


class TheGateIsStillThereTests(unittest.TestCase):
    def test_the_wake_up_still_passes_hook_trust(self) -> None:
        """Raising a run hours later proves trust the same as a hook does."""

        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/wake.py"
        ).read_text(encoding="utf-8")
        self.assertIn("hook_trust", source)


if __name__ == "__main__":
    unittest.main()


class LiftingAStopDoesNotRedoDoneWorkTests(unittest.TestCase):
    """A stop about acceptance leaves the work standing."""

    def test_the_state_machine_allows_returning_to_acceptance(self) -> None:
        from codex_autopilot.task_state import TASK_TRANSITIONS, TaskState

        self.assertIn(TaskState.IMPLEMENTED, TASK_TRANSITIONS[TaskState.BLOCKED])

    def test_unblock_chooses_by_whether_a_verdict_ever_happened(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/cli.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "TaskState.IMPLEMENTED if done_before else TaskState.READY", source
        )
        self.assertIn(
            'done_before = int(state.task_revisions.get(task_id, 0)) > 0', source
        )
