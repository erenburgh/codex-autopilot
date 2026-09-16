"""Написанное и никем не вызванное - это обещание без исполнения.

Один и тот же дефект за день нашёлся четырежды, и каждый раз дорого:

- ``audit_creation_causality`` проверял правило R1 и вызывался только из
  тестов: утверждение «цепочка причинности проверяется» не подкреплялось
  ничем;
- ``build_pipeline_engineer_prompt`` был снят как неиспользуемый, а на
  деле дорожку дежурного инженера просто не дописали;
- ``reconcile_desktop_runtime`` - функция восстановления после падения -
  тоже вызывалась только из тестов, поэтому мёртвая сессия не
  возвращалась в работу никогда;
- ``rate_limits``/``rate_limit_reset_at`` добывали время сброса лимита,
  которого барьер никогда не получал.

Тест закрывает класс целиком: у каждого публичного определения в
продакшене обязан быть путь вызова (R19). Исключение допустимо, но
только именное и с причиной.

ЧЕМ СЧИТАЕМ И ГДЕ ПРЕДЕЛ

Прежняя редакция считала ссылки подстрокой и обходила только
``tree.body``. Оба решения были неверны, и оба скрывали мёртвое:

- подстрока: ``planner_thread_title`` числился живым, потому что входит
  в ``replanner_thread_title``. Он был мёртв и снят 16.09;
- только ``tree.body``: методы классов не проверялись вовсе. Так
  прожили шесть методов ``ResourceLockCoordinator`` - второй путь к
  тому, что продакшен делает функциями модуля. Сняты 16.09, 165 строк.

Теперь имена берутся разбором AST, методы классов включены, а ссылка на
метод засчитывается по обращению к атрибуту.

Предел проверки объявлен честно: имя метода не говорит о владельце.
``coordinator.acquire`` и ``threading.Lock.acquire`` для счётчика
неразличимы, поэтому метод, чьё имя занято ещё кем-то, живым выглядит
всегда. Проверка поэтому НЕ полна: она не даёт ложных срабатываний, но
пропускает совпадающие имена. Замерено 16.09: из шести мёртвых методов
координатора по имени ловились три. Оставшиеся нашлись разбором
получателя вручную. Точный ответ требует вывода типов; до него счёт
здесь - нижняя оценка, а не полная.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


SRC = Path(__file__).resolve().parents[1] / "src" / "codex_autopilot"

# Точка входа, класс-контракт или сознательно публичный API. Причина
# обязательна: строка без причины - это возвращение той же болезни.
ALLOWED = {
    "main": "точка входа CLI",
    "handle_stop_hook": "вызывается Codex как хук, не нами",
    "handle_prompt_hook": "вызывается Codex как хук, не нами",
    "handle_post_tool_hook": "вызывается Codex как хук, не нами",
    "handle_interrupt_hook": "вызывается Codex как хук, не нами",
}

# Долг, найденный 16.09 доведённым детектором. Это НЕ исключения: у
# каждого нет продакшен-вызова, и каждый ждёт решения владелицы - снять
# или подключить. Список только сокращается: расти ему запрещает тест
# ниже, а запись, которая перестала быть мёртвой, обязана уйти отсюда.
KNOWN_DEBT = {
    "plan.validate_plan": "четвёртая обёртка над _validate_plan_payload; продакшен ходит через validate_migrating_plan, validate_persisted_plan и validate_plan_change",
    "ArtifactStagingStore.abandon": "ни одной ссылки ни в src, ни в тестах",
    "ArtifactStagingStore.workspace_for": "ни одной ссылки ни в src, ни в тестах",
    "ProjectMemory.ensure_healthy": "ни одного продакшен-вызова",
    "ProjectMemory.export_summary": "ни одного продакшен-вызова",
    "PipelineIncidentStore.signature_ledger": "читатель реестра подписей; подключается вместе с A5/R23 - решение владелицы от 16.09",
    "PreflightResult.lines": "ни одного продакшен-вызова",
    "EvidenceTrust.to_storage": "ни одной ссылки ни в src, ни в тестах",
}


def _trees() -> dict[str, ast.Module]:
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(SRC.glob("*.py"))
    }


def _public_definitions(trees: dict[str, ast.Module]) -> dict[str, tuple[str, str]]:
    """Публичные определения продакшена: имя для отчёта -> (модуль, имя для счёта).

    Функция верхнего уровня отчитывается как ``модуль.имя``, метод - как
    ``Класс.имя``: по нему его и ищут глазами.
    """

    found: dict[str, tuple[str, str]] = {}
    for module, tree in trees.items():
        stem = module.removesuffix(".py")
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    found[f"{stem}.{node.name}"] = (module, node.name)
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not sub.name.startswith("_"):
                            found[f"{node.name}.{sub.name}"] = (module, sub.name)
    return found


def _references(trees: dict[str, ast.Module]) -> tuple[set[str], set[str]]:
    """Имена и атрибуты, употреблённые где-либо в продакшене.

    Строковые константы попадают в оба набора: доступ через getattr и
    диспетчеризация по имени - тоже вызов, и молчать о них нельзя.
    """

    names: set[str] = set()
    attributes: set[str] = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.alias):
                names.add(node.name.split(".")[-1])
                if node.asname:
                    names.add(node.asname)
            elif isinstance(node, ast.Attribute):
                attributes.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
                attributes.add(node.value)
    return names, attributes


def _orphans() -> dict[str, str]:
    """Публичные определения без единой ссылки в продакшене."""

    trees = _trees()
    names, attributes = _references(trees)
    result: dict[str, str] = {}
    for reported, (module, short) in _public_definitions(trees).items():
        if short in ALLOWED:
            continue
        is_method = "." in reported and not reported.startswith(module.removesuffix(".py") + ".")
        used = attributes if is_method else names
        # Определение верхнего уровня ссылается на себя один раз - самим
        # def; в набор имён оно от этого не попадает, ast.Name его не
        # порождает. Поэтому достаточно проверить вхождение.
        if short not in used:
            result[reported] = module
    return result


class NoUnreachableContractTests(unittest.TestCase):
    def test_every_public_definition_has_a_production_caller(self) -> None:
        orphans = _orphans()
        surprises = sorted(set(orphans) - set(KNOWN_DEBT))
        self.assertEqual(
            surprises,
            [],
            "написано и никем не вызывается — либо подключить, либо снять, "
            "либо внести в ALLOWED с причиной: " + ", ".join(surprises),
        )

    def test_the_debt_list_only_shrinks(self) -> None:
        """Запись, переставшая быть мёртвой, обязана уйти из долга.

        Иначе список превращается в свалку, которая once-and-for-all
        глушит проверку: ровно так подстрочный счёт и прятал пятерых.
        """

        orphans = _orphans()
        healed = sorted(set(KNOWN_DEBT) - set(orphans))
        self.assertEqual(
            healed,
            [],
            "больше не мёртвое — убрать из KNOWN_DEBT: " + ", ".join(healed),
        )

    def test_every_exemption_carries_a_reason(self) -> None:
        """Список исключений - контракт. Пустая причина его обнуляет."""

        for name, reason in {**ALLOWED, **KNOWN_DEBT}.items():
            with self.subTest(name=name):
                self.assertTrue(reason.strip(), name)

    def test_the_exemption_list_has_no_stale_entries(self) -> None:
        """Исключение для того, чего уже нет, прячет следующую дыру."""

        defined = {short for _module, short in _public_definitions(_trees()).values()}
        for name in ALLOWED:
            with self.subTest(name=name):
                self.assertIn(name, defined)

    def test_the_counter_sees_methods_and_does_not_count_substrings(self) -> None:
        """Обе прежние болезни сразу, на живом дереве.

        ``replanner_thread_title`` содержит в себе ``planner_thread_title``;
        подстрочный счёт объявлял второй живым. И методы классов должны
        попадать в разбор - иначе шесть методов координатора снова
        проживут незамеченными.
        """

        trees = _trees()
        definitions = _public_definitions(trees)
        self.assertIn("thread_titles.replanner_thread_title", definitions)
        self.assertNotIn("thread_titles.planner_thread_title", definitions)
        self.assertIn(
            "ResourceLockCoordinator.transaction",
            definitions,
            "методы классов не попали в разбор",
        )


if __name__ == "__main__":
    unittest.main()


class EveryCommandHasAConsumerTests(unittest.TestCase):
    """Шестой пункт аудита 0.8.0: команды CLI, которые никому не нужны.

    Команда без названного потребителя - это либо инструмент, о котором
    никто не знает, либо остаток снятого пути. Оба случая одинаково
    вредны: первый не используют, второй продолжают поддерживать.
    """

    ROOT = Path(__file__).resolve().parents[1]

    # Машинные входы: их зовёт Codex или сам рантайм, а не человек.
    MACHINE = {"hook", "memory-mcp", "_relay_dispatch", "_dispatch"}
    # Пользовательские команды: описаны в README и GETTING_STARTED.
    USER = {"status", "stop", "resume", "logs", "doctor", "uninstall"}
    # Внутренние шаги start-skill, у каждой своя справка в --help.
    SETUP = {"bootstrap", "preflight", "start-skill", "arm"}

    def _commands(self) -> set[str]:
        import argparse
        import codex_autopilot.cli as cli

        for action in cli.parser()._actions:
            if isinstance(action, argparse._SubParsersAction):
                return set(action.choices)
        self.fail("подкоманды не найдены")

    def test_every_command_is_named_somewhere_a_reader_can_find(self) -> None:
        documented = " ".join(
            path.read_text(encoding="utf-8")
            for path in (
                self.ROOT / "README.md",
                self.ROOT / "GETTING_STARTED.md",
                self.ROOT / "src/codex_autopilot/ai_studio.py",
                *(self.ROOT / "plugins").rglob("SKILL.md"),
            )
        )
        orphans = []
        for command in sorted(self._commands()):
            if command in self.MACHINE | self.USER | self.SETUP:
                continue
            if f"codex-autopilot {command}" not in documented:
                orphans.append(command)
        self.assertEqual(
            orphans,
            [],
            "команда есть, а потребителя нет — описать там, где её вызывают, "
            "или снять: " + ", ".join(orphans),
        )
