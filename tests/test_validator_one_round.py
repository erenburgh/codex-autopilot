"""The plan validator reports every violation in one round.

The replanner has three attempts. The validator stopped at its first
violation, and two more checks waited behind it (Goal Contract coverage,
and the state conditions checked only at the commit after the plan
verifier's PASS - where a conflict took the dispatcher down). These tests
drive the production entry points: the worker's completion
(``complete_desktop_worker``), ``validate_plan_change``, the reservation
frontier, the on-call's own action. Fakes only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from _handoff import bump_task_checkpoint
from _relay import reserve_ready_frontier
from codex_autopilot.department_acceptance import DEPARTMENT_FIELDS
from codex_autopilot.lifecycle import complete_desktop_worker
from codex_autopilot.plan import load_plan, validate_plan, validate_plan_change
from codex_autopilot.plan_fields import ALLOWED_FIELDS, COMPATIBILITY_FIELDS
from codex_autopilot.plan_issues import PlanIssues
from codex_autopilot.plan_verification import PLAN_VERIFICATION_PREFIX
from codex_autopilot.resilience import PLAN_CHANGE_RESULT_PREFIX, active_plan_change
from test_plan_evolution import PlanEvolutionTests, graph, request_line, task

ROOT = Path(__file__).resolve().parents[1]


def reply(change_id: str, base: int, plan: dict) -> str:
    return PLAN_CHANGE_RESULT_PREFIX + " " + json.dumps(
        {"request_id": change_id, "base_graph_version": base, "plan": plan}, separators=(",", ":")
    )


def messages(exc: PlanIssues) -> list[str]:
    return [item.message for item in exc.issues]


def misspelled_department() -> tuple[dict, str, str]:
    """A department with `lead_role` written for `lead_role_id`, the v1.0 run's slip.

    Built from ``DEPARTMENT_FIELDS`` rather than a literal: the R30 line
    reworks the department's fields, and a test about naming the unknown and
    the missing together must not also pin which fields a department has.
    """

    dropped = "lead_role_id" if "lead_role_id" in DEPARTMENT_FIELDS else DEPARTMENT_FIELDS[-1]
    wrong = "lead_role" if dropped == "lead_role_id" else f"{dropped}_misspelled"
    assert wrong not in DEPARTMENT_FIELDS
    department = {name: name for name in DEPARTMENT_FIELDS if name != dropped}
    department[wrong] = "builder"
    return department, wrong, dropped


class _Replanning(unittest.TestCase):
    setUp = PlanEvolutionTests.setUp
    tearDown = PlanEvolutionTests.tearDown
    initialize = PlanEvolutionTests.initialize
    mark_active = staticmethod(PlanEvolutionTests.mark_active)
    candidate_with_prerequisite = PlanEvolutionTests.candidate_with_prerequisite

    def at_the_replanner(self, raw_graph=None):
        """A's worker asks for a plan change; the replanner PC1 is reserved."""

        cfg, store = self.initialize(raw_graph or graph([task("A")], max_workers=1))
        worker = next(
            item
            for item in reserve_ready_frontier(cfg, relay_owner_thread_id="owner", hook_gate=lambda _cfg: None)
            if item.task_id == "A"
        )
        self.mark_active(store, worker.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, "A", "Plan change requested.")
        replanner = complete_desktop_worker(
            cfg, thread_id="worker-A", turn_id="turn-A", final_message=request_line("A"),
            hook_gate=lambda _cfg: None,
        ).descriptors[0]
        self.assertEqual(replanner.kind, "replanner")
        return cfg, store, replanner

    def answer(self, cfg, store, replanner, message: str, thread: str = "replanner-PC1"):
        self.mark_active(store, replanner.reservation_token, thread)
        return complete_desktop_worker(
            cfg, thread_id=thread, turn_id=f"turn-{thread}", final_message=message,
            hook_gate=lambda _cfg: None,
        )

    def valid_candidate(self, cfg) -> dict:
        return self.candidate_with_prerequisite(load_plan(cfg.state_dir, cfg.profile))


class OneRefusalCarriesEveryDefectTests(_Replanning):
    def test_four_independent_defects_come_back_in_one_refusal_and_one_prompt(self) -> None:
        cfg, store, replanner = self.at_the_replanner()
        bad = self.valid_candidate(cfg)
        bad["nonsense_field"] = 1
        del bad["roles"][0]["name"]
        del bad["tasks"][0]["reasoning"]
        department, wrong, dropped = misspelled_department()
        bad["departments"] = [department]

        outcome = self.answer(cfg, store, replanner, reply("PC1", 1, bad))

        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_REJECTED")
        change = active_plan_change(store.load(), request_id="PC1")
        self.assertEqual(len(change["rejections"]), 1)
        issues = [item["message"] for item in change["rejections"][-1]["issues"]]
        self.assertEqual(len(issues), 4, issues)
        self.assertIn("plan has unknown fields: ['nonsense_field']", issues[0])
        self.assertIn("role 1.name must be a non-empty string", issues[1])
        self.assertIn(f"department 1 has unknown fields: [{wrong!r}]", issues[2])
        self.assertIn(f"missing required fields: [{dropped!r}]", issues[2])
        self.assertIn("task 1 requires reasoning", issues[3])
        prompt = outcome.descriptors[0].prompt
        self.assertIn("The previous attempt was rejected for 4 reasons;", prompt)
        # Each line is `path: message`, the path the collector recorded.
        for number, item in enumerate(change["rejections"][-1]["issues"], 1):
            self.assertIn(f"\n{number}. {item['path']}: {item['message']}", prompt)

    def test_two_defects_inside_one_task_are_both_reported(self) -> None:
        cfg, _store = self.initialize(graph([task("A")], max_workers=1))
        current = load_plan(cfg.state_dir, cfg.profile)
        bad = self.candidate_with_prerequisite(current)
        del bad["tasks"][0]["execution_mode_reason"]
        del bad["tasks"][0]["verification"]["max_revision_attempts"]
        with self.assertRaises(PlanIssues) as caught:
            validate_plan_change(current, bad, "adaptive")
        found = messages(caught.exception)
        self.assertEqual(len(found), 2, found)
        self.assertIn("task 1.execution_mode_reason must be a non-empty string", found[0])
        self.assertIn("task 1.verification.max_revision_attempts must be declared", found[1])

    def test_a_broken_task_id_raises_no_false_dependency_and_no_cycle_crash(self) -> None:
        cfg, _store = self.initialize(graph([task("A")], max_workers=1))
        current = load_plan(cfg.state_dir, cfg.profile)
        bad = self.candidate_with_prerequisite(current)
        broken = task("X")
        broken["title"] = ""
        bad["tasks"].append(broken)
        bad["tasks"].append(task("Y", depends_on=("X",)))
        with self.assertRaises(PlanIssues) as caught:
            validate_plan_change(current, bad, "adaptive")
        found = messages(caught.exception)
        # Y is read and checked; X, unread, is not reported as missing, and
        # the cycle search does not meet an id it has no entry for.
        self.assertEqual(found, ["task 3.title must be a non-empty string"])

    def test_the_immutables_are_compared_on_what_was_read(self) -> None:
        cfg, _store = self.initialize(graph([task("A")], max_workers=1))
        current = load_plan(cfg.state_dir, cfg.profile)
        bad = self.candidate_with_prerequisite(current)
        bad["goal"] = "A different goal."
        bad["tasks"][0]["title"] = ""
        with self.assertRaises(PlanIssues) as caught:
            validate_plan_change(current, bad, "adaptive")
        found = messages(caught.exception)
        self.assertEqual(len(found), 2, found)
        self.assertIn("task 1.title must be a non-empty string", found[0])
        self.assertEqual(found[1], "plan changes must not replace the run goal")

    def test_coverage_is_reported_in_the_same_round(self) -> None:
        contract = {
            "required_outcomes": [
                {"id": "test-outcome", "description": "The first result."},
                {"id": "second", "description": "The second result."},
            ],
            "deliverables": [{"id": "d", "description": "A deliverable."}],
            "constraints": [],
            "global_acceptance": [{"id": "g", "description": "Accepted."}],
        }
        b = task("B")
        b["produces_outcomes"] = ["second"]
        raw = graph([task("A"), b], max_workers=1)
        raw["goal_contract"] = contract
        cfg, store, replanner = self.at_the_replanner(raw)
        bad = self.valid_candidate(cfg)
        next(item for item in bad["tasks"] if item["id"] == "B")["produces_outcomes"] = ["test-outcome"]
        bad["roles"][0]["responsibilities"] = []

        outcome = self.answer(cfg, store, replanner, reply("PC1", 1, bad))

        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_REJECTED")
        issues = active_plan_change(store.load(), request_id="PC1")["rejections"][-1]["issues"]
        stages = [item["stage"] for item in issues]
        self.assertEqual(stages, ["roles", "coverage"], issues)
        self.assertIn("'second' has no producer", issues[1]["message"])

    def test_outcomes_and_r29_are_checked_per_task_beside_a_broken_one(self) -> None:
        """A broken task hides nothing of another task's own checks.

        Outcome bindings and the R29 floor depend on the task alone. Gated on
        every task being read, B's two violations surfaced only in the round
        after A's field was fixed.
        """

        cfg, _store = self.initialize(graph([task("A")], max_workers=1))
        current = load_plan(cfg.state_dir, cfg.profile)
        bad = self.candidate_with_prerequisite(current)
        bad["tasks"][0]["title"] = ""
        requester = bad["tasks"][1]
        requester["produces_outcomes"] = ["ghost"]
        requester["verification"]["deterministic_checks"] = []
        with self.assertRaises(PlanIssues) as caught:
            validate_plan_change(current, bad, "adaptive")
        found = [(item.stage, item.path) for item in caught.exception.issues]
        self.assertEqual(found, [
            ("tasks", "task 1.title"),
            ("outcomes", "task A.produces_outcomes"),
            ("acceptance", "task A.verification"),
        ])
        self.assertIn("unknown Goal Contract outcomes: ghost", caught.exception.issues[1].message)
        self.assertIn("full-suite deterministic check", caught.exception.issues[2].message)

    def test_a_wrong_schema_version_is_one_issue_among_the_rest(self) -> None:
        cfg, _store = self.initialize(graph([task("A")], max_workers=1))
        current = load_plan(cfg.state_dir, cfg.profile)
        bad = self.candidate_with_prerequisite(current)
        bad["schema_version"] = 2
        bad["goal"] = "Another goal."
        with self.assertRaises(PlanIssues) as caught:
            validate_plan_change(current, bad, "adaptive")
        self.assertEqual(messages(caught.exception), [
            "plan changes must use the canonical v0.9 schema",
            "plan changes must not replace the run goal",
        ])

    def test_a_fresh_plan_reports_its_schema_with_the_rest(self) -> None:
        """The planner's graph too: schema_version used to be a pregate."""

        raw = graph([task("A")])
        raw["schema_version"] = 4
        raw["tasks"][0]["title"] = ""
        with self.assertRaises(PlanIssues) as caught:
            validate_plan(raw, "adaptive")
        self.assertEqual(messages(caught.exception), [
            "plan.schema_version must be 3; v0.8 serial plans may use 2 or omit it",
            "task 1.title must be a non-empty string",
        ])

    def test_one_violation_reads_exactly_as_before(self) -> None:
        cfg, _store = self.initialize(graph([task("A")], max_workers=1))
        current = load_plan(cfg.state_dir, cfg.profile)
        bad = self.candidate_with_prerequisite(current)
        bad["goal"] = "Another goal."
        with self.assertRaises(PlanIssues) as caught:
            validate_plan_change(current, bad, "adaptive")
        self.assertEqual(str(caught.exception), "plan changes must not replace the run goal")
        self.assertIsInstance(caught.exception, ValueError)

    def test_the_order_does_not_depend_on_the_hash_seed(self) -> None:
        script = (
            "import json,sys\n"
            "sys.path[:0]=[%r,%r]\n"
            "from codex_autopilot.plan import validate_plan\n"
            "from codex_autopilot.plan_issues import PlanIssues\n"
            "from test_plan_evolution import graph, task\n"
            "raw=graph([task('A'),task('B'),task('C')])\n"
            "raw['zeta']=1; raw['alpha']=2\n"
            "for t in raw['tasks']: t.pop('title'); t['bogus']=1; t['other']=2\n"
            "try:\n"
            "    validate_plan(raw,'adaptive')\n"
            "except PlanIssues as exc:\n"
            "    print(json.dumps([i.message for i in exc.issues]))\n"
        ) % (str(ROOT / "src"), str(ROOT / "tests"))
        runs = []
        for seed in ("1", "2", "3"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            done = subprocess.run(
                [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
            )
            runs.append(done.stdout)
        self.assertTrue(runs[0].strip())
        self.assertEqual(len(json.loads(runs[0])), 7)
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])


class TheStateIsCheckedBeforeTheVerifierTests(_Replanning):
    def two_tasks(self):
        return self.at_the_replanner(graph([task("A"), task("B")], max_workers=1))

    def test_removing_an_existing_task_is_refused_before_the_plan_verifier(self) -> None:
        cfg, store, replanner = self.two_tasks()
        bad = self.valid_candidate(cfg)
        bad["tasks"] = [item for item in bad["tasks"] if item["id"] != "B"]

        outcome = self.answer(cfg, store, replanner, reply("PC1", 1, bad))

        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_REJECTED")
        self.assertNotIn("plan_verifier", [item.kind for item in outcome.descriptors])
        issues = active_plan_change(store.load(), request_id="PC1")["rejections"][-1]["issues"]
        self.assertIn("cannot remove tasks with durable history: ['B']", issues[-1]["message"])

    def _with_b(self, state_value: str):
        cfg, store, replanner = self.two_tasks()
        state = store.load()
        state.task_states["B"] = state_value
        store.save(state)
        candidate = self.valid_candidate(cfg)
        next(item for item in candidate["tasks"] if item["id"] == "B")["objective"] = "Rewritten."
        return cfg, store, self.answer(cfg, store, replanner, reply("PC1", 1, candidate))

    def test_a_verified_task_rewritten_is_refused_at_once(self) -> None:
        _cfg, store, outcome = self._with_b("VERIFIED")
        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_REJECTED")
        issues = active_plan_change(store.load(), request_id="PC1")["rejections"][-1]["issues"]
        self.assertEqual(issues[-1]["message"], "verified task B is immutable during plan evolution")

    def test_a_cancelled_task_rewritten_is_refused_at_once(self) -> None:
        """CANCELLED is absorbing: a rewrite of it can never be committed."""

        _cfg, store, outcome = self._with_b("CANCELLED")
        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_REJECTED")
        self.assertNotIn("plan_verifier", [item.kind for item in outcome.descriptors])
        issues = active_plan_change(store.load(), request_id="PC1")["rejections"][-1]["issues"]
        self.assertEqual(issues[-1]["stage"], "state")
        self.assertEqual(issues[-1]["message"], "cancelled task B is immutable during plan evolution")

    def test_an_advanced_task_rewritten_is_left_to_the_commit(self) -> None:
        cfg, store, outcome = self._with_b("IMPLEMENTED")
        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_PROPOSED")
        verifier = outcome.descriptors[0]
        self.assertEqual(verifier.kind, "plan_verifier")

        # By the commit B is still advanced: the verifier's PASS cannot be
        # applied. It used to raise out of the dispatcher.
        self.mark_active(store, verifier.reservation_token, "plan-verifier-PC1")
        passed = complete_desktop_worker(
            cfg, thread_id="plan-verifier-PC1", turn_id="turn-pv",
            final_message=PLAN_VERIFICATION_PREFIX + ' {"verdict":"PASS","issues":[]}',
            hook_gate=lambda _cfg: None,
        )

        self.assertEqual(passed.worker_status, "PLAN_REVISION_REQUIRED")
        self.assertEqual([item.kind for item in passed.descriptors], ["replanner"])
        self.assertEqual(load_plan(cfg.state_dir, cfg.profile).graph_version, 1)
        rejection = active_plan_change(store.load(), request_id="PC1")["rejections"][-1]
        self.assertEqual(rejection["issues"][0]["stage"], "reconcile")
        self.assertIn("advanced task B cannot be rewritten", rejection["issues"][0]["message"])
        self.assertIn("advanced task B cannot be rewritten", passed.descriptors[0].prompt)

        # The run's journal says what happened: the session and its
        # turn_completed event carry the outcome, not the verdict, which
        # stays in plan_verification_result.
        state = store.load()
        session = next(
            item for item in state.worker_sessions
            if item.get("reservation_token") == verifier.reservation_token
        )
        self.assertEqual(session["final_status"], "PLAN_REVISION_REQUIRED")
        self.assertEqual(session["plan_verification_result"]["verdict"], "PASS")
        self.assertIn("advanced task B cannot be rewritten", session["plan_commit_conflict"])
        completed = [
            item for item in state.lifecycle_journal
            if item.get("event") == "turn_completed"
            and item.get("reservation_token") == verifier.reservation_token
        ]
        self.assertEqual([item.get("detail") for item in completed], ["PLAN_REVISION_REQUIRED"])
        # Project Memory keeps the verifier's PASS - its word - and beside it
        # the runtime's note that the graph was not committed.
        from codex_autopilot.memory import ProjectMemory

        notes = [
            item for item in ProjectMemory(cfg.root).milestone_evidence("PLAN-v2", limit=100)
            if item.get("role") == "plan-commit"
        ]
        self.assertEqual(len(notes), 1)
        self.assertIn("did not commit graph v2", notes[0]["summary"])
        self.assertIn("verifier turn turn-pv", notes[0]["summary"])
        self.assertIn("advanced task B cannot be rewritten", notes[0]["summary"])
        # A dispatcher replay of the same turn writes no second note.
        from types import SimpleNamespace

        from codex_autopilot.plan_verification_lifecycle import _record_uncommitted_pass

        _record_uncommitted_pass(
            ProjectMemory(cfg.root), SimpleNamespace(graph_version=2), "turn-pv", "sha", "again"
        )
        self.assertEqual(
            len([
                item for item in ProjectMemory(cfg.root).milestone_evidence("PLAN-v2", limit=100)
                if item.get("role") == "plan-commit"
            ]),
            1,
        )


class ARuntimeConflictSpendsNoAttemptTests(_Replanning):
    def test_a_worker_still_active_at_the_commit_spends_no_semantic_revision(self) -> None:
        """The run's own state refused the commit, not the graph.

        Every reconcile conflict used to count as a semantic revision, so a
        worker still active, a lock still held or a graph that moved could
        burn the budget down to PLAN_VERIFICATION_REJECTED. Here the budget
        has one revision left, and a runtime conflict must not take it.
        """

        from codex_autopilot.plan_verification_lifecycle import MAX_SEMANTIC_PLAN_REVISIONS

        cfg, store, replanner = self.at_the_replanner(graph([task("A"), task("B")], max_workers=1))
        outcome = self.answer(cfg, store, replanner, reply("PC1", 1, self.valid_candidate(cfg)))
        verifier = outcome.descriptors[0]
        self.assertEqual(verifier.kind, "plan_verifier")
        state = store.load()
        change = active_plan_change(state, request_id="PC1")
        change["plan_verification_history"] = [
            {"at": "2026-09-24T00:00:00Z", "verdict": "REVISE"}
        ] * MAX_SEMANTIC_PLAN_REVISIONS
        # A worker reserved beside the verifier - which the drain gate is
        # there to prevent; the run's state is what is wrong, not the graph.
        state.max_parallel_workers = 2
        state.task_states["B"] = "RUNNING"
        state.active_task_ids = [*state.active_task_ids, "B"]
        store.save(state)

        self.mark_active(store, verifier.reservation_token, "plan-verifier-PC1")
        passed = complete_desktop_worker(
            cfg, thread_id="plan-verifier-PC1", turn_id="turn-pv",
            final_message=PLAN_VERIFICATION_PREFIX + ' {"verdict":"PASS","issues":[]}',
            hook_gate=lambda _cfg: None,
        )

        change = active_plan_change(store.load(), request_id="PC1")
        self.assertEqual(change["status"], "DRAINING")
        self.assertEqual(change.get("rejections") or [], [])
        self.assertEqual(change["plan_verification_history"][-1]["verdict"], "RUNTIME_CONFLICT")
        self.assertNotIn("proposed_plan", change)
        self.assertEqual(load_plan(cfg.state_dir, cfg.profile).graph_version, 1)
        self.assertEqual(passed.worker_status, "PLAN_REVISION_REQUIRED")

    def test_the_same_runtime_conflict_twice_calls_the_on_call(self) -> None:
        """Uncounted is not unbounded: the run's state that did not clear goes to the on-call.

        A fresh replanner cannot fix a worker that stays active beside the
        drain gate; left uncounted with no bound, the change would cycle
        replanner and verifier turns for ever with nobody told.
        """

        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        cfg, store, replanner = self.at_the_replanner(graph([task("A"), task("B")], max_workers=1))
        verifier = self.answer(cfg, store, replanner, reply("PC1", 1, self.valid_candidate(cfg))).descriptors[0]
        state = store.load()
        change = active_plan_change(state, request_id="PC1")
        change["plan_verification_history"] = [
            {"at": "2026-09-24T00:00:00Z", "verdict": "RUNTIME_CONFLICT"}
        ]
        state.max_parallel_workers = 2
        state.task_states["B"] = "RUNNING"
        state.active_task_ids = [*state.active_task_ids, "B"]
        store.save(state)

        self.mark_active(store, verifier.reservation_token, "plan-verifier-PC1")
        complete_desktop_worker(
            cfg, thread_id="plan-verifier-PC1", turn_id="turn-pv",
            final_message=PLAN_VERIFICATION_PREFIX + ' {"verdict":"PASS","issues":[]}',
            hook_gate=lambda _cfg: None,
        )

        state = store.load()
        self.assertIsNone(state.active_plan_change_id)
        ticket = next(
            item for item in PipelineIncidentStore(cfg.state_dir).load()["incidents"]
            if item["system_state"].get("stop_kind") == "plan_verification_rejected"
        )
        self.assertEqual(ticket["affected_task_ids"], ["A"])
        self.assertIn("drained workers", state.last_error)
        self.assertEqual(state.status, "RUNNING")


class TheProtocolLineIsPartOfTheRoundTests(_Replanning):
    def test_a_malformed_line_is_a_refusal_with_its_reasons(self) -> None:
        cfg, store, replanner = self.at_the_replanner()
        line = PLAN_CHANGE_RESULT_PREFIX + " " + json.dumps(
            {"request_id": "PC9", "base_graph_version": 1, "plan": self.valid_candidate(cfg), "note": "x"}
        )
        outcome = self.answer(cfg, store, replanner, line)

        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_REJECTED")
        issues = active_plan_change(store.load(), request_id="PC1")["rejections"][-1]["issues"]
        self.assertEqual([item["stage"] for item in issues], ["protocol", "protocol"], issues)
        self.assertEqual(issues[0]["accepted"], ["request_id", "base_graph_version", "plan"])
        self.assertIn("request_id must be 'PC1'", issues[1]["message"])
        self.assertIn("request_id must be 'PC1'", outcome.descriptors[0].prompt)

    def test_a_graph_that_moved_spends_no_attempt(self) -> None:
        cfg, store, replanner = self.at_the_replanner()
        state = store.load()
        active_plan_change(state, request_id="PC1")["base_graph_version"] = 7
        store.save(state)

        outcome = self.answer(cfg, store, replanner, reply("PC1", 7, self.valid_candidate(cfg)))

        change = active_plan_change(store.load(), request_id="PC1")
        self.assertEqual(change["rejections"], [])
        self.assertEqual(change["base_graph_version"], 1)
        self.assertEqual([item.kind for item in outcome.descriptors], ["replanner"])


class TheNextPromptTests(_Replanning):
    def test_a_repeated_issue_is_marked_and_every_attempt_is_listed(self) -> None:
        cfg, store, replanner = self.at_the_replanner()
        first = self.valid_candidate(cfg)
        first["nonsense_field"] = 1
        first["tasks"][0]["title"] = ""
        outcome = self.answer(cfg, store, replanner, reply("PC1", 1, first))
        second = self.valid_candidate(cfg)
        second["nonsense_field"] = 1
        outcome = self.answer(cfg, store, outcome.descriptors[0], reply("PC1", 1, second), "replanner-2")

        prompt = outcome.descriptors[0].prompt
        context = json.loads(prompt.split("AUTOPILOT_CONTEXT: ", 1)[1].split("\n", 1)[0])
        self.assertEqual([item["attempt"] for item in context["rejected_attempts"]], [1, 2])
        self.assertIn("title", context["rejected_attempts"][0]["issues"][1]["message"])
        self.assertIn("[repeated from attempt 1]", prompt)
        self.assertEqual(context["constraints"]["allowed_fields"], {k: list(v) for k, v in ALLOWED_FIELDS.items()})
        self.assertIn("independent", context["constraints"]["allowed_values"]["plan.tasks[].verification.policy"])

    def test_the_plan_verifiers_issues_reach_the_replanner_one_by_one(self) -> None:
        cfg, store, replanner = self.at_the_replanner()
        verifier = self.answer(cfg, store, replanner, reply("PC1", 1, self.valid_candidate(cfg))).descriptors[0]
        self.mark_active(store, verifier.reservation_token, "plan-verifier-PC1")
        verdict = {"verdict": "REVISE", "issues": [
            {"category": "necessity", "summary": "P is not needed.", "task_ids": ["P"], "outcome_ids": []},
            {"category": "dod_sufficiency", "summary": "A's DoD proves nothing.", "task_ids": ["A"],
             "outcome_ids": []},
        ]}
        outcome = complete_desktop_worker(
            cfg, thread_id="plan-verifier-PC1", turn_id="turn-pv",
            final_message=PLAN_VERIFICATION_PREFIX + " " + json.dumps(verdict), hook_gate=lambda _cfg: None,
        )
        prompt = outcome.descriptors[0].prompt
        self.assertIn("rejected for 2 reasons", prompt)
        # The ids live only in the issue's path; the numbered line names them.
        self.assertIn("\n1. P: necessity: P is not needed.", prompt)
        self.assertIn("\n2. A: dod_sufficiency: A's DoD proves nothing.", prompt)


class TheExhaustedBudgetIsTheOnCallsTests(_Replanning):
    """Three refusals hold the requester only, and the on-call raises a new round."""

    def exhaust(self, raw_graph=None):
        cfg, store, replanner = self.at_the_replanner(raw_graph)
        bad = self.valid_candidate(cfg)
        bad["nonsense_field"] = 1
        for attempt in range(3):
            outcome = self.answer(cfg, store, replanner, reply("PC1", 1, bad), f"replanner-{attempt}")
            replanner = outcome.descriptors[0]
        return cfg, store, outcome

    def test_the_run_goes_on_beside_the_held_requester(self) -> None:
        _cfg, store, outcome = self.exhaust(graph([task("A"), task("B")], max_workers=1))
        self.assertEqual(
            sorted((item.kind, item.task_id) for item in outcome.descriptors),
            [("implementation", "B"), ("pipeline_engineer", "A")],
        )
        state = store.load()
        self.assertNotEqual(state.status, "BLOCKED")
        self.assertEqual(state.task_states["B"], "RUNNING")

    def test_the_on_call_raises_a_fresh_round_that_knows_the_refusals(self) -> None:
        from _appserver_fakes import activate_via_app_server
        from codex_autopilot.engineer_stop_actions import request_plan_change

        cfg, store, outcome = self.exhaust()
        engineer = outcome.descriptors[0]
        self.assertEqual(engineer.kind, "pipeline_engineer")
        activate_via_app_server(cfg, self.root, engineer, "eng-1")
        incident_id = next(
            item["incident_id"] for item in store.load().worker_sessions
            if item.get("reservation_token") == engineer.reservation_token
        )

        request_plan_change(
            cfg, incident_id=incident_id, task_id="A", reason="another round, with the history",
            thread_id="eng-1",
        )

        change = active_plan_change(store.load())
        self.assertEqual(change["id"], "PC2")
        self.assertEqual(len(change["inherited_rejections"]), 3)
        self.assertEqual(change.get("rejections") or [], [])
        replanner = next(
            item for item in reserve_ready_frontier(cfg, relay_owner_thread_id="owner", hook_gate=lambda _cfg: None)
            if item.kind == "replanner"
        )
        context = json.loads(replanner.prompt.split("AUTOPILOT_CONTEXT: ", 1)[1].split("\n", 1)[0])
        self.assertEqual([item.get("from_plan_change") for item in context["rejected_attempts"]], ["PC1"] * 3)
        self.assertIn("nonsense_field", replanner.prompt.split("The previous attempt", 1)[1])
        # Its own budget: one refusal of PC2 does not stop it.
        bad = self.valid_candidate(cfg)
        bad["nonsense_field"] = 1
        again = self.answer(cfg, store, replanner, reply("PC2", 1, bad), "replanner-PC2")
        self.assertEqual([item.kind for item in again.descriptors], ["replanner"])
        self.assertIn("[repeated from attempt 3]", again.descriptors[0].prompt)


    def test_her_replan_answer_carries_the_refusals_too(self) -> None:
        from codex_autopilot.blocked_runs import escalate_to_owner
        from codex_autopilot.owner_answers import answer_task
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        cfg, store, _outcome = self.exhaust()
        ticket = next(
            item for item in PipelineIncidentStore(cfg.state_dir).load()["incidents"]
            if item["system_state"].get("stop_kind") == "plan_change_rejected"
        )
        escalate_to_owner(
            cfg, ticket["incident_id"], code="PRODUCT_DECISION", detail="d", at="2026-09-24T10:00:00+00:00",
            escalation={"diagnosis": "d", "recommendation": "r",
                        "options": [{"code": "replan", "means": "split A"}], "scope": "task"},
        )

        answer_task(cfg, "A", "split A in two", option="replan", raise_run=False)

        change = active_plan_change(store.load())
        self.assertTrue(change.get("requested_by_owner"))
        self.assertEqual(len(change["inherited_rejections"]), 3)


class OneSourceOfFieldsTests(unittest.TestCase):
    setUp = PlanEvolutionTests.setUp
    tearDown = PlanEvolutionTests.tearDown

    def plan(self) -> dict:
        raw = graph([task("A")])
        a = raw["tasks"][0]
        a["outputs"] = [{"id": "o", "description": "An output."}]
        a["context"] = {"memory_queries": []}
        return raw

    def objects(self, raw: dict) -> dict[str, dict]:
        a = raw["tasks"][0]
        raw.setdefault("departments", [])
        return {
            "plan": raw,
            "plan.roles[]": raw["roles"][0],
            "plan.tasks[]": a,
            "plan.tasks[].verification": a["verification"],
            "plan.tasks[].verification.deterministic_checks[]": a["verification"]["deterministic_checks"][0],
            "plan.tasks[].resources[]": a["resources"][0],
            "plan.tasks[].outputs[]": a["outputs"][0],
            "plan.tasks[].context": a["context"],
        }

    def issues_for(self, raw: dict) -> list:
        try:
            validate_plan(raw, "adaptive")
        except PlanIssues as exc:
            return list(exc.issues)
        except ValueError as exc:
            return [type("I", (), {"message": str(exc), "accepted": getattr(exc, "accepted", ())})()]
        return []

    def test_every_object_names_its_accepted_fields_and_they_are_the_single_source(self) -> None:
        validate_plan(self.plan(), "adaptive")  # the fixture itself is valid
        for path, obj in self.objects(self.plan()).items():
            with self.subTest(path):
                raw = self.plan()
                target = self.objects(raw)[path]
                target["bogus"] = 1
                unknown = [item for item in self.issues_for(raw) if "unknown fields: ['bogus']" in item.message]
                self.assertEqual(len(unknown), 1)
                self.assertIn(f"accepted fields are {sorted(ALLOWED_FIELDS[path])}", unknown[0].message)
                self.assertEqual(tuple(unknown[0].accepted), tuple(ALLOWED_FIELDS[path]))

    def test_every_key_but_the_departments_is_driven(self) -> None:
        # A key of ALLOWED_FIELDS left out of objects() is a set nothing
        # holds against the parser - plan.compatibility was, until the
        # independent review. The department keys are the R30 line's to
        # rework, and department_acceptance checks them by the same tuples.
        # plan.compatibility has its own driver below: a submitted plan may
        # not declare it at all, only a migrated run's persisted plan has it.
        skipped = set(ALLOWED_FIELDS) - set(self.objects(self.plan())) - {"plan.compatibility"}
        self.assertTrue(all(path.startswith("plan.departments") for path in skipped), skipped)

    def test_every_listed_field_is_accepted_by_the_parser(self) -> None:
        for path in self.objects(self.plan()):
            for name in ALLOWED_FIELDS[path]:
                with self.subTest(path=path, field=name):
                    raw = self.plan()
                    target = self.objects(raw)[path]
                    target.setdefault(name, None)
                    self.assertFalse(
                        [item for item in self.issues_for(raw) if "unknown fields" in item.message]
                    )


class TheCompatibilitySetIsHeldTests(unittest.TestCase):
    """plan.compatibility against COMPATIBILITY_FIELDS, both ways.

    Only a migrated v0.8 run's persisted plan carries it: ``validate_plan``
    and ``validate_migrating_plan`` refuse any declared compatibility, a plan
    change must repeat the current one exactly, and ``load_plan`` takes it
    only in the exact legacy form. So the set is held where it is written and
    read (the persisted round trip) and where ``plan_admission._compatibility``
    judges it, with the arguments ``validate_persisted_plan`` passes.
    """

    def legacy(self) -> dict:
        from test_acceptance_floor_integrity import _historical_legacy_acceptance, _legacy_graph, _task

        return _legacy_graph([_task("A", verification=_historical_legacy_acceptance())])

    def admit(self, raw: dict) -> list:
        from codex_autopilot.plan import _validate_plan_payload

        try:
            _validate_plan_payload(
                raw, "adaptive", inherited=None, migrated_milestone_ids=frozenset({"A"}),
                require_goal_contract=True, require_acceptance_class=True,
            )
        except PlanIssues as exc:
            return list(exc.issues)
        except ValueError as exc:
            return [type("I", (), {"message": str(exc), "accepted": getattr(exc, "accepted", ())})()]
        return []

    def test_a_migrated_plan_writes_and_reads_exactly_the_listed_fields(self) -> None:
        from codex_autopilot.plan import plan_to_dict
        from test_acceptance_floor_integrity import _load_persisted_legacy

        loaded = _load_persisted_legacy(self.legacy())
        self.assertTrue(loaded.legacy_serial)
        self.assertEqual(sorted(plan_to_dict(loaded)["compatibility"]), sorted(COMPATIBILITY_FIELDS))
        self.assertEqual(ALLOWED_FIELDS["plan.compatibility"], COMPATIBILITY_FIELDS)

    def test_every_listed_field_is_admitted_and_anything_else_names_them(self) -> None:
        self.assertEqual(self.admit(self.legacy()), [])
        raw = self.legacy()
        raw["compatibility"]["bogus"] = 1
        unknown = [item for item in self.admit(raw) if "unknown fields: ['bogus']" in item.message]
        self.assertEqual(len(unknown), 1)
        self.assertIn(f"accepted fields are {sorted(COMPATIBILITY_FIELDS)}", unknown[0].message)
        self.assertEqual(tuple(unknown[0].accepted), COMPATIBILITY_FIELDS)


class RefusalsNameWhatIsAcceptedTests(unittest.TestCase):
    def test_a_department_names_the_unknown_and_the_missing_in_one_line(self) -> None:
        from codex_autopilot.department_acceptance import DepartmentAcceptanceError, department_contract_from_raw

        department, wrong, dropped = misspelled_department()
        with self.assertRaises(DepartmentAcceptanceError) as caught:
            department_contract_from_raw(department, "department 1")
        self.assertIn(f"unknown fields: [{wrong!r}]", str(caught.exception))
        self.assertIn(f"missing required fields: [{dropped!r}]", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
