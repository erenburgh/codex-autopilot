from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from _plan_contract import initialize_verified_project as initialize_project
from codex_autopilot.config import load_config
from codex_autopilot.plan import (
    RESOURCE_KINDS,
    load_plan,
    plan_to_dict,
    save_plan,
    topological_order,
    validate_migrating_plan,
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
from _plan_contract import TEST_OUTCOME_ID, canonicalize_plan, canonical_verification


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
        "produces_outcomes": [TEST_OUTCOME_ID],
    }


def graph() -> dict:
    return canonicalize_plan({
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
            # R8: even a task that gates no dependents does not accept
            # itself. "Needs no verification" is the same self-judgement,
            # declared by the planner in advance.
            task("B"),
            task("C", dependencies=["A", "B"]),
        ],
    })


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


def migrate(raw: dict, profile: str = "adaptive"):
    """Мигрировать план v0.8.

    Происхождение доказывает не формат, а прогон, который мигрируют:
    свежий проект план v0.8 не впускает вовсе. В тестах этот прогон
    изображают его же вехи.
    """

    with tempfile.TemporaryDirectory(prefix="codex-autopilot-v08-input-") as temp:
        state_dir = Path(temp)
        (state_dir / "plan.json").write_text(json.dumps(raw), encoding="utf-8")
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
        return validate_migrating_plan(raw, profile, state_dir=state_dir)


def write_migrated_run_state(state_dir: Path) -> None:
    StateStore(state_dir).save(
        RunState(
            status="DONE",
            phase="DONE",
            execution_strategy="serial",
            max_parallel_workers=1,
            migrated_from_schema=4,
        )
    )


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

        # user_request is not taken from the replanner's reply but carried
        # over from the current plan. A verbatim echo used to be required -
        # on the live run that is 35 234 characters the model does not
        # reproduce: a legitimate plan change was rejected whole. Carrying
        # over is stricter: an echo could be faked, while a field nobody asks
        # for cannot be changed at all.
        changed_request = graph()
        changed_request["graph_version"] = 2
        changed_request["user_request"] = "A replacement request"
        carried = validate_plan_change(current, changed_request, "adaptive")
        self.assertEqual(carried.user_request, current.user_request)

        # goal is short, the model repeats it reliably, and a divergence there
        # means intent, not a copying error.
        changed_goal = graph()
        changed_goal["graph_version"] = 2
        changed_goal["goal"] = "A replacement goal"
        with self.assertRaisesRegex(ValueError, "replace the run goal"):
            validate_plan_change(current, changed_goal, "adaptive")

    def test_generic_role_is_rejected_when_a_concrete_role_contract_is_required(self):
        raw = graph()
        raw["roles"].append(role("legacy-worker", "Legacy serial worker"))
        raw["tasks"][0]["role"] = "legacy-worker"
        with self.assertRaisesRegex(ValueError, "concrete RoleProfile"):
            validate_plan(raw, "adaptive")

    def test_canonical_tasks_require_independent_acceptance_floor(self):
        mutations = [
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
            ["python3", "-m", "pytest", "--collect-only"],
            ["pytest", "--ignore=tests/integration"],
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

    def test_the_transition_table_does_not_promise_what_the_gate_refuses(self):
        """Таблица разрешала IMPLEMENTED -> VERIFIED, а гейт отклонял всегда.

        Две правды об одном ребре: читатель таблицы верил, что переход
        есть. Теперь таблица говорит то же, что R29.
        """

        from codex_autopilot.task_state import TASK_TRANSITIONS

        self.assertNotIn(TaskState.VERIFIED, TASK_TRANSITIONS[TaskState.IMPLEMENTED])

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


class V08CompatibilityTests(unittest.TestCase):
    def test_legacy_plan_becomes_an_explicit_serial_dag(self):
        plan = migrate(legacy_plan(), "adaptive")
        self.assertTrue(plan.legacy_serial)
        self.assertEqual(plan.execution_strategy, "serial")
        self.assertEqual(plan.max_parallel_workers, 1)
        self.assertEqual([item.depends_on for item in plan.tasks], [(), ("M1",), ("M2",)])
        self.assertEqual(topological_order(plan), ("M1", "M2", "M3"))

        state_dir = Path(tempfile.mkdtemp(prefix="codex-autopilot-v08-plan-"))
        save_plan(state_dir, plan)
        write_migrated_run_state(state_dir)
        saved = json.loads((state_dir / "plan.json").read_text())
        self.assertEqual(saved["schema_version"], 3)
        self.assertTrue(saved["compatibility"]["legacy_serial"])
        self.assertNotIn("milestones", saved)
        self.assertEqual(load_plan(state_dir, "adaptive"), plan)

    def test_legacy_migration_preserves_structured_milestone_roles(self):
        raw = legacy_plan(2)
        raw["user_request"] = "Keep the specialist assignment for every milestone."
        raw["roles"] = [
            role("resilience", "Resilience Engineer"),
            role("devops", "DevOps"),
        ]
        raw["milestones"][0]["role"] = "resilience"
        raw["milestones"][1]["role"] = "devops"

        plan = migrate(raw, "adaptive")

        self.assertEqual([item.role for item in plan.tasks], ["resilience", "devops"])
        self.assertEqual(
            [plan.role_map[item.role].name for item in plan.tasks],
            ["Resilience Engineer", "DevOps"],
        )
        self.assertEqual(plan.user_request, raw["user_request"])
        self.assertNotIn("legacy-worker", plan.role_map)
        # The migrated plan is re-read through the same entry as in
        # production: the runtime wrote its provenance, not the sender.
        state_dir = Path(tempfile.mkdtemp(prefix="codex-autopilot-v08-roles-"))
        save_plan(state_dir, plan)
        write_migrated_run_state(state_dir)
        restored = load_plan(state_dir, "adaptive")
        self.assertEqual(restored, plan)

    def test_legacy_structured_roles_are_never_inferred_or_collapsed(self):
        raw = legacy_plan(1)
        raw["roles"] = [role("ux", "UX Designer")]
        with self.assertRaisesRegex(ValueError, "role is required"):
            migrate(raw, "adaptive")

        raw["milestones"][0]["role"] = "ux"
        raw["roles"] = [role("legacy-worker", "Legacy serial worker")]
        raw["milestones"][0]["role"] = "legacy-worker"
        with self.assertRaisesRegex(ValueError, "generic legacy-worker"):
            migrate(raw, "adaptive")

    def test_v08_config_without_runtime_section_is_fail_closed_serial(self):
        root = Path(tempfile.mkdtemp(prefix="codex-autopilot-v08-config-"))
        state_dir = root / ".codex-autopilot"
        state_dir.mkdir()
        (state_dir / "config.toml").write_text(
            "\n".join(
                [
                    'profile = "adaptive"',
                    "",
                    "[project]",
                    f"root = {json.dumps(str(root))}",
                    "",
                    "[desktop]",
                    'permission_profile = ":workspace"',
                    f"skill_path = {json.dumps(str(SKILL))}",
                ]
            ),
            encoding="utf-8",
        )
        cfg = load_config(root)
        self.assertEqual(cfg.runtime.execution_strategy, "serial")
        self.assertEqual(cfg.runtime.max_parallel_workers, 1)
        self.assertEqual(cfg.runtime.computer_use_slots, 1)

    def test_v08_state_schema_migrates_in_memory_without_parallelism(self):
        state_dir = Path(tempfile.mkdtemp(prefix="codex-autopilot-v08-state-"))
        (state_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "schema_version": 4,
                    "run_id": "legacy-run",
                    "status": "RUNNING",
                    "phase": "RUNNING_TURN",
                    "milestone_index": 1,
                    "milestone_id": "M2",
                    "attempt": 2,
                    "worker_history": [
                        {"milestone_id": "M1", "status": "ROTATE"},
                        {"milestone_id": "M2", "status": "RUNNING"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        state = StateStore(state_dir).load()
        self.assertEqual(state.schema_version, 5)
        self.assertEqual(state.migrated_from_schema, 4)
        self.assertEqual(state.execution_strategy, "serial")
        self.assertEqual(state.max_parallel_workers, 1)
        self.assertEqual(state.task_states, {"M1": "VERIFIED", "M2": "RUNNING"})
        self.assertEqual(state.active_task_ids, ["M2"])

    def test_legacy_cursor_maps_to_complete_chain_states(self):
        plan = migrate(legacy_plan(), "adaptive")
        states = migrate_v08_task_states(
            plan,
            {
                "status": "RUNNING",
                "phase": "RUNNING_TURN",
                "milestone_index": 1,
                "milestone_id": "M2",
                "worker_history": [{"milestone_id": "M1", "status": "ROTATE"}],
            },
        )
        self.assertEqual(states, {"M1": "VERIFIED", "M2": "RUNNING", "M3": "WAITING"})

    def test_new_bootstrap_persists_explicit_runtime_limits(self):
        root = Path(tempfile.mkdtemp(prefix="codex-autopilot-v09-bootstrap-"))
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        state_dir = root / ".codex-autopilot"
        state_dir.mkdir()
        plan_file = state_dir / "bootstrap-plan.json"
        plan_file.write_text(json.dumps(graph()), encoding="utf-8")
        initialize_project(root, plan_file, profile="adaptive", skill_path=SKILL)

        cfg = load_config(root)
        state = StateStore(state_dir).load()
        self.assertEqual(cfg.runtime.execution_strategy, "parallel")
        self.assertEqual(cfg.runtime.max_parallel_workers, 3)
        self.assertEqual(state.execution_strategy, "parallel")
        self.assertEqual(state.task_states, {"A": "READY", "B": "READY", "C": "WAITING"})

    def test_serial_run_state_rejects_multiple_active_tasks(self):
        state_dir = Path(tempfile.mkdtemp(prefix="codex-autopilot-invalid-state-"))
        state = RunState(
            execution_strategy="serial",
            max_parallel_workers=2,
            task_states={"A": "RUNNING", "B": "RUNNING"},
            active_task_ids=["A", "B"],
        )
        with self.assertRaisesRegex(ValueError, "serial"):
            StateStore(state_dir).save(state)


if __name__ == "__main__":
    unittest.main()
