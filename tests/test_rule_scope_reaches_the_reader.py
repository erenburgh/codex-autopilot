"""R30's scope says who judges and by what - to each reader its own part.

History. On a real run (23 Sep 2026) a verifier read R30, looked for a
department and a rubric the plan never declared, and withheld acceptance of
finished work: 23 minutes of model time and a blocked run. The first answer
told the reader R30 was "NOT in force" - an exception from her rule written
into the prompt, beside a check that refuses a verdict not given by the
rubric. The second said the runtime had derived no department. Now the
runtime derives the department of every task from the plan's leads
(``department_runtime``), and the scope states it.

One text for every phase told a worker, a reviser and a screener that the
rubric was "loaded into department_acceptance" - a block only the lead's
prompt has. They would look for it and not find it: the same class of
failure as the 23 minutes. So the phases get different facts.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from _departments import DepartmentRun
from codex_autopilot.department_acceptance import DEPARTMENT_FIELDS, DepartmentAcceptanceError
from codex_autopilot.plan import validate_persisted_plan
from codex_autopilot.rules import RULES, rules_for_prompt

ROOT = Path(__file__).resolve().parents[1]
SAVED = json.loads((ROOT / "tests/fixtures/r30_saved_plans.json").read_text(encoding="utf-8"))


def _plan(*, without_lead: bool = False):
    raw = json.loads(json.dumps(SAVED["plain"]["saved_plan"]))
    if without_lead:
        for item in raw["tasks"]:
            item["verification"].pop("verifier_role")
    return validate_persisted_plan(raw, "adaptive")


def _r30(block):
    return next(item for item in block if item["id"] == "R30")


class EachReaderIsToldItsPartTests(unittest.TestCase):
    def test_without_a_task_no_scope_is_claimed(self) -> None:
        """The replanner rewrites a whole graph; there is no one task to scope to."""

        self.assertNotIn("scope", _r30(rules_for_prompt()))

    def test_the_lead_is_told_it_is_the_lead_and_where_its_rubric_is(self) -> None:
        plan = _plan()
        scope = _r30(rules_for_prompt(task=plan.task_map["M01"], plan=plan, phase="verification"))["scope"]
        self.assertIn("department 'Art Reviewer' (art-reviewer)", scope)
        self.assertIn("Lead Role 'Character Art Verifier' - you", scope)
        self.assertIn("department_acceptance", scope)

    def test_the_worker_is_told_who_judges_by_which_version_and_what(self) -> None:
        """No worker phase is sent to look for a block it does not have.

        Mutation: _r30_scope gives every phase the lead's text - a worker is
        told its rubric is in department_acceptance.
        """

        plan = _plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            from codex_autopilot.memory import ProjectMemory

            memory = ProjectMemory(root)
            for phase in ("implementation", "revision", "screening"):
                with self.subTest(phase=phase):
                    scope = _r30(rules_for_prompt(
                        task=plan.task_map["M02"], plan=plan, phase=phase, memory=memory
                    ))["scope"]
                    self.assertIn("accepted by Lead Role 'Character Art Verifier'", scope)
                    self.assertIn("department 'Art Reviewer' (art-reviewer) v1", scope)
                    self.assertIn("request-fidelity:", scope)
                    self.assertIn("Независимо сопоставить результат", scope)
                    self.assertNotIn("department_acceptance", scope)

    def test_a_task_without_a_lead_is_told_so_fail_closed(self) -> None:
        plan = _plan(without_lead=True)
        scope = _r30(rules_for_prompt(task=plan.task_map["M01"], plan=plan, phase="verification"))["scope"]
        self.assertTrue(scope.startswith("In force. No Lead Role is defined for this task"))
        self.assertIn("the runtime stops the task for the on-call", scope)

    def test_no_scope_ever_says_the_rule_is_off_or_excuses_the_rubric(self) -> None:
        """Mutation: bring back the old "NOT in force" text for an unbound task.

        "judge by it" and "do not look for one" are about the rubric itself,
        and the DoD is one of the rubric's criteria; what may never appear is
        the rule being off or another standard in the rubric's place.
        """

        for plan in (_plan(), _plan(without_lead=True)):
            for phase in ("implementation", "revision", "screening", "verification", None):
                entry = _r30(rules_for_prompt(task=plan.task_map["M01"], plan=plan, phase=phase))
                with self.subTest(phase=phase, scope=entry["scope"][:40]):
                    self.assertEqual(entry["mode"], "ENFORCED")
                    self.assertIn("versioned rubric is refused", entry["check"])
                    scope = entry["scope"].casefold()
                    for excuse in ("not in force", "withhold", "by its own definition of done", "derived no department"):
                        self.assertNotIn(excuse, scope)

    def test_only_r30_carries_a_scope_and_the_block_is_never_shortened(self) -> None:
        plan = _plan()
        block = rules_for_prompt(task=plan.task_map["M01"], plan=plan, phase="implementation")
        self.assertEqual([item["id"] for item in block if "scope" in item], ["R30"])
        self.assertEqual(len(block), len(RULES))
        self.assertEqual(len(rules_for_prompt()), len(RULES))


def _prompt_scope(prompt: str) -> str:
    """The R30 scope as the reader gets it: from the prompt the runtime launched."""

    start = prompt.index('{"id":"R30"')
    entry, _ = json.JSONDecoder().raw_decode(prompt[start:])
    return entry["scope"]


class EachSessionTheRuntimeLaunchesReadsItsPartTests(DepartmentRun):
    """The prompts of a run, reserved and completed through the lifecycle.

    This used to count the substring "phase=" in ai_studio's source. The
    independent check replaced the implementation envelope's phase=phase
    with phase="verification" - a worker, a reviser and a screener told the
    lead's "your rubric is in department_acceptance" - and that test, like
    every test that called rules_for_prompt itself, stayed green.
    """

    LEAD = "Lead Role 'Character Art Verifier' - you"
    WORKER = "your work will be accepted by Lead Role 'Character Art Verifier'"

    def test_worker_lead_and_reviser_each_read_their_own(self) -> None:
        """Mutations: ai_studio's build_prompt passes phase="verification", or
        phase="implementation", to rules_for_prompt instead of its phase."""

        implementation = self.reserve()[0]
        self.assertEqual(implementation.kind, "implementation")
        self.assertIn(self.WORKER, _prompt_scope(implementation.prompt))
        lead = self.implement(implementation, "worker-M01").descriptors[0]
        self.assertEqual(lead.kind, "verifier")
        self.assertIn(self.LEAD, _prompt_scope(lead.prompt))
        issue = {"code": "SILHOUETTE", "summary": "The silhouette drifts", "details": "Compare.", "dod_refs": [1]}
        revision = self.judge(lead, "lead-M01", self.verdict("M01", "REVISE", [issue])).descriptors[0]
        self.assertEqual(revision.kind, "revision")
        scope = _prompt_scope(revision.prompt)
        self.assertIn(self.WORKER, scope)
        self.assertNotIn("department_acceptance", scope)

    def test_the_screener_reads_the_workers_part(self) -> None:
        """Mutation: the screening envelope passes phase="verification"."""

        from codex_autopilot.config import load_config

        config = self.cfg.state_dir / "config.toml"
        text = config.read_text(encoding="utf-8")
        config.write_text(text.replace('skill_screening = "never"', 'skill_screening = "always"'), encoding="utf-8")
        self.cfg = load_config(self.root)
        screening = self.reserve()[0]
        self.assertEqual(screening.kind, "screening")
        scope = _prompt_scope(screening.prompt)
        self.assertIn(self.WORKER, scope)
        self.assertNotIn("department_acceptance", scope)


class TheReplannerIsToldDepartmentsAreTheRuntimesTests(unittest.TestCase):
    def test_the_replanner_names_one_lead_per_profession_and_writes_no_department(self) -> None:
        """It used to be handed the nested department and rubric field sets to fill.

        A real replanner spent its whole budget writing `lead_role` for
        `lead_role_id` in a field it never needed.
        """

        source = (ROOT / "src/codex_autopilot/lifecycle_prompts.py").read_text(encoding="utf-8")
        self.assertIn("Derived by the runtime: do not write departments", source)
        self.assertIn("one lead per profession", source)
        self.assertNotIn("allowed_department_fields", source)

    def test_the_stated_set_is_the_enforced_set(self) -> None:
        from codex_autopilot.department_acceptance import department_contract_from_raw
        from codex_autopilot.plan_fields import ALLOWED_FIELDS

        self.assertEqual(ALLOWED_FIELDS["plan.departments[]"], tuple(DEPARTMENT_FIELDS))
        with self.assertRaises(DepartmentAcceptanceError) as caught:
            department_contract_from_raw({"id": "a", "name": "A", "lead_role_id": "b", "extra": 1}, "department 1")
        self.assertIn(str(sorted(DEPARTMENT_FIELDS)), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
