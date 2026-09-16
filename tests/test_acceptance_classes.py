from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from _plan_contract import canonicalize_plan, canonical_verification
from codex_autopilot.acceptance import (
    AcceptanceClass,
    AcceptanceClassError,
    acceptance_class_from_raw,
    admits_semantic_judgment,
)
from codex_autopilot.plan import (
    plan_to_dict,
    validate_migrating_plan,
    validate_persisted_plan,
    validate_plan,
)
from codex_autopilot.plan_verification import (
    PLAN_VERIFICATION_PREFIX,
    build_plan_verification_prompt,
    parse_plan_verification_result,
)


def _plan(acceptance_class: str = "mixed") -> dict:
    return canonicalize_plan(
        {
            "schema_version": 3,
            "graph_version": 1,
            "goal": "Produce and independently accept one bounded result.",
            "user_request": "Produce and independently accept one bounded result.",
            "model_strategy": "auto",
            "execution_strategy": "auto",
            "max_parallel_workers": 2,
            "computer_use_slots": 1,
            "roles": [
                {
                    "id": "builder",
                    "name": "Builder",
                    "responsibilities": ["Produce the bounded result."],
                }
            ],
            "tasks": [
                {
                    "id": "A",
                    "title": "Choose the most compelling visual direction",
                    "objective": (
                        "Judge which visual direction best expresses the stated purpose."
                    ),
                    "definition_of_done": [
                        "The chosen direction communicates the intended purpose."
                    ],
                    "execution_mode": "code",
                    "execution_mode_reason": "Repository artifacts are sufficient.",
                    "reasoning": "high",
                    "role": "builder",
                    "depends_on": [],
                    "priority": 0,
                    "verification": canonical_verification(),
                    "resources": [],
                    "required_capabilities": [],
                    "context": {},
                    "outputs": [],
                    "tags": [],
                    "acceptance_class": acceptance_class,
                }
            ],
        }
    )


class AcceptanceClassContractTests(unittest.TestCase):
    def test_three_classes_are_typed_and_unknown_values_fail_closed(self) -> None:
        self.assertEqual(
            {item.value for item in AcceptanceClass},
            {"deterministic-complete", "mixed", "judgment"},
        )
        with self.assertRaisesRegex(AcceptanceClassError, "one of"):
            acceptance_class_from_raw("mostly-deterministic", "task.acceptance_class")

    def test_mechanical_checks_only_admit_mixed_and_judgment_to_semantic_review(self) -> None:
        self.assertFalse(admits_semantic_judgment("deterministic-complete"))
        self.assertTrue(admits_semantic_judgment("mixed"))
        self.assertTrue(admits_semantic_judgment("judgment"))

    def test_plan_round_trip_preserves_the_declared_class(self) -> None:
        for value in ("deterministic-complete", "mixed", "judgment"):
            with self.subTest(value=value):
                plan = validate_plan(_plan(value), "adaptive")
                self.assertEqual(plan.tasks[0].acceptance_class.value, value)
                self.assertEqual(
                    plan_to_dict(plan)["tasks"][0]["acceptance_class"], value
                )

    def test_unknown_plan_class_is_rejected_during_schema_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "acceptance_class"):
            validate_plan(_plan("subjective-ish"), "adaptive")

    def test_new_canonical_plan_requires_an_explicit_class(self) -> None:
        raw = _plan()
        del raw["tasks"][0]["acceptance_class"]

        with self.assertRaisesRegex(
            ValueError,
            r"task 1\.acceptance_class must be declared",
        ):
            validate_plan(raw, "adaptive")

    def test_exact_persisted_schema3_plan_keeps_the_pre_class_default(self) -> None:
        raw = _plan()
        del raw["goal_contract"]
        del raw["tasks"][0]["produces_outcomes"]
        del raw["tasks"][0]["acceptance_class"]

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "plan.json").write_text(json.dumps(raw), encoding="utf-8")
            (state_dir / "run-state.json").write_text(
                json.dumps({"schema_version": 5, "run_id": "existing-v09"}),
                encoding="utf-8",
            )

            persisted = validate_persisted_plan(
                raw,
                "adaptive",
                state_dir=state_dir,
            )
            migrating = validate_migrating_plan(
                raw,
                "adaptive",
                state_dir=state_dir,
            )

        self.assertEqual(persisted.tasks[0].acceptance_class, AcceptanceClass.MIXED)
        self.assertEqual(migrating.tasks[0].acceptance_class, AcceptanceClass.MIXED)

    def test_t6_judgment_claimed_deterministic_is_exposed_to_plan_verifier(self) -> None:
        """T6 is semantic plan validation, not an unreliable word heuristic.

        The bounded verifier sees both the subjective contract and its claimed
        class, receives an explicit fail-closed instruction, and can return a
        typed acceptance_class issue that the production protocol accepts as
        REVISE.  Runtime never upgrades green checks to VERIFIED (R29).
        """

        proposed = validate_plan(_plan("deterministic-complete"), "adaptive")
        prompt = build_plan_verification_prompt(proposed, ())
        raw_context = prompt.split("PLAN_VERIFICATION_CONTEXT: ", 1)[1].split(
            "\n\nEvaluate every dimension:", 1
        )[0]
        context = json.loads(raw_context)

        self.assertEqual(
            context["proposed_dag"][0]["acceptance_class"],
            "deterministic-complete",
        )
        self.assertFalse(
            context["proposed_dag"][0]["semantic_judgment_required"]
        )
        self.assertIn("subjective or purpose-dependent result", prompt)
        self.assertIn("must be REVISE (T6)", prompt)

        verdict = parse_plan_verification_result(
            f'{PLAN_VERIFICATION_PREFIX} '
            '{"verdict":"REVISE","issues":['
            '{"category":"acceptance_class",'
            '"summary":"The task requires purpose-dependent judgment.",'
            '"task_ids":["A"],"outcome_ids":[]}]}'
        )
        self.assertEqual(verdict.verdict, "REVISE")
        self.assertEqual(verdict.issues[0].category, "acceptance_class")
        self.assertEqual(verdict.issues[0].task_ids, ("A",))

    def test_class_never_weakens_the_r29_independent_floor(self) -> None:
        raw = _plan("deterministic-complete")
        raw["tasks"][0]["verification"]["policy"] = "deterministic"
        with self.assertRaisesRegex(ValueError, "must be \"independent\""):
            validate_plan(raw, "adaptive")


if __name__ == "__main__":
    unittest.main()
