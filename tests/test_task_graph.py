from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.config import load_config
from codex_autopilot.plan import (
    RESOURCE_KINDS,
    load_plan,
    plan_to_dict,
    save_plan,
    topological_order,
    validate_plan,
    validate_plan_change,
)
from codex_autopilot.run_state import RunState, StateStore
from codex_autopilot.task_state import (
    IllegalTaskTransition,
    TaskState,
    dependencies_eligible,
    initial_task_states,
    migrate_v08_task_states,
    transition_task,
    validate_task_states,
)
from _plan_contract import canonical_verification


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md"


def role(role_id: str, name: str | None = None) -> dict:
    return {
        "id": role_id,
        "name": name or role_id.title(),
        "responsibilities": [f"Own {role_id} work."],
        "domain_focus": ["runtime contracts"],
        "preferred_tools": ["repository", "tests"],
        "context_priorities": ["verified dependency outputs"],
        "verification_expectations": ["Attach deterministic evidence."],
    }


def task(
    task_id: str,
    *,
    dependencies: list[str] | None = None,
    role_id: str = "builder",
    required_verification: bool = True,
    policy: str = "independent",
) -> dict:
    verification = canonical_verification(
        verifier_role="reviewer",
        max_revision_attempts=3,
    )
    verification["policy"] = policy
    verification["required"] = required_verification
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Implement {task_id}.",
        "definition_of_done": [f"{task_id} is tested."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files and deterministic tests are sufficient.",
        "reasoning": "high",
        "role": role_id,
        "depends_on": dependencies or [],
        "priority": 20,
        "verification": verification,
        "resources": [],
        "required_capabilities": ["python"],
        "context": {
            "memory_queries": [task_id],
            "memory_record_ids": [],
            "dependency_outputs": dependencies or [],
            "max_memory_records": 6,
            "max_dependency_outputs": 4,
        },
        "outputs": [
            {
                "id": "implementation",
                "description": f"Implementation output for {task_id}.",
                "path": f"outputs/{task_id}.json",
                "required": True,
            }
        ],
        "tags": ["implementation"],
    }


def graph() -> dict:
    return {
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Ship a dependency-aware runtime.",
        "user_request": "Build the requested dependency-aware runtime exactly as specified.",
        "model_strategy": "auto",
        "execution_strategy": "parallel",
        "max_parallel_workers": 3,
        "computer_use_slots": 1,
        "roles": [role("builder"), role("reviewer")],
        "tasks": [
            task("A"),
            # R8: даже задача, не гейтящая зависимости, не принимает
            # сама себя. "Не требует верификации" - тот же самосуд,
            # объявленный планировщиком заранее.
            task("B"),
            task("C", dependencies=["A", "B"]),
        ],
    }


def legacy_plan(count: int = 3) -> dict:
    return {
        "schema_version": 2,
        "goal": "Finish serial milestones.",
        "model_strategy": "auto",
        "milestones": [
            {
                "id": f"M{index}",
                "title": f"Milestone {index}",
                "objective": f"Complete milestone {index}.",
                "definition_of_done": ["Verified evidence exists."],
                "execution_mode": "code",
                "execution_mode_reason": "Files and tests are sufficient.",
                "reasoning": "medium",
            }
            for index in range(1, count + 1)
        ],
    }


class TaskGraphSchemaTests(unittest.TestCase):
    def test_schema3_requires_verbatim_user_request(self):
        raw = graph()
        raw.pop("user_request")
        with self.assertRaisesRegex(ValueError, "plan.user_request"):
            validate_plan(raw, "adaptive")

    def test_canonical_schema_round_trips_all_contract_sections(self):
        raw = graph()
        raw["tasks"][0]["verification"].update(
            {
                "execution_mode": "code",
                "execution_mode_reason": "Independent review is repository-based.",
                "reasoning": "xhigh",
            }
        )
        raw["tasks"][0]["verification"]["deterministic_checks"].extend(
            [
                {
                    "id": "artifact",
                    "kind": "artifact",
                    "description": "Required output exists.",
                    "path": "outputs/A.json",
                },
                {
                    "id": "evidence",
                    "kind": "evidence",
                    "description": "Project Memory contains execution evidence.",
                },
            ]
        )
        raw["tasks"][0]["resources"] = [
            {
                "id": f"claim-{index}",
                "kind": kind,
                "target": f"target/{kind}",
                "access": "read" if index == 0 else "write" if index == 1 else "exclusive",
                "description": f"Exercise {kind} claim.",
            }
            for index, kind in enumerate(sorted(RESOURCE_KINDS))
        ]

        plan = validate_plan(raw, "adaptive")
        restored = validate_plan(plan_to_dict(plan), "adaptive")

        self.assertEqual(restored, plan)
        self.assertEqual(
            restored.user_request,
            "Build the requested dependency-aware runtime exactly as specified.",
        )
        self.assertEqual(topological_order(plan), ("A", "B", "C"))
        self.assertEqual({claim.kind for claim in plan.tasks[0].resources}, RESOURCE_KINDS)
        self.assertEqual(plan.roles[0].preferred_tools, ("repository", "tests"))
        self.assertEqual(plan.tasks[0].context.max_memory_records, 6)
        self.assertEqual(plan.tasks[0].outputs[0].id, "implementation")

    def test_schema_rejects_unknown_fields_and_malformed_nested_contracts(self):
        raw = graph()
        raw["tasks"][0]["surprise"] = True
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            validate_plan(raw, "adaptive")

        raw = graph()
        raw["tasks"][2]["verification"]["deterministic_checks"] = []
        with self.assertRaisesRegex(ValueError, "suite"):
            validate_plan(raw, "adaptive")

        raw = graph()
        raw["tasks"][0]["resources"] = [
            {"id": "source", "kind": "path", "target": "src", "access": "shared-write"}
        ]
        with self.assertRaisesRegex(ValueError, "access"):
            validate_plan(raw, "adaptive")

    def test_reference_validation_covers_dependencies_roles_verifiers_and_context(self):
        mutations = [
            (lambda raw: raw["tasks"][0].update(role="missing"), "unknown role"),
            (lambda raw: raw["tasks"][0].update(depends_on=["missing"]), "unknown dependency"),
            (
                lambda raw: raw["tasks"][0]["verification"].update(verifier_role="missing"),
                "unknown role",
            ),
            (
                lambda raw: raw["tasks"][2]["context"].update(dependency_outputs=["missing"]),
                "direct dependencies",
            ),
        ]
        for mutate, message in mutations:
            with self.subTest(message=message):
                raw = graph()
                mutate(raw)
                with self.assertRaisesRegex(ValueError, message):
                    validate_plan(raw, "adaptive")

    def test_initial_load_rejects_cycles_with_a_readable_path(self):
        raw = graph()
        raw["tasks"][0]["depends_on"] = ["C"]
        raw["tasks"][0]["context"]["dependency_outputs"] = ["C"]
        with self.assertRaisesRegex(ValueError, r"cycle: A -> C -> A"):
            validate_plan(raw, "adaptive")

    def test_plan_change_requires_next_version_and_revalidates_before_write(self):
        current = validate_plan(graph(), "adaptive")
        wrong_version = graph()
        with self.assertRaisesRegex(ValueError, "increment exactly once"):
            validate_plan_change(current, wrong_version, "adaptive")

        state_dir = Path(tempfile.mkdtemp(prefix="codex-autopilot-plan-change-"))
        save_plan(state_dir, current)
        before = (state_dir / "plan.json").read_bytes()
        cyclic = graph()
        cyclic["graph_version"] = 2
        cyclic["tasks"][0]["depends_on"] = ["C"]
        cyclic["tasks"][0]["context"]["dependency_outputs"] = ["C"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_plan_change(current, cyclic, "adaptive")
        self.assertEqual((state_dir / "plan.json").read_bytes(), before)

        valid = graph()
        valid["graph_version"] = 2
        changed = validate_plan_change(current, valid, "adaptive")
        save_plan(state_dir, changed)
        self.assertEqual(changed.graph_version, 2)
        self.assertEqual(load_plan(state_dir, "adaptive").graph_version, 2)

        # user_request не берётся из ответа реплэннера, а переносится из
        # текущего плана. Прежде требовалось дословное эхо - и в живом
        # прогоне это 35 234 символа, которые модель не воспроизводит:
        # законная смена плана отклонялась целиком. Перенос строже: эхо
        # можно было подделать, а поле, которого не спрашивают, изменить
        # нельзя вовсе.
        changed_request = graph()
        changed_request["graph_version"] = 2
        changed_request["user_request"] = "A replacement request"
        carried = validate_plan_change(current, changed_request, "adaptive")
        self.assertEqual(carried.user_request, current.user_request)

        # goal короткий, модель повторяет его надёжно, и расхождение там
        # означает намерение, а не ошибку копирования.
        changed_goal = graph()
        changed_goal["graph_version"] = 2
        changed_goal["goal"] = "A replacement goal"
        with self.assertRaisesRegex(ValueError, "replace the run goal"):
            validate_plan_change(current, changed_goal, "adaptive")

    def test_canonical_graph_cannot_forge_legacy_migration_provenance(self):
        current = validate_plan(graph(), "adaptive")
        counterfeit = graph()
        counterfeit["graph_version"] = current.graph_version + 1
        counterfeit["execution_strategy"] = "serial"
        counterfeit["max_parallel_workers"] = 1
        counterfeit["compatibility"] = {
            "migrated_from_schema": 2,
            "legacy_serial": True,
        }
        counterfeit_verification = counterfeit["tasks"][0]["verification"]
        counterfeit_verification.update(
            policy="self",
            deterministic_checks=[],
            max_revision_attempts=0,
        )

        with self.assertRaisesRegex(ValueError, "not part of the canonical schema"):
            validate_plan(counterfeit, "adaptive")
        with self.assertRaisesRegex(ValueError, "not part of the canonical schema"):
            validate_plan_change(current, counterfeit, "adaptive")

    def test_generic_role_is_rejected_when_a_concrete_role_contract_is_required(self):
        raw = graph()
        raw["roles"].append(role("legacy-worker", "Legacy serial worker"))
        raw["tasks"][0]["role"] = "legacy-worker"
        with self.assertRaisesRegex(ValueError, "concrete RoleProfile"):
            validate_plan(raw, "adaptive")

    def test_canonical_tasks_require_independent_acceptance_floor(self):
        mutations = [
            (
                lambda verification: verification.update(policy="self"),
                "must be \\\"independent\\\"",
            ),
            (
                lambda verification: verification.update(policy="deterministic"),
                "must be \\\"independent\\\"",
            ),
            (
                lambda verification: verification.update(policy="auto"),
                "must be \\\"independent\\\"",
            ),
            (
                lambda verification: verification.update(required=False),
                "required must be true",
            ),
            (
                lambda verification: verification.update(max_revision_attempts=1),
                "at least 2",
            ),
            (
                lambda verification: verification.pop("max_revision_attempts"),
                "must be declared",
            ),
            (
                lambda verification: verification.update(deterministic_checks=[]),
                "full-suite deterministic check",
            ),
        ]
        for mutate, message in mutations:
            with self.subTest(message=message):
                raw = graph()
                mutate(raw["tasks"][0]["verification"])
                with self.assertRaisesRegex(ValueError, message):
                    validate_plan(raw, "adaptive")

    def test_canonical_task_cannot_fall_back_to_default_self_acceptance(self):
        raw = graph()
        raw["tasks"][0].pop("verification")
        with self.assertRaisesRegex(ValueError, "verification must be an object"):
            validate_plan(raw, "adaptive")

        raw = graph()
        raw["tasks"][0]["verification"].pop("policy")
        with self.assertRaisesRegex(ValueError, "policy must be one of"):
            validate_plan(raw, "adaptive")

    def test_canonical_suite_check_requires_clean_identity_environment(self):
        for missing in (
            "CODEX_THREAD_ID=",
            "CODEX_TURN_ID=",
            "CODEX_SESSION_ID=",
        ):
            with self.subTest(missing=missing):
                raw = graph()
                argv = raw["tasks"][0]["verification"]["deterministic_checks"][0]["argv"]
                argv.remove(missing)
                with self.assertRaisesRegex(ValueError, "reset CODEX_THREAD_ID="):
                    validate_plan(raw, "adaptive")

    def test_canonical_suite_check_rejects_shell_or_nonzero_success_contract(self):
        raw = graph()
        check = raw["tasks"][0]["verification"]["deterministic_checks"][0]
        check["argv"] = ["sh", "-c", "python3 -m unittest discover -s tests"]
        with self.assertRaisesRegex(ValueError, "launched through env"):
            validate_plan(raw, "adaptive")

        raw = graph()
        raw["tasks"][0]["verification"]["deterministic_checks"][0][
            "expected_exit_code"
        ] = 1
        with self.assertRaisesRegex(ValueError, "successful command"):
            validate_plan(raw, "adaptive")

    def test_canonical_suite_check_rejects_noop_or_mislabelled_commands(self):
        for command in (["true"], ["python3", "-c", "raise SystemExit(0)"]):
            with self.subTest(command=command):
                raw = graph()
                argv = raw["tasks"][0]["verification"]["deterministic_checks"][0][
                    "argv"
                ]
                argv[4:] = command
                with self.assertRaisesRegex(
                    ValueError,
                    "full-suite deterministic check",
                ):
                    validate_plan(raw, "adaptive")

        raw = graph()
        argv = raw["tasks"][0]["verification"]["deterministic_checks"][0][
            "argv"
        ]
        argv[4:] = ["/usr/bin/uname"]
        with self.assertRaisesRegex(ValueError, "full-suite deterministic check"):
            validate_plan(raw, "adaptive")

    def test_canonical_suite_check_rejects_partial_runner_options(self):
        commands = (
            [
                "python3",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-k",
                "one_case",
            ],
            [
                "python3",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-kone_case",
            ],
            ["python3", "-m", "pytest", "--collect-only"],
            ["python3", "-m", "pytest", "-kone_case"],
            ["pytest", "-mnot_slow"],
            ["pytest", "--ignore=tests/integration"],
            ["python3", "tests/project_suite.py", "-kone_case"],
        )
        for command in commands:
            with self.subTest(command=command):
                raw = graph()
                argv = raw["tasks"][0]["verification"]["deterministic_checks"][0][
                    "argv"
                ]
                argv[4:] = command
                with self.assertRaisesRegex(
                    ValueError,
                    "full-suite deterministic check",
                ):
                    validate_plan(raw, "adaptive")

    def test_canonical_suite_check_rejects_project_controlled_env_wrapper(self):
        raw = graph()
        argv = raw["tasks"][0]["verification"]["deterministic_checks"][0]["argv"]
        argv[0] = "tools/env"
        with self.assertRaisesRegex(ValueError, "launched through env"):
            validate_plan(raw, "adaptive")

        raw = graph()
        argv = raw["tasks"][0]["verification"]["deterministic_checks"][0][
            "argv"
        ]
        argv[4:] = [
            "python3",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_one.py",
        ]
        with self.assertRaisesRegex(ValueError, "full-suite deterministic check"):
            validate_plan(raw, "adaptive")


class TaskStateContractTests(unittest.TestCase):
    def setUp(self):
        self.plan = validate_plan(graph(), "adaptive")

    def test_implemented_is_not_verified_when_verification_is_required(self):
        states = initial_task_states(self.plan)
        states = transition_task(self.plan, states, "A", TaskState.RUNNING)
        states = transition_task(self.plan, states, "A", TaskState.IMPLEMENTED)
        self.assertFalse(dependencies_eligible(self.plan, "C", states))
        with self.assertRaisesRegex(IllegalTaskTransition, "IMPLEMENTED cannot"):
            transition_task(self.plan, states, "A", TaskState.VERIFIED)

        states = transition_task(self.plan, states, "A", TaskState.VERIFYING)
        states = transition_task(self.plan, states, "A", TaskState.VERIFIED)
        self.assertFalse(dependencies_eligible(self.plan, "C", states))

        states = transition_task(self.plan, states, "B", TaskState.RUNNING)
        states = transition_task(self.plan, states, "B", TaskState.IMPLEMENTED)
        self.assertFalse(dependencies_eligible(self.plan, "C", states))
        states = transition_task(self.plan, states, "B", TaskState.VERIFYING)
        states = transition_task(self.plan, states, "B", TaskState.VERIFIED)
        self.assertTrue(dependencies_eligible(self.plan, "C", states))
        states = transition_task(self.plan, states, "C", TaskState.READY)
        self.assertEqual(states["C"], TaskState.READY.value)

    def test_successful_worker_cannot_skip_implemented(self):
        states = initial_task_states(self.plan)
        states = transition_task(self.plan, states, "A", TaskState.RUNNING)
        with self.assertRaisesRegex(IllegalTaskTransition, "RUNNING -> VERIFIED"):
            transition_task(self.plan, states, "A", TaskState.VERIFIED)

    def test_state_map_rejects_unknown_states_and_unmet_active_dependencies(self):
        with self.assertRaisesRegex(ValueError, "unknown task state"):
            validate_task_states(self.plan, {"A": "MAGIC", "B": "READY", "C": "WAITING"})
        with self.assertRaisesRegex(ValueError, "unmet dependencies"):
            validate_task_states(self.plan, {"A": "READY", "B": "READY", "C": "RUNNING"})


# Класс V08CompatibilityTests удалён вместе с форматом v0.8. Он проверял
# впуск планов, где задача принимала собственную работу; формат снят, и
# проверять больше нечего.

class CompactPatternIsNotAFullSuite(unittest.TestCase):
    """Фильтр в слитной форме - подмножество, а не полный набор.

    Замечание приёмки M1: «Проверка полного suite принимает компактный
    unittest -p фильтр». Формы `-p X` и `-p=X` отсекались, а слитная
    `-pX` проходила как полный прогон. Проверка, принимающая кусок
    набора за целое, не доказывает ничего - а на ней держится допуск
    задачи к приёмке.
    """

    def test_a_separate_pattern_is_rejected(self) -> None:
        from codex_autopilot.plan import _unittest_discovers_test_root

        self.assertFalse(
            _unittest_discovers_test_root(("discover", "-s", "tests", "-p", "test_plan*.py"))
        )

    def test_an_attached_pattern_is_rejected_too(self) -> None:
        from codex_autopilot.plan import _unittest_discovers_test_root

        self.assertFalse(
            _unittest_discovers_test_root(("discover", "-s", "tests", "-ptest_plan*.py"))
        )

    def test_the_default_pattern_still_counts_as_full(self) -> None:
        from codex_autopilot.plan import _unittest_discovers_test_root

        self.assertTrue(_unittest_discovers_test_root(("discover", "-s", "tests")))
        self.assertTrue(
            _unittest_discovers_test_root(("discover", "-s", "tests", "-ptest*.py"))
        )
