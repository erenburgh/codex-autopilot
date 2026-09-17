"""The acceptance floor cannot be bypassed by a claim or by a slice of the suite.

Two bypasses are closed here, and both are about one thing: a task must
not accept its own work (R8/R29).

1. Provenance. A migrated v0.8 plan is the only exemption from the
   acceptance floor, because the contract of a run already under way
   cannot be rewritten. The exemption must come by provenance, not by
   claim: otherwise a plan replacement writes itself ``compatibility``
   and slips out of independent verification.
2. Completeness of the run. The floor requires the full test suite.
   Package and language runners accept a filter as easily as the whole
   suite: ``cargo test one_case`` looks like ``cargo test``. A check that
   takes a subset for the whole proves nothing.
"""

from __future__ import annotations

import copy
import shutil
import json
from pathlib import Path
import tempfile
import unittest

from _plan_contract import (
    TEST_OUTCOME_ID,
    canonicalize_plan,
    canonical_verification,
    clean_suite_check,
)
from codex_autopilot.plan import (
    load_plan,
    plan_to_dict,
    persisted_legacy_milestone_ids,
    validate_persisted_plan,
    validate_migrating_plan,
    validate_plan,
    validate_plan_change,
)


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
        "produces_outcomes": [TEST_OUTCOME_ID],
        "acceptance_class": "mixed",
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
    return canonicalize_plan(payload)


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


def _historical_legacy_acceptance() -> dict:
    """The exact policy synthesized by the v0.8-to-v0.9 migration."""

    return {
        "policy": "self",
        "required": True,
        "deterministic_checks": [],
        "max_revision_attempts": 0,
    }


def _load_persisted_legacy(payload: dict):
    """Exercise the production loader with evidence of an existing v0.8 run."""

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-legacy-plan-") as raw:
        state_dir = Path(raw)
        (state_dir / "plan.json").write_text(json.dumps(payload), encoding="utf-8")
        (state_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "schema_version": 4,
                    "run_id": "existing-v08-run",
                    "status": "DONE",
                    "phase": "DONE",
                    "milestone_index": 0,
                    "milestone_id": "A",
                    "worker_history": [],
                }
            ),
            encoding="utf-8",
        )
        return load_plan(state_dir, "adaptive")


def _validate_migration(payload: dict, previous_ids: tuple[str, ...]):
    """Validate through the production migration entry point."""

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-v08-input-") as raw:
        state_dir = Path(raw)
        (state_dir / "plan.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "goal": "Existing legacy run.",
                    "model_strategy": "auto",
                    "milestones": [{"id": task_id} for task_id in previous_ids],
                }
            ),
            encoding="utf-8",
        )
        (state_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "schema_version": 4,
                    "run_id": "existing-v08-run",
                    "status": "DONE",
                }
            ),
            encoding="utf-8",
        )
        return validate_migrating_plan(
            payload,
            "adaptive",
            state_dir=state_dir,
        )


class MigrationProvenanceTests(unittest.TestCase):
    def test_a_submitted_plan_cannot_declare_itself_migrated(self) -> None:
        """Дыра создания: план объявлял происхождение прямо в теле.

        Присланный schema-3 план заявлял ``compatibility.legacy_serial``,
        получал историческое исключение и выходил из-под независимой
        приёмки целиком - на свежем проекте, где мигрировать нечего.
        """

        submitted = _legacy_graph([_task("A", verification=_self_accepting())])
        with self.assertRaises(ValueError) as caught:
            validate_plan(submitted, "adaptive")
        self.assertIn("never declared", str(caught.exception))

    def test_a_submitted_plan_still_fails_the_floor_without_the_claim(self) -> None:
        submitted = _graph([_task("A", verification=_self_accepting())])
        with self.assertRaises(ValueError) as caught:
            validate_plan(submitted, "adaptive")
        self.assertIn("independent", str(caught.exception))

    def test_a_genuinely_migrated_plan_still_loads_from_disk(self) -> None:
        """Мигрированный прогон обязан продолжать читаться.

        Его происхождение написал сам рантайм, а не отправитель, поэтому
        записанный вход его принимает - иначе откат совместимости v0.8
        не имел бы смысла.
        """

        persisted = _legacy_graph(
            [_task("A", verification=_historical_legacy_acceptance())]
        )
        plan = _load_persisted_legacy(persisted)
        self.assertTrue(plan.legacy_serial)
        self.assertEqual(plan.source_schema_version, 2)

    def test_early_migrated_plan_without_explicit_slot_field_keeps_default(self) -> None:
        persisted = _legacy_graph(
            [_task("A", verification=_historical_legacy_acceptance())]
        )
        persisted.pop("computer_use_slots")
        plan = _load_persisted_legacy(persisted)
        self.assertEqual(plan.computer_use_slots, 1)

    def test_legacy_claim_without_adjacent_run_state_is_rejected(self) -> None:
        persisted = _legacy_graph(
            [_task("A", verification=_historical_legacy_acceptance())]
        )
        with self.assertRaisesRegex(ValueError, "adjacent existing v0.8 run-state"):
            validate_persisted_plan(persisted, "adaptive")

    def test_proven_run_state_does_not_exempt_a_fabricated_self_policy(self) -> None:
        persisted = _legacy_graph([_task("A", verification=_self_accepting())])
        with self.assertRaisesRegex(ValueError, "independent"):
            _load_persisted_legacy(persisted)

    def test_new_independent_task_in_a_migrated_graph_stays_canonical(self) -> None:
        persisted = _legacy_graph(
            [
                _task("A", verification=_historical_legacy_acceptance()),
                _task("B"),
            ]
        )
        plan = _load_persisted_legacy(persisted)
        self.assertEqual(plan.task_map["A"].verification.policy, "self")
        self.assertEqual(plan.task_map["B"].verification.policy, "independent")

    def test_canonical_task_cannot_be_downgraded_by_a_later_v08_input(self) -> None:
        previous = _legacy_graph(
            [
                _task("A", verification=_historical_legacy_acceptance()),
                _task("B"),
            ]
        )
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-v08-downgrade-") as raw:
            state_dir = Path(raw)
            (state_dir / "plan.json").write_text(
                json.dumps(previous), encoding="utf-8"
            )
            (state_dir / "run-state.json").write_text(
                json.dumps(
                    {
                        "schema_version": 5,
                        "run_id": "migrated-run",
                        "status": "DONE",
                        "execution_strategy": "serial",
                        "max_parallel_workers": 1,
                        "migrated_from_schema": 4,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                persisted_legacy_milestone_ids(state_dir),
                frozenset({"A"}),
            )
            with self.assertRaisesRegex(ValueError, "not part of the run being migrated"):
                validate_migrating_plan(
                    self._v08_payload("B"),
                    "adaptive",
                    state_dir=state_dir,
                )

    def _v08_payload(self, milestone_id: str = "M1") -> dict:
        return {
            "schema_version": 2,
            "goal": "Legacy goal.",
            "user_request": "Legacy request exactly as specified.",
            "model_strategy": "auto",
            "milestones": [
                {
                    "id": milestone_id,
                    "title": "Legacy milestone",
                    "objective": "Do the legacy thing.",
                    "definition_of_done": ["it is done"],
                    "execution_mode": "code",
                    "execution_mode_reason": "Repository files are sufficient.",
                    "reasoning": "high",
                }
            ],
        }

    def test_a_fresh_project_cannot_use_the_legacy_format_at_all(self) -> None:
        """Сам формат v0.8 был обходом.

        Свежий проект подавал план schema-2 - и все его задачи выходили
        из-под независимой приёмки как «мигрированные», хотя мигрировать
        было нечего. Происхождение доказывает прогон, а не формат.
        """

        with self.assertRaises(ValueError) as caught:
            validate_plan(self._v08_payload(), "adaptive")
        self.assertIn("migrating an existing run", str(caught.exception))

    def test_a_migration_cannot_smuggle_in_a_milestone_of_its_own(self) -> None:
        """Новая веха под видом мигрированной - это новая работа.

        Формат v0.8 не умеет объявлять независимую приёмку вовсе: поля
        `verification` в нём нет. Поэтому веха, которой в мигрируемом
        прогоне не было, отвергается: новая работа добавляется
        канонической сменой плана.
        """

        with self.assertRaises(ValueError) as caught:
            _validate_migration(self._v08_payload("NEW"), ("M1",))
        self.assertIn("not part of the run being migrated", str(caught.exception))

    def test_an_actual_v08_payload_is_still_migrated(self) -> None:
        """Настоящий v0.8 приходит без compatibility и мигрируется."""

        legacy = {
            "schema_version": 2,
            "goal": "Legacy goal.",
            "user_request": "Legacy request exactly as specified.",
            "model_strategy": "auto",
            "milestones": [
                {
                    "id": "M1",
                    "title": "Legacy milestone",
                    "objective": "Do the legacy thing.",
                    "definition_of_done": ["it is done"],
                    "execution_mode": "code",
                    "execution_mode_reason": "Repository files are sufficient.",
                    "reasoning": "high",
                }
            ],
        }
        plan = _validate_migration(legacy, ("M1",))
        self.assertTrue(plan.legacy_serial)
        self.assertEqual(plan.execution_strategy, "serial")
        self.assertEqual(plan.max_parallel_workers, 1)

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
        current = _load_persisted_legacy(
            _legacy_graph(
                [_task("A", verification=_historical_legacy_acceptance())]
            )
        )
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
        current = _load_persisted_legacy(
            _legacy_graph(
                [_task("A", verification=_historical_legacy_acceptance())]
            )
        )
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["tasks"].append(_task("B", verification=_self_accepting()))
        with self.assertRaises(ValueError) as caught:
            validate_plan_change(current, candidate, "adaptive")
        self.assertIn("B", str(caught.exception))
        self.assertIn("independent", str(caught.exception))

    def test_a_migrated_task_cannot_have_its_acceptance_contract_rewritten(self) -> None:
        current = _load_persisted_legacy(
            _legacy_graph(
                [_task("A", verification=_historical_legacy_acceptance())]
            )
        )
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["tasks"][0]["verification"] = _self_accepting()
        with self.assertRaises(ValueError) as caught:
            validate_plan_change(current, candidate, "adaptive")
        self.assertIn("independent", str(caught.exception))

    def test_a_migrated_run_cannot_be_widened_to_parallel(self) -> None:
        current = _load_persisted_legacy(
            _legacy_graph(
                [_task("A", verification=_historical_legacy_acceptance())]
            )
        )
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["execution_strategy"] = "parallel"
        candidate["max_parallel_workers"] = 4
        with self.assertRaises(ValueError) as caught:
            validate_plan_change(current, candidate, "adaptive")
        self.assertIn("serial", str(caught.exception))

    def test_a_migrated_run_keeps_one_computer_use_slot(self) -> None:
        current = _load_persisted_legacy(
            _legacy_graph(
                [_task("A", verification=_historical_legacy_acceptance())]
            )
        )
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["computer_use_slots"] = 2
        with self.assertRaises(ValueError) as caught:
            validate_plan_change(current, candidate, "adaptive")
        self.assertIn("computer_use_slots=1", str(caught.exception))


def _legacy_verification() -> dict:
    """Точный контракт, который синтезирует миграция v0.8.

    Исключение достаётся только ему: задача, добавленная в мигрированный
    план уже после миграции, рождена под каноническим порогом и остаётся
    под ним.
    """

    return {
        "policy": "self",
        "required": True,
        "deterministic_checks": [],
        "max_revision_attempts": 0,
    }


class LegacyRepurposeTests(unittest.TestCase):
    """Под старым номером нельзя провести новую работу.

    Исключение из порога приёмки историческое, и держаться обязано на
    истории. Если сверять только контракт верификации, то у мигрированной
    задачи можно переписать саму суть - цель и признак готовности, -
    оставив слабый контракт приёмки нетронутым. Это новая работа, которая
    принимает саму себя (R8), просто под чужим номером.

    Чинить состав мигрированной задачи при этом можно: переписывать
    историю чужого прогона нельзя, но и запрещать ему ремонт - значит
    ставить его намертво.
    """

    def _migrated(self) -> tuple[dict, Path]:
        state_dir = Path(tempfile.mkdtemp(prefix="codex-autopilot-legacy-"))
        (state_dir / "run-state.json").write_text(
            json.dumps(
                {"schema_version": 5, "run_id": "run-1", "migrated_from_schema": 4}
            ),
            encoding="utf-8",
        )
        payload = _legacy_graph([_task("A", verification=_legacy_verification())])
        (state_dir / "plan.json").write_text(json.dumps(payload), encoding="utf-8")
        self.addCleanup(shutil.rmtree, state_dir, True)
        return payload, state_dir

    def test_rewriting_the_objective_loses_the_historical_exemption(self) -> None:
        payload, state_dir = self._migrated()
        current = validate_persisted_plan(payload, "adaptive", state_dir=state_dir)
        self.assertTrue(current.legacy_serial)
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["tasks"][0]["objective"] = "Совсем другая работа под старым номером."
        with self.assertRaises(ValueError) as caught:
            validate_plan_change(current, candidate, "adaptive")
        self.assertIn("independent", str(caught.exception))

    def test_rewriting_the_definition_of_done_loses_it_too(self) -> None:
        payload, state_dir = self._migrated()
        current = validate_persisted_plan(payload, "adaptive", state_dir=state_dir)
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["tasks"][0]["definition_of_done"] = ["совершенно другой признак"]
        with self.assertRaises(ValueError) as caught:
            validate_plan_change(current, candidate, "adaptive")
        self.assertIn("independent", str(caught.exception))

    def test_repairing_the_resources_is_still_allowed(self) -> None:
        payload, state_dir = self._migrated()
        current = validate_persisted_plan(payload, "adaptive", state_dir=state_dir)
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["tasks"][0]["resources"].append(
            {"id": "docs", "kind": "directory", "target": "docs", "access": "write"}
        )
        updated = validate_plan_change(current, candidate, "adaptive")
        self.assertTrue(updated.legacy_serial)
        self.assertEqual(updated.task_map["A"].resources[-1].target, "docs")


class FullSuiteClaimTests(unittest.TestCase):
    """Один фильтр в argv - и «полный прогон» перестаёт быть полным."""

    PARTIAL = (
        ("./noop-test-suite",),
        ("cargo", "test", "."),
        ("cargo", "test", "./..."),
        ("cargo", "test", "one_case"),
        ("cargo", "test", "--lib"),
        ("cargo", "test", "--no-run"),
        ("cargo", "test", "-p", "core"),
        ("cargo", "test", "-pcore"),
        ("npm", "run", "test", "--", "-tonecase"),
        ("npm", "test", "--", "-gonecase"),
        ("cargo", "test", "--test", "integration"),
        ("go", "test", "./pkg/foo"),
        ("go", "test", "./pkg/..."),
        ("go", "test"),
        ("go", "test", "."),
        ("go", "test", "./"),
        ("go", "test", "-run", "TestOnlyThis"),
        ("go", "test", "-run=TestOnlyThis"),
        ("npm", "test", "--", "one_case"),
        ("npm", "run", "test", "--", "-t", "one case"),
        ("yarn", "test", "--testNamePattern", "one case"),
        ("npm", "test", "--ignore-scripts"),
        ("dotnet", "test", "--filter", "Category=Fast"),
        ("dotnet", "test", "--list-tests"),
        ("gradle", "test", "--tests", "*Foo*"),
        ("gradle", "test", "--dry-run"),
        ("gradle", "test", "-x", "integrationTest"),
        ("mvn", "test", "-Dtest=FooTest"),
        ("mvn", "test", "-DskipTests"),
        ("make", "test", "-n"),
        ("make", "test", "--just-print"),
        ("make", "test", "-q"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "-ptest_plan*.py"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_plan*.py"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "--pattern=test_plan*.py"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "-kplan"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "--help"),
        ("python", "-m", "pytest", "-kplan"),
        ("python", "-m", "pytest", "--help"),
        ("pytest", "--collect-only"),
        ("python", "run_suite.py", "-k", "plan"),
    )

    WHOLE = (
        ("cargo", "test"),
        ("cargo", "test", "--workspace"),
        ("go", "test", "./..."),
        ("go", "test", "./...", "-count=1"),
        ("npm", "test"),
        ("npm", "test", "--", "--coverage"),
        ("make", "test"),
        ("make", "test", "V=1"),
        ("python", "-m", "unittest", "discover", "-s", "tests"),
        ("python", "-m", "unittest", "discover", "-s", "tests", "-ptest*.py"),
        ("python", "-m", "pytest"),
        ("pytest",),
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
