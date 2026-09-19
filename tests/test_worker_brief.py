"""A task's first message must be useful.

The thread becomes visible ~1.3 seconds after the turn starts - so the
moment the task appears in the interface and the moment the worker
starts speaking are one and the same. It used to be wasted: the sidebar
preview showed the identical line "Codex Autopilot AI Studio Runtime —
fresh implementation worker" on every task, and the reply began with
silent tool work.

Now the same appearance carries two things: a title by which the task is
seen in the list, and a briefing - what was taken, what will be
presented, by which path and who judges.
"""

from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from codex_autopilot.ai_studio import AIStudioRuntime
from codex_autopilot.plan import validate_plan
from _plan_contract import canonicalize_plan, canonical_verification


def _role(role_id: str, name: str) -> dict:
    return {"id": role_id, "name": name, "responsibilities": [f"Own {role_id}."]}


def _task(task_id: str, title: str, *, role: str, depends: tuple[str, ...] = (),
          verifier: str | None = None) -> dict:
    verification = canonical_verification(verifier_role=verifier)
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
    """The plan is built right here.

    The test reads nothing outside the repository: the release build
    separately forbids carrying foreign absolute paths into the archive,
    and it is what caught this test.
    """

    return validate_plan(
        canonicalize_plan({
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
                    verifier="reviewer",
                ),
            ],
        }),
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
        # The task's dependencies must be verified, otherwise the context
        # refuses to hand over their outputs - and that is the right refusal.
        states = {task.id: "VERIFIED" for task in self.plan.tasks}
        states[task_id] = "READY"
        return self.runtime.build_prompt(
            task_id, phase=phase, task_states=states, reservation_token="tok", **kwargs
        )

    def test_the_first_line_names_role_task_and_title(self) -> None:
        """The sidebar preview shows this line without opening the task."""

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
        """A deadline named at random is a promise nobody made."""

        prompt = self._prompt("implementation")
        self.assertIn("Сроков в нём нет", prompt)

    def test_the_verifier_briefs_too(self) -> None:
        """T4 declares a separate verifier: the title carries its role."""

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
