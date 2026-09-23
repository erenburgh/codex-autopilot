"""Every stop opens a ticket and every ticket reaches the owner.

A run stops in several different places: a task exhausts its hiring ladder,
a worker stops itself, a replanner uses every attempt, a verifier is
unreadable three times, plan verification is refused. Each was written
separately and stopped separately, and only the engineer's own escalation
told anybody.

From outside that reads as: the run stands still and the incident journal is
empty. On a real run (23 Sep 2026) 75 minutes ended at BLOCKED with no
incident file on disk at all, and the owner's question was why nobody came.
Nobody was called.

Opening a ticket does not move the decision. PRODUCTION stays outside the
engineer's remit, because accepting work is the owner's call. What changes is
that the owner is told, with the reason, and told that the call is theirs.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_autopilot.blocked_runs import stop_run
from codex_autopilot.engineer_authority import (
    INFRASTRUCTURE_INCIDENT_CLASSES,
    IncidentClass,
)
from codex_autopilot.pipeline_engineer import IncidentPhase, PipelineIncidentStore


# Every phase a run can stop in, and what the owner is told it is about.
STOPS = (
    ("BLOCKED", "M01 exhausted the hiring ladder", ("M01",)),
    ("PLAN_CHANGE_REJECTED", "the replanner used every attempt", ("M01",)),
    ("VERIFICATION_PROTOCOL_BLOCKED", "unreadable verdict three times", ("M01",)),
    ("PLAN_VERIFICATION_PROTOCOL_REJECTED", "the plan verifier was unreadable", ()),
    ("PLAN_VERIFICATION_REJECTED", "the plan verifier refused the plan", ()),
    ("PIPELINE_ENGINEER_NO_SUCCESSOR", "nobody could be assigned", ()),
)


class EveryStopIsFiledTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.cfg = mock.Mock(state_dir=self.state_dir)

    def _state(self):
        return mock.Mock(run_id="run-1", status="RUNNING", phase="DESKTOP_WORKERS_ACTIVE")

    def _stop(self, phase, reason, task_ids, state=None):
        state = state or self._state()
        incident_id = stop_run(
            self.cfg,
            state,
            phase=phase,
            reason=reason,
            summary=f"summary for {phase}.",
            at="2026-09-23T15:02:27+00:00",
            task_ids=task_ids,
        )
        return state, incident_id

    def _incidents(self):
        loaded = PipelineIncidentStore(self.state_dir).load().get("incidents")
        return list(loaded.values()) if isinstance(loaded, dict) else list(loaded or ())

    def test_each_stop_opens_a_ticket(self) -> None:
        for phase, reason, task_ids in STOPS:
            with self.subTest(phase=phase):
                self.setUp()
                _state, incident_id = self._stop(phase, reason, task_ids)
                self.assertIsNotNone(incident_id, f"{phase} filed nothing")
                self.assertEqual(len(self._incidents()), 1)

    def test_each_ticket_reaches_the_on_call_lane(self) -> None:
        """The engineer looks first; handing it to the owner is its verdict.

        A ticket left in DEGRADED is a ticket nobody reads: the on-call is
        reserved only for incidents in its own phase.
        """

        for phase, reason, task_ids in STOPS:
            with self.subTest(phase=phase):
                self.setUp()
                self._stop(phase, reason, task_ids)
                incident = self._incidents()[0]
                self.assertEqual(
                    incident["phase"], IncidentPhase.PIPELINE_ENGINEER.value
                )

    def test_each_ticket_is_one_a_resume_can_answer(self) -> None:
        """Resuming closes exactly the tickets waiting on a human."""

        from codex_autopilot.pipeline_engineer import IncidentPhase as P

        awaiting = {P.PIPELINE_ENGINEER.value, P.ESCALATE_TO_USER.value}
        for phase, reason, task_ids in STOPS:
            with self.subTest(phase=phase):
                self.setUp()
                self._stop(phase, reason, task_ids)
                self.assertIn(self._incidents()[0]["phase"], awaiting)

    def test_it_is_infrastructure_never_product_quality(self) -> None:
        for phase, reason, task_ids in STOPS:
            with self.subTest(phase=phase):
                self.setUp()
                self._stop(phase, reason, task_ids)
                incident = self._incidents()[0]
                self.assertEqual(
                    incident["classification"], IncidentClass.RUNTIME.value
                )
                self.assertIn(IncidentClass.RUNTIME, INFRASTRUCTURE_INCIDENT_CLASSES)

    def test_the_run_still_stops(self) -> None:
        state, _ = self._stop(*STOPS[0])
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.phase, "BLOCKED")
        self.assertIn("hiring ladder", state.last_error)

    def test_no_runbook_matches_so_nothing_is_retried(self) -> None:
        """A run that stopped needs a person, not another attempt."""

        self._stop(*STOPS[1])
        incident = self._incidents()[0]
        self.assertIsNone(incident["runbook_id"])
        self.assertEqual(incident["recovery_attempts"], 0)

    def test_stopping_twice_for_the_same_reason_keeps_one_ticket(self) -> None:
        state = self._state()
        self._stop(*STOPS[0], state=state)
        self._stop(*STOPS[0], state=state)
        self.assertEqual(len(self._incidents()), 1)

    def test_an_existing_ticket_is_reused_not_duplicated(self) -> None:
        """The engineer's own escalation already has one."""

        _state, incident_id = self._stop(*STOPS[0])
        stop_run(
            self.cfg,
            self._state(),
            phase="PIPELINE_ENGINEER_NO_SUCCESSOR",
            reason="no successor",
            summary="none.",
            at="2026-09-23T15:03:00+00:00",
            incident_id=incident_id,
        )
        self.assertEqual(len(self._incidents()), 1)

    def test_a_ticket_the_engineer_hands_back_reaches_the_owner(self) -> None:
        """The engineer looked, cannot make this call, and says so.

        That handover is the signal the owner waits on. Without it a stop is
        silent again, however many tickets exist.
        """

        _state, incident_id = self._stop(*STOPS[0])
        stop_run(
            self.cfg,
            self._state(),
            phase="PIPELINE_ENGINEER_ESCALATED",
            reason="the on-call engineer handed the incident to the user",
            summary="The call is not the engineer's to make.",
            at="2026-09-23T15:03:00+00:00",
            incident_id=incident_id,
            escalation_code="RECOVERY_EXHAUSTED",
        )
        incident = self._incidents()[0]
        self.assertEqual(incident["phase"], IncidentPhase.ESCALATE_TO_USER.value)

    def test_a_handover_without_a_closed_list_code_is_refused(self) -> None:
        """R13: the engineer names the reason from a closed list, or not at all."""

        _state, incident_id = self._stop(*STOPS[0])
        stop_run(
            self.cfg,
            self._state(),
            phase="PIPELINE_ENGINEER_ESCALATED",
            reason="no code given",
            summary="none.",
            at="2026-09-23T15:03:00+00:00",
            incident_id=incident_id,
            escalation_code="NOT_ON_THE_LIST",
        )
        self.assertEqual(
            self._incidents()[0]["phase"], IncidentPhase.PIPELINE_ENGINEER.value
        )

    def test_the_owner_is_told_what_is_wanted_of_them(self) -> None:
        self._stop(*STOPS[0])
        record = str(self._incidents()[0])
        self.assertIn("the decision is yours", record)
        self.assertIn("exhausted the hiring ladder", record)

    def test_a_failure_to_record_never_prevents_the_stop(self) -> None:
        with mock.patch(
            "codex_autopilot.pipeline_engineer.PipelineIncidentStore.open_incident",
            side_effect=OSError("disk gone"),
        ):
            state, incident_id = self._stop(*STOPS[0])
        self.assertIsNone(incident_id)
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.phase, "BLOCKED")


class NoStopBypassesTheDoorTests(unittest.TestCase):
    """A new stop written by hand would be a silent one again."""

    SOURCES = (
        "lifecycle_base.py",
        "lifecycle_completion.py",
        "plan_verification_lifecycle.py",
        "revision_budget.py",
    )

    def test_no_module_stops_a_run_without_opening_a_ticket(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src/codex_autopilot"
        for name in self.SOURCES:
            source = (root / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                stops = source.count('state.status = "BLOCKED"')
                if name == "lifecycle_base.py":
                    # The one derivation: `_finish_global_state` reads the task
                    # states back and names what the run as a whole is. The
                    # ticket was opened when the task itself blocked.
                    self.assertEqual(stops, 1)
                    derived = source.index("def _finish_global_state(")
                    self.assertGreater(
                        source.index('state.status = "BLOCKED"'), derived
                    )
                    continue
                self.assertEqual(
                    stops, 0, f"{name} stops the run without opening a ticket"
                )

    def test_the_door_is_the_only_writer_of_that_status(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/blocked_runs.py"
        ).read_text(encoding="utf-8")
        self.assertIn('state.status = "BLOCKED"', source)


if __name__ == "__main__":
    unittest.main()
