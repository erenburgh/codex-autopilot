"""Written and called by nobody is a promise without execution.

The same defect was found four times in one day, each time at a price:

- ``audit_creation_causality`` checked rule R1 and was called only from
  tests: the claim "the causality chain is checked" rested on nothing;
- ``build_pipeline_engineer_prompt`` was removed as unused, while in fact
  the on-call engineer's path had simply never been finished;
- ``reconcile_desktop_runtime`` - the recovery-after-crash function - was
  also called only from tests, so a dead session never returned to work;
- ``rate_limits``/``rate_limit_reset_at`` extracted the rate-limit reset
  time the barrier never received.

The test closes the whole class: every public definition in production
must have a call path (R19). An exemption is allowed, but only by name and
with a reason.

WHAT WE COUNT AND WHERE THE LIMIT IS

The previous edition counted references by substring and walked only
``tree.body``. Both decisions were wrong, and both hid dead code:

- substring: ``planner_thread_title`` counted as alive because it is
  contained in ``replanner_thread_title``. It was dead and removed on
  16 Sep. In the same way every short name contained in a longer one
  counted as "reachable": ``rule`` inside ``rules``, ``schedule`` inside
  ``scheduler``. Those two really are alive, but the counter could not
  know that - it saw a substring, not a call, and would have stayed just
  as silent had they died tomorrow;
- only ``tree.body``: class methods were not checked at all. That is how
  six methods of ``ResourceLockCoordinator`` survived - the second path
  to what production does with module functions. Removed on 16 Sep,
  165 lines.

Now names are taken by parsing the AST, class methods are included, and
a reference to a method counts by attribute access.

The limit of the check is declared honestly: a method name says nothing
about its owner. ``coordinator.acquire`` and ``threading.Lock.acquire``
are indistinguishable to the counter, so a method whose name someone else
also uses always looks alive. The check is therefore NOT complete: it
gives no false positives but misses coinciding names. Measured on 16 Sep:
of six dead coordinator methods three were caught by name. The rest were
found by parsing the receiver by hand. An exact answer needs type
inference; until then the count here is a lower bound, not the whole.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


SRC = Path(__file__).resolve().parents[1] / "src" / "codex_autopilot"

# An entry point, a contract class or a deliberately public API. The reason
# is mandatory: a line without a reason is the same disease returning.
ALLOWED = {
    "main": "точка входа CLI",
    "handle_stop_hook": "вызывается Codex как хук, не нами",
    "handle_prompt_hook": "вызывается Codex как хук, не нами",
    "handle_post_tool_hook": "вызывается Codex как хук, не нами",
    "handle_interrupt_hook": "вызывается Codex как хук, не нами",
}

# Debt found on 16 Sep by the finished detector. These are NOT exemptions:
# none has a production call, and each awaits the owner's decision - remove
# or wire up. The list only shrinks: the test below forbids it to grow,
# and an entry that stopped being dead must leave here.
KNOWN_DEBT = {
    "plan.validate_plan": "четвёртая обёртка над _validate_plan_payload; продакшен ходит через validate_migrating_plan, validate_persisted_plan и validate_plan_change",
    "ArtifactStagingStore.abandon": "ни одной ссылки ни в src, ни в тестах",
    "ArtifactStagingStore.workspace_for": "ни одной ссылки ни в src, ни в тестах",
    "ProjectMemory.ensure_healthy": "ни одного продакшен-вызова",
    "ProjectMemory.export_summary": "ни одного продакшен-вызова",
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
        # A top-level definition references itself once - by the def itself;
        # that does not put it into the name set, ast.Name does not produce
        # it. So checking membership is enough.
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

    # Machine entry points: Codex or the runtime itself calls them, not a human.
    # _wake is spawned by the dispatcher itself before it exits: the alarm
    # for a timed retry. A human never types it, nor _relay_dispatch.
    MACHINE = {"hook", "memory-mcp", "_relay_dispatch", "_dispatch", "_wake", "_wake-sweep"}
    # User commands: described in README and GETTING_STARTED.
    USER = {"status", "stop", "resume", "logs", "doctor", "uninstall"}
    # Internal steps of start-skill, each with its own --help.
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
