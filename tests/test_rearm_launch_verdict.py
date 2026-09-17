"""B2: перевзвод девопса различает «идёт» и «сломалось».

``devops-rearm-relay-owner`` взводит прогон так, что ход выполнит
предшественник на своём СЛЕДУЮЩЕМ Stop. Ветки в ближайшие пятнадцать
секунд быть не может по построению - а хвост команды читал булев
``launch_confirmed`` («все пункты True») и на False аннулировал решение
инженера. Гейт, который в своём окне пройти не может, отменял каждую
починку.

Замерено на построенном состоянии «только что перевзведён»: трёхзначный
``launch_verdict`` отвечает IN_PROGRESS - идёт, ничего не сломано, - а
``launch_confirmed`` отвечает False. У одного гейта было два потребителя
с разной семантикой: Stop-хук (control.py:802) уже различал три
вердикта, перевзвод - нет.

Теперь решение одно на оба: FAILED - аннулировать, IN_PROGRESS - не
аннулировать и не объявлять подтверждённым (R26: неизмеренное
помечается, а не выдумывается), CONFIRMED - подтверждено.
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


# Только что перевзведённый релей: резервация есть, диспетчер жив,
# отказов нет, а ветки ещё нет - ей взяться неоткуда до следующего Stop.
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

# Тот же релей, но диспетчер умер: решающий пункт False - это поломка.
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
        # Настоящий путь до RESOLVED: открыть, отдать инженеру, закрыть с
        # проходящей проверкой здоровья. Прямой правки фазы здесь нет.
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
