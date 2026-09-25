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

What the second check found in that exemption, and what closes it. It was
not limited to history: on a plan that already met R30 - the art-run
shape, M01 VERIFIED by art-reviewer - a change moving M03 to a new
"lax-lead" whose only expectation was "Anything goes." was admitted with no
issue, and the new department would have started from a fresh rubric
version 1 built from a profile the replanner wrote. Now a settled task is
history only as the current plan holds it, and a profession with accepted
work keeps its lead: one of those that accepted it, or the one the current
plan names for the rest; a task under way takes only a lead its profession
already names.
"""

from __future__ import annotations

from dataclasses import replace
import json
import unittest

from _departments import DepartmentRun, art_task, art_run_plan
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

    raw = art_run_plan()
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
            validate_department_leads(
                plan.tasks, plan.roles, exempt=unchanged, settled={"M01", "M02"}, inherited=plan
            ), []
        )
        # A task still to be accepted takes part, touched by the change or not.
        (split,) = validate_department_leads(
            plan.tasks, plan.roles, exempt=unchanged, settled={"M01"}, inherited=plan
        )
        self.assertIn("tasks of role 'character-artist' name several - art-reviewer: M03; sculpt-reviewer: M02", split)

    def test_the_split_is_history_only_as_the_current_plan_holds_it(self) -> None:
        """A settled task whose lead the change rewrote is not history.

        Mutation: ``_history`` takes every settled task, whatever the
        current plan says of it - the split M01's rewrite creates is not
        reported. Without the current plan settled means nothing.
        """

        current = self._plan()
        m01 = current.task_map["M01"]
        tasks = tuple(
            replace(m01, verification=replace(m01.verification, verifier_role="sculpt-reviewer"))
            if task.id == "M01" else task for task in current.tasks
        )
        found = validate_department_leads(
            tasks, current.roles, exempt=set(current.task_map) - {"M01"}, settled={"M01", "M02"},
            inherited=current,
        )
        self.assertEqual(len(found), 1, found)
        self.assertIn("art-reviewer: M03; sculpt-reviewer: M01", found[0])
        self.assertEqual(len(validate_department_leads(current.tasks, current.roles, settled={"M01", "M02"})), 1)

    def test_an_exempt_task_still_to_be_accepted_is_not_left_out(self) -> None:
        """Mutation: by_role skips exempt tasks - a new task naming another lead
        than an unchanged pending one is admitted, and the gate stops both."""

        plan = self._plan()
        m03 = plan.task_map["M03"]
        m04 = replace(m03, id="M04", verification=replace(m03.verification, verifier_role="sculpt-reviewer"))
        tasks = (*plan.tasks, m04)
        found = validate_department_leads(
            tasks, plan.roles, exempt={task.id for task in plan.tasks}, settled={"M01", "M02"}, inherited=plan
        )
        self.assertEqual(len(found), 1, found)
        self.assertIn("art-reviewer: M03; sculpt-reviewer: M04", found[0])


class AProfessionKeepsItsLeadTests(unittest.TestCase):
    """plan_admission.plan_change_candidate, called as the replanner's admission calls it."""

    LAX_LEAD = {
        "id": "lax-lead", "name": "Lax Lead", "version": "1.0.0",
        "responsibilities": ["Accept anything."], "verification_expectations": ["Anything goes."],
    }

    def _admit(self, current, m03_lead: str, settled):
        from codex_autopilot.plan_admission import IssueCollector, plan_change_candidate

        raw = plan_to_dict(current)
        raw["graph_version"] = current.graph_version + 1
        if "lax-lead" not in current.role_map:
            raw["roles"].append(dict(self.LAX_LEAD))
        next(task for task in raw["tasks"] if task["id"] == "M03")["verification"]["verifier_role"] = m03_lead
        collector = IssueCollector()
        plan_change_candidate(collector, current, raw, "adaptive", settled=settled)
        return [issue.message for issue in collector.issues]

    def test_the_second_checks_reproduction_on_the_art_run_shape_is_refused(self) -> None:
        """M01 VERIFIED by art-reviewer; M03 moved to lax-lead ("Anything goes.").

        Admitted with 0 issues before this fix, the new department starting
        from a fresh rubric v1 the replanner wrote. Mutation:
        validate_department_leads without ``found.extend(moved.values())`` -
        the list is empty.
        """

        current = validate_persisted_plan(art_run_plan(), "adaptive")
        (split,) = self._admit(current, "lax-lead", set())
        self.assertIn("name several - art-reviewer: M01; lax-lead: M03", split)
        (moved,) = self._admit(current, "lax-lead", {"M01"})
        self.assertIn("a profession keeps its lead once its work has been accepted", moved)
        self.assertIn("tasks of role 'character-artist' name 'lax-lead' (M03)", moved)
        self.assertIn("judged by ['art-reviewer']", moved)
        # Before any acceptance a profession's lead is still the planner's to name.
        raw = plan_to_dict(current)
        raw["roles"].append(dict(self.LAX_LEAD))
        for task in raw["tasks"]:
            if task["role"] == "character-artist":
                task["verification"]["verifier_role"] = "lax-lead"
        self.assertEqual(self._admit(validate_persisted_plan(raw, "adaptive"), "lax-lead", set()), [])

    def test_the_lead_the_current_plan_names_for_the_rest_may_be_kept(self) -> None:
        """A run from before R30: M01 accepted by art-reviewer, M03 names sculpt-reviewer.

        Keeping sculpt-reviewer or taking art-reviewer is admitted; lax-lead
        is not. Mutation: ``_moved_leads`` without the current plan's leads
        (``kept``) - keeping M03's own lead is refused, and the pre-R30 run
        can never be changed without moving work already under way.
        """

        raw = art_run_plan()
        raw["roles"].append(dict(SCULPT_REVIEWER))
        next(task for task in raw["tasks"] if task["id"] == "M03")["verification"]["verifier_role"] = "sculpt-reviewer"
        current = validate_persisted_plan(raw, "adaptive")
        self.assertEqual(self._admit(current, "sculpt-reviewer", {"M01"}), [])
        self.assertEqual(self._admit(current, "art-reviewer", {"M01"}), [])
        (moved,) = self._admit(current, "lax-lead", {"M01"})
        self.assertIn("the current plan names ['sculpt-reviewer'] for the rest", moved)


class APreR30LeadThatCannotLeadLocksNothingTests(unittest.TestCase):
    """The third check's gaps: accepted work whose lead could never lead today.

    Before R30 a task could name no verifier_role - the verifier was
    ``verifier_role or task.role`` - or its own role, and both were
    admitted; so was work verified by the generic legacy-worker. Reproduced
    by calling plan_change_candidate as the replanner's admission does, on
    the art-run shape with M01 VERIFIED: the gate stopped M03 for want of
    a lead, the on-call's change naming art-reviewer was refused with
    "judged by ['None']" (or ['character-artist']), naming the profession
    itself was refused as its own lead - no change could pass and the stop
    went to her. And a CANCELLED task, judged by no one, locked its
    profession's lead as if it had been accepted.
    """

    def _current(self, m01_lead: str | None, *, m03_lead: str | None = None):
        raw = art_run_plan()
        for task in raw["tasks"]:
            if task["role"] != "character-artist":
                continue
            lead = m01_lead if task["id"] == "M01" else m03_lead
            if lead is None:
                task["verification"].pop("verifier_role", None)
            else:
                task["verification"]["verifier_role"] = lead
        return validate_persisted_plan(raw, "adaptive")

    def _on_call_names(self, current, lead: str, task_states: dict) -> list[str]:
        from codex_autopilot.plan_admission import IssueCollector, plan_change_candidate

        change = plan_to_dict(current)
        change["graph_version"] = current.graph_version + 1
        next(task for task in change["tasks"] if task["id"] == "M03")["verification"]["verifier_role"] = lead
        collector = IssueCollector()
        plan_change_candidate(collector, current, change, "adaptive", settled=settled_task_ids(task_states))
        return [issue.message for issue in collector.issues]

    def test_a_profession_accepted_with_no_or_its_own_lead_gets_one_from_the_on_call(self) -> None:
        """M01 VERIFIED with no lead / its own role; M03 has no lead.

        The gate stops M03; the on-call's change naming art-reviewer is
        admitted, and after it M03's department is art-reviewer's.
        Mutation: ``_moved_leads`` counting every history task's lead
        (without ``_can_lead`` on ``accepted``) - each variant is refused
        with "judged by ['None']" / ['character-artist'].
        """

        for m01_lead in (None, "character-artist"):
            with self.subTest(m01_lead=m01_lead):
                current = self._current(m01_lead)
                with self.assertRaisesRegex(DepartmentAcceptanceError, "R30"):
                    derive_task_department(current, current.task_map["M03"], settled={"M01"})
                self.assertEqual(self._on_call_names(current, "art-reviewer", {"M01": "VERIFIED"}), [])
                raw = plan_to_dict(current)
                next(task for task in raw["tasks"] if task["id"] == "M03")["verification"]["verifier_role"] = "art-reviewer"
                changed = validate_persisted_plan(raw, "adaptive")
                department = derive_task_department(changed, changed.task_map["M03"], settled={"M01"})
                self.assertEqual(department.lead_role_id, "art-reviewer")

    def test_the_current_plans_own_lead_for_the_rest_is_not_a_lead_to_keep(self) -> None:
        """M01 VERIFIED and M03 both name character-artist, the pre-R30 own-lead case.

        The on-call's art-reviewer is admitted. And where the accepted work
        did have a lead (M01 by art-reviewer) while the rest names the
        profession itself, a new lead is refused naming only art-reviewer:
        the refusal must not offer the replanner a lead admission refuses.
        Mutation: ``kept`` without ``_can_lead`` - the refusal says "the
        current plan names ['character-artist']".
        """

        current = self._current("character-artist", m03_lead="character-artist")
        self.assertEqual(self._on_call_names(current, "art-reviewer", {"M01": "VERIFIED"}), [])
        current = self._current("art-reviewer", m03_lead="character-artist")
        (moved,) = [item for item in self._on_call_names(current, "reference-artist", {"M01": "VERIFIED"})
                    if "keeps its lead" in item]
        self.assertIn("judged by ['art-reviewer'] and the current plan names no lead for the rest", moved)

    def test_work_accepted_by_the_generic_legacy_worker_locks_no_lead(self) -> None:
        """A migrated task VERIFIED by legacy-worker, as a legacy_serial plan holds it.

        Called as graph_plan calls validate_department_leads (adaptive
        admission refuses a legacy-worker verifier outright, so the plans
        are built by hand). The change naming art-reviewer for M03 passes.
        Mutation: ``_can_lead`` without the legacy-worker clause - refused
        with "judged by ['legacy-worker']".
        """

        base = validate_persisted_plan(art_run_plan(), "adaptive")
        legacy = replace(base.role_map["art-reviewer"], id="legacy-worker", name="Legacy Serial Worker")

        def with_leads(m01: str, m03: str):
            tasks = tuple(
                replace(task, verification=replace(task.verification, verifier_role={"M01": m01, "M03": m03}[task.id]))
                if task.id in {"M01", "M03"} else task
                for task in base.tasks
            )
            return replace(base, roles=(*base.roles, legacy), tasks=tasks)

        current = with_leads("legacy-worker", "legacy-worker")
        changed = with_leads("legacy-worker", "art-reviewer")
        found = validate_department_leads(
            changed.tasks, changed.roles, exempt={"M01", "M02"},
            settled=settled_task_ids({"M01": "VERIFIED"}), inherited=current, report_unknown=False,
        )
        self.assertEqual(found, [])

    def test_a_cancelled_task_locks_no_lead(self) -> None:
        """M01 CANCELLED under art-reviewer; M03 moved to lax-lead before any acceptance.

        Admitted: no lead judged M01. The same change with M01 VERIFIED is
        refused. Mutation: ``_accepted_ids`` returning every settled id
        (CANCELLED counted as accepted) - the CANCELLED case is refused with
        "judged by ['art-reviewer']".
        """

        current = validate_persisted_plan(art_run_plan(), "adaptive")
        lax = AProfessionKeepsItsLeadTests.LAX_LEAD

        def admit(state: str) -> list[str]:
            from codex_autopilot.plan_admission import IssueCollector, plan_change_candidate

            change = plan_to_dict(current)
            change["graph_version"] = current.graph_version + 1
            change["roles"].append(dict(lax))
            next(task for task in change["tasks"] if task["id"] == "M03")["verification"]["verifier_role"] = "lax-lead"
            collector = IssueCollector()
            plan_change_candidate(collector, current, change, "adaptive",
                                  settled=settled_task_ids({"M01": state, "M02": "READY"}))
            return [issue.message for issue in collector.issues]

        self.assertEqual(admit("CANCELLED"), [])
        (moved,) = admit("VERIFIED")
        self.assertIn("judged by ['art-reviewer']", moved)
        settled = settled_task_ids({"M01": "VERIFIED", "M02": "CANCELLED", "M03": "READY"})
        self.assertEqual((set(settled), set(settled.accepted)), ({"M01", "M02"}, {"M01"}))


class TheStatusNamesTheLeadTests(DepartmentRun):
    def test_a_verifying_task_without_a_session_shows_its_professions_lead(self) -> None:
        """status._active_title named the worker's profession as the verifier.

        A task from before R30 without its own verifier_role, VERIFYING with
        no session descriptor: the title names the lead its profession has,
        and "No Lead Role" when there is none - never the worker. Mutation:
        the old ``verifier_role or task.role`` fallback - "Character Artist".
        """

        from codex_autopilot.status import project_status_snapshot

        raw = art_run_plan(lead_on_every_task=False)
        plan = validate_persisted_plan(raw, "adaptive")
        self.assertIsNone(plan.task_map["M03"].verification.verifier_role)
        state = self.store.load()
        state.task_states.update(M01="VERIFIED", M02="VERIFIED", M03="VERIFYING")
        (item,) = project_status_snapshot(self.cfg, state, plan)["verifying"]
        self.assertEqual(item["active_title"], "Character Art Verifier | Verify M03 | Model part M03")
        for task in raw["tasks"]:
            task["verification"].pop("verifier_role", None)
        (item,) = project_status_snapshot(self.cfg, state, validate_persisted_plan(raw, "adaptive"))["verifying"]
        self.assertTrue(item["active_title"].startswith("No Lead Role | Verify M03"), item["active_title"])


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

    def _candidate(self, m03_lead: str = "sculpt-reviewer") -> dict:
        current = self.plan()
        raw = plan_to_dict(current)
        raw["graph_version"] = current.graph_version + 1
        raw["roles"].append(dict(ANATOMY_LEAD))
        prerequisite = art_task("P", "reference-artist")
        prerequisite.update(produces_outcomes=[TEST_OUTCOME_ID], acceptance_class="mixed")
        for task in raw["tasks"]:
            if task["id"] == "M03":
                task["depends_on"] = ["P"]
                task["verification"]["verifier_role"] = m03_lead
        raw["tasks"].insert(0, prerequisite)
        return raw

    def _to_replanner(self):
        self.make_pre_r30()
        (worker,) = self.reserve()
        self.mark_active(worker.reservation_token, "worker-M03")
        bump_task_checkpoint(self.root, "M03", "Plan change requested.")
        (replanner,) = self.complete("worker-M03", self._request()).descriptors
        self.assertEqual(replanner.kind, "replanner")
        self.mark_active(replanner.reservation_token, "replanner-PC1")
        return replanner

    def _result(self, m03_lead: str = "sculpt-reviewer") -> str:
        from codex_autopilot.resilience import PLAN_CHANGE_RESULT_PREFIX

        return PLAN_CHANGE_RESULT_PREFIX + " " + json.dumps(
            {"request_id": "PC1", "base_graph_version": 1, "plan": self._candidate(m03_lead)},
            separators=(",", ":"),
        )

    def test_is_admitted_verified_and_applied(self) -> None:
        """The M01/M02 split is left as it was; the change goes through every gate.

        Its requester M03 is moved to sculpt-reviewer - one of the two leads
        its profession's accepted work had, the choice there is when that
        work had several - whose version-1 rubric is written at the commit,
        the only writer here. This test first gave M03 a brand-new
        anatomy-lead and called that admitted: the second check named it as
        the widening it locked in, and it is refused now (below).
        Mutations, each alone, each fails here: settled not passed by
        admit_replanner_result (refused), by plan_change_reservation's
        _unverifiable_proposal (a stop, no plan verifier) or
        _prompt_over_budget, by the plan verifier's descriptor, by
        _complete_plan_verifier, by commit_plan_change's revalidation (each
        raises), or by commit_plan_change's ensure_all_department_rubrics (no
        sculpt-reviewer rubric); graph_plan not passing the current plan to
        validate_department_leads (settled means nothing then, the split is
        refused); or the replanner's constraints without the sentences on
        accepted tasks.
        """

        from codex_autopilot.plan_verification import PLAN_VERIFICATION_PREFIX

        replanner = self._to_replanner()
        # Told up front, so it does not spend an attempt rewriting M02.
        self.assertIn("A task already VERIFIED or CANCELLED keeps the lead that judged it", replanner.prompt)
        self.assertIn("Once a profession's work is accepted its lead does not change", replanner.prompt)
        self.assertEqual(self.rubric_records("sculpt-reviewer"), [])
        proposed = self.complete("replanner-PC1", self._result())
        self.assertEqual(proposed.worker_status, "PLAN_CHANGE_PROPOSED", self.store.load().plan_changes)
        (verifier,) = proposed.descriptors
        self.assertEqual(verifier.kind, "plan_verifier")
        self.assertEqual(self.rubric_records("sculpt-reviewer"), [])
        self.mark_active(verifier.reservation_token, "plan-verifier-PC1")
        applied = self.complete("plan-verifier-PC1", PLAN_VERIFICATION_PREFIX + ' {"verdict":"PASS","issues":[]}')
        self.assertEqual(applied.worker_status, "PLAN_VERIFIED")
        self.assertEqual([item.task_id for item in applied.descriptors], ["P"])
        plan = self.plan()
        self.assertEqual(plan.graph_version, 2)
        self.assertEqual(plan.task_map["M02"].verification.verifier_role, "sculpt-reviewer")
        state = self.store.load()
        self.assertEqual((state.task_states["M01"], state.task_states["M02"]), ("VERIFIED", "VERIFIED"))
        self.assertEqual(plan.task_map["M03"].verification.verifier_role, "sculpt-reviewer")
        self.assertEqual(len(self.rubric_records("sculpt-reviewer")), 1)
        self.assertEqual(self.rubric_records("anatomy-lead"), [])

    def test_a_new_lead_for_the_rest_of_the_profession_goes_back_to_the_replanner(self) -> None:
        """What this class's first test used to admit: M03 moved to anatomy-lead.

        Refused at admission, back to the replanner with the leads it may
        choose among - nothing committed, no rubric written. Mutation:
        validate_department_leads without the moved-lead check
        (``found.extend(moved.values())``) - the change is proposed.
        """

        self._to_replanner()
        rejected = self.complete("replanner-PC1", self._result("anatomy-lead"))
        self.assertEqual(rejected.worker_status, "PLAN_CHANGE_REJECTED")
        (rejection,) = self.store.load().plan_changes[0]["rejections"]
        self.assertIn("a profession keeps its lead once its work has been accepted", rejection["reason"])
        self.assertIn("judged by ['art-reviewer', 'sculpt-reviewer']", rejection["reason"])
        self.assertEqual(self.plan().graph_version, 1)
        self.assertEqual(self.rubric_records("anatomy-lead"), [])


class TheOnCallCanNameTheLeadTests(ThePreR30Run):
    def test_a_task_naming_none_is_stopped_and_the_on_calls_change_passes(self) -> None:
        """M03 names no lead; its accepted colleagues were judged by two.

        The stop used to come at M03's lead reservation, after its worker
        had run (the gate was the first to notice). The roster notices it
        before M03 starts (staffing): one ticket holds M03 - the only task
        still to be accepted - with the derivation's words for the on-call,
        and the change it asks for - M03 names art-reviewer - is admitted.
        The run is under way (M01 and M02 accepted), so the ticket holds
        only what the roster leaves unstaffed - M03 - and R01 is not held
        by it. The first staffing commit held R01 too ("a roster that did
        not assemble starts nothing"); the second independent check
        (25 Sep 2026) showed that hold freezing sound departments mid-run,
        and a stop under way holds its own tasks again.
        Mutations: state_issues derives the requester's department without
        ``settled`` - the on-call's change is refused for the split it
        cannot touch; staffing._stop holds every unsettled task under way -
        R01 is held.
        """

        from codex_autopilot.engineer_reservation import tasks_paused_by_incidents

        from codex_autopilot.plan_admission import IssueCollector, plan_change_candidate, state_issues

        self.make_pre_r30(m03_lead=None)
        (engineer,) = self.reserve()
        self.assertEqual(engineer.kind, "pipeline_engineer")
        (ticket,) = _tickets(self.cfg, "staffing")
        self.assertEqual(ticket["affected_task_ids"], ["M03"])
        self.assertNotIn("R01", tasks_paused_by_incidents(self.cfg, self.plan()))
        self.assertIn("missing for: M03", ticket["system_state"]["diagnosis"])
        self.assertIn("accepted tasks were judged by several leads", ticket["system_state"]["diagnosis"])
        self.assertIn("still to be accepted", ticket["system_state"]["recommendation"])
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
    def _apply(self, mutate, m02_state: str, *, leads: dict | None = None):
        """M02 changed by ``mutate`` while in ``m02_state``, on the current plan
        a run from before R30 holds: the profession's pending tasks split
        between art-reviewer (M01, M02) and sculpt-reviewer (M03), or ``leads``."""

        from codex_autopilot.resilience import reconcile_plan_change_state

        current = self.plan()
        named = leads if leads is not None else {"M03": "sculpt-reviewer"}
        current = replace(current, tasks=tuple(
            replace(task, verification=replace(task.verification, verifier_role=named[task.id]))
            if task.id in named else task for task in current.tasks
        ))
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

    def test_work_under_way_is_never_moved_to_a_lead_its_profession_never_had(self) -> None:
        """The second check: names_only_its_lead let tasks under way be moved.

        A new lead for work already done is a new standard for it; the
        change is refused at the commit and goes back to the replanner.
        When the profession names no lead at all (a run from before R30 whose
        tasks left it out), any lead is its first. Mutations: the reconcile
        branch without the ``named`` check (no conflict), or without
        ``not named`` (the first lead of a leaderless profession refused).
        """

        from codex_autopilot.resilience import PlanChangeConflictError

        def moved(task):
            return replace(task, verification=replace(task.verification, verifier_role="reference-artist"))

        with self.assertRaisesRegex(
            PlanChangeConflictError, r"advanced task M02 can take only a lead its profession already "
            r"has \['art-reviewer', 'sculpt-reviewer'\]",
        ):
            self._apply(moved, "IMPLEMENTED")
        leaderless = {task_id: None for task_id in ("M01", "M02", "M03")}
        state = self._apply(moved, "IMPLEMENTED", leads=leaderless)
        self.assertEqual(state.task_states["M02"], "IMPLEMENTED")


if __name__ == "__main__":
    unittest.main()
