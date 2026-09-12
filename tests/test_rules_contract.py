"""Правила как исполняемый контракт.

Файл правил сам по себе ничего не удерживает. Здесь проверяется,
что заявленный режим контроля соответствует действительности,
и что нереализованные правила видны, а не молчат.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

from codex_autopilot.plan import validate_plan
from codex_autopilot.rules import CHECKED, ENFORCED, RULES, rule, rules_by_mode


SRC = Path(__file__).resolve().parent.parent / "src" / "codex_autopilot"

# Правило -> тест, который падает при его нарушении.
# Запись сюда означает: проверка существует и доказана.
IMPLEMENTED = {
    "R2": "test_r2_codex_app_task_api_is_absent_from_production",
    "R8": "test_r8_self_acceptance_is_rejected_by_plan_validation",
    "R21": "tests/test_clean_environment.py",
    "R9": "thread_titles._role_segment + test_workspace_ux",
}

# Правила, проверка которых ещё не написана. Список намеренно явный:
# пустая строка здесь означала бы, что всё покрыто, а это неправда.
PENDING = {
    "R1", "R3", "R4", "R5", "R6", "R7", "R10", "R11", "R12", "R13",
    "R14", "R15", "R16", "R17", "R18", "R19", "R20", "R22", "R23",
    "R24", "R25", "R26", "R27", "R28", "R29", "R30",
}


class RuleRegistryTests(unittest.TestCase):
    def test_every_rule_declares_a_mode_and_a_check(self) -> None:
        for item in RULES:
            with self.subTest(rule=item.id):
                self.assertIn(item.mode, {ENFORCED, CHECKED})
                self.assertTrue(item.statement.strip(), "формулировка пуста")
                self.assertTrue(item.check.strip(), "спецификация проверки пуста")

    def test_rule_ids_are_unique_and_contiguous(self) -> None:
        ids = [item.id for item in RULES]
        self.assertEqual(len(ids), len(set(ids)))
        numbers = sorted(int(item[1:]) for item in ids)
        self.assertEqual(numbers, list(range(1, len(ids) + 1)))

    def test_implemented_and_pending_together_cover_every_rule(self) -> None:
        covered = set(IMPLEMENTED) | PENDING
        self.assertEqual(
            covered,
            {item.id for item in RULES},
            "каждое правило либо реализовано, либо явно числится нереализованным",
        )
        self.assertFalse(
            set(IMPLEMENTED) & PENDING,
            "правило не может быть одновременно реализованным и ожидающим",
        )

    def test_no_rule_is_silently_downgraded(self) -> None:
        """Понижение режима запрещено: ENFORCED не становится CHECKED."""
        self.assertEqual(rule("R2").mode, ENFORCED)
        self.assertEqual(rule("R8").mode, ENFORCED)
        self.assertEqual(rule("R29").mode, ENFORCED)
        self.assertEqual(rule("R30").mode, ENFORCED)
        self.assertGreaterEqual(len(rules_by_mode(ENFORCED)), 17)


class EnforcedRuleTests(unittest.TestCase):
    def test_r2_codex_app_task_api_is_absent_from_production(self) -> None:
        """R2: задачи создаёт только диспетчер через App Server."""
        forbidden = ("create_thread", "send_message_to_thread", "fork_thread", "handoff_thread")
        offenders: list[str] = []
        for path in sorted(SRC.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for name in forbidden:
                # Разрешён только App Server thread/start; ищем именно
                # вызовы инструментов Codex App.
                for match in re.finditer(rf"\b(codex_app[^\n]*\b{name}|{name}\s*\()", text):
                    line = text[: match.start()].count("\n") + 1
                    if name == "create_thread" and "thread/start" in text.splitlines()[line - 1]:
                        continue
                    offenders.append(f"{path.name}:{line}:{name}")
        self.assertEqual(
            offenders,
            [],
            "Codex App task API запрещён правилом R2: создание идёт только "
            "через детерминированный диспетчер и App Server thread/start",
        )

    def test_r8_self_acceptance_is_rejected_by_plan_validation(self) -> None:
        """R8: каноническая задача не принимает сама себя."""
        data = {
            "schema_version": 3,
            "goal": "g",
            "user_request": "u",
            "model_strategy": "auto",
            "execution_strategy": "serial",
            "max_parallel_workers": 1,
            "computer_use_slots": 1,
            "roles": [
                {
                    "id": "builder",
                    "name": "Builder",
                    "responsibilities": ["build"],
                }
            ],
            "tasks": [
                {
                    "id": "A",
                    "title": "Task A",
                    "objective": "o",
                    "definition_of_done": ["d"],
                    "role": "builder",
                    "execution_mode": "code",
                    "execution_mode_reason": "files suffice",
                    "reasoning": "medium",
                    "verification": {"policy": "self", "required": True},
                }
            ],
        }
        with self.assertRaises(ValueError) as caught:
            validate_plan(data, "adaptive")
        self.assertIn("R8", str(caught.exception))

    def test_r8_exempts_a_migrated_v08_plan(self) -> None:
        """Мигрированный v0.8 план предшествует верификации и остаётся serial."""
        legacy = {
            "schema_version": 2,
            "goal": "g",
            "model_strategy": "auto",
            "milestones": [
                {
                    "title": "t",
                    "objective": "o",
                    "definition_of_done": ["d"],
                    "execution_mode": "code",
                    "execution_mode_reason": "files suffice",
                    "reasoning": "medium",
                }
            ],
        }
        plan = validate_plan(legacy, "adaptive")
        self.assertTrue(plan.legacy_serial)
        self.assertEqual(plan.tasks[0].verification.policy, "self")


if __name__ == "__main__":
    unittest.main()
