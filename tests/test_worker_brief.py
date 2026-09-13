"""Первое сообщение задачи обязано быть полезным.

Ветка становится видимой через ~1.3 секунды после старта хода - значит
момент появления задачи в интерфейсе и момент, когда воркер начинает
говорить, это один и тот же момент. Прежде он тратился впустую: превью
в сайдбаре показывало одинаковую строку "Codex Autopilot AI Studio
Runtime — свежий implementation worker" на всех задачах, а ответ
начинался с молчаливой работы инструментами.

Теперь то же самое появление несёт две вещи: заголовок, по которому
задачу видно в списке, и брифинг - что взято, что будет предъявлено,
каким путём и кто судит.
"""

from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from codex_autopilot.ai_studio import AIStudioRuntime
from codex_autopilot.plan import validate_plan


REFERENCE = Path("/Users/p.erenburg/Documents/Codex/autopilot-v1.0-materials/reference-plan-codex-thread-tools.json")


def _plan():
    return validate_plan(json.loads(REFERENCE.read_text(encoding="utf-8")), "adaptive")


class BriefTests(unittest.TestCase):
    def setUp(self) -> None:
        if not REFERENCE.is_file():
            self.skipTest("эталонный план недоступен")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / ".git").mkdir()
        skill = root / "SKILL.md"
        skill.write_text("skill", encoding="utf-8")
        self.plan = _plan()
        self.runtime = AIStudioRuntime(
            self.plan,
            root,
            language="ru",
            skill_path=skill,
        )

    def _prompt(self, phase: str, task_id: str = "T1", **kwargs) -> str:
        # Зависимости задачи обязаны быть проверены, иначе контекст
        # отказывается отдавать их выходы - и это правильный отказ.
        states = {task.id: "VERIFIED" for task in self.plan.tasks}
        states[task_id] = "READY"
        return self.runtime.build_prompt(
            task_id, phase=phase, task_states=states, reservation_token="tok", **kwargs
        )

    def test_the_first_line_names_role_task_and_title(self) -> None:
        """Эту строку показывает превью сайдбара, не открывая задачу."""

        first = self._prompt("implementation").splitlines()[0]
        self.assertIn("Runtime Engineer", first)
        self.assertIn("T1", first)
        self.assertIn("App Server client", first)
        self.assertNotIn("AI Studio Runtime", first)

    def test_the_brief_is_required_before_any_tool(self) -> None:
        prompt = self._prompt("implementation")
        self.assertIn("AUTOPILOT_BRIEF", prompt)
        self.assertIn("до любого чтения файлов и вызова инструментов", prompt)

    def test_the_brief_names_what_a_manager_asks(self) -> None:
        prompt = self._prompt("implementation")
        for field in ("Задача:", "Результат:", "Путь:", "Судья:", "Ресурсы:"):
            with self.subTest(field=field):
                self.assertIn(field, prompt)

    def test_the_brief_forbids_invented_estimates(self) -> None:
        """Срок, названный наугад, - обещание, которого никто не давал."""

        prompt = self._prompt("implementation")
        self.assertIn("Сроков в нём нет", prompt)

    def test_the_verifier_briefs_too(self) -> None:
        """У T4 объявлен отдельный проверяющий: заголовок несёт его роль."""

        prompt = self._prompt("verification", task_id="T4", verification_round=1)
        self.assertIn("AUTOPILOT_BRIEF", prompt)
        self.assertIn("Independent Reviewer", prompt.splitlines()[0])
        self.assertIn("T4", prompt.splitlines()[0])

    def test_the_english_prompt_carries_the_same_contract(self) -> None:
        self.runtime.language = "en"
        prompt = self._prompt("implementation")
        self.assertIn("AUTOPILOT_BRIEF", prompt)
        self.assertIn("before reading files or calling any tool", prompt)
        self.assertIn("a promise nobody made", prompt)


if __name__ == "__main__":
    unittest.main()
