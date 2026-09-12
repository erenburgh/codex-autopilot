"""P3: тикеты с нормализованной подписью, два уровня восстановления, раннбуки."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.pipeline_engineer import (
    PROMOTION_THRESHOLD,
    READ_ONLY_DIAGNOSTIC_ACTIONS,
    HealthcheckResult,
    IncidentClass,
    IncidentPhase,
    IncidentSignal,
    PipelineIncidentError,
    PipelineIncidentStore,
    SideEffectOutcome,
    incident_signature,
)

CHECK = "relay_transport_healthy"


def signal(
    signal_id: str,
    *,
    code: str = "transport_policy_rejected",
    task: str = "M9",
    surface: IncidentClass = IncidentClass.PIPELINE,
) -> IncidentSignal:
    return IncidentSignal(
        signal_id=signal_id,
        code=code,
        surface=surface,
        summary=f"Relay rejected for {task}.",
        affected_task_ids=(task,),
        operation="create_thread",
        side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
    )


def healthcheck(passed: bool = True) -> HealthcheckResult:
    return HealthcheckResult(
        name=CHECK, passed=passed, checks=("relay responds",), observed_at="t9"
    )


class SignatureTests(unittest.TestCase):
    def test_the_same_failure_under_different_signal_ids_shares_a_signature(self) -> None:
        """В живом прогоне два инцидента были одной поломкой, разведённой
        случайной частью signal_id."""

        self.assertEqual(
            incident_signature(signal("attempt-one")),
            incident_signature(signal("attempt-two")),
        )

    def test_the_same_failure_on_another_task_shares_a_signature(self) -> None:
        """Отказ транспорта на M4 и на M9 - одна инфраструктурная поломка."""

        self.assertEqual(
            incident_signature(signal("a", task="M4")),
            incident_signature(signal("b", task="M9")),
        )

    def test_a_different_code_gets_a_different_signature(self) -> None:
        self.assertNotEqual(
            incident_signature(signal("a")),
            incident_signature(signal("b", code="app_server_thread_start_failed")),
        )

    def test_free_text_never_changes_the_signature(self) -> None:
        from dataclasses import replace

        base = signal("a")
        self.assertEqual(
            incident_signature(base),
            incident_signature(replace(base, summary="совсем другой текст")),
        )


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PipelineIncidentStore(Path(self.temp.name))


class PersistedReservationTests(StoreTestCase):
    """Состояние с резервациями не читалось: TransportStatus и AuthorityKind
    не были определены ни в одном коммите, а ни один тест резерваций не
    создавал. В живом прогоне они есть."""

    def write_state(self, reservation: dict) -> None:
        import json

        from codex_autopilot.pipeline_engineer import INCIDENT_STATE_SCHEMA_VERSION

        (Path(self.temp.name) / "pipeline-incidents.json").write_text(
            json.dumps(
                {
                    "schema_version": INCIDENT_STATE_SCHEMA_VERSION,
                    "sequence": 0,
                    "recovery_slot": None,
                    "incidents": [],
                    "transport_reservations": [reservation],
                    "journal": [],
                }
            ),
            encoding="utf-8",
        )

    def test_state_with_an_acknowledged_reservation_loads(self) -> None:
        self.write_state(
            {
                "reservation_id": "r1",
                "status": "ACKNOWLEDGED",
                "authority_kind": "USER_AUTHORIZED_TASK",
                "actor_thread_id": "thread-a",
                "claim_token": "token-a",
            }
        )
        self.assertEqual(len(self.store.load()["transport_reservations"]), 1)

    def test_legacy_recovery_mandate_still_loads(self) -> None:
        self.write_state(
            {
                "reservation_id": "r2",
                "status": "CLAIMED",
                "authority_kind": "PIPELINE_RECOVERY_MANDATE",
                "actor_thread_id": "thread-b",
                "claim_token": "token-b",
            }
        )
        self.assertEqual(len(self.store.load()["transport_reservations"]), 1)

    def test_a_claim_without_any_authority_is_rejected(self) -> None:
        self.write_state(
            {
                "reservation_id": "r3",
                "status": "CLAIMED",
                "authority_kind": "INVENTED_AUTHORITY",
                "actor_thread_id": "thread-c",
                "claim_token": "token-c",
            }
        )
        with self.assertRaises(PipelineIncidentError):
            self.store.load()

    def test_state_from_a_version_without_the_signature_ledger_loads(self) -> None:
        """Реестр добавлен позже и намеренно не поднимает версию схемы."""

        self.write_state(
            {
                "reservation_id": "r4",
                "status": "RESERVED",
                "authority_kind": "USER_AUTHORIZED_TASK",
                "actor_thread_id": "thread-d",
                "claim_token": "token-d",
            }
        )
        self.assertEqual(self.store.load()["signatures"], {})


class RecurrenceTests(StoreTestCase):
    def test_recurrence_is_counted_under_one_signature(self) -> None:
        self.store.open_incident(signal("one"), at="t1")
        self.store.open_incident(signal("two"), at="t2")
        self.store.open_incident(signal("three", code="other_code"), at="t3")
        ledger = self.store.signature_ledger()
        counts = sorted(int(item["occurrences"]) for item in ledger.values())
        self.assertEqual(counts, [1, 2])

    def test_recurrence_is_announced_in_the_journal(self) -> None:
        self.store.open_incident(signal("one"), at="t1")
        self.store.open_incident(signal("two"), at="t2")
        events = [item["event"] for item in self.store.load()["journal"]]
        self.assertIn("incident_recurrence_observed", events)

    def test_reopening_the_same_signal_is_not_a_recurrence(self) -> None:
        """Повторная доставка одного сигнала - тот же тикет, не второй случай."""

        self.store.open_incident(signal("one"), at="t1")
        self.store.open_incident(signal("one"), at="t2")
        ledger = self.store.signature_ledger()
        self.assertEqual(
            [int(item["occurrences"]) for item in ledger.values()], [1]
        )


class TwoLevelRecoveryTests(StoreTestCase):
    def test_without_a_runbook_level_one_is_unavailable(self) -> None:
        incident = self.store.open_incident(signal("one"), at="t1")
        with self.assertRaises(PipelineIncidentError):
            self.store.begin_auto_recovery(
                incident["incident_id"], at="t2", owner_id="dispatcher"
            )
        self.assertEqual(
            self.store.route_incident(incident["incident_id"], at="t2"),
            IncidentPhase.AUTO_RECOVERY_FAILED,
        )

    def promoted_incident(self) -> dict:
        """Довести подпись до выученного раннбука через уровень 2."""

        actions = [READ_ONLY_DIAGNOSTIC_ACTIONS[0], READ_ONLY_DIAGNOSTIC_ACTIONS[1]]
        for index in range(PROMOTION_THRESHOLD):
            incident = self.store.open_incident(signal(f"attempt-{index}"), at=f"t{index}")
            self.store.ensure_pipeline_engineer(incident["incident_id"], at=f"t{index}")
            self.store.complete_pipeline_engineer(
                incident["incident_id"],
                success=True,
                at=f"t{index}",
                healthcheck=healthcheck(),
                actions=actions,
            )
        return self.store.open_incident(signal("after-promotion"), at="t9")

    def test_a_repeated_fix_becomes_a_runbook_and_skips_the_engineer(self) -> None:
        incident = self.promoted_incident()
        self.assertIsNotNone(incident["runbook_id"])
        self.assertEqual(incident["runbook_healthcheck"], CHECK)
        events = [item["event"] for item in self.store.load()["journal"]]
        self.assertIn("runbook_promoted", events)

    def test_level_one_succeeds_and_never_reaches_the_engineer(self) -> None:
        incident = self.promoted_incident()
        started = self.store.begin_auto_recovery(
            incident["incident_id"], at="t10", owner_id="dispatcher"
        )
        self.assertEqual(started["phase"], IncidentPhase.AUTO_RECOVERY.value)
        self.assertEqual(started["recovery_attempts"], 1)
        phase = self.store.complete_auto_recovery(
            incident["incident_id"],
            token=started["recovery_lock_token"],
            success=True,
            at="t11",
            healthcheck=healthcheck(),
        )
        self.assertEqual(phase, IncidentPhase.RECOVERED)

    def test_exhausted_budget_hands_over_to_the_engineer(self) -> None:
        incident = self.promoted_incident()
        incident_id = incident["incident_id"]
        phase = None
        for attempt in range(int(incident["retry_budget"])):
            started = self.store.begin_auto_recovery(
                incident_id, at=f"t{attempt}", owner_id="dispatcher"
            )
            phase = self.store.complete_auto_recovery(
                incident_id,
                token=started["recovery_lock_token"],
                success=False,
                at=f"t{attempt}",
            )
        self.assertEqual(phase, IncidentPhase.AUTO_RECOVERY_FAILED)
        package = self.store.ensure_pipeline_engineer(incident_id, at="t20")
        self.assertEqual(
            self.store.load()["incidents"][-1]["phase"],
            IncidentPhase.PIPELINE_ENGINEER.value,
        )
        self.assertTrue(package)

    def test_delay_grows_and_stops_at_the_ceiling(self) -> None:
        incident = self.promoted_incident()
        started = self.store.begin_auto_recovery(
            incident["incident_id"], at="t10", owner_id="dispatcher"
        )
        first = started["next_retry_at"]
        self.assertEqual(first, incident["retry_initial_seconds"])
        self.store.complete_auto_recovery(
            incident["incident_id"],
            token=started["recovery_lock_token"],
            success=False,
            at="t11",
        )
        second = self.store.begin_auto_recovery(
            incident["incident_id"], at="t12", owner_id="dispatcher"
        )["next_retry_at"]
        self.assertGreater(second, first)
        self.assertLessEqual(second, incident["retry_maximum_seconds"])


class PromotionSafetyTests(StoreTestCase):
    def resolve_with(self, actions: list[str], *, times: int) -> None:
        for _ in range(times):
            # Сквозной счётчик: один signal_id - один тикет, повторное
            # открытие вернуло бы уже решённый инцидент.
            self.issued = getattr(self, "issued", 0) + 1
            index = self.issued
            incident = self.store.open_incident(signal(f"s-{index}"), at=f"t{index}")
            self.store.ensure_pipeline_engineer(incident["incident_id"], at=f"t{index}")
            self.store.complete_pipeline_engineer(
                incident["incident_id"],
                success=True,
                at=f"t{index}",
                healthcheck=healthcheck(),
                actions=actions,
            )

    def promoted(self) -> list:
        return [
            item
            for item in self.store.signature_ledger().values()
            if item.get("promoted_runbook") is not None
        ]

    def test_one_success_is_not_enough(self) -> None:
        self.resolve_with([READ_ONLY_DIAGNOSTIC_ACTIONS[0]], times=1)
        self.assertEqual(self.promoted(), [])

    def test_an_action_outside_the_safe_list_is_never_promoted(self) -> None:
        """Уровень 1 работает без человека и не вправе делать ничего,
        кроме диагностики и ограниченной повторной попытки."""

        self.resolve_with(["restart_the_whole_machine"], times=PROMOTION_THRESHOLD + 1)
        self.assertEqual(self.promoted(), [])

    def test_a_forbidden_action_is_never_promoted(self) -> None:
        from codex_autopilot.pipeline_engineer import FORBIDDEN_ACTIONS

        self.resolve_with([FORBIDDEN_ACTIONS[0]], times=PROMOTION_THRESHOLD + 1)
        self.assertEqual(self.promoted(), [])

    def test_different_fixes_do_not_add_up(self) -> None:
        """Два разных способа - не подтверждение одного и того же."""

        self.resolve_with([READ_ONLY_DIAGNOSTIC_ACTIONS[0]], times=1)
        self.resolve_with([READ_ONLY_DIAGNOSTIC_ACTIONS[1]], times=1)
        self.assertEqual(self.promoted(), [])

    def test_an_escalation_is_not_a_solution(self) -> None:
        incident = self.store.open_incident(signal("one"), at="t1")
        self.store.ensure_pipeline_engineer(incident["incident_id"], at="t1")
        self.store.complete_pipeline_engineer(
            incident["incident_id"], success=False, at="t2", reason="не смог"
        )
        self.assertEqual(self.promoted(), [])


if __name__ == "__main__":
    unittest.main()
