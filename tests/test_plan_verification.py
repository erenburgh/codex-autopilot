from __future__ import annotations

import copy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from _plan_contract import (
    canonical_plan_verification,
    canonical_verification,
    initialize_verified_project,
)
from _relay import reserve_ready_frontier
from codex_autopilot.config import load_config
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.plan import plan_to_dict, validate_plan
from codex_autopilot.plan_verification import (
    FULL_PLAN_REVALIDATION,
    PLAN_PATCH_VERIFICATION,
    PlanVerificationError,
    build_plan_verification_prompt,
    deterministic_plan_issues,
    load_active_memory_constraints,
    plan_change_verification_mode,
    require_plan_verified,
)
from codex_autopilot.preflight import PreflightError, run_preflight
from codex_autopilot.run_state import RunState, StateStore
from codex_autopilot.scheduler import schedule


PRIVATE_PLANNER_MARKER = "PRIVATE-PLANNER-RATIONALE-MUST-NOT-LEAK"


def task(
    task_id: str,
    *,
    outcomes: tuple[str, ...] = ("playable",),
    depends_on: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Produce the bounded result for {task_id}.",
        "definition_of_done": [f"{task_id} is independently accepted."],
        "execution_mode": "code",
        "execution_mode_reason": PRIVATE_PLANNER_MARKER,
        "reasoning": "max",
        "role": "builder",
        "depends_on": list(depends_on),
        "priority": 0,
        "verification": canonical_verification(),
        "resources": [
            {
                "id": "private-resource",
                "kind": "directory",
                "target": f"private/{PRIVATE_PLANNER_MARKER}",
                "access": "write",
            }
        ],
        "required_capabilities": [],
        "context": {},
        "outputs": [
            {
                "id": f"{task_id.lower()}-artifact",
                "description": f"Artifact produced by {task_id}.",
                "path": f"dist/{task_id.lower()}.bin",
                "required": True,
            }
        ],
        "tags": [PRIVATE_PLANNER_MARKER],
        "produces_outcomes": list(outcomes),
        "acceptance_class": "mixed",
    }


def graph(tasks: list[dict[str, object]], *, version: int = 1) -> dict[str, object]:
    return {
        "schema_version": 3,
        "graph_version": version,
        "goal": "Ship the playable packaged result.",
        "user_request": PRIVATE_PLANNER_MARKER,
        "model_strategy": "auto",
        "execution_strategy": "auto",
        "max_parallel_workers": 2,
        "computer_use_slots": 1,
        "goal_contract": {
            "required_outcomes": [
                {"id": "playable", "description": "The result is playable."},
                {"id": "packaged", "description": "The result is packaged."},
            ],
            "deliverables": [
                {
                    "id": "build",
                    "description": "A launchable build exists.",
                    "path": "dist/build.bin",
                }
            ],
            "constraints": [
                {"id": "offline", "description": "The build works offline."}
            ],
            "global_acceptance": [
                {"id": "launches", "description": "The build launches."}
            ],
        },
        "roles": [
            {
                "id": "builder",
                "name": "Builder",
                "responsibilities": [PRIVATE_PLANNER_MARKER],
            }
        ],
        "tasks": tasks,
    }


def complete_plan(*, version: int = 1):
    return validate_plan(
        graph([task("A", outcomes=("playable", "packaged"))], version=version),
        "adaptive",
    )


class PlanVerificationAcceptanceTests(unittest.TestCase):
    def test_t2_incomplete_plan_fails_coverage_before_app_server_or_reservation(self) -> None:
        proposed = validate_plan(graph([task("A", outcomes=("playable",))]), "adaptive")
        issues = deterministic_plan_issues(proposed)
        self.assertEqual([item.category for item in issues], ["coverage"])
        self.assertEqual(issues[0].outcome_ids, ("packaged",))

        class AppServerMustNotStart:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("coverage failure reached App Server")

        with tempfile.TemporaryDirectory(prefix="plan-verification-t2-") as raw:
            root = Path(raw)
            (root / ".git").mkdir()
            with self.assertRaisesRegex(PreflightError, "coverage"):
                run_preflight(
                    root,
                    plan=proposed,
                    profile="adaptive",
                    skill_path=root / "not-needed-before-admission.md",
                    binary="/bin/echo",
                    client_factory=AppServerMustNotStart,
                    emit=None,
                    desktop_project_id="must-not-be-used",
                )
            self.assertFalse((root / ".codex-autopilot").exists())

    def test_scheduler_rejects_a_proposed_graph_without_plan_verified_receipt(self) -> None:
        proposed = complete_plan()
        state = RunState(
            run_id="run",
            graph_version=1,
            task_states={"A": "WAITING"},
            max_parallel_workers=2,
            task_ready_since={},
        )
        before = copy.deepcopy(asdict(state))

        with self.assertRaisesRegex(PlanVerificationError, "PLAN_PROPOSED"):
            schedule(proposed, state)

        self.assertEqual(asdict(state), before)

    def test_shared_reservation_frontier_cannot_bypass_plan_verified(self) -> None:
        with tempfile.TemporaryDirectory(prefix="plan-verification-frontier-") as raw:
            root = Path(raw)
            (root / ".git").mkdir()
            skill = root / "SKILL.md"
            skill.write_text("# fixture\n", encoding="utf-8")
            plan_file = root / "plan.json"
            plan_file.write_text(
                json.dumps(plan_to_dict(complete_plan())), encoding="utf-8"
            )
            initialize_verified_project(
                root,
                plan_file,
                profile="adaptive",
                skill_path=skill,
                desktop_project_id="desktop-project",
            )
            cfg = load_config(root)
            store = StateStore(root / ".codex-autopilot")
            state = store.load()
            state.plan_verification = None
            store.save(state)

            # The frontier builds nothing from an unverified graph. It used to
            # raise, which rolled back any completion that ended in this
            # reservation and told nobody; now the refusal is a stop - a
            # ticket that holds every task, and the on-call, whose prompt is
            # the incident package and never a task of this graph.
            reserved = reserve_ready_frontier(cfg, hook_gate=lambda _cfg: None)

            self.assertEqual([item.kind for item in reserved], ["pipeline_engineer"])
            self.assertEqual(
                [item["kind"] for item in store.load().worker_sessions],
                ["pipeline_engineer"],
            )
            from codex_autopilot.pipeline_engineer import PipelineIncidentStore

            ticket = PipelineIncidentStore(root / ".codex-autopilot").load()["incidents"][-1]
            self.assertEqual(ticket["system_state"]["stop_kind"], "plan_unverified")
            self.assertIn("PLAN_PROPOSED", ticket["summary"])

    def test_t4_patch_counter_and_critical_path_force_full_revalidation(self) -> None:
        current_raw = graph(
            [
                task("A", outcomes=("playable",)),
                task("B", outcomes=("packaged",)),
            ]
        )
        current = validate_plan(current_raw, "adaptive")
        local_raw = copy.deepcopy(current_raw)
        local_raw["graph_version"] = 2
        local = validate_plan(local_raw, "adaptive")

        self.assertEqual(
            plan_change_verification_mode(
                current,
                local,
                accepted_patches_since_full=0,
                full_revalidation_patches=3,
            ),
            PLAN_PATCH_VERIFICATION,
        )
        self.assertEqual(
            plan_change_verification_mode(
                current,
                local,
                accepted_patches_since_full=2,
                full_revalidation_patches=3,
            ),
            FULL_PLAN_REVALIDATION,
        )

        critical_raw = copy.deepcopy(local_raw)
        critical_raw["tasks"][1]["depends_on"] = ["A"]
        critical = validate_plan(critical_raw, "adaptive")
        self.assertEqual(
            plan_change_verification_mode(
                current,
                critical,
                accepted_patches_since_full=0,
                full_revalidation_patches=3,
            ),
            FULL_PLAN_REVALIDATION,
        )
        self.assertEqual(
            critical.goal_contract.to_dict(), current.goal_contract.to_dict()
        )
        self.assertIn(
            "Verification mode: FULL_PLAN_REVALIDATION",
            build_plan_verification_prompt(
                critical, (), mode=FULL_PLAN_REVALIDATION
            ),
        )

    def test_t4_semantic_change_on_critical_path_forces_full_revalidation(self) -> None:
        current_raw = graph(
            [
                task("A", outcomes=("playable",)),
                task("B", outcomes=("playable",), depends_on=("A",)),
                task("C", outcomes=("packaged",), depends_on=("B",)),
            ]
        )
        current = validate_plan(current_raw, "adaptive")
        candidate_raw = copy.deepcopy(current_raw)
        candidate_raw["graph_version"] = 2
        candidate_raw["tasks"][1]["objective"] = (
            "Produce a substantially different central result."
        )
        candidate_raw["tasks"][1]["definition_of_done"] = [
            "The different central result is independently accepted."
        ]
        candidate_raw["tasks"][1]["outputs"][0]["path"] = (
            "dist/different-central.bin"
        )
        candidate = validate_plan(candidate_raw, "adaptive")

        self.assertEqual(
            plan_change_verification_mode(
                current,
                candidate,
                accepted_patches_since_full=0,
                full_revalidation_patches=3,
            ),
            FULL_PLAN_REVALIDATION,
        )

    def test_plan_verifier_loads_every_active_constraint_without_truncation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="plan-verification-memory-") as raw:
            root = Path(raw)
            (root / ".git").mkdir()
            memory = ProjectMemory(root)
            for index in range(40):
                memory.add_constraint(
                    statement=f"constraint-{index:02d}",
                    origin="user",
                    created_by="plan-verification-test",
                )
            full_statement = "x" * 1_106 + "TAIL_REQUIRED"
            full_id = memory.add_constraint(
                statement=full_statement,
                origin="user",
                created_by="plan-verification-test",
            )["id"]

            constraints = load_active_memory_constraints(root)
            by_id = {item["id"]: item for item in constraints}

            self.assertEqual(len(constraints), 41)
            self.assertEqual(by_id[full_id]["statement"], full_statement)

            prompt = build_plan_verification_prompt(complete_plan(), constraints)
            raw_context = prompt.split("PLAN_VERIFICATION_CONTEXT: ", 1)[1].split(
                "\n\nEvaluate every dimension:", 1
            )[0]
            context = json.loads(raw_context)
            prompt_constraints = {item["id"]: item for item in context["constraints"]}
            self.assertEqual(len(prompt_constraints), 41)
            self.assertEqual(prompt_constraints[full_id]["statement"], full_statement)

    def test_t5_prompt_contains_only_the_four_authorized_inputs(self) -> None:
        proposed = complete_plan()
        prompt = build_plan_verification_prompt(
            proposed,
            (
                {
                    "id": "constraint-1",
                    "statement": "No network access.",
                    "scope": "project",
                    "origin": "user",
                    "private": PRIVATE_PLANNER_MARKER,
                },
            ),
        )
        raw_context = prompt.split("PLAN_VERIFICATION_CONTEXT: ", 1)[1].split(
            "\n\nEvaluate every dimension:", 1
        )[0]
        context = json.loads(raw_context)

        self.assertEqual(
            set(context),
            {"goal_contract", "constraints", "proposed_dag", "definition_of_done"},
        )
        self.assertEqual(
            set(context["constraints"][0]),
            {"id", "statement", "scope", "origin"},
        )
        self.assertNotIn(PRIVATE_PLANNER_MARKER, prompt)
        self.assertNotIn("user_request", prompt)
        self.assertNotIn("model_strategy", prompt)
        self.assertNotIn('"reasoning"', prompt)
        self.assertNotIn('"verification"', raw_context)
        self.assertNotIn('"resources"', raw_context)

    def test_plan_verified_receipt_is_bound_to_the_exact_graph(self) -> None:
        accepted = complete_plan()
        receipt = canonical_plan_verification(accepted)
        require_plan_verified(accepted, receipt)

        changed_raw = plan_to_dict(accepted)
        changed_raw["tasks"][0]["title"] = "Changed after verification"
        changed = validate_plan(changed_raw, "adaptive")
        with self.assertRaisesRegex(PlanVerificationError, "digest"):
            require_plan_verified(changed, receipt)


if __name__ == "__main__":
    unittest.main()
