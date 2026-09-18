"""B2: the DevOps re-arm tells "in progress" from "broken".

``devops-rearm-relay-owner`` arms the run so that the predecessor executes
the turn on its NEXT Stop. By construction there can be no thread within
the next fifteen seconds - yet the tail of the command read the boolean
``launch_confirmed`` ("every item True") and on False invalidated the
engineer's resolution. A gate that cannot pass inside its own window
cancelled every repair.

Measured on a constructed "just re-armed" state: the three-valued
``launch_verdict`` answers IN_PROGRESS - running, nothing broken - while
``launch_confirmed`` answers False. One gate had two consumers with
different semantics: the Stop hook (control.py:802) already told three
verdicts apart, the re-arm did not.

Now the decision is one for both: FAILED - invalidate, IN_PROGRESS -
neither invalidate nor declare confirmed (R26: the unmeasured is marked,
not invented), CONFIRMED - confirmed.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.control import _settle_rearmed_launch
from codex_autopilot.launch_gate import LaunchCheck, LaunchVerdict
from codex_autopilot.pipeline_engineer import (
    HealthcheckResult,
    IncidentClass,
    IncidentPhase,
    IncidentSignal,
    PipelineIncidentStore,
    SideEffectOutcome,
)


def _check(check_id: str, passed: bool | None) -> LaunchCheck:
    return LaunchCheck(check_id, "A", passed, f"{check_id}={passed}")


# A just re-armed relay: the reservation exists, the dispatcher is alive,
# there are no refusals, and there is no thread yet - it has nowhere to come
# from until the next Stop.
IN_PROGRESS = (
    _check("reserved", True),
    _check("thread_bound", False),
    _check("created_in_project", False),
    _check("send_acknowledged", False),
    _check("launch_report_written", False),
    _check("visible_in_desktop", None),
    _check("dispatcher_alive", True),
    _check("no_failure_after_launch", True),
)

# The same relay, but the dispatcher died: a deciding item at False is a breakage.
FAILED = tuple(
    _check("dispatcher_alive", False) if item.id == "dispatcher_alive" else item
    for item in IN_PROGRESS
)

CONFIRMED = tuple(_check(item.id, True) for item in IN_PROGRESS)


class RearmedLaunchVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PipelineIncidentStore(Path(self.temp.name) / ".codex-autopilot")
        # The real path to RESOLVED: open, hand to the engineer, close with
        # a passing healthcheck. No direct edit of the phase here.
        incident = self.store.open_incident(
            IncidentSignal(
                signal_id="rearm-verdict",
                code="transport_policy_rejected",
                surface=IncidentClass.PIPELINE,
                summary="Create was definitively rejected.",
                affected_task_ids=("A",),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
            ),
            at="t0",
        )
        self.incident_id = str(incident["incident_id"])
        self.store.ensure_pipeline_engineer(self.incident_id, at="t1")
        self.store.complete_pipeline_engineer(
            self.incident_id,
            success=True,
            at="t2",
            # The release line requires a repair to name what it did: a
            # resolution without a named action is refused (R26).
            actions=("rearm_relay_owner",),
            healthcheck=HealthcheckResult(
                name="relay-ready", passed=True, checks=("checked",), observed_at="t2"
            ),
        )
        self.assertEqual(self.phase(), IncidentPhase.RESOLVED)

    def phase(self) -> IncidentPhase:
        return IncidentPhase(str(self.store.incident_package(self.incident_id)["incident"]["phase"]))

    def settle(self, checks):
        return _settle_rearmed_launch(self.store, self.incident_id, checks, at="t3")

    def test_a_launch_still_in_progress_keeps_the_engineers_resolution(self) -> None:
        """Ровно тот случай, в котором гейт отменял каждую починку."""

        outcome = self.settle(IN_PROGRESS)
        self.assertEqual(outcome["launch_verdict"], LaunchVerdict.IN_PROGRESS.value)
        self.assertEqual(outcome["status"], "LAUNCH_IN_PROGRESS")
        self.assertFalse(outcome["launch_confirmed"], "идущий запуск не выдаётся за подтверждённый")
        self.assertEqual(
            self.phase(),
            IncidentPhase.RESOLVED,
            "решение инженера аннулировано за то, что ветка не появилась за 15 секунд",
        )

    def test_a_failed_launch_still_reopens_the_incident(self) -> None:
        """Смягчение не должно превратиться во всепрощение."""

        outcome = self.settle(FAILED)
        self.assertEqual(outcome["launch_verdict"], LaunchVerdict.FAILED.value)
        self.assertEqual(outcome["status"], "LAUNCH_NOT_CONFIRMED")
        self.assertFalse(outcome["launch_confirmed"])
        self.assertEqual(self.phase(), IncidentPhase.PIPELINE_ENGINEER)

    def test_a_confirmed_launch_is_rearmed(self) -> None:
        outcome = self.settle(CONFIRMED)
        self.assertEqual(outcome["launch_verdict"], LaunchVerdict.CONFIRMED.value)
        self.assertEqual(outcome["status"], "REARMED")
        self.assertTrue(outcome["launch_confirmed"])
        self.assertEqual(self.phase(), IncidentPhase.RESOLVED)

    def test_the_pending_steps_are_named_not_hidden(self) -> None:
        """R26: что именно ещё не наблюдаемо - названо, а не проглочено."""

        outcome = self.settle(IN_PROGRESS)
        pending = outcome["pending_checks"]
        self.assertIn("thread_bound", pending)
        self.assertIn("visible_in_desktop", pending)
        self.assertNotIn("reserved", pending)


if __name__ == "__main__":
    unittest.main()
