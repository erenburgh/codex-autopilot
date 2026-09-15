"""Порог приёмки нельзя обойти ни заявлением, ни куском набора.

Два обхода закрываются здесь, и оба про одно: задача не должна принимать
собственную работу (R8/R29).

1. Происхождение. Мигрированный план v0.8 - единственное исключение из
   порога приёмки, потому что переписывать контракт уже идущего прогона
   нельзя. Исключение обязано доставаться по происхождению, а не по
   заявлению: иначе замена плана пишет себе ``compatibility`` и выходит
   из-под независимой верификации.
2. Полнота прогона. Порог требует полного набора тестов. Запускальщики
   пакетов и языков принимают фильтр так же легко, как и весь набор:
   ``cargo test one_case`` выглядит как ``cargo test``. Проверка, которая
   принимает подмножество за целое, не доказывает ничего.
"""

from __future__ import annotations

import copy
import unittest

from _plan_contract import canonical_verification, clean_suite_check
from codex_autopilot.plan import plan_to_dict, validate_plan, validate_plan_change


def _task(task_id: str, *, verification: dict | None = None) -> dict:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Complete {task_id} safely.",
        "definition_of_done": [f"{task_id} is verified."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files and tests are sufficient.",
        "reasoning": "high",
        "role": "builder",
        "depends_on": [],
        "priority": 0,
        "verification": verification if verification is not None else canonical_verification(),
        "resources": [
            {
                "id": "tree",
                "kind": "directory",
                "target": f"src/{task_id.lower()}",
                "access": "write",
            }
        ],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
    }


def _graph(tasks: list[dict], **overrides) -> dict:
    payload: dict = {
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Exercise the acceptance floor.",
        "user_request": "Exercise the acceptance floor exactly as specified.",
        "model_strategy": "auto",
        "execution_strategy": "parallel",
        "max_parallel_workers": 2,
        "computer_use_slots": 1,
        "roles": [
            {
                "id": "builder",
                "name": "Builder",
                "responsibilities": ["Implement and replan bounded tasks."],
            }
        ],
        "tasks": tasks,
    }
    payload.update(overrides)
    return payload


def _legacy_graph(tasks: list[dict]) -> dict:
    return _graph(
        tasks,
        execution_strategy="serial",
        max_parallel_workers=1,
        compatibility={"migrated_from_schema": 2, "legacy_serial": True},
    )


def _self_accepting() -> dict:
    return {
        "policy": "self",
        "required": False,
        "deterministic_checks": [],
        "max_revision_attempts": 2,
    }


class MigrationProvenanceTests(unittest.TestCase):
    def test_a_plan_change_cannot_declare_itself_migrated(self) -> None:
        current = validate_plan(_graph([_task("A")]), "adaptive")
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["execution_strategy"] = "serial"
        candidate["max_parallel_workers"] = 1
        candidate["compatibility"] = {"migrated_from_schema": 2, "legacy_serial": True}
        candidate["tasks"][0]["verification"] = _self_accepting()
        with self.assertRaises(ValueError) as caught:
            validate_plan_change(current, candidate, "adaptive")
        self.assertIn("inherited from the current plan", str(caught.exception))

    def test_a_migrated_run_keeps_its_provenance_without_restating_it(self) -> None:
        current = validate_plan(_legacy_graph([_task("A", verification=_self_accepting())]), "adaptive")
        self.assertTrue(current.legacy_serial)
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["tasks"][0]["resources"].append(
            {"id": "docs", "kind": "directory", "target": "docs", "access": "write"}
        )
        updated = validate_plan_change(current, candidate, "adaptive")
        self.assertTrue(updated.legacy_serial)
        self.assertEqual(updated.source_schema_version, 2)

    def test_a_migrated_run_cannot_smuggle_in_a_new_self_accepting_task(self) -> None:
        current = validate_plan(_legacy_graph([_task("A", verification=_self_accepting())]), "adaptive")
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["tasks"].append(_task("B", verification=_self_accepting()))
        with self.assertRaises(ValueError) as caught:
            validate_plan_change(current, candidate, "adaptive")
        self.assertIn("B", str(caught.exception))
        self.assertIn("independent", str(caught.exception))

    def test_a_migrated_task_cannot_have_its_acceptance_contract_rewritten(self) -> None:
        current = validate_plan(_legacy_graph([_task("A")]), "adaptive")
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["tasks"][0]["verification"] = _self_accepting()
        with self.assertRaises(ValueError) as caught:
            validate_plan_change(current, candidate, "adaptive")
        self.assertIn("independent", str(caught.exception))

    def test_a_migrated_run_cannot_be_widened_to_parallel(self) -> None:
        current = validate_plan(_legacy_graph([_task("A")]), "adaptive")
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["execution_strategy"] = "parallel"
        candidate["max_parallel_workers"] = 4
        with self.assertRaises(ValueError) as caught:
            validate_plan_change(current, candidate, "adaptive")
        self.assertIn("serial", str(caught.exception))


class FullSuiteClaimTests(unittest.TestCase):
    """Один фильтр в argv - и «полный прогон» перестаёт быть полным."""

    PARTIAL = (
        ("cargo", "test", "one_case"),
        ("cargo", "test", "--lib"),
        ("cargo", "test", "-p", "core"),
        ("cargo", "test", "-pcore"),
        ("npm", "run", "test", "--", "-tonecase"),
        ("npm", "test", "--", "-gonecase"),
        ("cargo", "test", "--test", "integration"),
        ("go", "test", "./pkg/foo"),
        ("go", "test", "-run", "TestOnlyThis"),
        ("go", "test", "-run=TestOnlyThis"),
        ("npm", "test", "--", "one_case"),
        ("npm", "run", "test", "--", "-t", "one case"),
        ("yarn", "test", "--testNamePattern", "one case"),
        ("dotnet", "test", "--filter", "Category=Fast"),
        ("gradle", "test", "--tests", "*Foo*"),
        ("mvn", "test", "-Dtest=FooTest"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "-ptest_plan*.py"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_plan*.py"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "--pattern=test_plan*.py"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "-kplan"),
        ("python", "-m", "pytest", "-kplan"),
        ("python", "run_suite.py", "-k", "plan"),
    )

    WHOLE = (
        ("cargo", "test"),
        ("cargo", "test", "--workspace"),
        ("go", "test", "./..."),
        ("npm", "test"),
        ("npm", "test", "--", "--coverage"),
        ("make", "test"),
        ("make", "test", "V=1"),
        ("python", "-m", "unittest", "discover", "-s", "tests"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "-ptest*.py"),
    )

    def _plan_with_suite_argv(self, argv: tuple[str, ...]) -> dict:
        check = copy.deepcopy(clean_suite_check())
        check["argv"] = [
            "env",
            "CODEX_THREAD_ID=",
            "CODEX_TURN_ID=",
            "CODEX_SESSION_ID=",
            *argv,
        ]
        return _graph(
            [_task("A", verification=canonical_verification(checks=[]) | {
                "deterministic_checks": [check],
            })]
        )

    def test_a_filtered_runner_is_not_a_full_suite(self) -> None:
        for argv in self.PARTIAL:
            with self.subTest(argv=argv):
                with self.assertRaises(ValueError) as caught:
                    validate_plan(self._plan_with_suite_argv(argv), "adaptive")
                self.assertIn("full-suite", str(caught.exception))

    def test_a_whole_run_is_accepted(self) -> None:
        for argv in self.WHOLE:
            with self.subTest(argv=argv):
                plan = validate_plan(self._plan_with_suite_argv(argv), "adaptive")
                self.assertEqual(plan.tasks[0].id, "A")


if __name__ == "__main__":
    unittest.main()
