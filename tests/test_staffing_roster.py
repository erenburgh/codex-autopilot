"""What the roster carries, and what a stop under way holds (staffing, second check).

The independent check of 25 Sep 2026 deleted parts of the roster in a copy
of staffing.py - the acceptance rules, the skills, the roots audit, the
unknown-worker-role check - and every test still passed: nothing read them.
Each part is asserted here as the runtime writes it (``refresh_roster``, the
form the bootstrap, a committed plan change and the gate call).

It also found a roster that stopped assembling mid-run holding the whole
run: two departments in parallel, one rubric unreadable after the start,
and the other department's finished task stood IMPLEMENTED with no
acceptance. Before the start a roster that does not assemble starts
nothing; under way it holds the tasks it leaves unstaffed, and the rest go
on - through the production lifecycle here.

The third check found the run's facts frozen at the bootstrap: the
preflight's isolation record, written after it, and a roots audit recorded
at a wake-up never reached a complete roster of the current plan, nor the
board; and a task stopped without a ticket showed the run's last error, not
its own stop's reason (TheRunsFactsFollowTheRunTests,
AStoppedTaskShowsItsOwnReasonTests).
"""

from __future__ import annotations

from dataclasses import replace
from unittest import mock

from _departments import DepartmentRun, art_run_plan
from codex_autopilot.pipeline_engineer import PipelineIncidentStore


def _tickets(cfg, kind: str) -> list[dict]:
    return [
        item for item in PipelineIncidentStore(cfg.state_dir).load()["incidents"]
        if (item.get("system_state") or {}).get("stop_kind") == kind
    ]


def _full_shape() -> dict:
    """the art run, with M02 taking M01's output and delivering a file of its own."""

    raw = art_run_plan()
    m02 = raw["tasks"][1]
    m02["context"]["dependency_outputs"] = ["M01"]
    m02["outputs"] = [{"id": "sheet", "description": "The reference sheet.",
                       "path": "Art/M02/sheet.png", "required": True}]
    return raw


def _two_departments() -> dict:
    """Two departments at once: character work led by art-reviewer, reference work by character-artist."""

    raw = art_run_plan()
    raw["execution_strategy"] = "parallel"
    raw["max_parallel_workers"] = 3
    for task in raw["tasks"]:
        task["depends_on"] = []
    raw["tasks"][1]["verification"]["verifier_role"] = "character-artist"
    return raw


class TheRosterCarriesWhatItNamesTests(DepartmentRun):
    plan_payload = staticmethod(_full_shape)

    def roster(self, **kwargs):
        from codex_autopilot.staffing import refresh_roster

        return refresh_roster(self.cfg.state_dir, self.plan(), self.store.load(), occasion="test", **kwargs)

    def entry(self, roster, task_id: str) -> dict:
        return next(item for item in roster["tasks"] if item["id"] == task_id)

    def test_acceptance_model_dependencies_outputs_and_placement(self) -> None:
        """Mutations: _acceptance returns {}; _model returns "host settings";
        dependency_outputs or outputs dropped; placement always "root".
        """

        from codex_autopilot.models import MODEL_IDS, logical_model

        roster = self.roster()
        self.assertTrue(roster["complete"], roster["issues"])
        m01, m02 = self.entry(roster, "M01"), self.entry(roster, "M02")
        self.assertEqual(m01["acceptance"], {
            "acceptance_class": "mixed", "policy": "independent", "required": True,
            "deterministic_checks": ["suite"], "clean_suite": True, "max_revision_attempts": 2,
        })
        self.assertEqual(m01["model"], {
            "model": MODEL_IDS[logical_model("auto", "code")], "effort": "medium", "execution_mode": "code",
        })
        self.assertNotEqual(m01["model"]["model"], "host settings")
        self.assertEqual((m02["depends_on"], m02["dependency_outputs"], m02["outputs"]), (["M01"], ["M01"], ["sheet"]))
        self.assertEqual((m01["dependency_outputs"], m01["outputs"]), ([], []))
        # M02 writes a file through a write claim: it is staged; M01 declares no file.
        self.assertEqual(m02["placement"], {"workspace": "staged", "cwd": "staged workspace"})
        self.assertEqual(m01["placement"], {"workspace": "root", "cwd": "root"})
        self.assertEqual(roster["run"]["model_strategy"], "auto")

    def test_a_suite_that_is_not_clean_is_not_the_r29_suite(self) -> None:
        """Mutation: clean_suite always True."""

        from codex_autopilot.plan import VerificationCheck
        from codex_autopilot.staffing import build_roster

        plan = self.plan()
        loose = VerificationCheck(id="suite", kind="command", description="The suite, with her session.",
                                  argv=("python3", "-m", "unittest"))
        m03 = replace(plan.tasks[2], verification=replace(plan.tasks[2].verification, deterministic_checks=(loose,)))
        plan = replace(plan, tasks=(*plan.tasks[:2], m03))
        roster = build_roster(plan, self.store.load(), state_dir=self.cfg.state_dir, memory=self.memory,
                              screening=False, occasion="test")
        acceptance = self.entry(roster, "M03")["acceptance"]
        self.assertEqual((acceptance["deterministic_checks"], acceptance["clean_suite"]), (["suite"], False))

    def test_isolation_names_the_contract_the_dispatcher_will_use(self) -> None:
        """Not measured, a PASS of this run, a PASS measured by another binary.

        Mutations: staged_cwd without the measurement (always contract 1, or
        always 2 once PASS); ``proven`` from the root alone instead of
        record_matches - the PASS of another binary reads as contract 2.
        """

        from codex_autopilot.isolation_probe import (
            RECORD_VERSION, binary_identity, probe_workspace, runtime_code_identity, write_record,
        )

        roster = self.roster()
        isolation = roster["run"]["isolation"]
        self.assertEqual((isolation["outcome"], isolation["proven"]), ("NOT_MEASURED", False))
        self.assertIn("contract 1", isolation["staged_cwd"])
        record = {
            "version": RECORD_VERSION, "root": str(self.cfg.root), "outcome": "PASS",
            "workspace": str(probe_workspace(self.cfg.state_dir)),
            "base_profile": self.cfg.desktop.permission_profile,
            "codex_binary": binary_identity(self.cfg.desktop.binary),
            "runtime_code": runtime_code_identity(), "measured_at": "2026-09-25T00:00:00+00:00",
        }
        write_record(self.cfg.state_dir, record)
        roster = self.roster()
        isolation = roster["run"]["isolation"]
        self.assertEqual((isolation["outcome"], isolation["proven"], isolation["of_this_root"]), ("PASS", True, True))
        self.assertIn("contract 2", isolation["staged_cwd"])
        self.assertEqual(self.entry(roster, "M02")["placement"]["cwd"], "root (staged profile)")
        # Measured by another binary: the dispatcher measures again and uses
        # contract 1 meanwhile - the roster says the same.
        write_record(self.cfg.state_dir, {**record, "codex_binary": "/elsewhere/codex:1:1"})
        roster = self.roster()
        self.assertEqual((roster["run"]["isolation"]["proven"], roster["run"]["isolation"]["of_this_root"]), (False, True))
        self.assertEqual(self.entry(roster, "M02")["placement"]["cwd"], "staged workspace")

    def test_the_roots_audit_reaches_the_roster_and_the_board(self) -> None:
        """Mutations: _run_section drops the findings; board_summary drops "roots: N findings"."""

        from codex_autopilot.board import board_rows, board_summary
        from codex_autopilot.project_roots_audit import SIBLING_ROOTS, RootsAudit, RootsFinding, record_roots_audit

        roster = self.roster()
        self.assertEqual(roster["run"]["roots_audit"], {"recorded": False, "findings": []})
        state = self.store.load()
        finding = RootsFinding(code=SIBLING_ROOTS, status="proposed", project_id="desktop-project",
                               path=str(self.root.parent / "sibling"), detail="A sibling root is saved.")
        audit = RootsAudit(target=str(self.root), desktop_project_id="desktop-project", selected_project_id=None,
                           linked_project_id=None, codex_home=None, checked=[SIBLING_ROOTS], findings=[finding])
        record_roots_audit(state, audit, {}, occasion="preflight")
        self.store.save(state)
        roster = self.roster()
        self.assertEqual(roster["run"]["roots_audit"],
                         {"recorded": True, "findings": [{"code": SIBLING_ROOTS, "status": "proposed"}]})
        plan = self.plan()
        summary = board_summary(board_rows(self.cfg, plan, self.store.load()), roster, plan, "en")
        self.assertTrue(summary.endswith("roots: 1 findings"), summary)

    def test_skills_known_at_start(self) -> None:
        """A hire for this contract, a hire for another graph version, and screening ahead.

        Mutations: _skills returns {}; the hire read without its graph
        version - M02's hire for vanished work reads "screened".
        """

        from codex_autopilot.skill_packs import SkillReference
        from codex_autopilot.skill_screening import HiringDecision, HiringOutcome, record_hiring
        from codex_autopilot.staffing import build_roster

        state = self.store.load()
        hire = HiringOutcome(capability="mesh-modelling", rationale="Model the part.", necessity="required",
                             status="hired", skill=SkillReference("blender-pack", "1.0.0"))
        unmet = HiringOutcome(capability="sculpting", rationale="Sculpt details.", necessity="helpful",
                              status="unmet", reason="none in the catalog")
        record_hiring(state.task_hiring, task_id="M01", graph_version=state.graph_version,
                      decision=HiringDecision("M01", (hire, unmet)), requisition=None,
                      screened_by={"thread_id": "thread-s1"}, at="2026-09-25T00:00:00+00:00")
        record_hiring(state.task_hiring, task_id="M02", graph_version=state.graph_version + 1,
                      decision=HiringDecision("M02", (hire,)), requisition=None,
                      screened_by={"thread_id": "thread-s0"}, at="2026-09-24T00:00:00+00:00")
        self.store.save(state)
        roster = self.roster()  # screening off in this project's config
        self.assertEqual(self.entry(roster, "M01")["skills"],
                         {"planned": [], "hired": ["blender-pack"], "screening": "screened"})
        self.assertEqual(self.entry(roster, "M02")["skills"], {"planned": [], "hired": [], "screening": "off"})
        roster = build_roster(self.plan(), self.store.load(), state_dir=self.cfg.state_dir, memory=self.memory,
                              screening=True, occasion="test")
        self.assertEqual(self.entry(roster, "M03")["skills"]["screening"], "at task start")
        self.assertEqual(self.entry(roster, "M01")["skills"]["screening"], "screened")

    def test_a_worker_role_the_plan_does_not_have_is_a_violation(self) -> None:
        """The lead check derives a department for it; only the role check names it.

        Mutation: collect_graph_issues without the unknown-worker-role check -
        the roster assembles.
        """

        from codex_autopilot.staffing import build_roster

        plan = self.plan()
        plan = replace(plan, tasks=(replace(plan.tasks[0], role="ghost-artist"), *plan.tasks[1:]))
        roster = build_roster(plan, self.store.load(), state_dir=self.cfg.state_dir, memory=self.memory,
                              screening=False, occasion="test")
        self.assertFalse(roster["complete"])
        (issue,) = roster["issues"]
        self.assertEqual((issue["stage"], issue["path"], issue["task_ids"]), ("roles", "task M01.role", ["M01"]))
        self.assertIn("staffed by role 'ghost-artist', which is not a role of the plan", issue["message"])
        self.assertEqual(roster["unstaffed"], ["M01"])


class EveryViolationNamesItsTasksTests(DepartmentRun):
    def _broken(self):
        """M01 fine; M02 names its own profession; M03 starts off the hiring ladder.

        A lead the plan does not have on M03 would leave M01 unstaffed too:
        its profession would name two leads.
        """

        plan = self.plan()

        def lead(task, value):
            return replace(task, verification=replace(task.verification, verifier_role=value))

        return replace(plan, tasks=(plan.tasks[0], lead(plan.tasks[1], "reference-artist"),
                                    replace(plan.tasks[2], reasoning="ultra")))

    def test_before_the_start_all_under_way_the_named(self) -> None:
        """Called as the reservation calls it (``staffing_gate(cfg, plan, state)``).

        Mutations: _attribute reads no task from a message - M02's issue is
        the run's and, under way, the ticket holds only M03; _attribute
        reads no task from a path - M03 is not held; run_started always
        False - under way the ticket holds M01 as well.
        """

        from codex_autopilot.engineer_reservation import tasks_paused_by_incidents
        from codex_autopilot.staffing import build_roster, staffing_gate

        plan = self._broken()
        state = self.store.load()
        roster = build_roster(plan, state, state_dir=self.cfg.state_dir, memory=self.memory,
                              screening=False, occasion="test")
        self.assertEqual([item["task_ids"] for item in roster["issues"]], [["M02"], ["M03"]])
        self.assertEqual((roster["unstaffed"], roster["run_wide"]), (["M02", "M03"], False))
        self.assertEqual([item["staffed"] for item in roster["tasks"]], [True, False, False])
        # Under way: M01 was reserved and works.
        state.task_states["M01"] = "RUNNING"
        staffing_gate(self.cfg, plan, state)
        (ticket,) = _tickets(self.cfg, "staffing")
        self.assertEqual(ticket["affected_task_ids"], ["M02", "M03"])
        self.assertTrue(ticket["system_state"]["run_started"])
        self.assertEqual(tasks_paused_by_incidents(self.cfg, plan), {"M02", "M03"})

    def test_not_started_holds_the_whole_run(self) -> None:
        """The same roster before any reservation: M01 waits too. Mutation: run_started always True."""

        from codex_autopilot.staffing import run_started, staffing_gate

        plan = self._broken()
        state = self.store.load()
        self.assertFalse(run_started(state))
        staffing_gate(self.cfg, plan, state)
        (ticket,) = _tickets(self.cfg, "staffing")
        self.assertEqual(sorted(ticket["affected_task_ids"]), ["M01", "M02", "M03"])
        self.assertIn("no task starts until it does", ticket["summary"])


    def test_what_counts_as_the_start(self) -> None:
        """A plan verifier or an on-call is not the run's work; a worker is, whatever its task's state now.

        Mutations: run_started without the sessions - a task reserved and
        since BLOCKED reads as never started; the plan verifier's session
        counted - a run whose plan is being verified reads as started.
        """

        from codex_autopilot.staffing import run_started

        state = self.store.load()
        state.worker_sessions = [{"kind": "plan_verifier", "task_id": "M01"}, {"kind": "pipeline_engineer"}]
        self.assertFalse(run_started(state))
        state.task_states["M01"] = "BLOCKED"
        self.assertFalse(run_started(state))
        state.worker_sessions.append({"kind": "implementation", "task_id": "M01"})
        self.assertTrue(run_started(state))
        state.worker_sessions = []
        state.task_states["M01"] = "IMPLEMENTED"
        self.assertTrue(run_started(state))


class AStaffingStopUnderWayHoldsOnlyItsTasksTests(DepartmentRun):
    plan_payload = staticmethod(_two_departments)

    def _lose_the_rubric_of(self, department_id: str):
        from codex_autopilot import department_runtime

        real = department_runtime.ensure_department_rubric

        def ensure(memory, plan, department):
            if department.id == department_id:
                raise OSError("the rubric record cannot be read")
            return real(memory, plan, department)

        return mock.patch("codex_autopilot.department_runtime.ensure_department_rubric", side_effect=ensure)

    def _stale_roster(self) -> None:
        """Mid-run the stamp is no longer the plan's (as after a committed change): the gate rebuilds."""

        from codex_autopilot.staffing import load_roster, write_roster

        roster = load_roster(self.cfg.state_dir)
        roster["plan_sha256"] = "0" * 64
        write_roster(self.cfg.state_dir, roster)

    def test_the_other_department_goes_to_acceptance(self) -> None:
        """The independent check's probe, through the production lifecycle.

        Mutations: _stop holds every unsettled task under way too (the old
        hold) - M01 finishes and only the on-call is reserved; its lead is
        not; stop_context without ``unstaffed``.
        """

        workers = {item.task_id: item for item in self.reserve()}
        self.assertEqual(sorted(workers), ["M01", "M02", "M03"])
        self._stale_roster()
        with self._lose_the_rubric_of("character-artist"):
            outcome = self.implement(workers["M01"], "thread-m01")
        reserved = sorted((item.kind, item.task_id) for item in outcome.descriptors)
        self.assertIn(("verifier", "M01"), reserved)
        (ticket,) = _tickets(self.cfg, "staffing")
        self.assertEqual(ticket["affected_task_ids"], ["M02"])
        self.assertEqual(ticket["phase"], "PIPELINE_ENGINEER")
        self.assertIn("held: M02; the rest of the run goes on", ticket["summary"])
        self.assertIn("character-artist", ticket["system_state"]["diagnosis"])
        self.assertEqual(self.store.load().task_states["M01"], "VERIFYING")
        # The on-call's package names what is held and why, as the roster has it now.
        from codex_autopilot.stop_diagnosis import stop_context

        roster = stop_context(self.cfg, self.plan(), self.store.load(), ticket)["roster"]
        self.assertEqual(roster["unstaffed"], ["M02"])
        self.assertTrue(any("character-artist" in item for item in roster["issues"]), roster["issues"])

    def test_a_roster_that_cannot_be_built_under_way_holds_nothing(self) -> None:
        """No task is named: the ticket calls the on-call and the run goes on.

        Mutation: a run-wide failure holds every unsettled task under way -
        M01's lead is not reserved.
        """

        workers = {item.task_id: item for item in self.reserve()}
        self._stale_roster()
        with mock.patch("codex_autopilot.staffing.build_roster", side_effect=RuntimeError("roster code broke")):
            outcome = self.implement(workers["M01"], "thread-m01")
        self.assertIn(("verifier", "M01"), [(item.kind, item.task_id) for item in outcome.descriptors])
        (ticket,) = _tickets(self.cfg, "staffing")
        self.assertEqual(ticket["affected_task_ids"], [])
        self.assertIn("roster code broke", ticket["system_state"]["diagnosis"])
        self.assertIn("it holds no task; the run goes on", ticket["summary"])
        self.assertEqual(ticket["phase"], "PIPELINE_ENGINEER")


def _pass_record(cfg) -> dict:
    """A PASS of this root, profile, binary and runtime code, as the preflight measures it."""

    from codex_autopilot.isolation_probe import (
        RECORD_VERSION, binary_identity, probe_workspace, runtime_code_identity,
    )

    return {
        "version": RECORD_VERSION, "root": str(cfg.root), "outcome": "PASS",
        "workspace": str(probe_workspace(cfg.state_dir)),
        "base_profile": cfg.desktop.permission_profile,
        "codex_binary": binary_identity(cfg.desktop.binary),
        "runtime_code": runtime_code_identity(), "measured_at": "2026-09-25T00:00:00+00:00",
    }


class TheRunsFactsFollowTheRunTests(DepartmentRun):
    """The isolation record and the roots audit move without the plan (third check).

    The independent check initialized a run, wrote the preflight's PASS the
    way cli.py does - after initialize_project, whose bootstrap had built
    the roster - and reserved: the dispatcher used contract 2, the roster
    and the board said "isolation: not measured", and a roots finding
    recorded at a wake-up never reached either. A complete roster of the
    current plan was not rebuilt before the next plan change. Nothing here
    calls refresh_roster: the facts must arrive through the runtime's own
    paths.
    """

    plan_payload = staticmethod(_full_shape)

    def roster(self) -> dict:
        from codex_autopilot.staffing import load_roster

        return load_roster(self.cfg.state_dir)

    def board_file(self) -> str:
        return (self.cfg.state_dir / "BOARD.md").read_text(encoding="utf-8")

    def m02(self, roster: dict) -> dict:
        return next(item for item in roster["tasks"] if item["id"] == "M02")

    def test_the_preflights_record_reaches_the_roster_and_the_board_before_the_start(self) -> None:
        """Mutations: write_record without follow_isolation_record - both still say
        "not measured"; with_current_facts leaving the tasks' placement as
        built - M02 still "staged workspace".
        """

        from codex_autopilot.board import render_board
        from codex_autopilot.config import STATE_DIR_NAME
        from codex_autopilot.isolation_probe import isolation_proven, write_record

        self.assertEqual(self.roster()["run"]["isolation"]["outcome"], "NOT_MEASURED")
        # cli.py, after initialize_project: the preflight's record is written.
        write_record(self.root / STATE_DIR_NAME, _pass_record(self.cfg))
        self.assertTrue(isolation_proven(self.cfg))
        isolation = self.roster()["run"]["isolation"]
        self.assertEqual((isolation["outcome"], isolation["proven"]), ("PASS", True))
        self.assertIn("contract 2", isolation["staged_cwd"])
        self.assertEqual(self.m02(self.roster())["placement"], {"workspace": "staged", "cwd": "root (staged profile)"})
        self.assertIn("isolation: proven", self.board_file())
        # And after the first reservation pass, the reviewer's probe.
        self.reserve()
        self.assertTrue(self.roster()["run"]["isolation"]["proven"])
        head = render_board(self.cfg, self.plan(), self.store.load())[0]
        self.assertIn("isolation: proven", head)

    def test_a_record_that_stops_matching_moves_the_gate_and_the_board(self) -> None:
        """The Codex binary is updated mid-run: the dispatcher no longer takes the PASS.

        Mutations: staffing_gate without with_current_facts for a complete
        roster of this plan - the roster keeps "proven"; render_board reading
        load_roster instead of current_roster - the board keeps "proven";
        board_summary naming the outcome without ``proven`` - a PASS the
        dispatcher does not take reads "proven".
        """

        from codex_autopilot.board import render_board
        from codex_autopilot.isolation_probe import isolation_proven, write_record
        from codex_autopilot.staffing import staffing_gate

        write_record(self.cfg.state_dir, _pass_record(self.cfg))
        self.assertTrue(self.roster()["run"]["isolation"]["proven"])
        with mock.patch("codex_autopilot.isolation_probe.binary_identity", return_value="/new/codex:2:2"):
            self.assertFalse(isolation_proven(self.cfg))
            # The board, before anything rewrote the snapshot, says what the dispatcher will do.
            self.assertTrue(self.roster()["run"]["isolation"]["proven"])
            head = render_board(self.cfg, self.plan(), self.store.load())[0]
            self.assertIn("isolation: a PASS of another binary or runtime code", head)
            # The gate, as every reservation pass calls it.
            roster = staffing_gate(self.cfg, self.plan(), self.store.load())
        self.assertFalse(roster["run"]["isolation"]["proven"])
        on_disk = self.roster()
        self.assertFalse(on_disk["run"]["isolation"]["proven"])
        self.assertIn("contract 1", on_disk["run"]["isolation"]["staged_cwd"])
        self.assertEqual(self.m02(on_disk)["placement"]["cwd"], "staged workspace")
        self.assertTrue(on_disk["complete"])

    def test_a_roots_audit_recorded_at_a_wake_reaches_the_roster_and_the_board(self) -> None:
        """The pair refresh_run_roots_audit runs: record_roots_audit, then a save of state.

        A finding she has since resolved leaves both as well. Mutation:
        refresh_board_file without sync_run_facts - roster.json keeps the
        bootstrap's "no audit", then the resolved finding.
        """

        from codex_autopilot.project_roots_audit import SIBLING_ROOTS, RootsAudit, RootsFinding, record_roots_audit

        before = self.roster()["run"]["roots_audit"]
        finding = RootsFinding(code=SIBLING_ROOTS, status="proposed", project_id="desktop-project",
                               path=str(self.root.parent / "sibling"), detail="A sibling root is saved.")

        def audit(findings):
            return RootsAudit(target=str(self.root), desktop_project_id="desktop-project", selected_project_id=None,
                              linked_project_id=None, codex_home=None, checked=[SIBLING_ROOTS], findings=findings)

        state = self.store.load()
        record_roots_audit(state, audit([finding]), {}, occasion="wake")
        self.store.save(state)
        self.assertNotEqual(before, self.roster()["run"]["roots_audit"])
        self.assertEqual(self.roster()["run"]["roots_audit"],
                         {"recorded": True, "findings": [{"code": SIBLING_ROOTS, "status": "proposed"}]})
        self.assertIn("roots: 1 findings", self.board_file())
        self.reserve()
        self.assertIn("roots: 1 findings", self.board_file())
        # She resolved it; the next audit finds nothing.
        state = self.store.load()
        record_roots_audit(state, audit([]), {}, occasion="owner decision")
        self.store.save(state)
        self.assertEqual(self.roster()["run"]["roots_audit"], {"recorded": True, "findings": []})
        self.assertNotIn("roots:", self.board_file())


class AStoppedTaskShowsItsOwnReasonTests(DepartmentRun):
    """A task left BLOCKED without a ticket shows its stop's reason, not the run's last."""

    def test_the_reason_of_its_own_stop(self) -> None:
        """Two stops no ticket could record: M02's, then M03's, which becomes last_error.

        Mutations: the board's phrase from state.last_error (the old one) -
        M02 shows M03's cause; stop_run journaling no reason - M02 shows
        "no reason recorded".
        """

        from codex_autopilot.blocked_runs import stop_run
        from codex_autopilot.board import board_rows

        state = self.store.load()
        with mock.patch(
            "codex_autopilot.pipeline_engineer.PipelineIncidentStore.open_incident",
            side_effect=OSError("disk gone"),
        ):
            for task_id, cause in (("M02", "the reference sheet is missing"), ("M03", "the lead thread vanished")):
                state.task_states[task_id] = "BLOCKED"  # the caller blocks, then goes through the door
                stop_run(self.cfg, state, stop_kind="worker_blocked", phase="WORKER_BLOCKED",
                         reason=f"{task_id}: {cause}", summary=f"{task_id} stopped.",
                         at="2026-09-25T10:00:00+00:00", task_ids=[task_id])
        self.store.save(state)
        self.assertEqual(state.last_error, "M03: the lead thread vanished")
        rows = {row["id"]: row for row in board_rows(self.cfg, self.plan(), self.store.load())}
        self.assertEqual(rows["M02"]["category"], "stopped")
        self.assertEqual(rows["M02"]["state"], "stopped: M02: the reference sheet is missing")
        self.assertEqual(rows["M03"]["state"], "stopped: M03: the lead thread vanished")

    def test_without_a_stop_the_failure_its_own_session_recorded(self) -> None:
        """No stop names M01; its retired session says why (lifecycle_failures' failure_reason).

        Mutation: _task_stop_reason without the session fallback - "no reason recorded".
        """

        from codex_autopilot.board import board_rows

        (worker,) = self.reserve()
        state = self.store.load()
        session = next(item for item in state.worker_sessions if item["reservation_token"] == worker.reservation_token)
        session["status"] = "BLOCKED"
        session["failure_reason"] = "the worker's turn could not be started"
        state.task_states["M01"] = "FAILED"
        state.active_task_ids.remove("M01")
        state.last_error = "another task's cause"
        self.store.save(state)
        rows = {row["id"]: row for row in board_rows(self.cfg, self.plan(), self.store.load())}
        self.assertEqual(rows["M01"]["state"], "stopped: the worker's turn could not be started")
