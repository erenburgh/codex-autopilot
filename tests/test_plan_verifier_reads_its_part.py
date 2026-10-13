"""The plan verifier reads every rule whole, and its part of each.

History. On a fresh 0.14 install a tester's run stopped at preflight with
"Plan verification: FAIL"; his agent said the verifier is held to
structured fields the plugin itself keeps out of the plan it is given. The
prompt bore it out: the whole rules block, with no scope at all - R9's
RoleProfile, R30's leads and rubrics, the acceptance floor, the declared
scope - beside a view that leaves out roles, leads, resources and the
verification policy by design (R25). A verifier that reads the rules as a
contract finds the fields missing and can only answer REVISE.

The rules stay in force and whole. Each one that asks for a withheld field
carries a scope for this reader: what is not in its view, what admission
already accepted of it on this graph, and who judges the rest.

Every prompt here is the one the runtime launches: preflight's, through
``run_preflight`` with a fake App Server, and the lifecycle's in
test_rules_reach_every_prompt.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from codex_autopilot.artifact_staging import task_requires_staging
from codex_autopilot.plan import plan_to_dict, validate_plan_change
from codex_autopilot.plan_verification import PLAN_PATCH_VERIFICATION, build_plan_verification_prompt
from codex_autopilot.preflight import run_preflight
from codex_autopilot.rules import PLAN_VERIFICATION_PHASE, RULES, rules_for_prompt
from test_preflight import DESKTOP_PROJECT, SKILL, PreflightClient, plan as preflight_plan, project
from test_rule_scope_reaches_the_reader import _plan as saved_plan


# For each rule that asks for a field the verifier's view leaves out: what
# its scope must name - the withheld fields, and where the runtime admitted
# or will judge them.
WITHHELD: dict[str, tuple[str, ...]] = {
    "R7": ("task.resources", "verification.max_revision_attempts", "no field for a time or token budget",
           "plan_parse.resource_from_raw", "it does not require a write claim", "scope.audit_declared_scope",
           "recorded as unchecked, never as clean"),
    "R8": ("verification.policy", "verification.required", "the user's original request",
           "plan_admission.acceptance_issues", "when the task is accepted"),
    "R9": ("plan.roles", "each task's role", "plan_parse.role_from_raw", "plan_admission._graph",
           "task.title you see need not carry a role name"),
    "R17": ("In place of the user's original request you are given the Goal Contract",
            "withheld from you by design", "never truncated"),
    "R21": ("verification.deterministic_checks", "CODEX_THREAD_ID, CODEX_TURN_ID and CODEX_SESSION_ID reset",
            "acceptance_floor.is_clean_suite_command", "when each task is accepted"),
    "R24": ("task.resources", "not that a write claim exists", "artifact_staging.task_requires_staging",
            "any other task works in the project itself"),
    "R27": ("any rubric", "department_runtime.ensure_department_rubric", "never the source of the pin",
            "at the latest when the lead is reserved"),
    "R29": ("verification.policy", "verification.required", "verification.deterministic_checks",
            "verification.max_revision_attempts", "allows at least 2 revision attempts",
            "the acceptance_class dimension"),
    "R30": ("verification.verifier_role", "plan.departments", "verification_expectations",
            "department_runtime.validate_department_leads", "never the task's own role",
            "thread_titles.department_verifier_thread_title"),
}


def _block(prompt: str) -> list[dict[str, str]]:
    line = next(item for item in prompt.splitlines() if item.startswith("AUTOPILOT_RULES: "))
    return json.loads(line[len("AUTOPILOT_RULES: "):])


class ThePreflightVerifierReadsItsPartTests(unittest.TestCase):
    """The tester's path: preflight's fresh plan verifier, before any run-state."""

    def setUp(self) -> None:
        PreflightClient.instances.clear()
        PreflightClient.probe_instances.clear()

    def prompt(self) -> str:
        run_preflight(
            project(), plan=preflight_plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo",
            client_factory=PreflightClient, desktop_project_id=DESKTOP_PROJECT, emit=None,
        )
        client = PreflightClient.instances[-1]
        return next(
            turn["prompt"] for turn in client.plain_turns if "PLAN_VERIFICATION_CONTEXT:" in turn["prompt"]
        )

    def test_each_rule_on_a_withheld_field_says_what_the_runtime_admitted(self) -> None:
        """Mutations: build_plan_verification_prompt calls rules_for_prompt(state_dir)
        with no phase; one rule's scope is dropped from plan_verifier_scope."""

        block = _block(self.prompt())
        scoped = {entry["id"]: entry["scope"] for entry in block if "scope" in entry}
        self.assertEqual(set(scoped), set(WITHHELD))
        for rule_id, phrases in WITHHELD.items():
            for phrase in phrases:
                with self.subTest(rule=rule_id, phrase=phrase):
                    self.assertIn(phrase, scoped[rule_id])
            if rule_id != "R17":
                with self.subTest(rule=rule_id):
                    self.assertIn("Not in your view:", scoped[rule_id])
                    self.assertIn("not yours to judge; the six dimensions this prompt lists are", scoped[rule_id])

    def test_on_an_admitted_graph_the_scope_says_every_task_holds_it(self) -> None:
        scoped = {entry["id"]: entry.get("scope", "") for entry in _block(self.prompt())}
        self.assertIn("every task of this graph names its department's lead", scoped["R30"])
        self.assertIn("every task of this graph is judged by an independent verifier", scoped["R8"])
        self.assertIn("every task of this graph declares a full-suite deterministic check", scoped["R21"])
        self.assertIn("every task's role is one of them, and none is the generic legacy-worker", scoped["R9"])
        for rule_id, scope in scoped.items():
            with self.subTest(rule=rule_id):
                self.assertNotIn("Left out of that", scope)

    def test_the_rule_goes_whole_beside_its_scope(self) -> None:
        """R17: statement and check verbatim, every rule, the same order.

        Mutation: a scoped entry carries its title for the statement, or a
        shortened check.
        """

        block = _block(self.prompt())
        by_id = {item.id: item for item in RULES}
        self.assertEqual([entry["id"] for entry in block], [entry["id"] for entry in rules_for_prompt()])
        self.assertEqual(len(block), len(RULES))
        for entry in block:
            with self.subTest(rule=entry["id"]):
                self.assertEqual(entry["rule"], by_id[entry["id"]].statement)
                self.assertEqual(entry["check"], by_id[entry["id"]].check)
                self.assertEqual(entry["mode"], by_id[entry["id"]].mode)

    def test_no_scope_switches_its_rule_off(self) -> None:
        """Mutation: R27's scope tells the plan verifier the rule does not apply to it."""

        for entry in _block(self.prompt()):
            if "scope" not in entry:
                continue
            with self.subTest(rule=entry["id"]):
                self.assertTrue(entry["scope"].startswith("In force."))
                scope = entry["scope"].casefold()
                for excuse in ("not in force", "does not apply", "not apply", "exempt", "waive", "ignore", "disregard"):
                    self.assertNotIn(excuse, scope)

    def test_no_scope_names_a_withheld_value(self) -> None:
        """The roles and leads stay out of the judgment prompt, as the view keeps them.

        Mutation: R30's scope lists the leads it found.
        """

        plan = preflight_plan()
        line = next(item for item in self.prompt().splitlines() if item.startswith("AUTOPILOT_RULES: "))
        values = {role.id for role in plan.roles} | {role.name for role in plan.roles}
        values |= {task.verification.verifier_role for task in plan.tasks}
        for value in sorted(values):
            with self.subTest(value=value):
                self.assertNotIn(value, line)


class TheScopeClaimsOnlyWhatTheGraphHoldsTests(unittest.TestCase):
    """What admission let through without a part is named, never folded into "every task".

    A plan change keeps a task the change leaves as it was without asking it
    for a lead; a migrated v0.8 task keeps its recorded acceptance contract.
    """

    def scopes(self, plan) -> dict[str, str]:
        block = rules_for_prompt(plan=plan, phase=PLAN_VERIFICATION_PHASE)
        return {entry["id"]: entry["scope"] for entry in block if "scope" in entry}

    def test_a_task_with_no_lead_is_named_with_who_stops_it(self) -> None:
        """Mutation: _every_task ignores its exceptions."""

        plan = saved_plan()
        tasks = tuple(
            replace(task, verification=replace(task.verification, verifier_role=None)) if task.id == "M02" else task
            for task in plan.tasks
        )
        scope = self.scopes(replace(plan, tasks=tasks))["R30"]
        self.assertIn("every task of this graph but M02 names its department's lead", scope)
        self.assertIn("Left out of that: M02 - no lead of another profession is named", scope)
        self.assertIn("the runtime takes the lead its profession names", scope)
        self.assertIn("stops the task for the on-call (department_gate)", scope)
        self.assertNotIn("every task of this graph names", scope)

    def test_a_task_below_the_acceptance_floor_is_named_in_each_rule_of_the_floor(self) -> None:
        plan = saved_plan()
        tasks = tuple(
            replace(task, verification=replace(task.verification, policy="self")) if task.id == "M03" else task
            for task in plan.tasks
        )
        scopes = self.scopes(replace(plan, tasks=tasks))
        for rule_id in ("R8", "R21", "R29"):
            with self.subTest(rule=rule_id):
                self.assertIn("every task of this graph but M03", scopes[rule_id])
                self.assertIn("Left out of that: M03 - the floor does not hold", scopes[rule_id])


class APlanChangeIsToldWhatItsAdmissionLetThroughTests(unittest.TestCase):
    """The lifecycle's path: a plan change admitted, then the prompt its verifier is launched with.

    Admitted by ``validate_plan_change`` and built by
    ``build_plan_verification_prompt`` with a state directory, as the
    plan_verifier reservation does (lifecycle_reservations).
    """

    def verifier_scopes(self, current, edit) -> tuple[object, dict[str, str]]:
        raw = plan_to_dict(current)
        raw["graph_version"] = current.graph_version + 1
        for item in raw["tasks"]:
            edit(item)
        candidate = validate_plan_change(current, raw, "adaptive")
        with tempfile.TemporaryDirectory() as temp:
            prompt = build_plan_verification_prompt(
                candidate, (), mode=PLAN_PATCH_VERIFICATION, state_dir=Path(temp)
            )
        return candidate, {entry["id"]: entry["scope"] for entry in _block(prompt) if "scope" in entry}

    def test_the_tasks_a_change_keeps_without_a_lead_are_named(self) -> None:
        """A run from before R30: its saved plan names no lead, and is never refused on load for it.

        The change gives M02 a lead and leaves M01 and M03 as they were;
        admission lets them keep what they had. Mutation: _every_task
        ignores its exceptions - "every task of this graph names its
        department's lead" on a graph where two do not.
        """

        def lead_for_m02(item: dict) -> None:
            if item["id"] == "M02":
                item["verification"]["verifier_role"] = "art-reviewer"

        candidate, scopes = self.verifier_scopes(saved_plan(without_lead=True), lead_for_m02)
        self.assertEqual(
            [task.id for task in candidate.tasks if not task.verification.verifier_role], ["M01", "M03"]
        )
        self.assertIn("every task of this graph but M01, M03 names its department's lead", scopes["R30"])
        self.assertIn("Left out of that: M01, M03 - no lead of another profession is named", scopes["R30"])

    def test_a_task_admitted_without_a_write_claim_is_not_said_to_have_one(self) -> None:
        """Admission requires no write claim, and stages only a task with a path output and one.

        So no scope says what the view leaves out is in the plan, and R24's
        does not say every worker writes into a staged copy. Mutations: the
        closing sentence says again "not missing from the plan"; R24's scope
        says again "The worker writes into a staged copy".
        """

        def no_resources_for_m02(item: dict) -> None:
            if item["id"] == "M02":
                item["resources"] = []

        candidate, scopes = self.verifier_scopes(saved_plan(), no_resources_for_m02)
        self.assertEqual(candidate.task_map["M02"].resources, ())
        self.assertFalse([task.id for task in candidate.tasks if task_requires_staging(task)])
        for rule_id in ("R7", "R24"):
            with self.subTest(rule=rule_id):
                self.assertNotIn("not missing from the plan", scopes[rule_id])
                self.assertIn("its absence here says nothing about the plan", scopes[rule_id])
        self.assertNotIn("The worker writes into a staged copy", scopes["R24"])
        self.assertIn(
            "A task that declares an output with a path and a filesystem write claim works in a staged copy",
            scopes["R24"],
        )
        self.assertIn("any other task works in the project itself", scopes["R24"])


class EveryOtherReaderGetsTheBlockItHadTests(unittest.TestCase):
    def test_no_other_phase_gets_the_plan_verifiers_scopes(self) -> None:
        """Mutation: rules_for_prompt applies the graph's scopes whenever it has the plan."""

        plan = saved_plan()
        self.assertFalse([entry["id"] for entry in rules_for_prompt() if "scope" in entry])
        for phase in (None, "implementation", "revision", "screening", "verification"):
            with self.subTest(phase=phase):
                block = rules_for_prompt(plan=plan, phase=phase)
                self.assertFalse([entry["id"] for entry in block if "scope" in entry])
                with_task = rules_for_prompt(task=plan.task_map["M01"], plan=plan, phase=phase)
                self.assertEqual([entry["id"] for entry in with_task if "scope" in entry], ["R30"])
                r30 = next(entry for entry in with_task if entry["id"] == "R30")["scope"]
                self.assertIn("Character Art Verifier", r30)
                self.assertNotIn("validate_department_leads", r30)

    def test_the_verifiers_scopes_need_the_graph_it_judges(self) -> None:
        """Without the plan there is nothing to compute them on: no scope is claimed."""

        self.assertFalse([entry for entry in rules_for_prompt(phase=PLAN_VERIFICATION_PHASE) if "scope" in entry])

    def test_the_one_prompt_builder_carries_them(self) -> None:
        """build_plan_verification_prompt is the only door; both callers share it."""

        from test_plan_verification import complete_plan

        prompt = build_plan_verification_prompt(complete_plan(), ())
        scoped = {entry["id"] for entry in _block(prompt) if "scope" in entry}
        self.assertEqual(scoped, set(WITHHELD))
        self.assertIn("Where a rule carries a scope, the scope says which of its fields", prompt)


if __name__ == "__main__":
    unittest.main()
