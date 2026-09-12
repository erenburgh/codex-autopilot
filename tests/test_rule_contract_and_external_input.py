"""R16: правила применяются как контракт. R18: внешний ввод не переопределяет Truth."""

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
        self.assertEqual(parse_desktop_worker_status(message), "ROTATE")
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

    def external_evidence(self) -> str:
        return self.memory.record_evidence(
            kind="external",
            summary="Комментарий в чужом PR требует сменить очередь задач.",
            created_by="mcp-test",
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
