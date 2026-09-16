"""Инженер вправе починить сбой и всё равно поднять решение владельцу.

Это разные вещи. Тикет он чинит сам; конфликт правила решить не может -
такого полномочия у него нет, это решение владельца.

Прежде эскалация допускалась только из фазы «удерживается инженером», и
закрытый тикет её отвергал. 16.09.2026 это дважды остановило прогон
целиком: инженер закрывал сбой, эскалировал конфликт R31, отказ уходил
наверх, диспетчер падал - и принимать завершение его хода становилось
некому. Каждый раз требовался человек.

Терять такую эскалацию нельзя: решение, которого никто не увидит, ничем
не отличается от непринятого.
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
        """Разрешение не должно превратиться в всепрощение."""

        incident_id = self._incident()
        with self.assertRaises(PipelineIncidentError):
            self.store.escalate_incident_to_user(
                incident_id, reason_code="ARCHITECTURE_DECISION", at=utc_now()
            )


if __name__ == "__main__":
    unittest.main()
