"""Бюджет промпта и адресность Project Memory.

Один прогон v1.0 упёрся сразу в обе дыры. Промпт M1 занял 62 635
символов при потолке 64 000, из них 51 475 - дословная копия запроса
пользователя и 395 - сама задача. Потолок при этом был голой
константой без обоснования, а окно модели на том же ходе составляло
258 400 токенов. Одновременно `current` отвечала про веху с индексом
ноль независимо от того, кто спрашивает.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from codex_autopilot.ai_studio import (
    CHARS_PER_TOKEN,
    MAX_PROMPT_CHARS,
    MEMORY_SERVER_NAME,
    OBSERVED_CONTEXT_WINDOW_TOKENS,
    PROMPT_BUDGET_SHARE,
    AIStudioRuntime,
)
from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.config import load_config
from codex_autopilot.memory import MemoryValidationError, ProjectMemory
from codex_autopilot.memory_mcp import MemoryMcpServer, _combined_input_schema
from codex_autopilot.plan import validate_plan
from codex_autopilot.preflight import MEMORY_SERVER_NAME as PREFLIGHT_MEMORY_SERVER_NAME
from codex_autopilot.run_state import StateStore
from codex_autopilot.task_state import TaskState

from test_ai_studio import context_payload, role, task
from test_verification_lifecycle import graph, task as graph_task

# validate_plan нормализует текст, поэтому хвостовой пробел здесь
# дал бы расхождение длины на единицу.
HUGE_REQUEST = ("Собери продукт целиком. " * 2_200).strip()  # ~50 000 символов


class PromptBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-autopilot-budget-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")
        self.memory = ProjectMemory(self.root)
        self.memory.initialize()

    def runtime(self, user_request: str) -> AIStudioRuntime:
        plan = validate_plan(
            {
                "schema_version": 3,
                "goal": "Exercise the prompt budget.",
                "user_request": user_request,
                "model_strategy": "auto",
                "execution_strategy": "serial",
                "max_parallel_workers": 1,
                "computer_use_slots": 1,
                "roles": [role("integrator", "Release Integrator")],
                "tasks": [task("code-a", "integrator")],
            },
            "adaptive",
        )
        return AIStudioRuntime(
            plan, self.root, language="ru", skill_path=self.skill, memory=self.memory
        )

    def build(self, user_request: str) -> str:
        return self.runtime(user_request).build_prompt(
            "code-a",
            phase="implementation",
            task_states={"code-a": "READY"},
            reservation_token="fresh-1",
        )

    def test_ceiling_is_derived_from_the_observed_context_window(self) -> None:
        """Число обосновано, а не выдумано.

        Прежние 64 000 не имели ни комментария, ни строки в docs/, и
        составляли примерно шестую часть того, что модель принимает.
        """

        self.assertEqual(
            MAX_PROMPT_CHARS,
            int(OBSERVED_CONTEXT_WINDOW_TOKENS * PROMPT_BUDGET_SHARE * CHARS_PER_TOKEN),
        )
        self.assertGreater(MAX_PROMPT_CHARS, 64_000)
        self.assertLess(
            MAX_PROMPT_CHARS, OBSERVED_CONTEXT_WINDOW_TOKENS * CHARS_PER_TOKEN
        )

    def test_a_large_request_no_longer_decides_whether_a_task_can_run(self) -> None:
        """Главная проверка: подробное ТЗ больше не запрещает прогон.

        Пятьдесят тысяч символов запроса раньше означали, что ни одна
        задача плана не соберётся никогда.
        """

        self.assertGreater(len(HUGE_REQUEST), 49_000)
        prompt = self.build(HUGE_REQUEST)
        self.assertNotIn(HUGE_REQUEST[:200], prompt)
        # Промпт не просто пролез - он перестал зависеть от длины ТЗ.
        small = self.build("Сделай ровно то, о чём сказано.")
        self.assertLess(abs(len(prompt) - len(small)), 400)

    def test_the_reference_is_verifiable_and_the_worker_is_told_how_to_use_it(self) -> None:
        prompt = self.build(HUGE_REQUEST)
        reference = context_payload(prompt)["acceptance_gate"]["original_user_request"]
        self.assertFalse(reference["verbatim_in_prompt"])
        self.assertEqual(reference["chars"], len(HUGE_REQUEST))
        self.assertEqual(
            reference["sha256"],
            hashlib.sha256(HUGE_REQUEST.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(reference["retrieval"]["arguments"]["task_id"], "code-a")
        self.assertIn(MEMORY_SERVER_NAME, prompt)
        self.assertIn('"operation":"current"', prompt)

    def test_the_named_server_is_the_one_preflight_verifies(self) -> None:
        """Разойдись имена - воркер звал бы сервер, которого нет."""

        self.assertEqual(MEMORY_SERVER_NAME, PREFLIGHT_MEMORY_SERVER_NAME)

    def test_an_oversized_prompt_names_the_block_that_is_too_big(self) -> None:
        """Прежнее сообщение отправляло сужать задачу в 395 символов."""

        runtime = self.runtime("Сделай ровно то, о чём сказано.")
        oversized = "x" * (MAX_PROMPT_CHARS + 1_000)
        object.__setattr__(runtime.plan.task_map["code-a"], "objective", oversized)
        with self.assertRaises(Exception) as caught:
            runtime.build_prompt(
                "code-a",
                phase="implementation",
                task_states={"code-a": "READY"},
                reservation_token="fresh-1",
            )
        message = str(caught.exception)
        self.assertIn("Largest blocks:", message)
        self.assertIn("task=", message)


class CurrentAddressingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-autopilot-current-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "plan-input.json"
        payload = graph(graph_task("A"))
        payload["user_request"] = HUGE_REQUEST
        plan_file.write_text(json.dumps(payload), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
        )
        load_config(self.root)
        self.store = StateStore(self.root / ".codex-autopilot")
        self.server = MemoryMcpServer(self.root)

    def set_states(self, states: dict[str, str]) -> None:
        state = self.store.load()
        state.task_states = {**state.task_states, **states}
        self.store.save(state)

    def test_the_schema_lets_a_worker_name_its_task(self) -> None:
        """Без объявления в схеме аргумент невозможно передать."""

        current = next(
            choice
            for choice in _combined_input_schema()["oneOf"]
            if choice["properties"]["operation"].get("const") == "current"
        )
        self.assertIn("task_id", current["properties"])
        self.assertFalse(current["additionalProperties"])

    def test_a_named_task_gets_its_own_contract_and_the_full_request(self) -> None:
        self.set_states({"A": TaskState.RUNNING.value})
        answer = self.server._current({"task_id": "B"})
        self.assertEqual(answer["milestone"]["id"], "B")
        self.assertEqual(answer["user_request"], HUGE_REQUEST)

    def test_a_single_active_task_needs_no_name(self) -> None:
        self.set_states({"A": TaskState.RUNNING.value})
        self.assertEqual(self.server._current({})["milestone"]["id"], "A")

    def test_two_active_tasks_refuse_to_be_guessed(self) -> None:
        """Прежде здесь молча возвращалась веха с индексом ноль.

        В живом прогоне из 23 задач это означало, что каждый воркер,
        кроме первого, получал чужую задачу под видом своей.
        """

        self.set_states({"A": TaskState.RUNNING.value, "B": TaskState.RUNNING.value})
        with self.assertRaises(MemoryValidationError) as caught:
            self.server._current({})
        message = str(caught.exception)
        self.assertIn("task_id is required", message)
        self.assertIn("A", message)
        self.assertIn("B", message)

    def test_an_unknown_task_is_named_rather_than_substituted(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.server._current({"task_id": "M99"})
        self.assertIn("M99", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
