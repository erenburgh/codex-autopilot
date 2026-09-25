"""The roster before the first task, and the branch board (staffing, board).

Her decision of 23 Sep 2026: staff the run up front and see every branch -
without threads made in advance (measured not to survive, ``staffing``) and
without R2. The roster is derived by the runtime before any reservation and
checked whole; one that does not assemble does not start the run and goes to
the on-call through the one door with the full list. The board shows every
task on one line in the run's language, in ``status`` and in BOARD.md.
"""

from __future__ import annotations

from dataclasses import replace
import json
import re
from unittest import mock

from _departments import DepartmentRun, art_task, art_run_plan
from _plan_contract import canonicalize_plan
from codex_autopilot.pipeline_engineer import PipelineIncidentStore


def _tickets(cfg, kind: str) -> list[dict]:
    return [
        item for item in PipelineIncidentStore(cfg.state_dir).load()["incidents"]
        if (item.get("system_state") or {}).get("stop_kind") == kind
    ]


def _eighteen_tasks() -> dict:
    """The live art-run shape: 18 tasks, 3 roles, lead art-reviewer, no departments."""

    raw = art_run_plan()
    raw["tasks"] = [
        art_task(
            f"M{index:02d}",
            "character-artist" if index % 2 else "reference-artist",
            depends_on=(f"M{index - 1:02d}",) if index > 1 else (),
        )
        for index in range(1, 19)
    ]
    raw.pop("goal_contract", None)
    return canonicalize_plan(raw)


def _two_broken_siblings() -> dict:
    """A saved plan from before R30: M01 names no lead, M02 names its own profession."""

    raw = art_run_plan()
    raw["execution_strategy"] = "parallel"
    raw["max_parallel_workers"] = 3
    for task in raw["tasks"]:
        task["depends_on"] = []
    raw["tasks"][0]["verification"].pop("verifier_role")
    raw["tasks"][1]["verification"]["verifier_role"] = "reference-artist"
    raw["tasks"][2]["verification"].pop("verifier_role")
    return raw


class _Preadmitted(DepartmentRun):
    """A plan saved by a runtime from before R30's admission (the lead check off)."""

    def initialize(self, plan_file, skill) -> None:
        with mock.patch("codex_autopilot.plan_admission.validate_department_leads", return_value=[]), \
                mock.patch("_plan_contract._attach_test_lead"):
            super().initialize(plan_file, skill)


class TheArtRunShapeIsStaffedTests(DepartmentRun):
    plan_payload = staticmethod(_eighteen_tasks)

    def test_every_task_is_staffed_before_the_first_and_the_plan_digest_stands(self) -> None:
        """Department, lead, rubric v1, ladder, on-call, thread names - and plan.json untouched.

        Mutation: the bootstrap does not build the roster - roster.json is
        missing before the first reservation.
        """

        from codex_autopilot.plan_verification import plan_sha256
        from codex_autopilot.staffing import ON_CALL_ROLE, load_roster

        plan_bytes = (self.cfg.state_dir / "plan.json").read_bytes()
        roster = load_roster(self.cfg.state_dir)
        self.assertIsNotNone(roster)
        self.assertTrue(roster["complete"], roster["issues"])
        digest = plan_sha256(self.plan())
        self.assertEqual(roster["plan_sha256"], digest)
        self.assertEqual(len(roster["tasks"]), 18)
        (record,) = self.rubric_records("art-reviewer")
        for entry in roster["tasks"]:
            self.assertEqual(entry["department"]["id"], "art-reviewer")
            self.assertEqual(entry["lead"], {"id": "art-reviewer", "name": "Character Art Verifier"})
            self.assertEqual(entry["rubric"]["version"], 1)
            self.assertEqual(entry["rubric"]["record_id"], record["id"])
            self.assertRegex(entry["rubric"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(entry["ladder"]["start_effort"], "medium")
            self.assertEqual(entry["ladder"]["steps"], ["medium", "high", "xhigh", "max"])
            self.assertEqual(entry["on_call"], ON_CALL_ROLE)
            self.assertIn("Pipeline Engineer", entry["escalation_route"][0])
            self.assertEqual(entry["threads"]["lead"], f"Character Art Verifier | Verify {entry['id']} | Model part {entry['id']}")
        m01 = roster["tasks"][0]
        self.assertEqual(m01["threads"]["worker"], "Character Artist | M01 | Model part M01")
        self.assertEqual(roster["tasks"][1]["depends_on"], ["M01"])
        # Reachability: the first reservation takes the roster as it is and
        # starts M01; plan.json and its digest never move.
        (worker,) = self.reserve()
        self.assertEqual((worker.kind, worker.task_id), ("implementation", "M01"))
        self.assertEqual((self.cfg.state_dir / "plan.json").read_bytes(), plan_bytes)
        self.assertEqual(plan_sha256(self.plan()), digest)
        self.assertEqual(self.store.load().plan_verification["plan_sha256"], digest)
        self.assertEqual(load_roster(self.cfg.state_dir)["plan_sha256"], digest)


class ARosterIsCheckedWholeTests(DepartmentRun):
    def _broken_plan(self):
        """Three different violations: no lead, lead = worker, an unknown role."""

        plan = self.plan()

        def lead(task, value):
            return replace(task, verification=replace(task.verification, verifier_role=value))

        tasks = (
            lead(plan.task_map["M01"], None),
            lead(plan.task_map["M02"], "reference-artist"),
            lead(plan.task_map["M03"], "ghost-lead"),
        )
        return replace(plan, tasks=tasks)

    def test_three_violations_are_one_refusal_that_stops_the_run_for_the_on_call(self) -> None:
        """One list, one ticket, every task held.

        Called as the reservation calls it (``staffing_gate(cfg, plan,
        state)``). Mutation: collect_graph_issues keeps only the first
        violation - the ticket names one.
        """

        from codex_autopilot.staffing import build_roster, staffing_gate

        plan = self._broken_plan()
        state = self.store.load()
        roster = build_roster(plan, state, state_dir=self.cfg.state_dir, memory=self.memory,
                              screening=False, occasion="test")
        self.assertFalse(roster["complete"])
        messages = [item["message"] for item in roster["issues"]]
        self.assertEqual(len(messages), 3, messages)
        staffing_gate(self.cfg, plan, state)
        (ticket,) = _tickets(self.cfg, "staffing")
        self.assertEqual(sorted(ticket["affected_task_ids"]), ["M01", "M02", "M03"])
        diagnosis = ticket["system_state"]["diagnosis"]
        self.assertIn("plan has 3 issues", diagnosis)
        self.assertIn("missing for: M01", diagnosis)
        self.assertIn("equals the task's role for: M02 (reference-artist)", diagnosis)
        self.assertIn("names no role of the plan for: M03 ('ghost-lead')", diagnosis)
        self.assertEqual(len(ticket["system_state"]["roster_issues"]), 3)
        self.assertEqual(ticket["phase"], "PIPELINE_ENGINEER")
        # The same pass again files nothing new: one ticket per stop.
        staffing_gate(self.cfg, plan, state)
        self.assertEqual(len(_tickets(self.cfg, "staffing")), 1)


    def test_a_ladder_that_cannot_start_is_named_and_the_snapshot_still_writes(self) -> None:
        """An effort off the ladder is a violation; the roster is still written whole.

        Mutation: the ladder entry keeps the collector's FAILED marker - the
        snapshot cannot be serialised and the gate cannot write it.
        """

        from codex_autopilot.staffing import load_roster, refresh_roster

        plan = self.plan()
        plan = replace(plan, tasks=(replace(plan.tasks[0], reasoning="ultra"), *plan.tasks[1:]))
        roster = refresh_roster(self.cfg.state_dir, plan, self.store.load(), occasion="test", memory=self.memory)
        self.assertFalse(roster["complete"])
        (issue,) = roster["issues"]
        self.assertIn("starts at effort 'ultra'", issue["message"])
        self.assertIsNone(load_roster(self.cfg.state_dir)["tasks"][0]["ladder"])


class ARosterThatDoesNotAssembleDoesNotStartTests(_Preadmitted):
    plan_payload = staticmethod(_two_broken_siblings)

    def test_no_task_starts_and_the_board_says_why(self) -> None:
        """Through the production reservation: only the on-call, the tasks held.

        Mutation: _reserve_in_state without the staffing_gate call - M01..M03
        are reserved and RUNNING.
        """

        from codex_autopilot.control import status_text

        descriptors = self.reserve()
        self.assertEqual([item.kind for item in descriptors], ["pipeline_engineer"])
        state = self.store.load()
        self.assertEqual({state.task_states[key] for key in ("M01", "M02", "M03")}, {"READY"})
        (ticket,) = _tickets(self.cfg, "staffing")
        self.assertEqual(sorted(ticket["affected_task_ids"]), ["M01", "M02", "M03"])
        # Two classes of violation for three tasks: M01 and M03 in one line.
        self.assertIn("plan has 2 issues", ticket["system_state"]["diagnosis"])
        self.assertIn("missing for: M01, M03", ticket["system_state"]["diagnosis"])
        self.assertEqual(ticket["system_state"]["escalation_route"][0].split(" (")[0], "Pipeline Engineer")
        text = status_text(self.root)
        self.assertIn("staffing: roster incomplete, 2 violations", text)
        self.assertIn(f"stopped: the run's roster did not assemble (2 violations) — ticket {ticket['incident_id']} with the on-call", text)

    def test_the_on_calls_change_must_leave_the_whole_roster(self) -> None:
        """Asked from a staffing ticket, a change that fixes only its requester is refused.

        Mutation: state_issues without the requires_roster check - the graph
        that leaves M02 and M03 broken is admitted.
        """

        from codex_autopilot.plan import plan_to_dict
        from codex_autopilot.plan_admission import IssueCollector, plan_change_candidate, state_issues

        current = self.plan()
        change = plan_to_dict(current)
        change["graph_version"] = current.graph_version + 1
        change["tasks"][0]["verification"]["verifier_role"] = "art-reviewer"
        state = self.store.load()
        state.plan_changes = [{"id": "PC1", "requires_roster": True}]
        state.active_plan_change_id = "PC1"
        read = plan_change_candidate(IssueCollector(), current, change, "adaptive")
        issues = [message for _, message in state_issues(current, read, state, "M01")]
        self.assertTrue(any("roster must assemble" in item and "M02" in item for item in issues), issues)
        self.assertTrue(any("M03" in item for item in issues), issues)
        for task in change["tasks"]:
            task["verification"]["verifier_role"] = "art-reviewer"
        read = plan_change_candidate(IssueCollector(), current, change, "adaptive")
        self.assertEqual(state_issues(current, read, state, "M01"), [])


class ACommittedPlanChangeRestaffsTests(DepartmentRun):
    def test_the_roster_is_rebuilt_under_the_new_digest(self) -> None:
        """A new task with a new lead: staffed at COMMITTED, stamped with the new digest.

        Mutation: commit_plan_change without refresh_roster - roster.json
        still carries the old digest and no M04.
        """

        from codex_autopilot.plan import plan_to_dict, validate_plan_change
        from codex_autopilot.plan_verification import plan_sha256
        from codex_autopilot.resilience import commit_plan_change
        from codex_autopilot.staffing import load_roster

        current = self.plan()
        before = load_roster(self.cfg.state_dir)
        self.assertEqual(before["plan_sha256"], plan_sha256(current))
        raw = plan_to_dict(current)
        raw["graph_version"] = current.graph_version + 1
        raw["roles"] += [
            {"id": "rigger", "name": "Rigger", "version": "1.0.0", "responsibilities": ["Rig the character."]},
            {"id": "rig-lead", "name": "Rig Lead", "version": "1.0.0", "responsibilities": ["Accept rigs."]},
        ]
        task = json.loads(json.dumps(raw["tasks"][0]))
        task.update(id="M04", role="rigger", depends_on=["M03"])
        task["verification"]["verifier_role"] = "rig-lead"
        raw["tasks"].append(task)
        candidate = validate_plan_change(current, raw, self.cfg.profile)
        state = self.store.load()
        state.graph_version = candidate.graph_version
        state.task_states["M04"] = "WAITING"
        commit_plan_change(self.cfg.state_dir, profile=self.cfg.profile, current=current,
                           candidate=candidate, state=state, request_id="PC1")
        after = load_roster(self.cfg.state_dir)
        self.assertNotEqual(after["plan_sha256"], before["plan_sha256"])
        self.assertEqual(after["plan_sha256"], plan_sha256(candidate))
        self.assertEqual(after["occasion"], "plan_change")
        m04 = next(item for item in after["tasks"] if item["id"] == "M04")
        self.assertEqual(m04["lead"], {"id": "rig-lead", "name": "Rig Lead"})
        self.assertEqual(m04["rubric"]["version"], 1)
        self.assertTrue(after["complete"])


class _Board(DepartmentRun):
    language = "en"

    def initialize(self, plan_file, skill) -> None:
        from _plan_contract import initialize_verified_project

        initialize_verified_project(
            self.root, plan_file, profile="adaptive", skill_path=skill,
            desktop_project_id="desktop-project", language=self.language,
        )

    def phrases(self) -> dict[str, str]:
        from codex_autopilot.board import board_rows

        rows = board_rows(self.cfg, self.plan(), self.store.load())
        return {row["id"]: row["state"] for row in rows}

    def set_states(self, **states: str) -> None:
        state = self.store.load()
        state.task_states.update(states)
        self.store.save(state)

    def stop(self, task_id: str) -> str:
        from codex_autopilot.blocked_runs import stop_run

        state = self.store.load()
        incident = stop_run(self.cfg, state, stop_kind="verification_protocol", phase="VERIFICATION_PROTOCOL",
                            reason="three unreadable verdicts", summary=f"The verifier of {task_id} could not be read.",
                            at="2026-09-24T00:00:00+00:00", task_ids=(task_id,))
        self.store.save(state)
        return incident

    def escalate(self, incident_id: str) -> None:
        PipelineIncidentStore(self.cfg.state_dir).escalate_incident_to_user(
            incident_id, reason_code="PRODUCT_DECISION", at="2026-09-24T00:01:00+00:00",
            detail="the reference contradicts the request",
            escalation={"decision_needed": "which sheet is canonical",
                        "recommendation": "keep the front sheet"},
        )


class TheBoardSpeaksEnglishTests(_Board):
    def test_every_state_has_its_phrase(self) -> None:
        """Mutation: the VERIFIED and CANCELLED branches swapped - M01 reads "cancelled"."""

        self.assertEqual(self.phrases()["M02"], "waiting for M01")
        self.set_states(M02="READY")  # the phrase follows the dependencies, not the label
        self.assertEqual(self.phrases()["M02"], "waiting for M01")
        self.set_states(M01="VERIFIED")
        self.assertEqual(self.phrases()["M01"], "accepted")
        self.assertEqual(self.phrases()["M02"], "ready to start")
        self.set_states(M02="VERIFYING")
        self.assertEqual(self.phrases()["M02"], "under acceptance by Character Art Verifier")
        self.set_states(M02="REVISION_REQUIRED")
        self.assertEqual(self.phrases()["M02"], "revision 1 of 2, hire 1, effort medium")
        incident = self.stop("M03")
        self.assertEqual(
            self.phrases()["M03"],
            f"stopped: The verifier of M03 could not be read. — ticket {incident} with the on-call",
        )
        self.escalate(incident)
        phrase = self.phrases()["M03"]
        self.assertTrue(phrase.startswith(
            "waits for you: which sheet is canonical — recommendation: keep the front sheet — answer: "
            "codex-autopilot unblock --project "), phrase)
        self.assertIn("--task M03", phrase)


class TheBoardSpeaksRussianTests(_Board):
    language = "ru"

    def test_every_state_has_its_phrase(self) -> None:
        """Mutation: the ticket branches swapped - an escalated ticket reads «стоит»."""

        self.assertEqual(self.phrases()["M02"], "ждёт M01")
        self.set_states(M01="VERIFIED", M02="IMPLEMENTED")
        self.assertEqual(self.phrases()["M01"], "принята")
        self.assertEqual(self.phrases()["M02"], "ждёт приёмки")
        incident = self.stop("M03")
        self.assertEqual(
            self.phrases()["M03"],
            f"стоит: The verifier of M03 could not be read. — билет {incident} у дежурного",
        )
        self.escalate(incident)
        phrase = self.phrases()["M03"]
        self.assertTrue(phrase.startswith(
            "ждёт тебя: which sheet is canonical — рекомендация: keep the front sheet — ответ: "
            "codex-autopilot unblock --project "), phrase)

    def test_the_summary_counts_a_mixed_run(self) -> None:
        """Accepted, working, waiting, stopped, waiting for her - each counted once.

        Mutation: the summary counts an escalated ticket as stopped.
        """

        from codex_autopilot.board import board_rows, board_summary
        from codex_autopilot.staffing import load_roster

        raw = self.plan()
        self.set_states(M01="VERIFIED", M02="RUNNING")
        incident = self.stop("M03")
        self.escalate(incident)
        rows = board_rows(self.cfg, raw, self.store.load())
        summary = board_summary(rows, load_roster(self.cfg.state_dir), raw, "ru")
        self.assertEqual(
            summary,
            "Итого: принято 1 · в работе 1 · ждёт 0 · стоит 0 · ждёт тебя 1 — штат собран; "
            "изоляция: не измерена",
        )
        # BLOCKED under a ticket handed to her is hers, not "stopped".
        self.set_states(M03="BLOCKED")
        rows = board_rows(self.cfg, raw, self.store.load())
        self.assertEqual([row["category"] for row in rows], ["accepted", "working", "yours"])


class _BoardAlongARevision:
    """One task driven through the production lifecycle: work, acceptance, revision.

    A mixin, so that only its two language classes run it.

    The independent check (25 Sep 2026) broke the working phrase, the last
    report and the threads column in a copy of board.py and every test
    still passed. Each is asserted here as the lifecycle leaves it.
    """

    expected: dict[str, str] = {}

    def row(self) -> dict:
        from codex_autopilot.board import board_rows

        return next(row for row in board_rows(self.cfg, self.plan(), self.store.load()) if row["id"] == "M01")

    def test_the_working_phrase_the_report_and_the_threads(self) -> None:
        """Mutations (each kills this test): RUNNING shows the "ready to start"
        phrase; _thread_label drops the thread id; _report always "—" (or
        takes the checkpoint's heading); _threads prints "x" instead of
        kind, status and id; the REVISING branch without its thread.
        """

        from codex_autopilot.control import status_text

        words = self.expected
        (worker,) = self.reserve()
        row = self.row()
        self.assertEqual(row["state"], words["pending"])
        self.assertEqual((row["report"], row["threads"]), ("—", "implementation CREATE_REQUESTED —"))
        self.mark_active(worker.reservation_token, "thread-w1")
        row = self.row()
        self.assertEqual(row["state"], words["working"])
        self.assertEqual(row["category"], "working")
        self.assertEqual(row["threads"], "implementation ACTIVE thread-w1")
        lead = self.implement(worker, "thread-w1").descriptors[0]
        self.mark_active(lead.reservation_token, "thread-l1")
        row = self.row()
        self.assertEqual(row["state"], words["acceptance"])
        # The first line of substance of the handoff, not its "# M01" heading.
        self.assertEqual(row["report"], "Work done.")
        self.assertEqual(row["threads"], "implementation COMPLETED thread-w1; verifier ACTIVE thread-l1")
        issue = {"code": "SILHOUETTE", "summary": "The silhouette drifts",
                 "details": "Compare the side view.", "dod_refs": [1]}
        revision = self.judge(lead, "thread-l1", self.verdict("M01", "REVISE", [issue])).descriptors[0]
        self.mark_active(revision.reservation_token, "thread-w2")
        row = self.row()
        self.assertEqual(self.store.load().task_states["M01"], "REVISING")
        self.assertEqual(row["state"], words["revising"])
        self.assertEqual(
            row["threads"],
            "implementation COMPLETED thread-w1; verifier COMPLETED thread-l1; revision ACTIVE thread-w2",
        )
        # Reachability: the detailed status prints both columns.
        self.assertIn(words["line"], status_text(self.root))


class TheBoardAlongARevisionInEnglishTests(_BoardAlongARevision, _Board):
    language = "en"
    expected = {
        "pending": "working: Character Artist | M01 | Model part M01",  # named, no id yet
        "working": "working: Character Artist | M01 | Model part M01 (thread-w1)",
        "acceptance": "under acceptance by Character Art Verifier — "
                      "Character Art Verifier | Verify M01 | Model part M01 (thread-l1)",
        "revising": "revision 1 of 2, hire 1, effort medium — "
                    "working: Character Artist | M01-R1 | Revise Model part M01 (thread-w2)",
        "line": "— report: Work done. — threads: implementation COMPLETED thread-w1; "
                "verifier COMPLETED thread-l1; revision ACTIVE thread-w2",
    }


class TheBoardAlongARevisionInRussianTests(_BoardAlongARevision, _Board):
    language = "ru"
    expected = {
        "pending": "работает: Character Artist | M01 | Model part M01",
        "working": "работает: Character Artist | M01 | Model part M01 (thread-w1)",
        "acceptance": "на приёмке у Character Art Verifier — "
                      "Character Art Verifier | Verify M01 | Model part M01 (thread-l1)",
        "revising": "ревизия 1 из 2, наём 1, effort medium — "
                    "работает: Character Artist | M01-R1 | Revise Model part M01 (thread-w2)",
        "line": "— отчёт: Work done. — треды: implementation COMPLETED thread-w1; "
                "verifier COMPLETED thread-l1; revision ACTIVE thread-w2",
    }


class BoardFileTests(_Board):
    language = "ru"

    def test_is_rewritten_at_every_save_and_never_fails_one(self) -> None:
        """BOARD.md follows the state; a failing board still saves the state.

        Mutations: StateStore.save without refresh_board_file - BOARD.md still
        says «ждёт M01» for M02; refresh_board_file re-raising - save raises.
        """

        board = self.cfg.state_dir / "BOARD.md"
        text = board.read_text(encoding="utf-8")
        self.assertIn("# Доска веток", text)
        self.assertIn("| M02 Model part M02 |", text)
        self.assertIn("ждёт M01", text)
        self.set_states(M01="VERIFIED")
        text = board.read_text(encoding="utf-8")
        self.assertRegex(text, r"\| M01 Model part M01 \| [^|]*\| принята \|")
        self.assertNotIn("ждёт M01", text)
        self.assertIn("Итого: принято 1", text)
        with mock.patch("codex_autopilot.board.render_board_markdown", side_effect=RuntimeError("disk full")):
            state = self.store.load()
            state.task_states["M02"] = "READY"
            self.store.save(state)
        self.assertEqual(self.store.load().task_states["M02"], "READY")
        # The file is left as it was, not half-written.
        self.assertEqual(board.read_text(encoding="utf-8"), text)


class TheStatusShowsTheBoardTests(_Board):
    language = "ru"

    def test_the_card_and_the_report_carry_the_board(self) -> None:
        """Reachability from ``codex-autopilot status`` and the hook's card.

        Mutation: render_short_status without the board lines.
        """

        from codex_autopilot.control import status_text

        card = status_text(self.root, detailed=False)
        self.assertIn("Доска веток — Итого: принято 0 · в работе 0 · ждёт 3 · стоит 0 · ждёт тебя 0", card)
        self.assertIn("- M02 Model part M02 — Art Reviewer · Character Art Verifier, рубрика v1 — ждёт M01", card)
        report = status_text(self.root)
        self.assertRegex(report, re.escape("- M01 Model part M01 — ") + r".* — отчёт: — — треды: —")


class RoutingReadsTheRosterTests(DepartmentRun):
    def _stop(self, task_id: str) -> dict:
        from codex_autopilot.blocked_runs import stop_run

        state = self.store.load()
        incident = stop_run(self.cfg, state, stop_kind="launch_refused", phase="LAUNCH_REFUSED",
                            reason="r", summary="s", at="2026-09-24T00:00:00+00:00", task_ids=(task_id,))
        return next(item for item in PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
                    if item["incident_id"] == incident)

    def _roster_route(self, route: list[str]) -> None:
        """The roster as the runtime wrote it, with the run's route replaced."""

        from codex_autopilot.staffing import load_roster, write_roster

        roster = load_roster(self.cfg.state_dir)
        roster["run"]["escalation_route"] = route
        write_roster(self.cfg.state_dir, roster)

    def test_every_stop_ticket_names_the_route_the_rule_takes(self) -> None:
        """Mutation: blocked_runs files without escalation_route."""

        from codex_autopilot.staffing import ESCALATION_ROUTE

        ticket = self._stop("M01")
        self.assertEqual(ticket["system_state"]["escalation_route"], list(ESCALATION_ROUTE))
        self.assertEqual(ticket["phase"], "PIPELINE_ENGINEER")

    def test_the_route_is_the_rosters_and_the_on_call_stays_first(self) -> None:
        """The ticket carries the roster's route; a roster cannot move the on-call.

        The independent check (25 Sep 2026): the roster held the same
        constant the test compared with, so a door that never read the
        roster passed. Here the roster names a route of its own.

        Mutations: blocked_runs._route returns ESCALATION_ROUTE without
        reading the roster - the first ticket has the constant; escalation_route
        without the on-call-first check - the second ticket names the owner
        first, and the ticket still goes to the on-call either way.
        """

        route = ["Pipeline Engineer (on-call): every stop, first", "owner: R13 or R23 (this roster's words)"]
        self._roster_route(route)
        ticket = self._stop("M01")
        self.assertEqual(ticket["system_state"]["escalation_route"], route)
        self.assertEqual(ticket["phase"], "PIPELINE_ENGINEER")
        # A roster that would send stops to her first does not change the rule.
        from codex_autopilot.staffing import ESCALATION_ROUTE

        self._roster_route(["owner: every stop", "Pipeline Engineer (on-call)"])
        ticket = self._stop("M02")
        self.assertEqual(ticket["system_state"]["escalation_route"], list(ESCALATION_ROUTE))
        self.assertEqual(ticket["phase"], "PIPELINE_ENGINEER")


class AStaffingTicketHoldsTheWholeRunTests(DepartmentRun):
    """Which tasks a staffing ticket holds, and that it keeps holding them all.

    The independent check (25 Sep 2026): the docs said "every task still to
    be accepted" while the code held every unsettled task, and a task a
    plan change added while the ticket was open was held by nothing.
    """

    def _broken(self, plan, *extra):
        def lead(task, value):
            return replace(task, verification=replace(task.verification, verifier_role=value))

        return replace(plan, tasks=(plan.tasks[0], lead(plan.tasks[1], None), plan.tasks[2], *extra))

    def test_a_migrated_task_with_no_acceptance_ahead_is_held_too(self) -> None:
        """The run does not start: the legacy self-accepted M01 waits with M02 and M03.

        Called as the reservation calls it. Mutation: _stop holds only tasks
        with acceptance ahead (drops the exempt set) - M01 is not held and
        tasks_paused_by_incidents lets it be reserved.
        """

        from codex_autopilot.engineer_reservation import tasks_paused_by_incidents
        from codex_autopilot.plan import VerificationPolicy
        from codex_autopilot.staffing import build_roster, staffing_gate

        plan = self.plan()
        legacy = replace(plan.tasks[0], verification=VerificationPolicy(
            policy="self", required=True, deterministic_checks=(), max_revision_attempts=0))
        plan = self._broken(replace(plan, legacy_serial=True, tasks=(legacy, *plan.tasks[1:])))
        state = self.store.load()
        roster = build_roster(plan, state, state_dir=self.cfg.state_dir, memory=self.memory,
                              screening=False, occasion="test")
        m01 = roster["tasks"][0]
        self.assertFalse(m01["acceptance_ahead"])  # exempt: no violation is M01's
        self.assertFalse(any("M01" in item["message"] for item in roster["issues"]), roster["issues"])
        staffing_gate(self.cfg, plan, state)
        (ticket,) = _tickets(self.cfg, "staffing")
        self.assertEqual(sorted(ticket["affected_task_ids"]), ["M01", "M02", "M03"])
        self.assertEqual(tasks_paused_by_incidents(self.cfg, plan), {"M01", "M02", "M03"})

    def test_a_task_added_while_the_ticket_is_open_is_held(self) -> None:
        """A plan change adds M04; the roster is still incomplete; M04 is held by the same ticket.

        Mutation: _stop returns the open ticket without hold_more_tasks - M04
        is paused by nothing and could be reserved before the roster assembles.
        """

        from codex_autopilot.engineer_reservation import tasks_paused_by_incidents
        from codex_autopilot.staffing import staffing_gate

        plan = self._broken(self.plan())
        state = self.store.load()
        staffing_gate(self.cfg, plan, state)
        (first,) = _tickets(self.cfg, "staffing")
        self.assertEqual(sorted(first["affected_task_ids"]), ["M01", "M02", "M03"])
        m04 = replace(plan.tasks[2], id="M04", depends_on=())
        grown = replace(plan, graph_version=plan.graph_version + 1, tasks=(*plan.tasks, m04))
        state.task_states["M04"] = "READY"
        staffing_gate(self.cfg, grown, state)
        (ticket,) = _tickets(self.cfg, "staffing")  # the same ticket, not a second one
        self.assertEqual(ticket["incident_id"], first["incident_id"])
        self.assertEqual(ticket["affected_task_ids"], ["M01", "M02", "M03", "M04"])
        self.assertEqual(tasks_paused_by_incidents(self.cfg, grown), {"M01", "M02", "M03", "M04"})
        events = [item["event"] for item in PipelineIncidentStore(self.cfg.state_dir).load()["journal"]]
        self.assertIn("incident_hold_widened", events)
        # A pass with nothing new widens nothing.
        staffing_gate(self.cfg, grown, state)
        self.assertEqual(_tickets(self.cfg, "staffing")[0]["affected_task_ids"], ["M01", "M02", "M03", "M04"])
        self.assertEqual(events.count("incident_hold_widened"), 1)

    def test_a_hold_that_cannot_be_widened_is_a_ticket_of_its_own(self) -> None:
        """The journal refuses the widening: M04 still gets held, by a ticket through the door.

        Mutation: the except branch of _stop returns the open ticket - M04
        is held by nothing.
        """

        from codex_autopilot.engineer_reservation import tasks_paused_by_incidents
        from codex_autopilot.pipeline_engineer import PipelineIncidentError
        from codex_autopilot.staffing import staffing_gate

        plan = self._broken(self.plan())
        state = self.store.load()
        staffing_gate(self.cfg, plan, state)
        grown = replace(plan, graph_version=plan.graph_version + 1,
                        tasks=(*plan.tasks, replace(plan.tasks[2], id="M04", depends_on=())))
        with mock.patch.object(PipelineIncidentStore, "hold_more_tasks",
                               side_effect=PipelineIncidentError("journal locked")):
            staffing_gate(self.cfg, grown, state)
        first, second = _tickets(self.cfg, "staffing")
        self.assertEqual(sorted(first["affected_task_ids"]), ["M01", "M02", "M03"])
        self.assertEqual(second["affected_task_ids"], ["M04"])
        self.assertEqual(second["phase"], "PIPELINE_ENGINEER")
        self.assertIn("M04", tasks_paused_by_incidents(self.cfg, grown))

