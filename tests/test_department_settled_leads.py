"""R30 on a plan from before it: the lead of a profession is the one with work still to accept.

What this replaces. Admission before R30 let one profession name several
verifier_role values (8d6055d checked only unknown and legacy roles). The
first cut of R30 read every task of a role to find its one lead, and the
independent check reproduced where that goes on such a plan: two VERIFIED
tasks of one role judged by two leads - immutable during a plan change -
refused every later change, an unrelated prerequisite of another worker
included; the role's other tasks could never get a lead, since the split
stayed; the on-call's own plan change could not pass validation; the
replanner spent its attempts and the stop went to her. The code comment at
the exemption said the opposite.

Now a task already VERIFIED or CANCELLED keeps the lead that judged it and
names nothing for the rest (``department_runtime.settled_task_ids``), at
admission and at run time alike; a task still to be accepted takes part in
"one profession, one lead" whether the change touched it or not; and naming
the lead of a task already under way is not a rewrite of its work
(``resilience.names_only_its_lead``), so the on-call can always give a
profession one lead.
"""

from __future__ import annotations

from dataclasses import replace
import json
import unittest

from _departments import DepartmentRun, art_task, beyondness_plan
from _handoff import bump_task_checkpoint
from _plan_contract import TEST_OUTCOME_ID
from codex_autopilot.department_acceptance import DepartmentAcceptanceError
from codex_autopilot.department_runtime import (
    derive_task_department,
    settled_task_ids,
    validate_department_leads,
)
from codex_autopilot.pipeline_engineer import PipelineIncidentStore
from codex_autopilot.plan import plan_to_dict, validate_persisted_plan

SCULPT_REVIEWER = {
    "id": "sculpt-reviewer",
    "name": "Sculpt Reviewer",
    "version": "1.0.0",
    "responsibilities": ["Accept sculpts against the reference sheet."],
}
ANATOMY_LEAD = {
    "id": "anatomy-lead",
    "name": "Anatomy Lead",
    "version": "1.0.0",
    "responsibilities": ["Accept character work for anatomy."],
    "verification_expectations": ["Every joint reads correctly from three views."],
}


def _profession_plan() -> dict:
    """Three tasks of one profession, one of another; every task named art-reviewer."""

    raw = beyondness_plan()
    template = raw["tasks"][0]
    tasks = []
    for task_id, role, depends_on in (
        ("M01", "character-artist", ()),
        ("M02", "character-artist", ()),
        ("M03", "character-artist", ()),
        ("R01", "reference-artist", ("M03",)),
    ):
        task = json.loads(json.dumps(template))
        task.update(art_task(task_id, role, depends_on=depends_on))
        task["produces_outcomes"] = list(template["produces_outcomes"])
        task["acceptance_class"] = template["acceptance_class"]
        task["priority"] = 1 if task_id == "M01" else 0
        tasks.append(task)
    raw["tasks"] = tasks
    raw["roles"].append(dict(SCULPT_REVIEWER))
    return raw


def _tickets(cfg, kind: str) -> list[dict]:
    return [
        item for item in PipelineIncidentStore(cfg.state_dir).load()["incidents"]
        if (item.get("system_state") or {}).get("stop_kind") == kind
    ]


class ThePreR30Run(DepartmentRun):
    plan_payload = staticmethod(_profession_plan)

    def make_pre_r30(self, *, m03_lead: str | None = "art-reviewer") -> None:
        """The plan a run from before R30 holds: M01 and M02 VERIFIED by two leads.

        Written as the old runtime left it - plan.json and its receipt -
        since admission today refuses the split it is made of.
        """

        from codex_autopilot.plan_verification import plan_sha256

        path = self.cfg.state_dir / "plan.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        tasks = {task["id"]: task for task in raw["tasks"]}
        tasks["M02"]["verification"]["verifier_role"] = "sculpt-reviewer"
        if m03_lead is None:
            tasks["M03"]["verification"].pop("verifier_role")
        else:
            tasks["M03"]["verification"]["verifier_role"] = m03_lead
        path.write_text(json.dumps(raw), encoding="utf-8")
        state = self.store.load()
        state.plan_verification = dict(state.plan_verification, plan_sha256=plan_sha256(self.plan()))
        state.task_states.update(M01="VERIFIED", M02="VERIFIED")
        state.task_ready_since = {
            key: value for key, value in state.task_ready_since.items() if key not in {"M01", "M02"}
        }
        self.store.save(state)


class AdmissionLeavesAcceptedTasksOutTests(unittest.TestCase):
    """validate_department_leads, called as plan_admission.graph_plan calls it."""

    def _plan(self):
        raw = _profession_plan()
        tasks = {task["id"]: task for task in raw["tasks"]}
        tasks["M02"]["verification"]["verifier_role"] = "sculpt-reviewer"
        return validate_persisted_plan(raw, "adaptive")

    def test_a_split_among_accepted_tasks_is_history_not_a_violation(self) -> None:
        """The independent check's reproduction, and what still is a violation.

        Mutation: validate_department_leads counts settled tasks in its
        one-lead check - the first call returns the split again.
        """

        plan = self._plan()
        unchanged = {task.id for task in plan.tasks}
        self.assertEqual(
            validate_department_leads(plan.tasks, plan.roles, exempt=unchanged, settled={"M01", "M02"}), []
        )
        # A task still to be accepted takes part, touched by the change or not.
        (split,) = validate_department_leads(plan.tasks, plan.roles, exempt=unchanged, settled={"M01"})
        self.assertIn("tasks of role 'character-artist' name several - art-reviewer: M03; sculpt-reviewer: M02", split)

    def test_an_exempt_task_still_to_be_accepted_is_not_left_out(self) -> None:
        """Mutation: by_role skips exempt tasks - a new task naming another lead
        than an unchanged pending one is admitted, and the gate stops both."""

        plan = self._plan()
        m03 = plan.task_map["M03"]
        m04 = replace(m03, id="M04", verification=replace(m03.verification, verifier_role="sculpt-reviewer"))
        tasks = (*plan.tasks, m04)
        found = validate_department_leads(
            tasks, plan.roles, exempt={task.id for task in plan.tasks}, settled={"M01", "M02"}
        )
        self.assertEqual(len(found), 1, found)
        self.assertIn("art-reviewer: M03; sculpt-reviewer: M04", found[0])


class TheRunTimeLeadIsTheLiveOneTests(unittest.TestCase):
    def test_each_task_reads_its_own_department(self) -> None:
        """Mutation: derive_task_department ignores ``settled`` - M03 raises the split."""

        raw = _profession_plan()
        tasks = {task["id"]: task for task in raw["tasks"]}
        tasks["M02"]["verification"]["verifier_role"] = "sculpt-reviewer"
        plan = validate_persisted_plan(raw, "adaptive")
        settled = settled_task_ids({"M01": "VERIFIED", "M02": "VERIFIED", "M03": "READY", "R01": "WAITING"})
        self.assertEqual(settled, {"M01", "M02"})
        self.assertEqual(derive_task_department(plan, plan.task_map["M03"], settled=settled).id, "art-reviewer")
        self.assertEqual(derive_task_department(plan, plan.task_map["M02"], settled=settled).id, "sculpt-reviewer")
        with self.assertRaisesRegex(DepartmentAcceptanceError, "several leads"):
            derive_task_department(plan, plan.task_map["M03"])
        # A pending task naming none takes the accepted work's lead only when
        # that was one - here it was two, and the diagnosis says so.
        tasks["M03"]["verification"].pop("verifier_role")
        plan = validate_persisted_plan(raw, "adaptive")
        with self.assertRaisesRegex(DepartmentAcceptanceError, "accepted tasks were judged by several leads"):
            derive_task_department(plan, plan.task_map["M03"], settled=settled)


class TheRestOfTheProfessionIsJudgedByItsLeadTests(ThePreR30Run):
    def test_worker_lead_and_verdict_all_read_the_live_lead(self) -> None:
        """M03 of a pre-R30 run is judged by its profession's one live lead.

        Mutations, each alone, each fails here: admit_verifier without
        ``settled`` (a department_lead stop instead of a lead);
        build_descriptor's verifier_route without it (the reservation
        raises); AIStudioRuntime.build_prompt without it (the worker is told
        no lead is defined; the lead's prompt cannot be built);
        complete_desktop_worker's verdict_acceptance or verifier_route
        without it (the verdict raises); authorize_rubric_proposal without it.
        """

        from codex_autopilot.department_runtime import authorize_rubric_proposal

        self.make_pre_r30()
        (worker,) = self.reserve()
        self.assertEqual(worker.task_id, "M03")
        self.assertIn("accepted by Lead Role 'Character Art Verifier'", worker.prompt)
        outcome = self.implement(worker, "worker-M03")
        self.assertEqual(_tickets(self.cfg, "department_lead"), [])
        (lead,) = [item for item in outcome.descriptors if item.kind == "verifier"]
        self.assertEqual(lead.task_id, "M03")
        self.assertIn("Lead Role 'Character Art Verifier' - you", lead.prompt)
        self.assertIn('"department_acceptance"', lead.prompt)
        self.mark_active(lead.reservation_token, "lead-M03")
        self.assertEqual(
            authorize_rubric_proposal(self.root, "art-reviewer", "lead-M03"), "Character Art Verifier"
        )
        self.judge(lead, "lead-M03", self.verdict("M03"))
        self.assertEqual(self.store.load().task_states["M03"], "VERIFIED")

    def test_an_accepted_tasks_first_verdict_counts_in_its_own_department(self) -> None:
        """The second-lead ordinal reads a settled task's department from its own lead.

        Mutation: second_lead_gate or _first_verdict_of without ``settled`` -
        M01's earlier acceptance is not counted (or M03 has no department),
        and the second acceptance of art-reviewer is not measured.
        """

        from codex_autopilot.config import load_config

        path = self.cfg.state_dir / "config.toml"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("[runtime]\n", "[runtime]\nsecond_lead_every = 2\n", 1), encoding="utf-8")
        self.cfg = load_config(self.root)
        # M01 is accepted while every task still named art-reviewer: the
        # department's first acceptance, not measured (1 of every 2).
        (first,) = self.reserve()
        self.assertEqual(first.task_id, "M01")
        first_lead = next(item for item in self.implement(first, "worker-M01").descriptors if item.kind == "verifier")
        (worker,) = self.judge(first_lead, "lead-M01", self.verdict("M01")).descriptors
        self.assertEqual((worker.kind, worker.task_id), ("implementation", "M03"))
        self.assertEqual(self.store.load().task_states["M01"], "VERIFIED")
        self.make_pre_r30()
        lead = next(item for item in self.implement(worker, "worker-M03").descriptors if item.kind == "verifier")
        deferred = self.judge(lead, "lead-M03", self.verdict("M03"))
        self.assertEqual(self.store.load().task_states["M03"], "VERIFYING")
        self.assertEqual([(item.kind, item.task_id) for item in deferred.descriptors], [("verifier", "M03")])


class TheScreenerIsToldTheLiveLeadTests(ThePreR30Run):
    def initialize(self, plan_file, skill) -> None:
        from _plan_contract import initialize_verified_project

        initialize_verified_project(
            self.root, plan_file, profile="adaptive", skill_path=skill,
            desktop_project_id="desktop-project", skill_screening="always",
        )

    def test_the_screening_brief_names_who_will_accept(self) -> None:
        """Mutation: build_descriptor's screening prompt without the task states,
        or build_screening_prompt ignoring them - the brief says no lead is
        defined for M03."""

        self.make_pre_r30()
        (screening,) = self.reserve()
        self.assertEqual((screening.kind, screening.task_id), ("screening", "M03"))
        self.assertIn("accepted by Lead Role 'Character Art Verifier'", screening.prompt)
        self.assertNotIn("No Lead Role is defined", screening.prompt)


class APlanChangeOnThePreR30RunTests(ThePreR30Run):
    def _request(self) -> str:
        from codex_autopilot.resilience import PLAN_CHANGE_REQUEST_PREFIX

        return PLAN_CHANGE_REQUEST_PREFIX + " " + json.dumps({
            "request_version": 1, "kind": "prerequisite", "target_task_id": "M03",
            "summary": "Add the anatomy sheet", "rationale": "M03 cannot be modelled without it.",
            "change": {"description": "Prepare the anatomy sheet.", "suggested_task_id": "P"},
            "evidence_ids": [],
        }, separators=(",", ":"))

    def _candidate(self) -> dict:
        current = self.plan()
        raw = plan_to_dict(current)
        raw["graph_version"] = current.graph_version + 1
        raw["roles"].append(dict(ANATOMY_LEAD))
        prerequisite = art_task("P", "reference-artist")
        prerequisite.update(produces_outcomes=[TEST_OUTCOME_ID], acceptance_class="mixed")
        for task in raw["tasks"]:
            if task["id"] == "M03":
                task["depends_on"] = ["P"]
                task["verification"]["verifier_role"] = "anatomy-lead"
        raw["tasks"].insert(0, prerequisite)
        return raw

    def test_is_admitted_verified_and_applied(self) -> None:
        """The M01/M02 split is left as it was; the change goes through every gate.

        Its requester M03 is also given a new lead, anatomy-lead, whose
        version-1 rubric is written at the commit - the only writer here.
        Mutations, each alone, each fails here: settled not passed by
        admit_replanner_result (refused), by plan_change_reservation's
        _unverifiable_proposal (a stop, no plan verifier) or
        _prompt_over_budget, by the plan verifier's descriptor, by
        _complete_plan_verifier, by commit_plan_change's revalidation (each
        raises), or by commit_plan_change's ensure_all_department_rubrics (no
        anatomy-lead rubric); or the replanner's constraints without the
        sentence on accepted tasks.
        """

        from codex_autopilot.plan_verification import PLAN_VERIFICATION_PREFIX
        from codex_autopilot.resilience import PLAN_CHANGE_RESULT_PREFIX

        self.make_pre_r30()
        (worker,) = self.reserve()
        self.mark_active(worker.reservation_token, "worker-M03")
        bump_task_checkpoint(self.root, "M03", "Plan change requested.")
        (replanner,) = self.complete("worker-M03", self._request()).descriptors
        self.assertEqual(replanner.kind, "replanner")
        # Told up front, so it does not spend an attempt rewriting M02.
        self.assertIn("A task already VERIFIED or CANCELLED keeps the lead that judged it", replanner.prompt)
        self.mark_active(replanner.reservation_token, "replanner-PC1")
        result = PLAN_CHANGE_RESULT_PREFIX + " " + json.dumps(
            {"request_id": "PC1", "base_graph_version": 1, "plan": self._candidate()}, separators=(",", ":")
        )
        proposed = self.complete("replanner-PC1", result)
        self.assertEqual(proposed.worker_status, "PLAN_CHANGE_PROPOSED", self.store.load().plan_changes)
        (verifier,) = proposed.descriptors
        self.assertEqual(verifier.kind, "plan_verifier")
        self.assertEqual(self.rubric_records("anatomy-lead"), [])
        self.mark_active(verifier.reservation_token, "plan-verifier-PC1")
        applied = self.complete("plan-verifier-PC1", PLAN_VERIFICATION_PREFIX + ' {"verdict":"PASS","issues":[]}')
        self.assertEqual(applied.worker_status, "PLAN_VERIFIED")
        self.assertEqual([item.task_id for item in applied.descriptors], ["P"])
        plan = self.plan()
        self.assertEqual(plan.graph_version, 2)
        self.assertEqual(plan.task_map["M02"].verification.verifier_role, "sculpt-reviewer")
        state = self.store.load()
        self.assertEqual((state.task_states["M01"], state.task_states["M02"]), ("VERIFIED", "VERIFIED"))
        self.assertEqual(len(self.rubric_records("anatomy-lead")), 1)


class TheOnCallCanNameTheLeadTests(ThePreR30Run):
    def test_a_task_naming_none_is_stopped_and_the_on_calls_change_passes(self) -> None:
        """M03 names no lead; its accepted colleagues were judged by two.

        The gate stops M03 alone with that diagnosis, and the change the
        on-call asks for - M03 names art-reviewer - is admitted. Mutation:
        state_issues derives the requester's department without
        ``settled`` - the on-call's change is refused for the split it
        cannot touch.
        """

        from codex_autopilot.plan_admission import IssueCollector, plan_change_candidate, state_issues

        self.make_pre_r30(m03_lead=None)
        (worker,) = self.reserve()
        self.implement(worker, "worker-M03")
        (ticket,) = _tickets(self.cfg, "department_lead")
        self.assertEqual(ticket["affected_task_ids"], ["M03"])
        self.assertIn("accepted tasks were judged by several leads", ticket["system_state"]["diagnosis"])
        self.assertIn("not yet accepted", ticket["system_state"]["recommendation"])
        current = self.plan()
        change = plan_to_dict(current)
        change["graph_version"] = current.graph_version + 1
        next(task for task in change["tasks"] if task["id"] == "M03")["verification"]["verifier_role"] = "art-reviewer"
        state = self.store.load()
        state.plan_changes = [{"id": "PC1", "requires_lead": True}]
        state.active_plan_change_id = "PC1"
        settled = settled_task_ids(state.task_states)
        collector = IssueCollector()
        read = plan_change_candidate(collector, current, change, "adaptive", settled=settled)
        self.assertEqual(collector.issues, [])
        self.assertEqual(state_issues(current, read, state, "M03"), [])


class NamingALeadIsNotARewriteTests(ThePreR30Run):
    def _apply(self, mutate, m02_state: str):
        from codex_autopilot.resilience import reconcile_plan_change_state

        current = self.plan()
        m02 = current.task_map["M02"]
        tasks = tuple(mutate(task) if task.id == "M02" else task for task in current.tasks)
        candidate = replace(current, tasks=tasks, graph_version=current.graph_version + 1)
        state = self.store.load()
        state.task_states["M02"] = m02_state
        state.plan_changes = [{"id": "PC1", "status": "VERIFYING"}]
        state.active_plan_change_id = "PC1"
        self.assertIsNotNone(m02)
        return reconcile_plan_change_state(current, candidate, state, request_id="PC1", requester_task_id="M03")

    @staticmethod
    def _lead(task):
        return replace(task, verification=replace(task.verification, verifier_role="sculpt-reviewer"))

    def test_an_advanced_task_takes_its_professions_lead_and_keeps_its_work(self) -> None:
        """Mutation: reconcile_plan_change_state without the lead-only branch -
        an IMPLEMENTED M02 cannot take the lead, and three leads on three
        advanced tasks of one profession could never become one."""

        from codex_autopilot.resilience import PlanChangeConflictError

        state = self._apply(self._lead, "IMPLEMENTED")
        self.assertEqual(state.task_states["M02"], "IMPLEMENTED")
        applied = next(item for item in state.resilience_journal if item.get("event") == "plan_change_applied")
        self.assertNotIn("M02", applied["detail"]["affected_task_ids"])
        # Anything more than the lead is still a rewrite of work under way,
        # and a CANCELLED task stays as it was.
        with self.assertRaisesRegex(PlanChangeConflictError, "advanced task M02"):
            self._apply(lambda task: replace(self._lead(task), objective="Something else."), "IMPLEMENTED")
        with self.assertRaisesRegex(PlanChangeConflictError, "advanced task M02"):
            self._apply(self._lead, "CANCELLED")


if __name__ == "__main__":
    unittest.main()
