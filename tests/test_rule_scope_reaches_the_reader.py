"""A rule the runtime does not apply must not read as one that it does.

R30 - acceptance by the department lead against a versioned rubric - is
written unconditionally and carries mode ENFORCED. The runtime activates it
only when the task declares both the department-binding and the
rubric-binding logical resource; `task_department_binding` returns None
otherwise and nothing downstream asks for a lead or a rubric.

The prompt block sent the statement and dropped that gate. On a real run
(23 Sep 2026) a verifier read R30, looked for a department and a rubric that
the plan never declared, and withheld acceptance of finished work. The worker
asked for a prerequisite, the replanner tried to invent a department, and the
run blocked after three rejected attempts - 23 minutes of model time on a
rule that did not apply to the task.
"""

from __future__ import annotations

from pathlib import Path
import types
import unittest

from codex_autopilot.department_acceptance import (
    DEPARTMENT_FIELDS,
    DepartmentAcceptanceError,
    RUBRIC_REFERENCE_FIELDS,
)
from codex_autopilot.rules import RULES, rules_for_prompt


def _resource(**fields):
    return types.SimpleNamespace(**fields)


def _task(*resources):
    return types.SimpleNamespace(id="T1", resources=tuple(resources))


BOUND = (
    _resource(
        id="department-binding",
        kind="logical",
        access="read",
        target="department-id:character-art",
    ),
    _resource(
        id="rubric-binding",
        kind="logical",
        access="read",
        target="project-memory:department/character-art/rubric",
    ),
)


class WhatTheReaderIsToldTests(unittest.TestCase):
    def test_without_a_task_no_scope_is_claimed(self) -> None:
        """The replanner rewrites a whole graph; there is no one task to scope to."""

        entry = self._r30(rules_for_prompt())
        self.assertNotIn("scope", entry)

    def test_a_task_with_no_binding_is_told_the_rule_is_not_in_force(self) -> None:
        entry = self._r30(rules_for_prompt(task=_task(
            _resource(id="asset-files", kind="directory", access="write", target="Art"),
            _resource(id="blender-ui", kind="logical", access="write", target="ui:blender"),
        )))
        self.assertIn("NOT in force", entry["scope"])
        self.assertIn("definition of done", entry["scope"])

    def test_the_rule_still_reads_as_enforced(self) -> None:
        """Scope says where it applies, not that it is optional where it does."""

        entry = self._r30(rules_for_prompt(task=_task()))
        self.assertEqual(entry["mode"], "ENFORCED")
        self.assertIn("NOT in force", entry["scope"])

    def test_a_bound_task_is_told_which_department(self) -> None:
        entry = self._r30(rules_for_prompt(task=_task(*BOUND)))
        self.assertIn("In force", entry["scope"])
        self.assertIn("character-art", entry["scope"])

    def test_a_half_bound_task_is_not_quietly_excused(self) -> None:
        """One claim of the pair is a malformed binding, not an absent one."""

        entry = self._r30(rules_for_prompt(task=_task(BOUND[0])))
        self.assertIn("In force", entry["scope"])
        self.assertNotIn("NOT in force", entry["scope"])

    def test_only_conditional_rules_carry_scope(self) -> None:
        scoped = [i["id"] for i in rules_for_prompt(task=_task(*BOUND)) if "scope" in i]
        self.assertEqual(scoped, ["R30"])

    def test_the_block_is_never_shortened_by_scoping(self) -> None:
        """R17: the rules block is never truncated."""

        self.assertEqual(len(rules_for_prompt(task=_task())), len(RULES))
        self.assertEqual(len(rules_for_prompt()), len(RULES))

    def _r30(self, block):
        return next(item for item in block if item["id"] == "R30")


class TheRealBlockedTaskTests(unittest.TestCase):
    """The shape that actually blocked: M01 of the beyondness run."""

    def test_m01s_resources_put_r30_out_of_scope(self) -> None:
        m01 = _task(*[
            _resource(id=name, kind="logical", access="write", target=name)
            for name in (
                "asset-files",
                "handoff",
                "source-reference",
                "blend-owner",
                "blender-ui",
                "unreal-ui",
            )
        ])
        entry = next(i for i in rules_for_prompt(task=m01) if i["id"] == "R30")
        self.assertIn("NOT in force", entry["scope"])


class TheRefusalNamesWhatIsAcceptedTests(unittest.TestCase):
    """R31: a refusal names what IS accepted."""

    def test_an_unknown_department_field_is_answered_with_the_accepted_set(self) -> None:
        from codex_autopilot.department_acceptance import department_contract_from_raw

        with self.assertRaises(DepartmentAcceptanceError) as caught:
            department_contract_from_raw(
                {
                    "id": "character-art",
                    "name": "Character Art",
                    "lead_role": "lead",
                    "rubric": {"record_id": "R", "version": 1, "sha256": "x"},
                },
                "department 1",
            )
        message = str(caught.exception)
        self.assertIn("lead_role", message)
        self.assertIn("lead_role_id", message, "the accepted spelling must appear")
        self.assertIn("accepted fields are", message)


class TheNestedContractIsStatedTests(unittest.TestCase):
    def test_the_replanner_is_given_the_nested_field_sets(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_prompts.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"allowed_department_fields": sorted(DEPARTMENT_FIELDS)', source)
        self.assertIn(
            '"allowed_rubric_reference_fields": sorted(RUBRIC_REFERENCE_FIELDS)', source
        )

    def test_the_stated_set_is_the_enforced_set(self) -> None:
        """One name for both, so the prompt cannot drift from the parser."""

        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/department_acceptance.py"
        ).read_text(encoding="utf-8")
        self.assertIn("_exact_keys(data, set(DEPARTMENT_FIELDS), label)", source)
        self.assertIn("_exact_keys(data, set(RUBRIC_REFERENCE_FIELDS), label)", source)
        self.assertEqual(set(DEPARTMENT_FIELDS), {"id", "name", "lead_role_id", "rubric"})
        self.assertEqual(set(RUBRIC_REFERENCE_FIELDS), {"record_id", "version", "sha256"})


class TheWorkerEnvelopeCarriesTheTaskTests(unittest.TestCase):
    def test_both_task_bearing_envelopes_scope_the_rules(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/ai_studio.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(source.count("rules_for_prompt(self.state_dir, task=task)"), 2)


if __name__ == "__main__":
    unittest.main()
