"""Первый запуск - единственный момент, когда пользователь смотрит.

Всё, что ему нужно знать, чтобы прогон шёл без него, скилл обязан
сказать до первого воркера: как смотреть, что значит идущая задача,
как менять план, что случается на поломке. И ни один обещанный способ
посмотреть не должен быть словом, которого хук не знает.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

from codex_autopilot import control

ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted((ROOT / "plugins").rglob("SKILL.md"))


def onboarding_block(text: str) -> str:
    """Блок онбординга одной строкой: переносы внутри абзацев не считаются."""

    start = text.index("## Onboarding")
    end = text.index("\n## ", start + 5)
    return " ".join(text[start:end].split())


class OnboardingTests(unittest.TestCase):
    def test_both_skills_carry_the_onboarding_before_the_first_worker(self) -> None:
        for path in SKILLS:
            text = path.read_text(encoding="utf-8")
            self.assertIn("## Onboarding", text, path.name)
            self.assertLess(
                text.index("## Onboarding"),
                text.index("## Start a run"),
                f"{path}: онбординг идёт до запуска, а не после",
            )

    def test_the_onboarding_answers_the_questions_users_asked(self) -> None:
        for path in SKILLS:
            block = onboarding_block(path.read_text(encoding="utf-8"))
            for needle in (
                "/hooks",            # где дать доверие хуку
                "Always",            # что ответить на memory-tool
                "sidebar",           # почему задач не видно до "статус"
                "held by",           # задача удерживается до конца
                "correct the result by hand",  # правки руками после
                "plan change",       # как добавить незапланированное
                "Never hand-edit",   # чего не трогать
                "Pipeline Engineer", # кто чинит
                "resumes by itself", # лимит кончился - продолжит сам
            ):
                self.assertIn(needle, block, f"{path.name}: в онбординге нет «{needle}»")

    def test_every_phrase_the_onboarding_promises_is_one_the_hook_knows(self) -> None:
        """Обещанное слово, которого хук не знает, - сломанная дверь."""

        known = {
            control._normalized_prompt(item)
            for item in (
                control.STATUS_PROMPTS
                | control.PAUSE_PROMPTS
                | control.RESUME_PROMPTS
                | control.UNINSTALL_PROMPTS
            )
        }
        for path in SKILLS:
            block = onboarding_block(path.read_text(encoding="utf-8"))
            how_to_look = block.split("**How to look.**", 1)[1].split(" 3. ", 1)[0]
            promised = re.findall(r"`([^`]+)`", how_to_look)
            self.assertGreaterEqual(len(promised), 8, f"{path.name}: обещаний подозрительно мало")
            for phrase in promised:
                self.assertIn(
                    control._normalized_prompt(phrase),
                    known,
                    f"{path.name}: обещано «{phrase}», а хук такого не знает",
                )

    def test_tasks_is_a_word_the_hook_answers(self) -> None:
        for phrase in ("задачи", "tasks", "покажи задачи", "show tasks"):
            self.assertIn(control._normalized_prompt(phrase), control.STATUS_PROMPTS)

    def test_the_engineer_writes_for_the_user_in_the_run_language(self) -> None:
        import inspect

        from codex_autopilot.ai_studio import AIStudioRuntime

        source = inspect.getsource(AIStudioRuntime.build_pipeline_engineer_prompt)
        self.assertIn("Run language: `{self.language}`", source)
        self.assertIn("never translated", source)


if __name__ == "__main__":
    unittest.main()
