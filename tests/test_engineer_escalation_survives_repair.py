"""The engineer may repair a fault and still raise a decision to the owner.

Those are different things. The ticket it repairs itself; a rule
conflict it cannot resolve - it has no such authority, that is the
owner's decision.

Escalation used to be allowed only from the "held by the engineer"
phase, and a closed ticket rejected it. On 16 Sep 2026 that stopped the
whole run twice: the engineer closed the fault, escalated the R31
conflict, the refusal went up, the dispatcher crashed - and nobody was
left to accept the completion of its turn. Each time a human was needed.

Such an escalation must not be lost: a decision nobody will see is no
different from one never made.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.pipeline_engineer import (
    IncidentClass,
    IncidentPhase,
    IncidentSignal,
    PipelineIncidentError,
    HealthcheckResult,
    PipelineIncidentStore,
    SideEffectOutcome,
)
from codex_autopilot.run_state import utc_now


class EscalationAfterRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PipelineIncidentStore(Path(self.temp.name))

    def _incident(self) -> str:
        record = self.store.open_incident(
            IncidentSignal(
                signal_id="probe:M5",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                summary="завершение воркера некому было принять",
                affected_task_ids=("M5",),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
                system_state={},
            ),
            at=utc_now(),
        )
        return str(record["incident_id"])

    def test_escalation_after_the_incident_was_resolved_is_accepted(self) -> None:
        incident_id = self._incident()
        self.store.route_incident(incident_id, at=utc_now())
        self.store.ensure_pipeline_engineer(incident_id, at=utc_now())
        self.store.complete_pipeline_engineer(
            incident_id,
            success=True,
            actions=("reconcile_durable_journal",),
            at=utc_now(),
            healthcheck=HealthcheckResult(
                name="fail-safe",
                passed=True,
                checks=("durable records prove the thread was created and started",),
                observed_at=utc_now(),
            ),
        )

        phase = self.store.escalate_incident_to_user(
            incident_id,
            reason_code="ARCHITECTURE_DECISION",
            at=utc_now(),
            detail="R31: late gate rejected a completed worker",
        )
        self.assertIs(phase, IncidentPhase.ESCALATE_TO_USER)

    def test_escalating_an_untouched_incident_is_still_refused(self) -> None:
        """Permission must not turn into forgiving everything."""

        incident_id = self._incident()
        with self.assertRaises(PipelineIncidentError):
            self.store.escalate_incident_to_user(
                incident_id, reason_code="ARCHITECTURE_DECISION", at=utc_now()
            )


if __name__ == "__main__":
    unittest.main()
