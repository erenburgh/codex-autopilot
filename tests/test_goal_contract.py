from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from _plan_contract import canonical_verification

from codex_autopilot.goal_contract import (
    GoalContractError,
    validate_goal_contract,
)
from codex_autopilot.ai_studio import AIStudioRuntime
from codex_autopilot.plan import (
    plan_to_dict,
    validate_migrating_plan,
    validate_persisted_plan,
    validate_plan,
    validate_plan_change,
)


def goal_contract() -> dict:
    return {
        "required_outcomes": [
            {"id": "playable", "description": "A playable level can be entered."},
            {"id": "packaged", "description": "The build can be packaged and launched."},
        ],
        "deliverables": [
            {
                "id": "build",
                "description": "A launchable packaged build.",
                "path": "dist/game.zip",
            }
        ],
        "constraints": [
            {"id": "offline", "description": "The build must work offline."}
        ],
        "global_acceptance": [
            {"id": "launches", "description": "The packaged build launches."}
        ],
    }


def task(task_id: str = "M1", outcomes: list[str] | None = None) -> dict:
    return {
        "id": task_id,
        "title": "Build slice",
        "objective": "Produce a playable packaged slice.",
        "definition_of_done": ["The slice is independently accepted."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files and tests are sufficient.",
        "reasoning": "medium",
        "role": "builder",
        "depends_on": [],
        "priority": 0,
        "verification": canonical_verification(),
        "resources": [],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
        "acceptance_class": "mixed",
        **(
            {"produces_outcomes": outcomes}
            if outcomes is not None
            else {}
        ),
    }


def graph(*, include_contract: bool = True) -> dict:
    payload = {
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Ship a vertical slice.",
        "user_request": "Create a playable packaged vertical slice.",
        "model_strategy": "auto",
        "execution_strategy": "auto",
        "max_parallel_workers": 2,
        "computer_use_slots": 1,
        "roles": [
            {
                "id": "builder",
                "name": "Runtime Engineer",
                "responsibilities": ["Build the accepted vertical slice."],
            }
        ],
        "tasks": [task(outcomes=["playable", "packaged"])],
    }
    if include_contract:
        payload["goal_contract"] = goal_contract()
    return payload


class GoalContractSchemaTests(unittest.TestCase):
    def test_structured_contract_loads_and_round_trips(self) -> None:
        contract = validate_goal_contract(goal_contract())

        self.assertEqual(contract.outcome_ids, {"playable", "packaged"})
        self.assertEqual(contract.deliverables[0].path, "dist/game.zip")
        self.assertEqual(contract.to_dict(), goal_contract())

    def test_contract_rejects_prose_instead_of_structured_entries(self) -> None:
        raw = goal_contract()
        raw["required_outcomes"] = ["player can enter playable level"]

        with self.assertRaisesRegex(
            GoalContractError,
            r"required_outcomes\[1\] must be an object",
        ):
            validate_goal_contract(raw)

    def test_contract_requires_all_sections_and_non_vacuous_success(self) -> None:
        raw = goal_contract()
        del raw["constraints"]
        with self.assertRaisesRegex(GoalContractError, "constraints must be declared"):
            validate_goal_contract(raw)

        raw = goal_contract()
        raw["global_acceptance"] = []
        with self.assertRaisesRegex(GoalContractError, "global_acceptance must be non-empty"):
            validate_goal_contract(raw)


class GoalContractPlanIntegrationTests(unittest.TestCase):
    def test_new_canonical_plan_loads_structured_contract_and_task_outcomes(self) -> None:
        plan = validate_plan(graph(), "adaptive")

        self.assertIsNotNone(plan.goal_contract)
        self.assertEqual(plan.tasks[0].produces_outcomes, ("playable", "packaged"))
        serialized = plan_to_dict(plan)
        self.assertEqual(serialized["goal_contract"], goal_contract())
        self.assertEqual(
            serialized["tasks"][0]["produces_outcomes"],
            ["playable", "packaged"],
        )

    def test_task_without_produced_outcome_is_rejected(self) -> None:
        raw = graph()
        raw["tasks"] = [task()]

        with self.assertRaisesRegex(
            ValueError,
            "task M1 must declare at least one produces_outcomes entry",
        ):
            validate_plan(raw, "adaptive")

    def test_task_cannot_claim_an_unknown_or_duplicate_outcome(self) -> None:
        raw = graph()
        raw["tasks"] = [task(outcomes=["missing"])]
        with self.assertRaisesRegex(ValueError, "unknown Goal Contract outcomes: missing"):
            validate_plan(raw, "adaptive")

        raw = graph()
        raw["tasks"] = [task(outcomes=["playable", "playable"])]
        with self.assertRaisesRegex(ValueError, "produces_outcomes must not contain duplicates"):
            validate_plan(raw, "adaptive")

    def test_existing_v09_persisted_plan_remains_loadable_without_contract(self) -> None:
        raw = graph(include_contract=False)
        raw["tasks"] = [task()]

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "plan.json").write_text(json.dumps(raw), encoding="utf-8")
            (state_dir / "run-state.json").write_text(
                json.dumps({"schema_version": 5, "run_id": "existing-v09"}),
                encoding="utf-8",
            )
            plan = validate_persisted_plan(raw, "adaptive", state_dir=state_dir)

        self.assertIsNone(plan.goal_contract)
        self.assertEqual(plan.tasks[0].produces_outcomes, ())

    def test_persisted_compatibility_requires_adjacent_durable_state(self) -> None:
        raw = graph(include_contract=False)
        raw["tasks"] = [task()]

        with self.assertRaisesRegex(
            ValueError,
            "plan.goal_contract must be declared as a structured Goal Contract",
        ):
            validate_persisted_plan(raw, "adaptive")

    def test_new_schema3_submission_requires_goal_contract(self) -> None:
        raw = graph(include_contract=False)
        raw["tasks"] = [task()]

        with self.assertRaisesRegex(
            ValueError,
            "plan.goal_contract must be declared as a structured Goal Contract",
        ):
            validate_plan(raw, "adaptive")

    def test_production_validator_requires_contract_for_a_new_plan(self) -> None:
        raw = graph(include_contract=False)
        raw["tasks"] = [task()]

        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(
            ValueError,
            "plan.goal_contract must be declared as a structured Goal Contract",
        ):
            validate_migrating_plan(raw, "adaptive", state_dir=Path(tmp))

    def test_unrelated_persisted_state_does_not_grandfather_new_payload(self) -> None:
        persisted = graph(include_contract=False)
        persisted["tasks"] = [task()]
        submitted = copy.deepcopy(persisted)
        submitted["goal"] = "A different new run."

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "plan.json").write_text(
                json.dumps(persisted), encoding="utf-8"
            )
            (state_dir / "run-state.json").write_text(
                json.dumps({"schema_version": 5, "run_id": "existing-v09"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "plan.goal_contract must be declared as a structured Goal Contract",
            ):
                validate_migrating_plan(submitted, "adaptive", state_dir=state_dir)

    def test_production_validator_loads_exact_persisted_v09_plan(self) -> None:
        raw = graph(include_contract=False)
        raw["tasks"] = [task()]

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "plan.json").write_text(json.dumps(raw), encoding="utf-8")
            (state_dir / "run-state.json").write_text(
                json.dumps({"schema_version": 5, "run_id": "existing-v09"}),
                encoding="utf-8",
            )
            plan = validate_migrating_plan(raw, "adaptive", state_dir=state_dir)

        self.assertIsNone(plan.goal_contract)

    def test_crash_recovery_accepts_only_a_hashed_persisted_plan_change(self) -> None:
        current = graph(include_contract=False)
        current["tasks"] = [task()]
        target = copy.deepcopy(current)
        target["graph_version"] = 2

        def digest(payload: dict) -> str:
            return hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "plan.json").write_text(
                json.dumps(current), encoding="utf-8"
            )
            (state_dir / "run-state.json").write_text(
                json.dumps({"schema_version": 5, "run_id": "existing-v09"}),
                encoding="utf-8",
            )
            transaction = {
                "schema_version": 1,
                "status": "PREPARED",
                "base_plan_sha256": digest(current),
                "target_plan_sha256": digest(target),
                "target_plan": target,
            }
            transaction_path = state_dir / "plan-change-transaction.json"
            transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
            recovered = validate_persisted_plan(
                target, "adaptive", state_dir=state_dir
            )
            self.assertIsNone(recovered.goal_contract)

            transaction["base_plan_sha256"] = "0" * 64
            transaction_path.write_text(json.dumps(transaction), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "plan.goal_contract must be declared as a structured Goal Contract",
            ):
                validate_persisted_plan(target, "adaptive", state_dir=state_dir)

    def test_plan_change_must_preserve_goal_contract(self) -> None:
        current = validate_plan(graph(), "adaptive")
        candidate = plan_to_dict(current)
        candidate["graph_version"] = 2
        candidate["goal_contract"] = copy.deepcopy(candidate["goal_contract"])
        candidate["goal_contract"]["required_outcomes"][0]["description"] = "Changed target."

        with self.assertRaisesRegex(ValueError, "must not replace the Goal Contract"):
            validate_plan_change(current, candidate, "adaptive")

    def test_worker_receives_goal_contract_before_task_and_dod(self) -> None:
        plan = validate_plan(graph(), "adaptive")
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AIStudioRuntime(
                plan,
                Path(tmp),
                language="en",
                skill_path=Path(tmp) / "SKILL.md",
                memory=object(),
            )
            prompt = runtime.build_prompt(
                "M1",
                phase="implementation",
                task_states={"M1": "RUNNING"},
                reservation_token="token",
            )

        raw = prompt.split("AUTOPILOT_CONTEXT: ", 1)[1].split("\n\n", 1)[0]
        context = json.loads(raw)
        self.assertEqual(context["goal_contract"], goal_contract())
        self.assertEqual(
            context["task"]["produces_outcomes"],
            ["playable", "packaged"],
        )
        self.assertLess(raw.index('"rules"'), raw.index('"goal_contract"'))
        self.assertLess(raw.index('"goal_contract"'), raw.index('"task"'))
        self.assertLess(raw.index('"task"'), raw.index('"definition_of_done"'))


if __name__ == "__main__":
    unittest.main()
