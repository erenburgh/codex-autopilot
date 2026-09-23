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

    def test_a_finished_run_is_left_alone(self) -> None:
        self._ticket_with_the_on_call()
        self.assertFalse(wake.is_stranded(self.cfg, _state(status="DONE")))

    def test_blocked_is_not_a_reason_to_leave_a_ticket_unread(self) -> None:
        """BLOCKED used to mean "a human stopped it" here. It never did.

        The stop door itself wrote BLOCKED before the on-call had looked,
        and this predicate then skipped exactly the runs whose ticket was
        waiting for an engineer. BLOCKED is derived now; a ticket in the
        lane with no engineer is stranded whatever the status says.
        """

        self._ticket_with_the_on_call()
        self.assertTrue(wake.is_stranded(self.cfg, _state(status="BLOCKED")))

    def test_blocked_with_everything_waiting_for_her_is_not_stranded(self) -> None:
        """What waits for the owner is not the runtime's to raise."""

        store = PipelineIncidentStore(self.cfg.state_dir)
        self._ticket_with_the_on_call()
        incident_id = store.load()["incidents"][0]["incident_id"]
        store.escalate_incident_to_user(
            incident_id, reason_code="PRODUCT_DECISION", at="2026-09-23T16:20:00+00:00"
        )
        state = _state(status="BLOCKED", task_states={"M01": "BLOCKED"}, worker_sessions=[])
        self.assertFalse(wake.is_stranded(self.cfg, state))

    def test_an_engineer_at_work_under_its_dispatcher_is_not_raised_twice(self) -> None:
        """It used to pass with no dispatcher at all: "an engineer is pending".

        That was the silent stop the independent check reproduced - an
        engineer ACTIVE with a dead dispatcher kept the lane shut. Pending
        is not enough; its dispatcher must be alive.
        """

        import os

        self._ticket_with_the_on_call()
        engineer = {
            "kind": "pipeline_engineer",
            "status": "ACTIVE",
            "thread_id": "t-1",
            "automatic_dispatch_state": "RUNNING",
            "automatic_dispatch_pid": os.getpid(),
        }
        self.assertFalse(
            wake.is_stranded(self.cfg, _state(worker_sessions=[engineer]))
        )

    def test_an_engineer_whose_dispatcher_died_mid_turn_is_stranded(self) -> None:
        """The independent check's case: completion raised, the process died."""

        self._ticket_with_the_on_call()
        engineer = {
            "kind": "pipeline_engineer",
            "status": "ACTIVE",
            "thread_id": "t-1",
            "automatic_dispatch_state": "RUNNING",
            "automatic_dispatch_pid": 999_999_999,
        }
        self.assertTrue(
            wake.is_stranded(self.cfg, _state(worker_sessions=[engineer]))
        )

    def test_a_running_worker_whose_dispatcher_died_is_stranded(self) -> None:
        """No ticket at all: the worker's dispatcher alone consumed its turn."""

        worker = {
            "kind": "implementation",
            "task_id": "M01",
            "status": "ACTIVE",
            "thread_id": "t-1",
            "automatic_dispatch_state": "RUNNING",
            "automatic_dispatch_pid": 999_999_999,
        }
        state = _state(task_states={"M01": "RUNNING"}, worker_sessions=[worker])
        self.assertTrue(wake.is_stranded(self.cfg, state))

    def test_a_create_in_doubt_that_a_ticket_holds_is_the_tickets(self) -> None:
        """Nothing the wake-up can do for it; a wake every sweep is noise."""

        ambiguous = {
            "kind": "implementation",
            "task_id": "M01",
            "status": "AMBIGUOUS",
            "thread_id": None,
            "automatic_dispatch_pid": None,
        }
        state = _state(task_states={"M01": "RUNNING"}, worker_sessions=[ambiguous])
        self.assertTrue(wake.is_stranded(self.cfg, state))
        self._ticket_with_the_on_call()
        PipelineIncidentStore(self.cfg.state_dir).escalate_incident_to_user(
            PipelineIncidentStore(self.cfg.state_dir).load()["incidents"][0]["incident_id"],
            reason_code="PRODUCT_DECISION",
            at="2026-09-23T16:20:00+00:00",
        )
        self.assertFalse(wake.is_stranded(self.cfg, state))

    def test_an_engineers_create_in_doubt_is_stranded_even_when_its_task_is_held(self) -> None:
        """Its anchor IS the held task; left pending it shuts the lane."""

        self._ticket_with_the_on_call()
        ambiguous = {
            "kind": "pipeline_engineer",
            "task_id": "M01",
            "status": "AMBIGUOUS",
            "thread_id": None,
            "automatic_dispatch_pid": None,
        }
        state = _state(task_states={"M01": "READY"}, worker_sessions=[ambiguous])
        self.assertTrue(wake.is_stranded(self.cfg, state))

    def test_ready_work_with_no_session_at_all_is_stranded(self) -> None:
        """The generalisation of the engineer's "would idle forever"."""

        state = _state(status="WAITING", task_states={"M01": "READY"}, worker_sessions=[])
        self.assertTrue(wake.is_stranded(self.cfg, state))

    def test_a_run_she_has_not_started_is_never_started_for_her(self) -> None:
        state = _state(status="READY", task_states={"M01": "READY"}, worker_sessions=[])
        self.assertFalse(wake.is_stranded(self.cfg, state))

    def test_a_reservation_whose_dispatcher_died_is_stranded(self) -> None:
        """The Stop hook raised these; the wake-up did not know the case."""

        stalled = {
            "kind": "implementation",
            "status": "CREATE_REQUESTED",
            "thread_id": None,
            "automatic_dispatch_state": "RUNNING",
            "automatic_dispatch_pid": 999_999_999,
        }
        state = _state(task_states={"M01": "RUNNING"}, worker_sessions=[stalled])
        self.assertTrue(wake.is_stranded(self.cfg, state))

    def test_a_worker_thinking_under_its_live_dispatcher_is_not_stranded(self) -> None:
        import os

        live = {
            "kind": "implementation",
            "status": "ACTIVE",
            "thread_id": "t-1",
            "automatic_dispatch_state": "RUNNING",
            "automatic_dispatch_pid": os.getpid(),
        }
        state = _state(task_states={"M01": "RUNNING"}, worker_sessions=[live])
        self.assertFalse(wake.is_stranded(self.cfg, state))

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


class TheWakeUpItselfNoLongerSkipsBlockedTests(unittest.TestCase):
    """run_wake and the sweep skipped BLOCKED on their own, whatever
    is_stranded said: fixing only the predicate left two doors shut."""

    def setUp(self) -> None:
        from codex_autopilot.run_state import RunState, StateStore

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        (root / ".codex-autopilot").mkdir()
        self.cfg = types.SimpleNamespace(state_dir=root / ".codex-autopilot", root=root)
        self.store = StateStore(self.cfg.state_dir)
        self.store.save(RunState(status="BLOCKED", phase="BLOCKED", task_states={"M01": "BLOCKED"}))

    def test_run_wake_goes_on_to_reserve_a_blocked_run(self) -> None:
        reserved: list[str] = []

        def reserve(cfg, **_kwargs):
            reserved.append("asked")
            return ()

        with mock.patch.object(wake, "due_wake_epoch", return_value=0):
            wake.run_wake(
                self.cfg,
                at_epoch=0,
                owner="owner-thread",
                owner_turn="owner-turn",
                now=lambda: 10.0,
                sleep=lambda _s: None,
                reserve=reserve,
                spawn_relay=lambda *a, **k: 1,
                revive=lambda cfg: (),
            )
        self.assertEqual(reserved, ["asked"])

    def test_an_empty_frontier_raises_a_reservation_whose_dispatcher_died(self) -> None:
        with mock.patch.object(wake, "due_wake_epoch", return_value=0):
            wake.run_wake(
                self.cfg,
                at_epoch=0,
                owner="owner-thread",
                owner_turn="owner-turn",
                now=lambda: 10.0,
                sleep=lambda _s: None,
                reserve=lambda cfg, **_k: (),
                spawn_relay=lambda *a, **k: 1,
                revive=lambda cfg: (4242,),
            )
        last = self.store.load().resilience_journal[-1]
        self.assertEqual(last["event"], "wake_dispatched")
        self.assertEqual(last["detail"]["pids"], [4242])

    def test_revoked_hook_trust_is_told_to_her_with_what_to_do(self) -> None:
        """The one stop the on-call cannot take: raising it passes the same
        trust gate, and going around the gate is hers to decide. So she is
        told directly - it used to be a silent wake_skipped."""

        from codex_autopilot.hook_trust import HookPreflightError

        def refuse(cfg, **_kwargs):
            raise HookPreflightError("the Stop hook is not trusted")

        with mock.patch.object(wake, "due_wake_epoch", return_value=0):
            wake.run_wake(
                self.cfg,
                at_epoch=0,
                owner="owner-thread",
                owner_turn="owner-turn",
                now=lambda: 10.0,
                sleep=lambda _s: None,
                reserve=refuse,
                spawn_relay=lambda *a, **k: 1,
            )
        journal = self.store.load().resilience_journal
        self.assertEqual(journal[-2]["event"], "owner_signalled")
        self.assertEqual(journal[-2]["detail"]["key"], "hook_trust")
        self.assertIn("restore trust", journal[-1]["detail"]["recommendation"])

    def test_the_sweep_does_not_call_a_blocked_run_stopped(self) -> None:
        (self.cfg.root / ".codex-autopilot" / "config.toml").write_text("", encoding="utf-8")
        with mock.patch.object(wake, "due_wake_epoch", return_value=None):
            outcome = wake.sweep(roots=[str(self.cfg.root)], load=lambda _root: self.cfg)
        self.assertEqual(outcome[str(self.cfg.root)], "nothing due")

    def test_her_pause_still_stops_everything(self) -> None:
        self.store.request_pause()
        with mock.patch.object(wake, "due_wake_epoch", return_value=0):
            wake.run_wake(
                self.cfg,
                at_epoch=0,
                owner="owner-thread",
                owner_turn="owner-turn",
                now=lambda: 10.0,
                sleep=lambda _s: None,
                reserve=lambda cfg, **_k: self.fail("a paused run was reserved"),
                spawn_relay=lambda *a, **k: 1,
            )
        (self.cfg.root / ".codex-autopilot" / "config.toml").write_text("", encoding="utf-8")
        outcome = wake.sweep(roots=[str(self.cfg.root)], load=lambda _root: self.cfg)
        self.assertEqual(outcome[str(self.cfg.root)], "stopped")


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
