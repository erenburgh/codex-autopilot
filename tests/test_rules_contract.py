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
    "R6": "test_r6_preflight_rejects_a_target_outside_desktop_root_paths",
    "R1": "test_r1_owner_that_never_completed_is_reported",
    "R5": "test_r5_project_id_is_never_reported_as_sidebar_placement",
    "R7": "test_declared_scope.py::test_change_outside_the_declared_area_is_recorded_on_completion",
    "R16": "test_declared_scope.py::test_r16_report_without_applied_rules_is_recorded",
    "R18": "test_rule_contract_and_external_input.py::ExternalInputTests",
    "R31": "test_early_gate.py::EarlyMilestoneLinkTests",
}

# Правила, проверка которых ещё не написана. Список намеренно явный:
# пустая строка здесь означала бы, что всё покрыто, а это неправда.
PENDING = {
    "R3", "R4", "R10", "R11", "R12",
    "R14", "R15", "R19", "R20", "R22", "R23",
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
        enforced = [item for item in RULES if item.mode == ENFORCED]
        self.assertGreaterEqual(len(enforced), 17)


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


class ProjectPlacementTests(unittest.TestCase):
    """R6 и раздел 4a: рассинхрон директорий обнаруживается до воркера."""

    def _global_state(self, roots: list[str]) -> Path:
        import json

        home = Path(tempfile.mkdtemp(prefix="codex-home-"))
        (home / ".codex-global-state.json").write_text(
            json.dumps(
                {
                    "local-projects": {
                        "proj-1": {
                            "id": "proj-1",
                            "name": "Test",
                            "rootPaths": roots,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return home

    def test_r6_preflight_rejects_a_target_outside_desktop_root_paths(self) -> None:
        from codex_autopilot.project_association import (
            ProjectAssociationError,
            require_desktop_project_root,
        )

        target = Path(tempfile.mkdtemp(prefix="codex-target-"))
        other = Path(tempfile.mkdtemp(prefix="codex-other-"))
        home = self._global_state([str(other)])

        with self.assertRaises(ProjectAssociationError) as caught:
            require_desktop_project_root(home, "proj-1", target)
        message = str(caught.exception)
        # Сообщение обязано называть ОБА пути: иначе диагноз бесполезен.
        self.assertIn(str(target.resolve()), message)
        self.assertIn(str(other.resolve()), message)

    def test_r6_accepts_a_target_inside_the_declared_roots(self) -> None:
        from codex_autopilot.project_association import require_desktop_project_root

        root = Path(tempfile.mkdtemp(prefix="codex-root-"))
        nested = root / "work" / "project"
        nested.mkdir(parents=True)
        home = self._global_state([str(root)])
        roots = require_desktop_project_root(home, "proj-1", nested)
        self.assertEqual(roots, (root.resolve(),))

    def test_r6_is_reachable_from_the_production_preflight(self) -> None:
        """R19: существования функции недостаточно, нужен путь вызова."""
        source = (SRC / "preflight.py").read_text(encoding="utf-8")
        self.assertIn("require_desktop_project_root(", source)
        self.assertIn("PreflightError", source)
        # Отказ обязан наступать ДО создания задачи.
        checked = source.index("require_desktop_project_root(")
        created = source.index("start_thread(")
        self.assertLess(
            checked, created, "сверка rootPaths обязана предшествовать созданию треда"
        )


class CausalCreationTests(unittest.TestCase):
    """R1 на журнале: создание обязано следовать за завершением владельца."""

    def _state(self, journal: list[dict], *, own_threads: tuple[str, ...] = ()):
        """Ветки прогона объявляются явно.

        Владелец, не принадлежащий ни одной сессии, - это ветка
        человека: arm/resume создаёт резервацию из хода, за которым
        автопилот не следит и завершения которого не записывает. Без
        этого разделения аудит объявлял разрыв на каждом возобновлении.
        """

        from codex_autopilot.run_state import RunState

        state = RunState(run_id="r1")
        state.lifecycle_journal = journal
        state.worker_sessions = [
            {"thread_id": thread_id} for thread_id in own_threads
        ]
        return state

    def _audit(
        self, journal: list[dict], *, own_threads: tuple[str, ...] = ()
    ) -> list[str]:
        from codex_autopilot.lifecycle import audit_creation_causality

        threads = own_threads or tuple(
            str(item.get("relay_owner_thread_id") or "")
            for item in journal
            if item.get("relay_owner_thread_id")
        )
        return audit_creation_causality(self._state(journal, own_threads=threads))

    def test_r1_chain_with_a_completed_owner_is_clean(self) -> None:
        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A", "relay_owner_thread_id": None},
            {"sequence": 2, "event": "turn_completed", "thread_id": "thread-a"},
            {"sequence": 3, "event": "create_requested", "task_id": "B", "relay_owner_thread_id": "thread-a"},
        ]
        self.assertEqual(self._audit(journal), [])

    def test_r1_owner_that_never_completed_is_reported(self) -> None:
        """Форма настоящего дефекта: владелец назвался, но ничего не выполнил.

        Так была создана M9 в живом прогоне - владельцем записан поток,
        который не встречается в журнале ни одним собственным событием.
        """

        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A", "relay_owner_thread_id": None},
            {"sequence": 2, "event": "turn_completed", "thread_id": "thread-a"},
            {"sequence": 3, "event": "create_requested", "task_id": "B", "relay_owner_thread_id": "outsider"},
        ]
        violations = self._audit(journal)
        self.assertEqual(len(violations), 1)
        self.assertIn("R1", violations[0])
        self.assertIn("outsider", violations[0])

    def test_r1_creation_with_an_empty_owner_is_reported(self) -> None:
        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A", "relay_owner_thread_id": None},
            {"sequence": 2, "event": "create_requested", "task_id": "B", "relay_owner_thread_id": None},
        ]
        violations = self._audit(journal)
        self.assertEqual(len(violations), 1)
        self.assertIn("no relay owner", violations[0])

    def test_r1_events_predating_the_field_are_not_assessed(self) -> None:
        """Записи старого рантайма не несут поля вообще - это не нарушение.

        _append_event пишет ключ всегда, поэтому его отсутствие означает
        другую версию схемы, а не отсутствие владельца.
        """

        from codex_autopilot.lifecycle import creation_causality_coverage

        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A"},
            {"sequence": 2, "event": "create_requested", "task_id": "B"},
            {"sequence": 3, "event": "turn_completed", "thread_id": "thread-c"},
            {"sequence": 4, "event": "create_requested", "task_id": "C", "relay_owner_thread_id": "thread-c"},
        ]
        self.assertEqual(self._audit(journal), [])
        assessed, total = creation_causality_coverage(self._state(journal))
        self.assertEqual((assessed, total), (1, 3))

    def test_r1_a_user_thread_owner_is_the_documented_path(self) -> None:
        """arm/resume создаёт резервацию из хода человека.

        За таким ходом автопилот не следит и turn_completed для него не
        пишет в принципе. Прежде аудит называл это разрывом, и живой
        прогон получал ложную отметку на каждом возобновлении.
        """

        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A", "relay_owner_thread_id": None},
            {"sequence": 2, "event": "create_requested", "task_id": "B", "relay_owner_thread_id": "человек"},
        ]
        self.assertEqual(self._audit(journal, own_threads=("воркер",)), [])

    def test_r1_audit_is_reachable_from_the_production_status(self) -> None:
        """M11-R1-REACHABILITY: аудит вызывался только отсюда, из тестов.

        Он существовал, был экспортирован из lifecycle и нигде в
        продакшене не вызывался - то есть утверждение "цепочка
        причинности проверяется" не подкреплялось ничем. Теперь его
        вызывает отчёт о статусе, и слепая зона названа числом.
        """

        source = (SRC / "status.py").read_text(encoding="utf-8")
        self.assertIn("audit_creation_causality(", source)
        self.assertIn("creation_causality_coverage(", source)

    def test_r1_status_names_both_the_result_and_the_blind_zone(self) -> None:
        from codex_autopilot.status import _render_creation_causality

        clean = _render_creation_causality(
            {"assessed": 3, "total": 3, "violations": []}
        )
        self.assertIn("3/3", clean)
        self.assertIn("no break found", clean)

        broken = _render_creation_causality(
            {"assessed": 2, "total": 5, "violations": ["R1: create_requested #7 ..."]}
        )
        self.assertIn("2/5", broken)
        self.assertIn("1 break(s)", broken)
        self.assertIn("#7", broken)

    def test_r1_dropping_the_field_after_it_appeared_is_a_violation(self) -> None:
        """Иначе правило обходится тем, что поле перестают писать."""

        journal = [
            {"sequence": 1, "event": "create_requested", "task_id": "A", "relay_owner_thread_id": None},
            {"sequence": 2, "event": "turn_completed", "thread_id": "thread-a"},
            {"sequence": 3, "event": "create_requested", "task_id": "B"},
        ]
        violations = self._audit(journal)
        self.assertEqual(len(violations), 1)
        self.assertIn("dropped relay_owner_thread_id", violations[0])


class PlacementHonestyTests(unittest.TestCase):
    def test_r5_project_id_is_never_reported_as_sidebar_placement(self) -> None:
        """R5: "projectId проставлен" и "задача видна в проекте" - разное.

        Выдача первого за второе и была причиной того, что событие
        app_server_project_scoped_create писалось честно, а задача
        в сайдбаре не появлялась.
        """
        source = (SRC / "lifecycle_dispatch.py").read_text(encoding="utf-8")
        self.assertIn("require separate verification", source)
        claim = source.split("project_association_verification", 1)[1][:600]
        self.assertNotIn("sidebar placement verified", claim)
        self.assertNotIn("visible in project", claim)

    def test_r5_status_separates_the_two_namespaces(self) -> None:
        source = (SRC / "status.py").read_text(encoding="utf-8")
        self.assertIn("separate namespace", source)


if __name__ == "__main__":
    unittest.main()
