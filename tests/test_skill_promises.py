"""Скилл не вправе обещать того, чего рантайм не делает.

Два обещания стоили дорого. Первое: раздел отчёта велел держать ход
открытым и стримить лестницу, пока не стартует задача, - а диспетчер ждёт
именно завершения этого хода, и запуск не наступал никогда. Второе: после
починки там же осталось "the [✓] lines the user already sees come from
there", хотя хук перешёл на continue и его сообщение не показывается.

Третье обещание - дорожка девопса: семь абзацев про то, как DevOps чинит
и перезаводит, при полном отсутствии кода, который создаёт инженера.
"""

from __future__ import annotations

from pathlib import Path
import unittest

SKILL = (
    Path(__file__).resolve().parents[1]
    / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md"
)


class LaunchReportPromiseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL.read_text(encoding="utf-8")

    def test_the_skill_does_not_claim_the_launch_report_is_visible(self) -> None:
        self.assertNotIn("the user already sees", self.text)
        self.assertIn("not visible", self.text)

    def test_the_skill_names_the_way_to_look(self) -> None:
        """Невидимый отчёт допустим, молчание про него - нет."""

        self.assertIn("статус", self.text)

    def test_the_skill_forbids_polling_inside_the_initiating_turn(self) -> None:
        self.assertIn("never run it inside the initiating turn", self.text)


class PipelineEngineerPromiseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL.read_text(encoding="utf-8")

    def test_the_skill_says_no_engineer_worker_is_created(self) -> None:
        self.assertIn("Nothing creates an engineer worker", self.text)

    def test_the_procedure_names_real_commands(self) -> None:
        for command in ("relay-status", "relay-complete", "relay-fail",
                        "devops-rearm-relay-owner"):
            with self.subTest(command=command):
                self.assertIn(command, self.text)

    def test_the_procedure_keeps_ambiguous_outcomes_stopped(self) -> None:
        """Неизвестный побочный эффект - единственный случай, когда стоять
        правильно. Замена задачи здесь раздвоила бы работу."""

        self.assertIn("AMBIGUOUS", self.text)
        self.assertIn("Never create a", self.text)


class CommandsInTheSkillExistTests(unittest.TestCase):
    """Процедура бесполезна, если называет команду, которой нет."""

    def test_every_named_helper_command_is_a_real_subcommand(self) -> None:
        import re

        from codex_autopilot.cli import parser

        available = set()
        for action in parser()._subparsers._group_actions:
            available.update(action.choices)
        named = set(re.findall(r"scripts/codex-autopilot (\S+)", SKILL.read_text(encoding="utf-8")))
        named |= {
            match
            for match in re.findall(r"`(relay-\w+|devops-[\w-]+)", SKILL.read_text(encoding="utf-8"))
        }
        missing = sorted(named - available)
        self.assertEqual(missing, [], f"скилл называет несуществующие команды: {missing}")


if __name__ == "__main__":
    unittest.main()
