"""Прогон не имеет права молча стоять на approval, который никто не закроет.

Замеренный случай. Первый `start-skill` упал на "Timed out waiting for App
Server": холодный старт App Server грузил каждый установленный плагин, один из
них - чужой, со сломанным YAML, - и штатных 60 секунд не хватило. Модель
приняла таймаут за отказ в правах, как ей и велел скилл ("If the approval
instead names CODEX_HOME, request only normal read/write access"), и запустила
ту же команду повторно, приложив к ней запрос доступа к `~/.codex`.

Codex поднял нативный диалог на саму команду `start-skill`. Диспетчер по
своему правилу не отвечает на approvals - preflight вышел с кодом 2. Диалог
остался висеть в задаче, на которую пользователь не смотрел, инициирующий ход
показывал "думаю", и так полчаса.

Три места, где это чинится, и их проверки здесь:

1. рукопожатие получает собственный бюджет и говорит, чем оно не является;
2. скилл больше не велит отвечать на сбой запросом прав;
3. preflight называет единственный возможный вопрос до первой долгой проверки,
   а чужой approval объясняет словами, а не сырым JSON.

Плюс правило execpolicy, которое снимает сам повод спрашивать.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from codex_autopilot import appserver, preflight  # noqa: E402
import register_execpolicy  # noqa: E402

SKILLS = (
    ROOT / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md",
    ROOT / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md",
)


class HandshakeBudgetTests(unittest.TestCase):
    def test_initialize_has_its_own_budget_larger_than_a_plain_request(self) -> None:
        self.assertGreater(appserver.INITIALIZE_TIMEOUT, 60)

    def test_connect_explains_that_a_timeout_is_not_a_permission_problem(self) -> None:
        class StubProc:
            returncode = None
            stdin = stdout = stderr = object()

        client = appserver.AppServerClient("codex", Path("/dev/null"))

        def fail(*_args, **_kwargs):
            raise appserver.AppServerError("Timed out waiting for App Server; stderr=...")

        client.popen_factory = lambda *a, **k: StubProc()  # type: ignore[assignment]
        client._initialize_request = fail  # type: ignore[method-assign]
        client._read_stdout = lambda: None  # type: ignore[method-assign]
        client._read_stderr = lambda: None  # type: ignore[method-assign]

        with self.assertRaises(appserver.AppServerError) as caught:
            client.connect()

        message = str(caught.exception)
        self.assertIn("CODEX_HOME", message)
        self.assertIn("useless", message)


class SkillNoLongerEscalatesTests(unittest.TestCase):
    def test_no_skill_tells_the_model_to_answer_a_failure_with_a_rights_request(self) -> None:
        for skill in SKILLS:
            with self.subTest(skill=skill.name):
                text = skill.read_text(encoding="utf-8")
                self.assertNotIn("If the approval instead names `CODEX_HOME`, request", text)
                self.assertIn("Never attach a permission request to `start-skill`", text)

    def test_every_skill_requires_direct_argv_so_execpolicy_can_match(self) -> None:
        for skill in SKILLS:
            with self.subTest(skill=skill.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("direct argv", text)
                self.assertIn("execpolicy matches argv tokens", text)


class PreflightSpeaksBeforeItWaitsTests(unittest.TestCase):
    def test_the_only_possible_question_is_named_before_the_first_check(self) -> None:
        self.assertIn(preflight.MEMORY_PREFLIGHT_TITLE, preflight.ANNOUNCEMENT)

    def test_announcement_is_emitted_ahead_of_the_project_line(self) -> None:
        seen: list[str] = []
        try:
            preflight.run_preflight(
                Path("/definitely/not/a/project"),
                plan=None,
                profile="adaptive",
                skill_path=Path("/definitely/not/a/skill"),
                emit=seen.append,
            )
        except Exception:
            pass
        self.assertIn(preflight.ANNOUNCEMENT, seen)
        self.assertLess(
            seen.index(preflight.ANNOUNCEMENT),
            next(i for i, line in enumerate(seen) if line.startswith("Project:")),
        )

    def test_a_foreign_approval_is_explained_rather_than_dumped(self) -> None:
        class Approval(Exception):
            payload = {
                "method": "item/commandExecution/requestApproval",
                "params": {"kind": "command", "reason": "Разрешить доступ к /Users/x/.codex"},
            }

        message = preflight._unexpected_approval_message(Approval())
        self.assertIn("kind=command", message)
        self.assertIn("Разрешить доступ к /Users/x/.codex", message)
        self.assertIn("not rerun it for access", message)
        self.assertNotIn('"method"', message)


class ExecpolicyRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rules = Path(self.tmp.name) / "rules" / "default.rules"

    def test_it_allows_only_the_commands_of_the_initiating_turn(self) -> None:
        text = register_execpolicy.register("/x/scripts/codex-autopilot", self.rules)
        self.assertIn("start-skill", text)
        self.assertIn("timeline", text)
        for forbidden in ("devops-resolve-incident", "uninstall", "hook"):
            self.assertNotIn(forbidden, text)

    def test_reinstall_replaces_the_old_block_instead_of_stacking_it(self) -> None:
        register_execpolicy.register("/old/scripts/codex-autopilot", self.rules)
        text = register_execpolicy.register("/new/scripts/codex-autopilot", self.rules)
        self.assertNotIn("/old/scripts/codex-autopilot", text)
        self.assertEqual(text.count(register_execpolicy.MARKER), 1)

    def test_foreign_rules_survive(self) -> None:
        self.rules.parent.mkdir(parents=True)
        self.rules.write_text('prefix_rule(pattern=["cp"], decision="allow")\n', encoding="utf-8")
        text = register_execpolicy.register("/x/scripts/codex-autopilot", self.rules)
        self.assertIn('prefix_rule(pattern=["cp"], decision="allow")', text)


if __name__ == "__main__":
    unittest.main()
