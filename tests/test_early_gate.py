"""R31: the check refuses where the error can still be fixed.

The completion gate asks for evidence linked to the milestone. A record
without milestone_id used to be accepted silently, and the refusal came
only after the whole turn was spent.

Measured on a live run: worker M2 recorded four pieces of evidence,
putting the milestone id into created_by ("M2-FILE-EXISTS",
"M2-EXACT-CONTENT") instead of milestone_id. Nothing landed in
milestone_evidence, completion was refused with "M2 returned completion
without new Project Memory evidence", the turn was lost entirely, and a
ticket was opened on M1.
"""

from __future__ import annotations

from datetime import datetime, timezone
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

    def test_a_milestone_that_names_no_task_is_refused_too(self) -> None:
        """R31 refused the wrong half: absence, but not a wrong name.

        The gate exists so the record can reach the completion gate. It
        refused only a MISSING milestone_id and accepted any non-empty
        string - including the very label the measured worker used in
        created_by. Such a record lands under a milestone nobody will ask
        about, completion is refused for "no new evidence", and the turn is
        lost exactly as it was before the gate existed.
        """

        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(
                self.evidence(milestone_id="M2-FILE-EXISTS")
            )
        text = str(caught.exception)
        self.assertIn("M2-FILE-EXISTS", text)
        self.assertIn("M2", text)
        self.assertEqual(
            list(self.server.memory.milestone_evidence("M2", after_audit_id=0, limit=10)),
            [],
        )

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
    по отказу M2 назвал affected_task_ids=['M1'], а лестница "on failure"
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
        self.assertIn('_print_relay_timeline(cfg, cursor.token, "on failure")', body)
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


class PlacementDeadlineTests(unittest.TestCase):
    """M11-R5: неизмеренное размещение не остаётся неопределённым вечно.

    OUTSIDE и ABSENT уже были решающими и уходили в один нормализованный
    тикет. А вот случай "nobody is left to measure it" - диспетчер умер между
    созданием ветки и гейтом размещения - держал вердикт в IN_PROGRESS
    навсегда: тикет не заводился, задача не двигалась, и снаружи это
    выглядело как будто запуск всё ещё идёт.
    """

    def _check(self, session, *, now):
        from codex_autopilot.launch_gate import _desktop_visibility

        return _desktop_visibility("T1", "thread-1", session, now=lambda: now)

    def test_a_fresh_unmeasured_placement_stays_undecided(self) -> None:
        """Окно между созданием и записью размещения - не отказ."""

        check = self._check(
            {"create_acknowledged_at": "2026-09-13T12:00:00+00:00"},
            now=datetime(2026, 9, 13, 12, 0, 30, tzinfo=timezone.utc).timestamp(),
        )
        self.assertIsNone(check.passed)

    def test_a_stale_unmeasured_placement_fails(self) -> None:
        from codex_autopilot.launch_gate import (
            PLACEMENT_MEASUREMENT_DEADLINE_SECONDS,
        )

        check = self._check(
            {"create_acknowledged_at": "2026-09-13T12:00:00+00:00"},
            now=(
                datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc).timestamp()
                + PLACEMENT_MEASUREMENT_DEADLINE_SECONDS
                + 1
            ),
        )
        self.assertIs(check.passed, False)
        self.assertIn("nobody is left to measure it", check.detail)

    def test_without_a_creation_stamp_nothing_is_declared_overdue(self) -> None:
        """Нет отметки - нет срока. Подменять одно другим здесь нельзя."""

        check = self._check({}, now=datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(check.passed)

    def test_a_failed_placement_reaches_the_normalized_ticket(self) -> None:
        """Отрицательный пункт обязан ронять вердикт, иначе тикета нет."""

        from codex_autopilot.launch_gate import (
            LaunchCheck,
            LaunchVerdict,
            launch_verdict,
        )

        checks = (
            LaunchCheck("reserved", "T1", True, ""),
            LaunchCheck("visible_in_desktop", "T1", False, "nobody is left to measure it"),
        )
        self.assertIs(launch_verdict(checks), LaunchVerdict.FAILED)


class HandoffObservationTests(unittest.TestCase):
    """M11-R5, вторая половина: редактируемость наблюдается, а не гейтится.

    Замерено на живом сервере: canAcceptDirectInput приходит null и в
    thread/read незагруженной ветки, и во всех тридцати строках
    thread/list. На таком поле гейт не строится - оно не различает
    "нельзя править" и "никто не держит".
    """

    def test_the_observation_carries_what_the_server_said(self) -> None:
        from codex_autopilot.launch_gate import placement_observation

        class Fake:
            def read_thread(self, thread_id):
                return {
                    "canAcceptDirectInput": None,
                    "status": {"type": "notLoaded"},
                    "originator": "Codex Desktop",
                    "threadSource": None,
                }

        observed = placement_observation(Fake(), "t-1")
        self.assertTrue(observed["observed"])
        self.assertIsNone(observed["can_accept_direct_input"])
        self.assertEqual(observed["status_type"], "notLoaded")
        self.assertEqual(observed["originator"], "Codex Desktop")

    def test_a_failed_read_is_recorded_as_not_observed(self) -> None:
        from codex_autopilot.launch_gate import placement_observation

        class Broken:
            def read_thread(self, thread_id):
                raise RuntimeError("thread not found")

        observed = placement_observation(Broken(), "t-1")
        self.assertFalse(observed["observed"])
        self.assertIn("thread not found", observed["reason"])
