from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from codex_autopilot.ai_studio import AIStudioRuntime, ContextBoundaryError
from codex_autopilot.pipeline_engineer import (
    AuthorityKind,
    AuthorityProof,
    AuthorizationTopologyError,
    HealthcheckResult,
    IncidentClass,
    IncidentPhase,
    IncidentSignal,
    PipelineIncidentError,
    PipelineIncidentStore,
    SideEffectOutcome,
    TransportClaim,
    TransportStatus,
    classify_incident,
    requires_pipeline_engineer,
    select_runbook,
)
from codex_autopilot.plan import validate_plan
from codex_autopilot.resources import build_scheduler_availability
from codex_autopilot.run_state import RunState
from codex_autopilot.scheduler import schedule


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
            "verification": {"policy": "self", "required": True},
            "resources": [],
            "required_capabilities": [],
            "context": {},
            "outputs": [],
            "tags": [],
        }

    return {
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
    }


class IncidentClassificationTests(unittest.TestCase):
    def test_structured_taxonomy_and_ambiguous_side_effect_override(self) -> None:
        runtime = IncidentSignal(
            signal_id="runtime-1",
            code="runtime_channel_closed",
            surface=IncidentClass.RUNTIME,
            summary="Local channel closed.",
            affected_task_ids=("A",),
        )
        self.assertEqual(classify_incident(runtime), IncidentClass.RUNTIME)
        self.assertEqual(select_runbook(runtime).id, "reopen-local-runtime-channel")

        ambiguous = IncidentSignal(
            signal_id="transport-1",
            code="connection_lost",
            surface=IncidentClass.INTEGRATION,
            summary="Create result was not observed.",
            affected_task_ids=("A",),
            operation="create_thread",
            side_effect_outcome=SideEffectOutcome.UNKNOWN,
        )
        self.assertEqual(
            classify_incident(ambiguous),
            IncidentClass.AMBIGUOUS_SIDE_EFFECT,
        )
        self.assertIsNone(select_runbook(ambiguous))

    def test_production_and_policy_failures_have_no_self_healing_runbook(self) -> None:
        for surface in (IncidentClass.PRODUCTION, IncidentClass.POLICY):
            with self.subTest(surface=surface):
                signal = IncidentSignal(
                    signal_id=surface.value,
                    code="quality_or_policy_failure",
                    surface=surface,
                    summary="Not an infrastructure repair.",
                    affected_task_ids=("A",),
                )
                self.assertEqual(classify_incident(signal), surface)
                self.assertIsNone(select_runbook(signal))

    def test_known_failed_transport_policy_rejection_is_a_pipeline_incident(self) -> None:
        signal = IncidentSignal(
            signal_id="run:reservation:create:policy-rejected",
            code="transport_policy_rejected",
            surface=IncidentClass.PIPELINE,
            summary="The fixed create relay was definitively rejected.",
            affected_task_ids=("A",),
            operation="create_thread",
            side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
        )
        self.assertEqual(classify_incident(signal), IncidentClass.PIPELINE)
        self.assertIsNone(select_runbook(signal))


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

    def test_legacy_recovery_mandate_is_read_only_compatible(self) -> None:
        state = {
            "schema_version": 1,
            "sequence": 0,
            "recovery_slot": None,
            "incidents": [],
            "transport_reservations": [
                {
                    "reservation_id": "legacy-transport",
                    "status": "CLAIMED",
                    "authority_kind": "PIPELINE_RECOVERY_MANDATE",
                    "actor_thread_id": "legacy-owner",
                    "claim_token": "legacy-token",
                }
            ],
            "journal": [],
        }
        self.store.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(
            self.store.load()["transport_reservations"][0]["authority_kind"],
            "PIPELINE_RECOVERY_MANDATE",
        )
        with self.assertRaises(ValueError):
            AuthorityKind("PIPELINE_RECOVERY_MANDATE")

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

    def test_only_affected_task_is_paused_and_recovery_has_lock_slot_budget_backoff(self) -> None:
        incident_id = self._open()
        state = RunState(
            graph_version=1,
            execution_strategy="parallel",
            max_parallel_workers=2,
            task_states={"A": "READY", "B": "READY"},
        )
        decision = schedule(
            self.plan,
            state,
            build_scheduler_availability(self.plan, state, self.root),
        )
        self.assertEqual(decision.selected_task_ids, ("B",))
        self.assertIn(
            f"incident:{incident_id}:DEGRADED",
            decision.reasons_for("A"),
        )
        self.assertEqual(self.store.route_incident(incident_id, at="t1"), IncidentPhase.DEGRADED)

        first = self.store.claim_auto_recovery(
            incident_id,
            owner_id="deterministic-supervisor",
            now_epoch=100,
            at="t2",
        )
        self.assertEqual(first.slot, 0)
        self.assertEqual(first.attempt, 1)
        with self.assertRaisesRegex(PipelineIncidentError, "not allowlisted"):
            self.store.record_recovery_action(
                incident_id,
                first.token,
                action="delete_project_state",
                at="t3",
            )
        self.store.record_recovery_action(
            incident_id,
            first.token,
            action="reopen_owned_local_channel",
            at="t3",
        )
        self.assertEqual(
            self.store.complete_auto_recovery(
                incident_id,
                first.token,
                success=False,
                now_epoch=100,
                at="t4",
                reason="channel still closed",
            ),
            IncidentPhase.DEGRADED,
        )
        with self.assertRaisesRegex(PipelineIncidentError, "backoff"):
            self.store.claim_auto_recovery(
                incident_id,
                owner_id="deterministic-supervisor",
                now_epoch=114,
                at="t5",
            )
        second = self.store.claim_auto_recovery(
            incident_id,
            owner_id="deterministic-supervisor",
            now_epoch=115,
            at="t6",
        )
        self.assertEqual(second.attempt, 2)
        self.assertEqual(
            self.store.complete_auto_recovery(
                incident_id,
                second.token,
                success=False,
                now_epoch=115,
                at="t7",
            ),
            IncidentPhase.AUTO_RECOVERY_FAILED,
        )
        failed = self.store.incident_package(incident_id)["incident"]
        self.assertTrue(requires_pipeline_engineer(failed))

    def test_fresh_pipeline_engineer_is_infrastructure_only_and_escalates_on_failure(self) -> None:
        incident_id = self._open(signal_id="agent-route")
        first = self.store.claim_auto_recovery(
            incident_id,
            owner_id="supervisor",
            now_epoch=0,
            at="t1",
        )
        self.store.complete_auto_recovery(
            incident_id,
            first.token,
            success=False,
            now_epoch=0,
            at="t2",
        )
        second = self.store.claim_auto_recovery(
            incident_id,
            owner_id="supervisor",
            now_epoch=15,
            at="t3",
        )
        self.store.complete_auto_recovery(
            incident_id,
            second.token,
            success=False,
            now_epoch=15,
            at="t4",
        )
        package = self.store.activate_pipeline_engineer(incident_id, at="t5")
        self.assertEqual(package["role"]["name"], "Pipeline Engineer · On call")
        self.assertIn("delete_project_state", package["forbidden_actions"])
        runtime = AIStudioRuntime(
            self.plan,
            self.root,
            language="en",
            skill_path=self.skill,
        )
        self.assertEqual(runtime.system_roles()[0].name, "Pipeline Engineer · On call")
        prompt = runtime.build_pipeline_engineer_prompt(
            package,
            reservation_token="incident-worker-1",
        )
        self.assertIn("Pipeline Engineer · On call", prompt)
        self.assertIn("durable authorization already covers every fixed", prompt)
        self.assertIn("never creates, forks, starts, or messages", prompt)
        self.assertEqual(
            self.store.complete_pipeline_engineer(
                incident_id,
                success=False,
                at="t6",
                reason="bounded repair failed again",
            ),
            IncidentPhase.ESCALATE_TO_USER,
        )

        production = self.store.open_incident(
            IncidentSignal(
                signal_id="production-quality",
                code="quality_check_failed",
                surface=IncidentClass.PRODUCTION,
                summary="Tests expose a product defect.",
                affected_task_ids=("B",),
            ),
            at="t7",
        )
        self.assertEqual(
            self.store.route_incident(production["incident_id"], at="t8"),
            IncidentPhase.ESCALATE_TO_USER,
        )
        with self.assertRaises(PipelineIncidentError):
            self.store.incident_package(production["incident_id"])
        with self.assertRaises(ContextBoundaryError):
            runtime.build_pipeline_engineer_prompt(
                {
                    "incident": production,
                    "forbidden_actions": package["forbidden_actions"],
                },
                reservation_token="forbidden",
            )

    def test_passing_healthcheck_is_mandatory_before_resume(self) -> None:
        incident_id = self._open(signal_id="healthcheck")
        claim = self.store.claim_auto_recovery(
            incident_id,
            owner_id="supervisor",
            now_epoch=0,
            at="t1",
        )
        with self.assertRaisesRegex(PipelineIncidentError, "healthcheck"):
            self.store.complete_auto_recovery(
                incident_id,
                claim.token,
                success=True,
                at="t2",
            )
        self.assertFalse(self.store.can_resume_task("A"))
        with self.assertRaisesRegex(PipelineIncidentError, "declared runbook"):
            self.store.complete_auto_recovery(
                incident_id,
                claim.token,
                success=True,
                at="t2b",
                healthcheck=HealthcheckResult(
                    name="some_other_check",
                    passed=True,
                    checks=("looked healthy",),
                    observed_at="t2b",
                ),
            )
        self.assertEqual(
            self.store.complete_auto_recovery(
                incident_id,
                claim.token,
                success=True,
                at="t3",
                healthcheck=HealthcheckResult(
                    name=claim.healthcheck,
                    passed=True,
                    checks=("round trip returned expected response",),
                    observed_at="t3",
                ),
            ),
            IncidentPhase.RECOVERED,
        )
        self.assertTrue(self.store.can_resume_task("A"))
        self.store.resolve_recovered(incident_id, at="t4")
        self.assertEqual(self.store.status_snapshot()["phase"], "HEALTHY")

    def test_crash_reconciliation_keeps_unknown_lock_and_never_infers_health(self) -> None:
        incident_id = self._open(signal_id="crash")
        claim = self.store.claim_auto_recovery(
            incident_id,
            owner_id="supervisor",
            now_epoch=0,
            at="t1",
        )
        self.assertEqual(
            self.store.reconcile_recovery_after_crash({}, now_epoch=1, at="t2"),
            IncidentPhase.AUTO_RECOVERY,
        )
        self.assertEqual(self.store.status_snapshot()["recovery_slot"]["token"], claim.token)
        self.assertEqual(
            self.store.reconcile_recovery_after_crash(
                {claim.token: "TERMINAL_SUCCEEDED"},
                now_epoch=2,
                at="t3",
            ),
            IncidentPhase.DEGRADED,
        )
        self.assertFalse(self.store.can_resume_task("A"))


class TransportAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="transport-authority-")
        self.store = PipelineIncidentStore(Path(self.temp.name) / ".codex-autopilot")
        self.run_state = RunState(run_id="run-1")
        self.run_state.lifecycle_journal_sequence = 1
        self.run_state.lifecycle_journal = [
            {
                "sequence": 1,
                "event": "turn_completed",
                "task_id": "M7",
                "thread_id": "m7-thread",
                "turn_id": "m7-turn",
            }
        ]
        self.digest = hashlib.sha256(b"exact create payload").hexdigest()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_causal_relay_uses_run_authorization_and_ambiguous_transport_is_not_repeated(self) -> None:
        reservation = self.store.reserve_transport_from_lifecycle(
            self.run_state,
            causal_event_sequence=1,
            operation="create_thread",
            payload_sha256=self.digest,
            destination_task_id="M8",
            at="t1",
        )
        self.assertIsNone(reservation["authority_kind"])
        claim = self.store.claim_transport(
            reservation["reservation_id"],
            actor_thread_id="m7-thread",
            proof=AuthorityProof(
                kind=AuthorityKind.AUTOPILOT_RUN,
                evidence_id="initiating-user-run-authorization",
                subject_thread_id="m7-thread",
            ),
            at="t2",
        )
        self.store.verify_transport_claim(claim)

        other = self.store.reserve_transport_from_lifecycle(
            self.run_state,
            causal_event_sequence=1,
            operation="send_message_to_thread",
            payload_sha256=self.digest,
            destination_task_id="M8",
            at="t2",
        )
        with self.assertRaisesRegex(AuthorizationTopologyError, "forwarded user text"):
            self.store.claim_transport(
                other["reservation_id"],
                actor_thread_id="authorized-task",
                proof=AuthorityProof(
                    kind="HOOK_DELEGATION",  # type: ignore[arg-type]
                    evidence_id="forwarded-words",
                    subject_thread_id="authorized-task",
                ),
                at="t2",
            )

        forged = TransportClaim(
            reservation_id=claim.reservation_id,
            token="forged-token",
            operation=claim.operation,
            payload_sha256=claim.payload_sha256,
            actor_thread_id=claim.actor_thread_id,
            authority_kind=claim.authority_kind,
            authority_evidence_id=claim.authority_evidence_id,
            destination_task_id=claim.destination_task_id,
        )
        with self.assertRaisesRegex(AuthorizationTopologyError, "durable authority journal"):
            self.store.verify_transport_claim(forged)
        self.store.mark_transport_requested(
            reservation["reservation_id"],
            claim.token,
            actor_thread_id="m7-thread",
            at="t4",
        )
        self.assertEqual(
            self.store.reconcile_transport(
                reservation["reservation_id"],
                outcome=SideEffectOutcome.UNKNOWN,
                at="t5",
                detail="connection closed after request",
            ),
            TransportStatus.AMBIGUOUS,
        )
        with self.assertRaisesRegex(AuthorizationTopologyError, "already requested"):
            self.store.mark_transport_requested(
                reservation["reservation_id"],
                claim.token,
                actor_thread_id="m7-thread",
                at="t6",
            )
        self.assertEqual(
            self.store.reconcile_transport(
                reservation["reservation_id"],
                outcome=SideEffectOutcome.KNOWN_SUCCEEDED,
                receipt_id="thread-m8",
                at="t7",
            ),
            TransportStatus.ACKNOWLEDGED,
        )

    def test_transport_requires_exact_authoritative_completion_event(self) -> None:
        self.run_state.lifecycle_journal[0]["event"] = "create_requested"
        with self.assertRaisesRegex(AuthorizationTopologyError, "turn_completed"):
            self.store.reserve_transport_from_lifecycle(
                self.run_state,
                causal_event_sequence=1,
                operation="send_message_to_thread",
                payload_sha256=self.digest,
                destination_task_id="M8",
                at="t1",
            )


if __name__ == "__main__":
    unittest.main()
