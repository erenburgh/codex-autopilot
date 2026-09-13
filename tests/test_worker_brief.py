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


def _role(role_id: str, name: str) -> dict:
    return {"id": role_id, "name": name, "responsibilities": [f"Own {role_id}."]}


def _task(task_id: str, title: str, *, role: str, depends: tuple[str, ...] = (),
          policy: str = "deterministic", verifier: str | None = None) -> dict:
    verification: dict = {"policy": policy, "required": True, "max_revision_attempts": 2}
    if policy == "deterministic":
        verification["deterministic_checks"] = [
            {
                "id": f"{task_id}-tests",
                "kind": "command",
                "description": "Run the deterministic tests.",
                "argv": ["python3", "-m", "unittest"],
                "timeout_seconds": 600,
                "expected_exit_code": 0,
            }
        ]
    if verifier:
        verification["verifier_role"] = verifier
    return {
        "id": task_id,
        "title": title,
        "objective": f"Implement {title}.",
        "definition_of_done": [f"{task_id} is tested."],
        "execution_mode": "code",
        "execution_mode_reason": "Files and deterministic tests are sufficient.",
        "reasoning": "medium",
        "role": role,
        "depends_on": list(depends),
        "priority": 0,
        "verification": verification,
        "resources": [],
        "required_capabilities": [],
        "context": {"dependency_outputs": list(depends)},
        "outputs": [],
        "tags": [],
    }


def _plan():
    """План строится здесь же.

    Тест не читает ничего за пределами репозитория: сборка релиза
    отдельно запрещает уносить в архив чужие абсолютные пути, и она же
    этот тест и поймала.
    """

    return validate_plan(
        {
            "schema_version": 3,
            "graph_version": 1,
            "goal": "Ship the thread tool.",
            "user_request": "Build the thread tool exactly as specified.",
            "model_strategy": "auto",
            "execution_strategy": "auto",
            "max_parallel_workers": 2,
            "roles": [
                _role("runtime-engineer", "Runtime Engineer"),
                _role("reviewer", "Independent Reviewer"),
            ],
            "tasks": [
                _task("T1", "App Server client and command registry", role="runtime-engineer"),
                _task(
                    "T4",
                    "End-to-end acceptance and documentation",
                    role="runtime-engineer",
                    depends=("T1",),
                    policy="independent",
                    verifier="reviewer",
                ),
            ],
        },
        "adaptive",
    )


class BriefTests(unittest.TestCase):
    def setUp(self) -> None:
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
