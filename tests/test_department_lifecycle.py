"""R30 through the production lifecycle: bootstrap, reservation, completion.

The run is the beyondness shape (``_departments``): three professions, one
lead (art-reviewer, "Character Art Verifier"), no departments written
anywhere. What used to happen to it: the verifier was the task's own
verifier_role only because the planner happened to write one, the rules told
it R30 was not in force, and M01 could never be bound to a rubric.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest import mock

from _departments import DepartmentRun, beyondness_plan
from codex_autopilot.department_acceptance import RubricReference, rubric_scope, store_department_rubric
from codex_autopilot.pipeline_engineer import PipelineIncidentStore


def _tickets(cfg, kind: str) -> list[dict]:
    return [
        item for item in PipelineIncidentStore(cfg.state_dir).load()["incidents"]
        if (item.get("system_state") or {}).get("stop_kind") == kind
    ]


class TheRubricIsThereBeforeTheFirstTaskTests(DepartmentRun):
    def test_bootstrap_writes_version_one_of_every_department(self) -> None:
        """Mutation: remove ensure_all_department_rubrics from the bootstrap."""

        records = self.rubric_records("art-reviewer")
        self.assertEqual(len(records), 1)
        self.assertEqual(self.store.load().worker_sessions, [])
        record = self.memory.get_record(str(records[0]["id"]))
        self.assertEqual(json.loads(record["statement"])["version"], 1)
        self.assertEqual(record["created_by"], "codex-autopilot-runtime")

    def test_m01_is_judged_by_its_lead_by_version_one(self) -> None:
        """M01 has no dependencies and still gets its lead, title and rubric.

        Mutation: build department_acceptance only for a task with selected
        dependency outputs (the old pin source) - M01's prompt has none.
        """

        m01 = self.reserve()[0]
        verifier = self.implement(m01, "worker-M01").descriptors[0]
        self.assertEqual(verifier.kind, "verifier")
        self.assertEqual(verifier.title, "Character Art Verifier | Verify M01 | Model part M01")
        record_id = str(self.rubric_records("art-reviewer")[0]["id"])
        self.assertIn('"department_acceptance"', verifier.prompt)
        self.assertIn(f'"record_id":"{record_id}"', verifier.prompt)
        self.assertIn("lead-expectation-1", verifier.prompt)
        self.assertIn('the exact attestation "rubric":{"record_id":"' + record_id, verifier.prompt)
        accepted = self.judge(verifier, "lead-M01", self.verdict("M01"))
        self.assertEqual(accepted.worker_status, "PASS")
        self.assertEqual(self.store.load().task_states["M01"], "VERIFIED")


class ARunAlreadyUnderWayTests(DepartmentRun):
    def initialize(self, plan_file, skill) -> None:
        # A run bootstrapped by the runtime from before R30: no rubric at all.
        with mock.patch("codex_autopilot.bootstrap.ensure_all_department_rubrics", return_value={}):
            super().initialize(plan_file, skill)

    def test_the_rubric_lands_at_the_leads_reservation(self) -> None:
        """Under the coordinator lock the lead's reservation writes version 1.

        Mutation: admit_verifier and the prompt only read (ensure=False) -
        M01 is stopped for want of a rubric instead of being judged.
        """

        self.assertEqual(self.rubric_records("art-reviewer"), [])
        verifier = self.implement(self.reserve()[0], "worker-M01").descriptors[0]
        self.assertEqual(verifier.kind, "verifier")
        self.assertEqual(len(self.rubric_records("art-reviewer")), 1)
        self.assertEqual(_tickets(self.cfg, "department_lead"), [])


class AVerdictWithoutTheRubricTests(DepartmentRun):
    def reach_the_lead(self):
        return self.implement(self.reserve()[0], "worker-M01").descriptors[0]

    def test_is_a_recorded_refusal_and_a_fresh_lead(self) -> None:
        """Not an incident: the reason is on record and the next lead reads it.

        Mutation: completion raises WorkerProtocolError for a missing
        attestation (as before) - an incident and a stall instead.
        """

        verifier = self.reach_the_lead()
        outcome = self.judge(verifier, "lead-1", 'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}')
        self.assertEqual(outcome.worker_status, "VERIFICATION_REJECTED")
        state = self.store.load()
        self.assertEqual(len(state.verification_rejections["M01"]), 1)
        self.assertNotIn("counted", state.verification_rejections["M01"][0])
        self.assertEqual(state.task_states["M01"], "VERIFYING")
        fresh = outcome.descriptors[0]
        self.assertEqual(fresh.kind, "verifier")
        self.assertIn("exactly three top-level fields", fresh.prompt)
        self.assertEqual(PipelineIncidentStore(self.cfg.state_dir).load()["incidents"], [])

    def test_a_verdict_by_a_superseded_version_is_refused_but_not_counted(self) -> None:
        """The rubric advanced while the lead judged: record, recount nothing, judge again.

        Three times over, and the task is not stopped: these refusals are
        not the lead's mistake. Mutation: count every refusal toward
        MAX_VERIFICATION_REJECTIONS - the third stops the task.
        """

        verifier = self.reach_the_lead()
        for round_index in range(3):
            given = RubricReference(**json.loads(
                self.verdict("M01").split("AUTOPILOT_VERIFICATION: ", 1)[1]
            )["rubric"])
            self._advance(round_index + 2)
            stale = self.verdict("M01", rubric=given.to_dict())
            outcome = self.judge(verifier, f"lead-{round_index}", stale)
            self.assertEqual(outcome.worker_status, "VERIFICATION_REJECTED")
            verifier = outcome.descriptors[0]
            self.assertEqual(verifier.kind, "verifier")
            self.assertIn(f'"version":{round_index + 2}', verifier.prompt)
        state = self.store.load()
        self.assertEqual([item.get("counted") for item in state.verification_rejections["M01"]], [False] * 3)
        self.assertEqual(_tickets(self.cfg, "verification_protocol"), [])
        final = self.judge(verifier, "lead-final", self.verdict("M01"))
        self.assertEqual(final.worker_status, "PASS")

    def _advance(self, version: int) -> None:
        evidence = str(self.memory.record_evidence(
            kind="test", summary="outcome", command="compare", result="PASS", exit_code=0, created_by="t")["id"])
        self.memory._record_runtime_verification_result(
            task_id="M01", check_id="independent-acceptance", policy="independent", verdict="REVISE",
            summary="earlier acceptance", evidence_ids=[evidence], created_by="Character Art Verifier",
            provider="codex-desktop", provider_thread_id=f"earlier-{version}", provider_turn_id="t",
            details={"department_acceptance": {"department": {"id": "art-reviewer"}}},
        )
        store_department_rubric(
            self.memory, department_id="art-reviewer", version=version,
            criteria=[{"id": f"c{version}", "requirement": "The silhouette matches the sheet."}],
            evidence_ids=[evidence], created_by="Character Art Verifier",
        )


class ALeadFromBeforeR30Tests(DepartmentRun):
    def test_is_refused_uncounted_and_replaced_by_a_lead_with_the_rubric(self) -> None:
        """Launched by the old runtime, with no rubric to load: not its mistake, not a fault.

        Mutation: verdict_acceptance raises whenever the rubric cannot be
        loaded - the old lead's completion becomes an incident.
        """

        verifier = self.implement(self.reserve()[0], "worker-M01").descriptors[0]
        state = self.store.load()
        session = next(item for item in state.worker_sessions if item["reservation_token"] == verifier.reservation_token)
        session["descriptor"]["prompt"] = "a verifier prompt written by a runtime from before R30"
        self.store.save(state)
        with mock.patch("codex_autopilot.department_runtime.stored_rubric_versions", return_value=()):
            outcome = self.judge(verifier, "old-lead", 'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}')
        self.assertEqual(outcome.worker_status, "VERIFICATION_REJECTED")
        rejection = self.store.load().verification_rejections["M01"][-1]
        self.assertIs(rejection["counted"], False)
        self.assertIn("launched without its department's rubric", rejection["reason"])
        fresh = outcome.descriptors[0]
        self.assertEqual(fresh.kind, "verifier")
        self.assertIn('"department_acceptance"', fresh.prompt)
        self.assertEqual(PipelineIncidentStore(self.cfg.state_dir).load()["incidents"], [])


def _two_professions() -> dict:
    raw = beyondness_plan()
    raw["execution_strategy"] = "parallel"
    raw["max_parallel_workers"] = 2
    raw["tasks"][1]["depends_on"] = []
    raw["tasks"] = raw["tasks"][:2]  # M01 character-artist, M02 reference-artist, siblings
    raw["tasks"][0]["verification"].pop("verifier_role")
    return raw


class ATaskWithNoLeadStopsAloneTests(DepartmentRun):
    plan_payload = staticmethod(_two_professions)

    def initialize(self, plan_file, skill) -> None:
        # A saved plan from before R30's admission: M01's profession names no lead.
        with mock.patch("codex_autopilot.plan_admission.validate_department_leads", return_value=[]), \
                mock.patch("_plan_contract._attach_test_lead"):
            super().initialize(plan_file, skill)

    def test_its_neighbour_is_judged_while_the_on_call_looks(self) -> None:
        """One ticket holds M01; M02 goes on to its lead in the same run.

        It used to raise inside the reservation: the pass rolled back - with
        the completion of the neighbour that called it - and no ticket was
        filed. Mutation: admit_verifier re-raises instead of stopping.
        """

        first, second = self.reserve()
        stopped = self.implement(first if first.task_id == "M01" else second, "worker-M01")
        self.assertEqual([item.kind for item in stopped.descriptors], ["pipeline_engineer"])
        state = self.store.load()
        self.assertEqual(state.task_states["M01"], "IMPLEMENTED")
        (ticket,) = _tickets(self.cfg, "department_lead")
        self.assertEqual(ticket["affected_task_ids"], ["M01"])
        self.assertIn("devops-request-plan-change", ticket["system_state"]["recommendation"])
        self.assertIn("'character-artist'", ticket["system_state"]["recommendation"])
        neighbour = self.implement(second if first.task_id == "M01" else first, "worker-M02")
        self.assertIn(("verifier", "M02"), [(item.kind, item.task_id) for item in neighbour.descriptors])

    def test_the_on_calls_request_carries_the_lead_requirement(self) -> None:
        """Mutation: request_plan_change without marking requires_lead."""

        from codex_autopilot.engineer_stop_actions import request_plan_change
        from codex_autopilot.resilience import active_plan_change

        first, second = self.reserve()
        stopped = self.implement(first if first.task_id == "M01" else second, "worker-M01")
        engineer = next(item for item in stopped.descriptors if item.kind == "pipeline_engineer")
        self.mark_active(engineer.reservation_token, "on-call")
        (ticket,) = _tickets(self.cfg, "department_lead")
        result = request_plan_change(
            self.cfg, incident_id=str(ticket["incident_id"]), task_id="M01",
            reason="name the lead of character-artist", thread_id="on-call",
        )
        record = active_plan_change(self.store.load(), request_id=result["plan_change_id"])
        self.assertTrue(record["requires_lead"])

    def test_the_on_calls_plan_change_must_name_the_lead(self) -> None:
        """A change asked for a lead stop is refused until the requester has one.

        Mutation: state_issues without the requires_lead check - a graph
        that leaves M01 without a lead is admitted, and the stop comes back.
        """

        from codex_autopilot.plan import plan_to_dict
        from codex_autopilot.plan_admission import IssueCollector, plan_change_candidate, state_issues

        current = self.plan()
        change = plan_to_dict(current)
        change["graph_version"] = current.graph_version + 1
        state = self.store.load()
        state.plan_changes = [{"id": "PC1", "requires_lead": True}]
        state.active_plan_change_id = "PC1"
        read = plan_change_candidate(IssueCollector(), current, change, "adaptive")
        issues = state_issues(current, read, state, "M01")
        self.assertTrue(any("requester M01 has no department lead" in message for _, message in issues), issues)
        change["tasks"][0]["verification"]["verifier_role"] = "art-reviewer"
        read = plan_change_candidate(IssueCollector(), current, change, "adaptive")
        self.assertEqual(state_issues(current, read, state, "M01"), [])


class ALaunchThatCannotBeBuiltTests(DepartmentRun):
    plan_payload = staticmethod(lambda: _two_professions_with_leads())

    def test_gives_back_what_the_reservation_took_and_stops_that_task(self) -> None:
        """Mutation: build_or_hold without its except - the raise rolls the pass back."""

        from codex_autopilot.ai_studio import AIStudioRuntime, ContextBoundaryError

        first, second = self.reserve()
        real = AIStudioRuntime.build_prompt

        def refuse(runtime, task_id, *args, **kwargs):
            if kwargs.get("phase") == "verification" and task_id == "M01":
                raise ContextBoundaryError("verification prompt for M01 is 999999 characters")
            return real(runtime, task_id, *args, **kwargs)

        m01 = first if first.task_id == "M01" else second
        before = self.store.load()
        with mock.patch.object(AIStudioRuntime, "build_prompt", refuse):
            outcome = self.implement(m01, "worker-M01")
        state = self.store.load()
        self.assertEqual(state.task_states["M01"], "IMPLEMENTED")
        self.assertEqual(state.task_attempts["M01"], before.task_attempts["M01"])
        self.assertFalse([item for item in state.worker_sessions if item["task_id"] == "M01" and item["kind"] == "verifier"])
        self.assertEqual(len(_tickets(self.cfg, "launch_refused")), 1)
        self.assertIn("pipeline_engineer", [item.kind for item in outcome.descriptors])
        self.assertEqual([lock for lock in state.resource_locks if "M01" in json.dumps(lock)], [])


def _two_professions_with_leads() -> dict:
    raw = beyondness_plan()
    raw["execution_strategy"] = "parallel"
    raw["max_parallel_workers"] = 2
    raw["tasks"][1]["depends_on"] = []
    raw["tasks"] = raw["tasks"][:2]
    return raw


class APlanChangeBringsItsDepartmentsRubricTests(DepartmentRun):
    def _candidate(self):
        from codex_autopilot.plan import plan_to_dict, validate_plan_change

        current = self.plan()
        raw = plan_to_dict(current)
        raw["graph_version"] = current.graph_version + 1
        raw["roles"] += [
            {"id": "rigger", "name": "Rigger", "version": "1.0.0", "responsibilities": ["Rig the character."]},
            {"id": "rig-lead", "name": "Rig Lead", "version": "1.0.0", "responsibilities": ["Accept rigs."],
             "verification_expectations": ["Every joint deforms without tearing."]},
        ]
        task = json.loads(json.dumps(raw["tasks"][0]))
        task.update(id="M04", role="rigger", depends_on=["M03"])
        task["verification"]["verifier_role"] = "rig-lead"
        raw["tasks"].append(task)
        return current, validate_plan_change(current, raw, self.cfg.profile)

    def _commit(self, current, candidate) -> None:
        from codex_autopilot.resilience import commit_plan_change

        state = self.store.load()
        state.graph_version = candidate.graph_version
        state.task_states["M04"] = "WAITING"
        commit_plan_change(
            self.cfg.state_dir, profile=self.cfg.profile, current=current, candidate=candidate,
            state=state, request_id="PC1",
        )

    def test_the_new_department_has_version_one_before_any_reservation(self) -> None:
        """Mutation: remove ensure_all_department_rubrics from commit_plan_change."""

        current, candidate = self._candidate()
        self.assertEqual(self.rubric_records("rig-lead"), [])
        self._commit(current, candidate)
        self.assertEqual(len(self.rubric_records("rig-lead")), 1)

    def test_a_failing_rubric_write_does_not_wedge_the_commit(self) -> None:
        """After COMMITTED and tolerated; recovery does not repeat it.

        Mutation: ensure_all_department_rubrics lets the failure out - the
        commit raises after its transaction and every reservation would.
        """

        from codex_autopilot.resilience import PLAN_CHANGE_TRANSACTION_FILE, recover_plan_change_transaction

        current, candidate = self._candidate()
        with mock.patch("codex_autopilot.department_runtime.ensure_department_rubric", side_effect=OSError("disk")):
            self._commit(current, candidate)
        transaction = json.loads((self.cfg.state_dir / PLAN_CHANGE_TRANSACTION_FILE).read_text())
        self.assertEqual(transaction["status"], "COMMITTED")
        recover_plan_change_transaction(self.cfg.state_dir, self.cfg.profile)
        self.assertEqual(self.plan().graph_version, candidate.graph_version)


class AnAmbiguousRubricIsTheOnCallsToRepairTests(DepartmentRun):
    def test_the_stray_record_is_superseded_and_the_task_returns(self) -> None:
        """Mutation: supersede_rubric_record without its runtime-v1 guard."""

        from codex_autopilot.department_gate import supersede_rubric_record
        from codex_autopilot.engineer_stop_actions import EngineerStopActionError, require_stop_ticket_closable

        v1 = self.memory.get_record(str(self.rubric_records("art-reviewer")[0]["id"]))
        stray = self.memory.record_verified_fact(
            statement=v1["statement"], evidence_ids=[v1["evidence"][0]["id"]],
            verification_method="written before the scope was reserved", created_by="old-model",
            scope=rubric_scope("art-reviewer"), reserved_scope=True,
        )
        outcome = self.implement(self.reserve()[0], "worker-M01")
        (ticket,) = _tickets(self.cfg, "department_lead")
        self.assertIn("devops-supersede-rubric", ticket["system_state"]["recommendation"])
        self.assertIn(str(stray["id"]), ticket["system_state"]["diagnosis"])
        engineer = next(item for item in outcome.descriptors if item.kind == "pipeline_engineer")
        self.mark_active(engineer.reservation_token, "on-call")
        incident = str(ticket["incident_id"])
        with self.assertRaisesRegex(EngineerStopActionError, "runtime's own version 1"):
            supersede_rubric_record(self.cfg, incident_id=incident, record_id=str(v1["id"]),
                                    reason="wrong one", thread_id="on-call")
        with self.assertRaisesRegex(EngineerStopActionError, "only the on-call"):
            supersede_rubric_record(self.cfg, incident_id=incident, record_id=str(stray["id"]),
                                    reason="stray", thread_id="worker-M01")
        supersede_rubric_record(self.cfg, incident_id=incident, record_id=str(stray["id"]),
                                reason="stray second v1", thread_id="on-call")
        self.assertEqual(self.memory.get_record(str(stray["id"]))["status"], "superseded")
        self.assertEqual(len(self.rubric_records("art-reviewer")), 1)
        require_stop_ticket_closable(
            self.cfg, incident, ["supersede_department_rubric", "return_stopped_task"], "on-call"
        )


class OnlyTheLeadOrTheOnCallProposesTests(DepartmentRun):
    def test_a_worker_of_the_department_never_changes_its_standard(self) -> None:
        """Mutation: authorize_rubric_proposal accepts any session of the run."""

        from codex_autopilot.department_acceptance import DepartmentAcceptanceError
        from codex_autopilot.department_runtime import authorize_rubric_proposal

        m01 = self.reserve()[0]
        self.mark_active(m01.reservation_token, "worker-M01")
        with self.assertRaisesRegex(DepartmentAcceptanceError, "never changes the standard"):
            authorize_rubric_proposal(self.root, "art-reviewer", "worker-M01")
        with self.assertRaisesRegex(DepartmentAcceptanceError, "no pending session"):
            authorize_rubric_proposal(self.root, "art-reviewer", "stranger")
        verifier = self.implement(m01, "worker-M01").descriptors[0]
        self.mark_active(verifier.reservation_token, "lead-M01")
        self.assertEqual(authorize_rubric_proposal(self.root, "art-reviewer", "lead-M01"), "Character Art Verifier")
        with self.assertRaisesRegex(DepartmentAcceptanceError, "not 'rig-lead'"):
            authorize_rubric_proposal(self.root, "rig-lead", "lead-M01")


class ALeadThatOutlivedItsAcceptanceTests(DepartmentRun):
    def _accepted(self) -> dict:
        verifier = self.implement(self.reserve()[0], "worker-M01").descriptors[0]
        self.judge(verifier, "lead-M01", self.verdict("M01"))
        state = self.store.load()
        session = next(item for item in state.worker_sessions if item.get("thread_id") == "lead-M01")
        session["completed_at"] = "2026-01-01T00:00:00+00:00"
        self.store.save(state)
        return session

    def _client(self, turns):
        calls = []

        class Client:
            def __init__(self, *_args):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read_thread(self, thread_id):
                calls.append(thread_id)
                return {"id": thread_id, "turns": turns}

        return Client, calls

    def test_a_later_turn_is_a_recorded_defect_not_a_stop(self) -> None:
        """Mutation: count no turns after the runtime's - nothing is recorded."""

        from codex_autopilot.department_audit import LEAD_AUDIT_CHECK, audit_lead_sessions
        from codex_autopilot.rules import violation_counts

        session = self._accepted()
        client, calls = self._client([
            {"id": session["turn_id"], "status": "completed"},
            {"id": "her-message", "status": "completed"},
        ])
        findings = audit_lead_sessions(self.cfg, client_factory=client)
        self.assertEqual([item["turns_after"] for item in findings], [[{"id": "her-message", "status": "completed"}]])
        self.assertEqual(violation_counts(self.cfg.state_dir).get("R30"), 1)
        results = self.memory.list_verification_results(task_id="M01", limit=20).records
        self.assertIn(LEAD_AUDIT_CHECK, [item["check_id"] for item in results])
        self.assertEqual(PipelineIncidentStore(self.cfg.state_dir).load()["incidents"], [])
        self.assertEqual(self.store.load().task_states["M01"], "VERIFIED")
        audit_lead_sessions(self.cfg, client_factory=client)
        self.assertEqual(calls, ["lead-M01"], "each lead is read once")

    def test_a_foreign_turn_still_running_is_a_ticket_that_holds_nothing(self) -> None:
        from codex_autopilot.department_audit import audit_lead_sessions

        session = self._accepted()
        client, _ = self._client([
            {"id": session["turn_id"], "status": "completed"},
            {"id": "someone", "status": "inProgress"},
        ])
        audit_lead_sessions(self.cfg, client_factory=client)
        (ticket,) = _tickets(self.cfg, "lead_outlived")
        self.assertEqual(ticket["affected_task_ids"], [])
        self.assertEqual(ticket["context_task_id"], "M01")

    def test_a_clean_lead_is_read_once_and_left_alone(self) -> None:
        from codex_autopilot.department_audit import audit_lead_sessions
        from codex_autopilot.rules import violation_counts

        session = self._accepted()
        client, _ = self._client([{"id": session["turn_id"], "status": "completed"}])
        self.assertEqual(audit_lead_sessions(self.cfg, client_factory=client), [])
        self.assertIsNone(violation_counts(self.cfg.state_dir).get("R30"))
        state = self.store.load()
        audited = next(item for item in state.worker_sessions if item.get("thread_id") == "lead-M01")
        self.assertFalse(audited["lead_audit"]["outlived"])

    def test_the_sweep_and_the_wake_up_run_the_audit(self) -> None:
        """Mutation: remove the audit call from wake.sweep."""

        from codex_autopilot import wake

        with mock.patch("codex_autopilot.department_audit.audit_lead_sessions") as audit:
            wake.sweep(roots=[str(self.root)], spawn=lambda *_a, **_k: 0)
        audit.assert_called()


class ASecondLeadMeasuresTheRubricTests(DepartmentRun):
    def setUp(self) -> None:
        super().setUp()
        path = self.cfg.state_dir / "config.toml"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("[runtime]\n", "[runtime]\nsecond_lead_every = 1\n", 1), encoding="utf-8")
        from codex_autopilot.config import load_config

        self.cfg = load_config(self.root)
        self.assertEqual(self.cfg.runtime.second_lead_every, 1)

    def test_the_first_verdict_is_applied_and_the_disagreement_recorded(self) -> None:
        """A second fresh lead judges the same work; the first lead's verdict stands.

        Mutation: second_lead_gate applies the second lead's verdict - M01
        goes to revision on the second lead's word.
        """

        from codex_autopilot.department_audit import SECOND_LEAD_CHECK

        first = self.implement(self.reserve()[0], "worker-M01").descriptors[0]
        deferred = self.judge(first, "lead-1", self.verdict("M01"))
        self.assertEqual(deferred.worker_status, "PASS")
        state = self.store.load()
        self.assertEqual(state.task_states["M01"], "VERIFYING")
        second = deferred.descriptors[0]
        self.assertEqual((second.kind, second.task_id), ("verifier", "M01"))
        self.assertEqual(second.title, first.title)
        issue = {"code": "SILHOUETTE", "summary": "The silhouette drifts", "details": "Compare the side view.", "dod_refs": [1]}
        self.judge(second, "lead-2", self.verdict("M01", "REVISE", [issue]))
        state = self.store.load()
        self.assertEqual(state.task_states["M01"], "VERIFIED")
        results = self.memory.list_verification_results(task_id="M01", limit=20).records
        measured = next(item for item in results if item["check_id"] == SECOND_LEAD_CHECK)
        details = self.memory.get_verification_result(measured["id"])["details"]["second_lead"]
        self.assertEqual((details["primary_verdict"], details["second_verdict"], details["agrees"]), ("PASS", "REVISE", False))
        self.assertEqual(details["department_disagreement"]["rate"], 1.0)
        self.assertEqual(
            [item["check_id"] for item in results].count("independent-acceptance"), 1,
            "the second lead's verdict is a measurement, not a second acceptance",
        )

    def test_the_default_is_every_fifth_acceptance(self) -> None:
        from codex_autopilot.config import DEFAULT_SECOND_LEAD_EVERY, RuntimeConfig

        self.assertEqual(RuntimeConfig().second_lead_every, DEFAULT_SECOND_LEAD_EVERY)
        self.assertEqual(DEFAULT_SECOND_LEAD_EVERY, 5)


if __name__ == "__main__":
    unittest.main()
