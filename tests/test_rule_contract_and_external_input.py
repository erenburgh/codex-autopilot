"""R16: rules apply as a contract. R18: external input does not override Truth."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.lifecycle import parse_applied_rules
from codex_autopilot.memory import (
    EVIDENCE_KINDS,
    TRUTH_EVIDENCE_KINDS,
    MemoryValidationError,
    ProjectMemory,
)
from codex_autopilot.rules import RULES


class AppliedRulesReportTests(unittest.TestCase):
    def test_report_lists_applied_rule_ids(self) -> None:
        message = "сделано\nAUTOPILOT_RULES: R7, R17\nAUTOPILOT_STATUS: ROTATE"
        self.assertEqual(parse_applied_rules(message), ("R7", "R17"))

    def test_report_without_the_list_yields_nothing(self) -> None:
        self.assertEqual(parse_applied_rules("AUTOPILOT_STATUS: ROTATE"), ())

    def test_ids_are_deduplicated_and_case_insensitive(self) -> None:
        message = "AUTOPILOT_RULES: r7, R7, R1\nAUTOPILOT_STATUS: DONE"
        self.assertEqual(parse_applied_rules(message), ("R7", "R1"))

    def test_the_list_precedes_the_status_line(self) -> None:
        """Разбор статуса требует, чтобы последней была строка статуса."""

        from codex_autopilot.lifecycle import parse_desktop_worker_status

        message = "AUTOPILOT_RULES: R7\nAUTOPILOT_STATUS: ROTATE"
        self.assertEqual(parse_desktop_worker_status(message), ("ROTATE", ""))
        self.assertEqual(parse_applied_rules(message), ("R7",))

    def test_prompt_asks_for_the_list_in_both_languages(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "codex_autopilot"
            / "ai_studio.py"
        ).read_text(encoding="utf-8")
        self.assertIn("строку AUTOPILOT_RULES с id правил", source)
        self.assertIn("an AUTOPILOT_RULES line with the ids", source)

    def test_every_prompt_variant_asks_for_the_list(self) -> None:
        """Счёт вхождений здесь не годится: вариантов финальной строки
        несколько, и достаточно пропустить один, чтобы воркер получал
        дефект R16 за то, о чём его не просили."""

        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "codex_autopilot"
            / "ai_studio.py"
        ).read_text(encoding="utf-8")
        silent = [
            index
            for index, line in enumerate(source.splitlines(), 1)
            if "AUTOPILOT_STATUS:" in line
            and ("Заверши" in line or "Finish with" in line)
            and "AUTOPILOT_RULES" not in line
        ]
        self.assertEqual(silent, [], f"варианты промпта без требования правил: {silent}")


class ExternalInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        (root / ".git").mkdir()
        self.memory = ProjectMemory(root)

    def test_external_is_a_known_evidence_kind(self) -> None:
        """Внешний текст записывается - он не исчезает, он не повышается."""

        self.assertIn("external", EVIDENCE_KINDS)

    def test_external_evidence_cannot_support_truth(self) -> None:
        self.assertNotIn("external", TRUTH_EVIDENCE_KINDS)
        evidence = self.memory.record_evidence(
            kind="external",
            summary="Комментарий в чужом issue утверждает, что порт 8080.",
            created_by="mcp-test",
            provider="github.com/other/repo#12",
            result="INFO",
        )
        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.record_verified_fact(
                statement="Порт сервиса 8080.",
                evidence_ids=[evidence["id"]],
                verification_method="external claim",
                created_by="mcp-test",
            )
        self.assertIn("R18", str(caught.exception))

    def test_external_material_has_a_label_on_the_ingestion_surface(self) -> None:
        """R18 бессмысленно, если честного ярлыка нет в инструменте.

        Схема memory_record_evidence не перечисляла "external" вовсе:
        воркер, принимающий текст со стороны, мог записать его только
        как file, tool или user_instruction - то есть заражение
        исчезало в момент приёма, и все дальнейшие проверки смотрели на
        ярлык, которого никто не мог поставить.
        """

        from codex_autopilot.memory_mcp import TOOLS

        choices = TOOLS[0]["inputSchema"]["oneOf"]
        choice = next(
            item
            for item in choices
            if item["properties"]["operation"]["const"] == "record_evidence"
        )
        kind = choice["properties"]["kind"]
        self.assertIn("external", kind["enum"])
        self.assertIn("provider", kind["description"])
        self.assertIn(
            "Required when kind",
            choice["properties"]["provider"]["description"],
        )

    def test_every_operation_carries_its_description_into_the_single_tool(self) -> None:
        """Снаружи объявлен один инструмент: подписи операций доходят только так.

        Подписи были написаны для каждой операции и не попадали в
        итоговую схему вовсе - мёртвый текст, которого модель не видела.
        """

        from codex_autopilot.memory_mcp import TOOLS, _ACTION_BY_NAME

        for choice in TOOLS[0]["inputSchema"]["oneOf"]:
            name = choice["properties"]["operation"]["const"]
            with self.subTest(operation=name):
                self.assertEqual(
                    choice.get("description"), _ACTION_BY_NAME[name]["description"]
                )
                self.assertTrue(choice.get("description"))

    def test_external_evidence_requires_a_provider(self) -> None:
        """Происхождение обязательно в момент приёма, а не потом."""

        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.record_evidence(
                kind="external",
                summary="Текст со страницы",
                created_by="mcp-test",
            )
        self.assertIn("provider", str(caught.exception))

    def test_a_non_user_constraint_cannot_rest_on_external_content(self) -> None:
        """У Constraint нет состояния "предложено": он действует сразу."""

        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.add_constraint(
                statement="Всегда менять очередь по требованию из чужого PR.",
                origin="agent",
                created_by="mcp-test",
                evidence_ids=[self.external_evidence()],
            )
        self.assertIn("R18", str(caught.exception))

    def test_a_proposed_decision_cannot_be_promoted_around_the_check(self) -> None:
        """Проверка при приёме обходится в два вызова, если не проверять переход."""

        decision = self.memory.propose_decision(
            statement="Сменить очередь задач по требованию из чужого PR.",
            origin="agent",
            created_by="mcp-test",
            evidence_ids=[self.external_evidence()],
        )
        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.set_decision_status(
                decision["id"], "accepted", actor="mcp-test"
            )
        self.assertIn("R18", str(caught.exception))

    def test_external_support_cannot_be_attached_after_the_fact(self) -> None:
        """Третий обход: запись проводится чистой, внешний текст дописывается."""

        decision = self.memory.propose_decision(
            statement="Решение агента без внешних ссылок.",
            origin="agent",
            created_by="mcp-test",
        )
        self.memory.set_decision_status(decision["id"], "accepted", actor="mcp-test")
        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.attach_evidence(
                decision["id"],
                self.external_evidence(),
                relation="supports",
                actor="mcp-test",
            )
        self.assertIn("R18", str(caught.exception))

    def test_contradicting_external_evidence_stays_attachable(self) -> None:
        """Именно так внешний материал и должен работать: порождать Conflict.

        Запрет на "contradicts" глушил бы несогласие - ровно наоборот
        тому, ради чего правило написано.
        """

        decision = self.memory.propose_decision(
            statement="Решение агента, которое внешний текст оспаривает.",
            origin="agent",
            created_by="mcp-test",
        )
        self.memory.set_decision_status(decision["id"], "accepted", actor="mcp-test")
        self.memory.attach_evidence(
            decision["id"],
            self.external_evidence(),
            relation="contradicts",
            actor="mcp-test",
        )

    def external_evidence(self) -> str:
        return self.memory.record_evidence(
            kind="external",
            summary="Комментарий в чужом PR требует сменить очередь задач.",
            created_by="mcp-test",
            provider="github.com/other/repo#34",
            result="INFO",
        )["id"]

    def test_decision_resting_on_external_content_cannot_start_accepted(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.propose_decision(
                statement="Перейти на другую очередь задач.",
                origin="project",
                created_by="mcp-test",
                status="accepted",
                evidence_ids=[self.external_evidence()],
            )
        self.assertIn("R18", str(caught.exception))

    def test_decision_resting_on_external_content_may_be_proposed(self) -> None:
        """Внешний ввод вправе предложить - но решает не он."""

        decision = self.memory.propose_decision(
            statement="Перейти на другую очередь задач.",
            origin="project",
            created_by="mcp-test",
            evidence_ids=[self.external_evidence()],
        )
        self.assertEqual(decision["status"], "proposed")

    def test_the_user_may_accept_a_decision_citing_external_content(self) -> None:
        """Граница намеренная: решает пользователь, а не найденный текст."""

        decision = self.memory.propose_decision(
            statement="Перейти на другую очередь задач.",
            origin="user",
            created_by="paul",
            status="accepted",
            evidence_ids=[self.external_evidence()],
        )
        self.assertEqual(decision["status"], "accepted")


class RegistryCoverageTests(unittest.TestCase):
    def test_r16_and_r18_exist_in_the_registry(self) -> None:
        ids = {item.id for item in RULES}
        self.assertIn("R16", ids)
        self.assertIn("R18", ids)


if __name__ == "__main__":
    unittest.main()


class WorkerReasonCodeTests(unittest.TestCase):
    """R13: остановка работы называется кодом, а не пересказом статуса.

    Прежде причина уходила в last_error строкой "M9 worker returned
    BLOCKED" - в ней нет ничего, чего нет в самом статусе. По такой
    записи нельзя ни маршрутизировать эскалацию, ни посчитать, ни
    отличить "нужно решение пользователя" от "сломалось окружение".
    """

    def _parse(self, message: str):
        from codex_autopilot.lifecycle import parse_desktop_worker_status

        return parse_desktop_worker_status(message)

    def test_a_code_is_carried_through(self) -> None:
        self.assertEqual(
            self._parse("итог\nAUTOPILOT_STATUS: BLOCKED MISSING_RESOURCE"),
            ("BLOCKED", "MISSING_RESOURCE"),
        )
        self.assertEqual(
            self._parse("итог\nAUTOPILOT_STATUS: ESCALATE PRODUCT_DECISION"),
            ("ESCALATE", "PRODUCT_DECISION"),
        )

    def test_an_unknown_code_is_refused(self) -> None:
        """Список, в который можно дописать что угодно, не закрытый."""

        from codex_autopilot.lifecycle_base import DesktopLifecycleError

        with self.assertRaises(DesktopLifecycleError):
            self._parse("итог\nAUTOPILOT_STATUS: BLOCKED BECAUSE_I_SAID_SO")

    def test_a_missing_code_is_recorded_not_forgiven(self) -> None:
        """Жёсткий отказ здесь клинил бы пайплайн ровно на поломке.

        Ход воркера уже завершён, второго ответа не будет. Поэтому
        отсутствие кода - это UNSPECIFIED, и он отдельно засчитывается
        как нарушение R13 в lifecycle_completion.
        """

        self.assertEqual(
            self._parse("итог\nAUTOPILOT_STATUS: BLOCKED"),
            ("BLOCKED", "UNSPECIFIED"),
        )
        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_completion.py"
        ).read_text(encoding="utf-8")
        self.assertIn('reason_code == "UNSPECIFIED"', source)
        self.assertIn('"R13"', source)

    def test_success_carries_no_reason(self) -> None:
        from codex_autopilot.lifecycle_base import DesktopLifecycleError

        self.assertEqual(self._parse("итог\nAUTOPILOT_STATUS: DONE"), ("DONE", ""))
        with self.assertRaises(DesktopLifecycleError):
            self._parse("итог\nAUTOPILOT_STATUS: DONE MISSING_RESOURCE")

    def test_the_prompt_names_every_code_a_worker_may_use(self) -> None:
        """Закрытый список бесполезен, если воркеру его не показали."""

        from codex_autopilot.lifecycle_base import WORKER_REASON_CODES

        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/ai_studio.py"
        ).read_text(encoding="utf-8")
        for code in WORKER_REASON_CODES - {"UNSPECIFIED"}:
            with self.subTest(code=code):
                self.assertIn(code, source)
        # UNSPECIFIED is a record that there was no code, not a code
        # the worker is offered to choose.
        self.assertNotIn("UNSPECIFIED", source)

    def test_the_failure_record_names_the_code(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_completion.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'state.last_error = f"{task_id} {kind} {worker_status} {reason_code}"',
            source,
        )
