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

    def test_the_skill_says_the_engineer_is_created_as_a_worker(self) -> None:
        """Прежде скилл честно говорил, что воркера не создаёт никто.

        Теперь создаёт - и обещание снова должно совпадать с рантаймом,
        только в другую сторону.
        """

        self.assertIn("creates it as a visible worker task", self.text)
        self.assertNotIn("Nothing creates an engineer worker", self.text)

    def test_the_skill_states_the_engineer_repair_authority(self) -> None:
        """R13: пользователь не участвует в выборе способа фикса."""

        self.assertIn("full authority to repair", self.text)
        self.assertIn("The user does not choose the repair", self.text)

    def test_the_skill_requires_a_code_for_escalation(self) -> None:
        self.assertIn("RECOVERY_EXHAUSTED", self.text)
        self.assertIn("A bare escalation is refused", self.text)

    def test_the_procedure_names_real_commands(self) -> None:
        for command in ("relay-status", "relay-complete", "relay-fail",
                        "devops-rearm-relay-owner"):
            with self.subTest(command=command):
                self.assertIn(command, self.text)

    def test_the_procedure_keeps_ambiguous_outcomes_stopped(self) -> None:
        """Неизвестный побочный эффект - единственный случай, когда стоять
        правильно. Замена задачи здесь раздвоила бы работу."""

        self.assertIn("AMBIGUOUS", self.text)
        self.assertIn("never guess", self.text)


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


class EntrypointDefaultsTests(unittest.TestCase):
    """M11-ENTRYPOINT-DEFAULTS: шаблон скилла - фактический дефолт прогона.

    plan.py объявляет schema-3 умолчанием auto и двух воркеров, но план
    пишет не plan.py, а планировщик по образцу из SKILL.md. Образец нёс
    execution_strategy="serial" и max_parallel_workers=1, то есть каждый
    новый прогон входил в serial явно и никогда не достигал дефолта. Это
    сильнее умолчания: явное значение в файле нельзя переопределить.
    """

    SKILLS = (
        Path(__file__).resolve().parents[1]
        / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md",
        Path(__file__).resolve().parents[1]
        / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md",
    )

    def test_both_templates_emit_the_declared_v09_defaults(self) -> None:
        from codex_autopilot.plan import (
            DEFAULT_EXECUTION_STRATEGY,
            DEFAULT_MAX_PARALLEL_WORKERS,
        )

        expected = (
            f'"execution_strategy":"{DEFAULT_EXECUTION_STRATEGY}",'
            f'"max_parallel_workers":{DEFAULT_MAX_PARALLEL_WORKERS}'
        )
        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIn(expected, text)
                self.assertNotIn('"execution_strategy":"serial"', text)

    def test_both_templates_explain_that_siblings_are_the_parallelism(self) -> None:
        """Дефолт auto ничего не даёт графу, выстроенному в цепочку."""

        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("declared as siblings", text)
                self.assertIn("legacy_serial", text)

    def test_both_templates_warn_that_a_shared_write_serializes_siblings(self) -> None:
        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                self.assertIn(
                    "the resource lock",
                    skill.read_text(encoding="utf-8"),
                )


class RoleNameLanguageTests(unittest.TestCase):
    """Роль - профессия, а профессии во всей среде названы по-английски.

    Правило языка велело писать в языке прогона всё, кроме протокольных
    идентификаторов, и планировщик послушно переводил имена ролей. А
    формат заголовка ветки дописывает английские Verifier и Verify -
    получалось "Инженер основания Verifier | M1 | Verify ...", половина
    на половину. Это не вкусовщина: смешанный заголовок производит сам
    код, а не человек.
    """

    SKILLS = (
        Path(__file__).resolve().parents[1]
        / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md",
        Path(__file__).resolve().parents[1]
        / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md",
    )

    def test_both_skills_exempt_role_names_from_the_run_language(self) -> None:
        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("stay in English always", text)
                self.assertIn("Resilience Engineer", text)

    def test_both_skills_say_why_rather_than_only_what(self) -> None:
        """Правило без причины планировщик переиначит при первом конфликте."""

        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("`Verifier` and `Verify`", text)
                self.assertIn("half-translated title", text)

    def test_the_title_format_really_appends_english_words(self) -> None:
        """Обоснование правила проверяется, а не принимается на слово."""

        from codex_autopilot.thread_titles import verifier_thread_title

        title = verifier_thread_title("M1", "Create the foundation", role_name="Foundation Engineer")
        self.assertIn("Verifier", title)
        self.assertIn("Verify", title)
