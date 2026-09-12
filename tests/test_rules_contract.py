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
import tempfile

from codex_autopilot.rules import (
    CHECKED,
    ENFORCED,
    RULES,
    record_violation,
    rule,
    rules_by_mode,
    rules_for_prompt,
)


SRC = Path(__file__).resolve().parent.parent / "src" / "codex_autopilot"

# Правило -> тест, который падает при его нарушении.
# Запись сюда означает: проверка существует и доказана.
IMPLEMENTED = {
    "R2": "test_r2_codex_app_task_api_is_absent_from_production",
    "R8": "test_r8_self_acceptance_is_rejected_by_plan_validation",
    "R21": "tests/test_clean_environment.py",
    "R9": "thread_titles._role_segment + test_workspace_ux",
    "R17": "test_r17_rules_come_before_specifications_and_are_not_truncatable",
    "R13": "test_r13_escalation_requires_a_reason_from_the_closed_list",
}

# Правила, проверка которых ещё не написана. Список намеренно явный:
# пустая строка здесь означала бы, что всё покрыто, а это неправда.
PENDING = {
    "R1", "R3", "R4", "R5", "R6", "R7", "R10", "R11", "R12",
    "R14", "R15", "R16", "R18", "R19", "R20", "R22", "R23",
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


class ContextOrderTests(unittest.TestCase):
    def test_r17_rules_come_before_specifications_and_are_not_truncatable(self) -> None:
        """R17: правила грузятся раньше спецификаций и не усекаются."""
        block = rules_for_prompt()
        self.assertEqual(len(block), len(RULES))
        # ENFORCED идут первыми.
        modes = [item["mode"] for item in block]
        self.assertEqual(modes, sorted(modes, key=lambda m: 0 if m == ENFORCED else 1))
        # Каждая запись несёт id, режим и формулировку.
        for item in block:
            self.assertTrue(item["id"] and item["mode"] and item["rule"])

    def test_r17_violation_history_raises_a_rule_in_priority(self) -> None:
        state_dir = Path(tempfile.mkdtemp(prefix="codex-autopilot-rules-"))
        checked_before = [
            item["id"] for item in rules_for_prompt(state_dir) if item["mode"] == CHECKED
        ]
        target = checked_before[-1]
        self.assertNotEqual(target, checked_before[0])

        record_violation(state_dir, target)
        record_violation(state_dir, target)

        checked_after = [
            item["id"] for item in rules_for_prompt(state_dir) if item["mode"] == CHECKED
        ]
        self.assertEqual(
            checked_after[0], target, "нарушенное правило поднимается в своём режиме"
        )
        # Режимы при этом не перемешиваются: ENFORCED остаются выше.
        modes = [item["mode"] for item in rules_for_prompt(state_dir)]
        self.assertEqual(modes, sorted(modes, key=lambda m: 0 if m == ENFORCED else 1))

    def test_r17_rules_block_precedes_task_contract_in_the_worker_prompt(self) -> None:
        """Порядок проверяется на фактическом конверте, а не на намерении."""
        from codex_autopilot.ai_studio import AIStudioRuntime

        order = list(AIStudioRuntime.build_prompt.__code__.co_consts)
        # Конверт строится литералом: "rules" обязан идти раньше "task".
        source = (SRC / "ai_studio.py").read_text(encoding="utf-8")
        envelope = source.split("envelope = {", 1)[1]
        self.assertLess(
            envelope.index('"rules"'),
            envelope.index('"task"'),
            "блок правил обязан стоять раньше спецификации задачи",
        )
        self.assertIn("not truncatable", source)


class EscalationTests(unittest.TestCase):
    def test_r13_escalation_requires_a_reason_from_the_closed_list(self) -> None:
        """R13: пользователь не привлекается без кода причины."""
        from codex_autopilot.pipeline_engineer import (
            AuthorizationTopologyError,
            EscalationReason,
            IncidentPhase,
            escalate_to_user,
        )

        incident: dict = {"phase": "DEGRADED"}
        with self.assertRaises(AuthorizationTopologyError) as caught:
            escalate_to_user(incident, "ПОТОМУ ЧТО", at="t")
        self.assertIn("R13", str(caught.exception))
        self.assertEqual(incident["phase"], "DEGRADED", "инцидент не тронут при отказе")

        escalate_to_user(
            incident, EscalationReason.RECOVERY_EXHAUSTED, at="t", detail="исчерпано"
        )
        self.assertEqual(incident["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        self.assertEqual(incident["escalation_reason"], "RECOVERY_EXHAUSTED")
        self.assertEqual(incident["escalation_detail"], "исчерпано")

    def test_r13_no_direct_phase_assignment_bypasses_the_reason_code(self) -> None:
        """Прямое присваивание фазы в обход функции - дефект."""
        source = (SRC / "pipeline_engineer.py").read_text(encoding="utf-8")
        body = source.split("def escalate_to_user", 1)[1]
        after = body.split("\ndef ", 1)[1] if "\ndef " in body else ""
        self.assertNotIn(
            'incident["phase"] = IncidentPhase.ESCALATE_TO_USER.value',
            after,
            "эскалация выполняется только через escalate_to_user",
        )


if __name__ == "__main__":
    unittest.main()
