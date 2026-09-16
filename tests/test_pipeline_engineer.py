from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from codex_autopilot.ai_studio import AIStudioRuntime, ContextBoundaryError
from codex_autopilot.pipeline_engineer import (
    HealthcheckResult,
    IncidentClass,
    IncidentPhase,
    IncidentSignal,
    PipelineIncidentStore,
    SideEffectOutcome,
)
from codex_autopilot.plan import validate_plan
from codex_autopilot.resources import build_scheduler_availability
from codex_autopilot.run_state import RunState
from codex_autopilot.scheduler import schedule
from _plan_contract import canonicalize_plan, canonical_verification


def graph() -> dict[str, object]:
    def task(task_id: str) -> dict[str, object]:
        return {
            "id": task_id,
            "title": f"Task {task_id}",
            "objective": f"Complete {task_id}.",
            "definition_of_done": [f"{task_id} is verified."],
            "execution_mode": "code",
            "execution_mode_reason": "Repository code and tests are sufficient.",
            "reasoning": "medium",
            "role": "builder",
            "depends_on": [],
            "priority": 0,
            "verification": canonical_verification(),
            "resources": [],
            "required_capabilities": [],
            "context": {},
            "outputs": [],
            "tags": [],
        }

    return canonicalize_plan({
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Test incident isolation.",
        "user_request": "Test incident isolation against the full recovery contract.",
        "model_strategy": "auto",
        "execution_strategy": "parallel",
        "max_parallel_workers": 2,
        "computer_use_slots": 1,
        "roles": [
            {
                "id": "builder",
                "name": "Builder",
                "responsibilities": ["Build one task."],
            }
        ],
        "tasks": [task("A"), task("B")],
    })




class RecoveryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pipeline-engineer-")
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        (self.root / ".codex-autopilot").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# Test skill\n", encoding="utf-8")
        self.store = PipelineIncidentStore(self.root / ".codex-autopilot")
        self.plan = validate_plan(graph(), "adaptive")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _open(self, *, signal_id: str = "incident-a") -> str:
        incident = self.store.open_incident(
            IncidentSignal(
                signal_id=signal_id,
                code="runtime_channel_closed",
                surface=IncidentClass.RUNTIME,
                summary="Runtime channel closed for task A.",
                affected_task_ids=("A",),
                system_state={"phase": "AWAITING_DESKTOP_SEND", "active": ["A", "B"]},
                recent_events=({"event": "send_requested", "task_id": "A"},),
            ),
            at="2026-09-11T00:00:00Z",
        )
        return str(incident["incident_id"])

    def test_pipeline_engineer_activation_is_idempotent_when_no_runbook_exists(self) -> None:
        incident = self.store.open_incident(
            IncidentSignal(
                signal_id="policy-rejected-create",
                code="transport_policy_rejected",
                surface=IncidentClass.PIPELINE,
                summary="Create was definitively rejected.",
                affected_task_ids=("A",),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
            ),
            at="t0",
        )
        first = self.store.ensure_pipeline_engineer(incident["incident_id"], at="t1")
        second = self.store.ensure_pipeline_engineer(incident["incident_id"], at="t2")
        self.assertEqual(first["incident"]["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
        self.assertEqual(first["incident"]["incident_id"], second["incident"]["incident_id"])
        events = [item["event"] for item in self.store.load()["journal"]]
        self.assertEqual(events.count("incident_opened"), 1)
        self.assertEqual(events.count("auto_recovery_unavailable"), 1)
        self.assertEqual(events.count("pipeline_engineer_requested"), 1)


    def test_failed_post_healthcheck_rearm_reopens_same_incident(self) -> None:
        incident = self.store.open_incident(
            IncidentSignal(
                signal_id="rearm-invalidated",
                code="transport_policy_rejected",
                surface=IncidentClass.PIPELINE,
                summary="Create was definitively rejected.",
                affected_task_ids=("A",),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
            ),
            at="t0",
        )
        package = self.store.ensure_pipeline_engineer(incident["incident_id"], at="t1")
        self.store.complete_pipeline_engineer(
            incident["incident_id"],
            success=True,
            at="t2",
            healthcheck=HealthcheckResult(
                name="relay-ready",
                passed=True,
                checks=("checked",),
                observed_at="t2",
            ),
        )
        reopened = self.store.invalidate_pipeline_engineer_resolution(
            incident["incident_id"],
            at="t3",
            reason="hook trust changed",
        )
        self.assertEqual(reopened["incident"]["incident_id"], package["incident"]["incident_id"])
        self.assertEqual(reopened["incident"]["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
        self.assertIsNone(reopened["incident"]["healthcheck"])
        self.assertIsNone(reopened["incident"]["resolved_at"])








if __name__ == "__main__":
    unittest.main()
