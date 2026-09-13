"""R31: проверка отказывает там, где ошибку ещё можно исправить.

Ворота завершения спрашивают свидетельства, связанные с вехой. Запись без
milestone_id принималась молча, и отказ наступал уже после того, как весь
ход потрачен.

Замерено на живом прогоне: воркер M2 записал четыре свидетельства, положив
идентификатор вехи в created_by ("M2-FILE-EXISTS", "M2-EXACT-CONTENT")
вместо milestone_id. В milestone_evidence не легло ничего, завершение
отклонили с "M2 returned completion without new Project Memory evidence",
ход пропал целиком, а на M1 завели тикет.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_autopilot.memory import MemoryValidationError
from codex_autopilot.memory_mcp import MemoryMcpServer


class EarlyMilestoneLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        self.state = self.root / ".codex-autopilot"
        self.state.mkdir()
        (self.state / "config.toml").write_text("", encoding="utf-8")
        self.set_active(["M2"])
        self.server = MemoryMcpServer(self.root)

    def set_active(self, task_ids: list[str]) -> None:
        (self.state / "run-state.json").write_text(
            json.dumps({"active_task_ids": task_ids}), encoding="utf-8"
        )

    def evidence(self, **extra) -> dict:
        payload = {
            "kind": "test",
            "summary": "beta file contains beta",
            "created_by": "M2-FILE-EXISTS",
            "command": "cat autopilot-080-b.txt",
            "result": "beta",
            "exit_code": 0,
        }
        payload.update(extra)
        return payload

    def test_unlinked_evidence_is_refused_while_a_milestone_is_active(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(self.evidence())
        self.assertIn("M2", str(caught.exception))

    def test_the_refusal_names_what_is_missing(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(self.evidence())
        text = str(caught.exception)
        self.assertIn("milestone_id", text)
        self.assertIn("created_by", text)

    def test_the_milestone_is_never_substituted_for_the_worker(self) -> None:
        """Связь, которую воркер не назвал, была бы выдуманной."""

        with self.assertRaises(MemoryValidationError):
            self.server._record_evidence(self.evidence())
        rows = list(
            self.server.memory.milestone_evidence("M2", after_audit_id=0, limit=10)
        )
        self.assertEqual(rows, [])

    def test_linked_evidence_passes(self) -> None:
        result = self.server._record_evidence(self.evidence(milestone_id="M2"))
        self.assertTrue(result)

    def test_without_an_active_milestone_the_link_stays_optional(self) -> None:
        """Вне вехи свидетельство привязывать не к чему."""

        self.set_active([])
        self.assertTrue(self.server._record_evidence(self.evidence()))


if __name__ == "__main__":
    unittest.main()


class FailureNamesTheFailingTaskTests(unittest.TestCase):
    """Тикет называет задачу, на которой произошёл отказ.

    Цикл диспетчера переходит от задачи к задаче, переприсваивая свой
    token. Обработчик отказа снаружи держал исходный, и отказ на поздней
    задаче приписывался первой. Замерено: тикет incident-78e67b38680498f1
    по отказу M2 назвал affected_task_ids=['M1'], а лестница "на отказе"
    напечатала шаги уже проверенной M1.
    """

    def test_the_cursor_follows_the_loop(self) -> None:
        from codex_autopilot.cli import _RelayCursor

        cursor = _RelayCursor("token-m1")
        cursor.token = "token-m2"
        self.assertEqual(cursor.token, "token-m2")

    def test_the_failure_handler_reads_the_cursor_not_the_initial_token(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/cli.py"
        ).read_text(encoding="utf-8")
        head = source[source.index("def _run_automatic_relay_dispatch") :]
        body = head[: head.index("\ndef ", 1)]
        self.assertIn('_print_relay_timeline(cfg, cursor.token, "на отказе")', body)
        self.assertIn("_record_detached_dispatch_failure(cfg, cursor.token, error)", body)

    def test_the_loop_updates_the_cursor_each_iteration(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/cli.py"
        ).read_text(encoding="utf-8")
        head = source[source.index("def _automatic_relay_loop") :]
        body = head[: head.index("\ndef ", 1)]
        self.assertIn("cursor.token = token", body)


class RefusalNamesWhatIsAcceptedTests(unittest.TestCase):
    """R31: отказа должно хватать, чтобы исправиться без чтения исходников.

    Замерено на живом прогоне M1: девять отказов подряд. Воркер перебирал
    имена видов свидетельств - filesystem_verification, command_output,
    test_result, verification, - каждый раз получая только "unsupported
    evidence kind", затем угадывал параметр пути, затем ушёл читать
    исходники плагина командой rg. Шесть минут против двадцати трёх секунд
    у v0.7, где памяти не было вовсе.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        state = self.root / ".codex-autopilot"
        state.mkdir()
        (state / "config.toml").write_text("", encoding="utf-8")
        (state / "run-state.json").write_text(
            json.dumps({"active_task_ids": []}), encoding="utf-8"
        )
        self.server = MemoryMcpServer(self.root)

    def test_an_unsupported_kind_lists_the_supported_ones(self) -> None:
        from codex_autopilot.memory import EVIDENCE_KINDS

        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(
                {"kind": "command_output", "summary": "s", "created_by": "w"}
            )
        text = str(caught.exception)
        for kind in sorted(EVIDENCE_KINDS):
            self.assertIn(kind, text)

    def test_a_missing_path_names_the_parameter(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(
                {"kind": "file", "summary": "s", "created_by": "w"}
            )
        self.assertIn("path=", str(caught.exception))

    def test_an_unknown_argument_lists_the_accepted_ones(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(
                {
                    "kind": "file",
                    "summary": "s",
                    "created_by": "w",
                    "project_path": "x",
                }
            )
        text = str(caught.exception)
        self.assertIn("project_path", text)
        self.assertIn("milestone_id", text)
        self.assertIn("artifact_path", text)
