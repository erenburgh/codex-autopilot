from __future__ import annotations

from pathlib import Path
import unittest

from codex_autopilot.ai_studio import AIStudioRuntime
from codex_autopilot.cli import parser as cli_parser
from codex_autopilot.plan import Plan, validate_plan
from codex_autopilot.project_association import resolve_preflight_project
from codex_autopilot.run_state import RunState
from codex_autopilot.scheduler import schedule
from codex_autopilot.task_state import IllegalTaskTransition, TaskState, initial_task_states, transition_task
from codex_autopilot.thread_titles import (
    implementation_thread_title,
    planner_thread_title,
    replanner_thread_title,
    revision_thread_title,
    verifier_thread_title,
)


def _role(role_id: str, name: str) -> dict[str, object]:
    return {
        "id": role_id,
        "name": name,
        "responsibilities": [f"Own {name} outcomes."],
    }


def _task(
    task_id: str,
    role_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    execution_mode: str = "code",
    verifier_role: str | None = None,
    verifier_mode: str | None = None,
    policy: str = "independent",
) -> dict[str, object]:
    verification: dict[str, object] = {
        "policy": policy,
        "required": True,
        "max_revision_attempts": 2,
    }
    if policy == "deterministic":
        verification["deterministic_checks"] = [
            {
                "id": "exact-artifact",
                "kind": "artifact",
                "description": "The required artifact exists.",
                "path": "artifact.txt",
            }
        ]
    if verifier_role:
        verification["verifier_role"] = verifier_role
    if verifier_mode:
        verification.update(
            {
                "execution_mode": verifier_mode,
                "execution_mode_reason": "Visual judgment requires Computer Use.",
                "reasoning": "high",
            }
        )
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Complete {task_id}.",
        "definition_of_done": [f"{task_id} is verified."],
        "execution_mode": execution_mode,
        "execution_mode_reason": (
            "The task requires Computer Use."
            if execution_mode == "computer_use"
            else "Repository checks are sufficient."
        ),
        "reasoning": "medium",
        "role": role_id,
        "depends_on": list(depends_on),
        "priority": 0,
        "verification": verification,
        "resources": [],
        "required_capabilities": [],
        "context": {"dependency_outputs": list(depends_on)},
        "outputs": [],
        "tags": [],
    }


def _plan(
    tasks: list[dict[str, object]],
    *,
    roles: list[dict[str, object]] | None = None,
    include_execution_defaults: bool = True,
) -> Plan:
    payload: dict[str, object] = {
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Exercise the v0.9 acceptance contract.",
        "user_request": "Exercise the v0.9 acceptance contract exactly.",
        "model_strategy": "auto",
        "roles": roles or [_role("builder", "Builder")],
        "tasks": tasks,
    }
    if include_execution_defaults:
        payload.update(
            {
                "execution_strategy": "parallel",
                "max_parallel_workers": 3,
                "computer_use_slots": 1,
            }
        )
    return validate_plan(payload, "adaptive")


def _state(plan: Plan) -> RunState:
    return RunState(
        graph_version=plan.graph_version,
        execution_strategy=plan.execution_strategy,
        max_parallel_workers=plan.max_parallel_workers,
        computer_use_slots=plan.computer_use_slots,
        task_states=initial_task_states(plan),
        task_attempts={task.id: 0 for task in plan.tasks},
        task_revisions={task.id: 0 for task in plan.tasks},
    )


class V09ContractRegressionTests(unittest.TestCase):
    """Exact source-request contracts that the candidate must satisfy."""

    def test_auto_is_the_default_product_execution_strategy(self) -> None:
        plan = _plan(
            [_task("T1", "builder")],
            include_execution_defaults=False,
        )
        self.assertEqual(plan.execution_strategy, "auto")

    def test_start_skill_defaults_to_the_desktop_owned_v09_runtime(self) -> None:
        """Поверхность больше не выбирается: она одна.

        Флаг --worker-surface снят вместе с headless-путём, который не мог
        выполниться. Контракт теперь в том, что другой поверхности нет.
        """

        from codex_autopilot.config import DESKTOP_OWNED_SURFACE, WORKER_SURFACES

        self.assertEqual(WORKER_SURFACES, {DESKTOP_OWNED_SURFACE})
        args = cli_parser().parse_args(["start-skill", "--plan-file", "plan.json"])
        self.assertFalse(hasattr(args, "worker_surface"))

    def test_thread_titles_match_the_required_human_readable_shapes(self) -> None:
        self.assertEqual(
            implementation_thread_title(
                "T44", "Create Weapon Model", role_name="3D Artist"
            ),
            "3D Artist | T44 | Create Weapon Model",
        )
        self.assertEqual(
            verifier_thread_title(
                "T44", "Create Weapon Model", role_name="3D Artist"
            ),
            "3D Artist Verifier | T44 | Verify Weapon Model",
        )
        self.assertEqual(
            revision_thread_title(
                "T44", 1, "Create Weapon Model", role_name="3D Artist"
            ),
            "3D Artist | T44-R1 | Revise Weapon Model",
        )
        self.assertEqual(
            planner_thread_title("Build Analytics Dashboard"),
            "Planner | PLAN | Build Analytics Dashboard",
        )
        self.assertEqual(
            replanner_thread_title("PC-04", "Add Missing Migration Step"),
            "Planner | PC-04 | Add Missing Migration Step",
        )

    def test_unrelated_initiating_project_is_not_a_target_fallback(self) -> None:
        project, source = resolve_preflight_project(
            Path("/target/repository"),
            [
                {
                    "id": "initiating-only",
                    "name": "Unrelated initiating project",
                    "roots": [{"path": "/initiating/project"}],
                }
            ],
        )
        self.assertIsNone(project)
        self.assertIsNone(source)

    def test_deterministic_policy_still_requires_the_verifier(self) -> None:
        plan = _plan([_task("T1", "builder", policy="deterministic")])
        states = {"T1": TaskState.IMPLEMENTED.value}
        # Product decision of 2026-09-12 (rule R29): the verifier is always
        # required. Deterministic checks are admission to judgement, never a
        # substitute for it. Green checks mean "ready to show the lead", not
        # "done". The alternative requires somebody to decide which contracts
        # are exhaustively machine-checkable, and that somebody would be the
        # planner -- a model. That is self-assessment moved one level up.
        with self.assertRaises(IllegalTaskTransition):
            transition_task(plan, states, "T1", TaskState.VERIFIED)
        # The lawful path stays IMPLEMENTED -> VERIFYING -> VERIFIED.
        via_verifier = transition_task(plan, states, "T1", TaskState.VERIFYING)
        via_verifier = transition_task(plan, via_verifier, "T1", TaskState.VERIFIED)
        self.assertEqual(via_verifier["T1"], TaskState.VERIFIED.value)


class AIStudioAcceptanceShapeTests(unittest.TestCase):
    def test_shape_a_independent_implementation_branches_then_integration(self) -> None:
        plan = _plan(
            [
                _task("backend", "backend"),
                _task("frontend", "frontend"),
                _task("integration", "integrator", depends_on=("backend", "frontend")),
            ],
            roles=[
                _role("backend", "Backend Engineer"),
                _role("frontend", "Frontend Engineer"),
                _role("integrator", "Release Integrator"),
            ],
        )
        state = _state(plan)
        self.assertEqual(
            schedule(plan, state).selected_task_ids,
            ("backend", "frontend"),
        )
        state.task_states.update(
            {
                "backend": TaskState.VERIFIED.value,
                "frontend": TaskState.VERIFIED.value,
            }
        )
        self.assertEqual(schedule(plan, state).selected_task_ids, ("integration",))

    def test_shape_b_research_analysis_fact_verification_pipeline(self) -> None:
        plan = _plan(
            [
                _task("research", "researcher"),
                _task("analysis", "analyst", depends_on=("research",)),
                _task("fact-check", "fact-checker", depends_on=("analysis",)),
            ],
            roles=[
                _role("researcher", "Researcher"),
                _role("analyst", "Analyst"),
                _role("fact-checker", "Fact Verification Specialist"),
            ],
        )
        state = _state(plan)
        self.assertEqual(schedule(plan, state).selected_task_ids, ("research",))
        state.task_states["research"] = TaskState.VERIFIED.value
        self.assertEqual(schedule(plan, state).selected_task_ids, ("analysis",))
        state.task_states["analysis"] = TaskState.VERIFIED.value
        self.assertEqual(schedule(plan, state).selected_task_ids, ("fact-check",))

    def test_shape_c_code_continues_while_computer_use_is_serialized(self) -> None:
        roles = [
            _role("builder", "Backend Engineer"),
            _role("operator", "Desktop Operator"),
            _role("visual-qa", "Visual QA"),
        ]
        plan = _plan(
            [
                _task("code", "builder"),
                _task(
                    "gui-a",
                    "operator",
                    execution_mode="computer_use",
                    verifier_role="visual-qa",
                    verifier_mode="computer_use",
                ),
                _task("gui-b", "operator", execution_mode="computer_use"),
            ],
            roles=roles,
        )
        decision = schedule(plan, _state(plan))
        self.assertEqual(decision.selected_task_ids, ("code", "gui-a"))
        self.assertIn("capability_capacity:computer_use", decision.reasons_for("gui-b"))

        runtime = AIStudioRuntime(
            plan,
            Path.cwd(),
            language="en",
            skill_path=Path(__file__),
        )
        self.assertEqual(runtime.route("code").model_id, "gpt-5.6-sol")
        self.assertEqual(runtime.route("gui-a").model_id, "gpt-6-astra")
        self.assertEqual(
            runtime.route("gui-a", phase="verification").role_id,
            "visual-qa",
        )


if __name__ == "__main__":
    unittest.main()
