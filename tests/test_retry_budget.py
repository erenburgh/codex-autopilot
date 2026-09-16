"""R23: повтор одной и той же неудачи ограничен.

Правило заявлено ENFORCED, а потолка не было вовсе. ``maximum_attempts``
стоял в конфиге со значением 96 и не читался никем: три упоминания на
репозиторий - шаблон, дефолт, разбор - и ни одного потребителя. Работали
только ``initial_seconds`` и ``maximum_seconds``, то есть повтор шёл
бесконечно, а не сутки.

Стену, которая ограничивала повторы случайно, сняли в этот же день.
Раньше протокольная ошибка модели роняла диспетчер, и прогон вставал -
требовал человека. После правки «нечитаемый вердикт возвращается
верифаеру» тот же отказ штатно уходит в RETRY_WAIT и повторяется. Если
модель ошибается устойчиво одинаково - а мы видели именно это, один и
тот же формат финальной строки, - цикл идёт сам по себе, и ограничивать
его стало нечему.

Счёт ведётся по СИГНАТУРЕ, а не по задаче: одна поломка у двух задач -
одна поломка. Вид отказа называет вызывающий (``failure_code``), потому
что сигнатура в этом проекте отвечает на вопрос «что сломалось», а
свободный текст ``reason`` на него не отвечает - ровно то основание, по
которому из ``incident_signature`` выкинуты summary и affected_task_ids.

На потолке прогон не встаёт: заводится тикет дежурному инженеру, и пауза
тикета разрывает цикл. Остановка наступает дальше, когда исчерпан уже
инженер - этого требует R3.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from _gates import patch_hook_trust_gates
from _plan_contract import initialize_verified_project as initialize_project
from _relay import reserve_ready_frontier
from codex_autopilot.config import load_config
from codex_autopilot.lifecycle_base import PENDING_SESSION_STATUSES
from codex_autopilot.lifecycle_failures import record_desktop_failure
from codex_autopilot.pipeline_engineer import PipelineIncidentStore
from codex_autopilot.run_state import StateStore
from test_desktop_lifecycle import graph, task


class RetryBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        raw = graph(max_workers=2)
        raw["tasks"] = [task("A", path="src/a"), task("B", path="src/b")]
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(raw), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)

    # --- инструменты ---------------------------------------------------

    def pending_token(self, task_id: str) -> str | None:
        """Токен уже открытой резервации задачи, если она есть."""

        for item in self.store.load().worker_sessions:
            if item.get("task_id") != task_id:
                continue
            if item.get("status") in PENDING_SESSION_STATUSES:
                return str(item["reservation_token"])
        return None

    def fail_once(self, task_id: str, code: str, *, rate_limited: bool = False) -> None:
        """Один настоящий отказ задачи: берём её слот и роняем.

        При двух слотах фронтир забирает обе задачи сразу, поэтому
        резервировать нужно только тогда, когда открытой резервации нет.
        """

        token = self.pending_token(task_id)
        if token is None:
            state = self.store.load()
            now = max([0, *state.task_retry_at.values()]) or None
            reserve_ready_frontier(self.cfg, now_epoch=now)
            token = self.pending_token(task_id)
        self.assertIsNotNone(token, f"{task_id} не зарезервирована")
        record_desktop_failure(
            self.cfg,
            token,
            reason=f"обстоятельства попытки для {task_id}",
            failure_code=code,
            definitive=True,
            rate_limited=rate_limited,
            reserve_other_ready=False,
        )

    def attempts(self, code: str) -> int:
        return int(self.store.load().failure_signature_attempts.get(code, 0))

    def incidents_for(self, code: str) -> list[dict]:
        raw = PipelineIncidentStore(self.cfg.state_dir).load()
        return [item for item in raw["incidents"] if item.get("code") == code]

    # --- сами проверки -------------------------------------------------

    def test_the_same_signature_stops_looping_at_the_cap(self) -> None:
        cap = self.cfg.retry.maximum_attempts
        self.assertEqual(cap, 5, "потолок по умолчанию изменился молча")

        for _ in range(cap - 1):
            self.fail_once("A", "worker_protocol_rejected")
        self.assertEqual(self.attempts("worker_protocol_rejected"), cap - 1)
        self.assertEqual(
            self.incidents_for("worker_protocol_rejected"),
            [],
            "до потолка инженера не зовут",
        )

        self.fail_once("A", "worker_protocol_rejected")
        self.assertEqual(self.attempts("worker_protocol_rejected"), cap)

        opened = self.incidents_for("worker_protocol_rejected")
        self.assertEqual(len(opened), 1, "на потолке заводится ровно один тикет")
        self.assertIn("A", opened[0]["affected_task_ids"])

        paused = PipelineIncidentStore(self.cfg.state_dir).status_snapshot()
        self.assertIn(
            "A",
            paused["paused_task_ids"],
            "цикл разрывает именно пауза тикета",
        )

    def test_a_different_signature_does_not_share_the_cap(self) -> None:
        """Счёт по сигнатуре: разные поломки не складываются в один потолок."""

        for _ in range(3):
            self.fail_once("A", "worker_protocol_rejected")
        for _ in range(3):
            self.fail_once("A", "app_server_rpc_failed")

        self.assertEqual(self.attempts("worker_protocol_rejected"), 3)
        self.assertEqual(self.attempts("app_server_rpc_failed"), 3)
        self.assertEqual(self.incidents_for("worker_protocol_rejected"), [])
        self.assertEqual(self.incidents_for("app_server_rpc_failed"), [])

    def test_the_cap_counts_the_breakage_not_the_task(self) -> None:
        """Одна поломка у двух задач - одна поломка.

        Сигнатура намеренно не включает задачу: то же основание, по
        которому affected_task_ids не входит в incident_signature.
        """

        self.fail_once("A", "app_server_rpc_failed")
        self.fail_once("B", "app_server_rpc_failed")
        self.assertEqual(
            self.attempts("app_server_rpc_failed"),
            2,
            "счёт по задаче вместо сигнатуры",
        )

    def test_waiting_for_a_rate_limit_does_not_spend_the_cap(self) -> None:
        """У лимита свой барьер и своя причина.

        Тратить на него потолок значит останавливать прогон за чужой
        счёт: задача не сломана, она ждёт.
        """

        for _ in range(6):
            self.fail_once("A", "app_server_rpc_failed", rate_limited=True)

        self.assertEqual(self.attempts("app_server_rpc_failed"), 0)
        self.assertEqual(self.incidents_for("app_server_rpc_failed"), [])

    def test_a_failure_without_a_named_kind_is_refused_with_the_accepted_list(self) -> None:
        """R31: отказ называет принятое, чтобы не читать исходники."""

        from codex_autopilot.lifecycle_base import DesktopLifecycleError

        pending = reserve_ready_frontier(self.cfg)
        with self.assertRaises(DesktopLifecycleError) as caught:
            record_desktop_failure(
                self.cfg,
                pending[0].reservation_token,
                reason="что-то сломалось",
                failure_code="   ",
                definitive=True,
                reserve_other_ready=False,
            )
        message = str(caught.exception)
        self.assertIn("failure_code", message)
        self.assertIn("worker_protocol_rejected", message)


if __name__ == "__main__":
    unittest.main()
