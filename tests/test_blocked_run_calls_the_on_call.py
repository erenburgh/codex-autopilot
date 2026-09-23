"""Every stop opens a ticket, and every ticket reaches the on-call first.

A run stops in several different places: a task exhausts its hiring ladder,
a worker stops itself, a replanner uses every attempt, a verifier is
unreadable three times, plan verification is refused. Each was written
separately and stopped separately, and only the engineer's own escalation
told anybody. On a real run (23 Sep 2026) 75 minutes ended at BLOCKED with no
incident file on disk at all.

0.13.0 gave every stop a ticket but kept one exception: a stop of one task
while its neighbours worked was filed and NOT routed, so the engineer - who
then came instead of work - would not freeze them. Its ticket was to be
routed when the run went idle, by a branch that ran after the reservation;
the door itself set BLOCKED, and the wake-up skipped BLOCKED. Nobody came.
The owner's requirement allows no exception: any stop calls the on-call.

Opening a ticket does not move the decision. PRODUCTION stays outside the
engineer's remit, because accepting work is the owner's call. What changes is
that someone looks, and the owner is told what is hers with a diagnosis.
"""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import types
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from codex_autopilot.blocked_runs import escalate_to_owner, stop_run
from codex_autopilot.engineer_authority import (
    INFRASTRUCTURE_INCIDENT_CLASSES,
    IncidentClass,
)
from codex_autopilot.pipeline_engineer import IncidentPhase, PipelineIncidentStore
from codex_autopilot.run_state import RunState

SRC = Path(__file__).resolve().parents[1] / "src" / "codex_autopilot"

# Every kind of stop, the phase it names, and the tasks it holds.
STOPS = (
    ("ladder_exhausted", "BLOCKED", "M01 exhausted the hiring ladder", ("M01",)),
    ("worker_blocked", "BLOCKED", "M01 implementation BLOCKED MISSING_RESOURCE", ("M01",)),
    ("plan_change_rejected", "PLAN_CHANGE_REJECTED", "the replanner used every attempt", ("M01",)),
    ("verification_protocol", "VERIFICATION_PROTOCOL_BLOCKED", "unreadable verdict three times", ("M01",)),
    ("plan_verification_protocol", "PLAN_VERIFICATION_PROTOCOL_REJECTED", "the plan verifier was unreadable", ("M01",)),
    ("plan_verification_rejected", "PLAN_VERIFICATION_REJECTED", "the plan verifier refused the plan", ("M01",)),
    ("verifier_routing", "VERIFIER_ROUTING_BLOCKED", "no verifier can be routed", ("M01",)),
    ("no_successor", "PIPELINE_ENGINEER_NO_SUCCESSOR", "nobody could be assigned", ()),
    ("orphan_block", "ORPHAN_BLOCK", "M01 is BLOCKED and no open ticket holds it", ("M01",)),
)


class EveryStopIsFiledTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.cfg = types.SimpleNamespace(state_dir=self.state_dir)

    def _state(self) -> RunState:
        return RunState(run_id="run-1", status="RUNNING", phase="DESKTOP_WORKERS_ACTIVE")

    def _stop(self, stop_kind, phase, reason, task_ids, state=None, **extra):
        state = state or self._state()
        incident_id = stop_run(
            self.cfg,
            state,
            stop_kind=stop_kind,
            phase=phase,
            reason=reason,
            summary=f"summary for {phase}.",
            at="2026-09-23T15:02:27+00:00",
            task_ids=task_ids,
            **extra,
        )
        return state, incident_id

    def _incidents(self):
        return list(PipelineIncidentStore(self.state_dir).load().get("incidents") or ())

    def _resolve(self, incident_id: str) -> None:
        PipelineIncidentStore(self.state_dir).resolve_escalation_by_user(
            incident_id, at="2026-09-23T15:10:00+00:00", note="answered"
        )

    def test_each_stop_opens_a_ticket_in_the_on_call_lane(self) -> None:
        """No stop is filed and left: a ticket in DEGRADED is read by nobody."""

        for stop in STOPS:
            with self.subTest(stop_kind=stop[0]):
                self.setUp()
                _state, incident_id = self._stop(*stop)
                self.assertIsNotNone(incident_id, f"{stop[0]} filed nothing")
                incident = self._incidents()[0]
                self.assertEqual(incident["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
                self.assertEqual(incident["system_state"]["stop_kind"], stop[0])

    def test_the_door_has_no_switch_to_leave_a_ticket_unrouted(self) -> None:
        self.assertNotIn("route", inspect.signature(stop_run).parameters)

    def test_it_is_infrastructure_never_product_quality(self) -> None:
        for stop in STOPS:
            with self.subTest(stop_kind=stop[0]):
                self.setUp()
                self._stop(*stop)
                incident = self._incidents()[0]
                self.assertEqual(incident["classification"], IncidentClass.RUNTIME.value)
                self.assertIn(IncidentClass.RUNTIME, INFRASTRUCTURE_INCIDENT_CLASSES)

    def test_the_door_does_not_decide_the_run_status(self) -> None:
        """It used to write BLOCKED before anyone had looked.

        The run's status is derived in run_status after the reservation;
        the door only records the reason where the status command reads it.
        """

        state, _ = self._stop(*STOPS[0])
        self.assertEqual((state.status, state.phase), ("RUNNING", "DESKTOP_WORKERS_ACTIVE"))
        self.assertIn("hiring ladder", state.last_error)
        self.assertEqual(state.resilience_journal[-1]["event"], "run_stop_filed")

    def test_no_runbook_matches_so_nothing_is_retried(self) -> None:
        """A run that stopped needs someone to look, not another attempt."""

        self._stop(*STOPS[2])
        incident = self._incidents()[0]
        self.assertIsNone(incident["runbook_id"])
        self.assertEqual(incident["recovery_attempts"], 0)

    def test_a_learned_runbook_never_stands_between_a_stop_and_the_on_call(self) -> None:
        """Three identical repairs promote a runbook for the signature.

        For a stop that meant the ticket stayed in DEGRADED - "requires an
        exhausted or unavailable auto-recovery" - and the refusal was
        swallowed: silent again.
        """

        with mock.patch(
            "codex_autopilot.pipeline_engineer._promoted_runbook",
            return_value={"id": "learned-1", "healthcheck": None},
        ):
            self._stop(*STOPS[0])
        incident = self._incidents()[0]
        self.assertEqual(incident["runbook_id"], "learned-1")
        self.assertEqual(incident["phase"], IncidentPhase.PIPELINE_ENGINEER.value)

    def test_stopping_twice_while_the_ticket_is_open_keeps_one_ticket(self) -> None:
        state = self._state()
        self._stop(*STOPS[0], state=state)
        self._stop(*STOPS[0], state=state)
        self.assertEqual(len(self._incidents()), 1)

    def test_a_stop_after_the_first_was_answered_is_a_new_ticket(self) -> None:
        """The ticket id is a hash of the signal id.

        It was run:phase:task, and the ladder and a worker's BLOCKED share
        phase="BLOCKED". After the owner answered the first stop, the second
        stop of the same task got the old RESOLVED ticket back, the engineer
        was never asked, and the stop was silent.
        """

        _state, first = self._stop(*STOPS[0])
        self._resolve(first)
        _state, second = self._stop(*STOPS[0])
        self.assertNotEqual(first, second)
        incidents = {item["incident_id"]: item for item in self._incidents()}
        self.assertEqual(incidents[first]["phase"], IncidentPhase.RESOLVED.value)
        self.assertEqual(incidents[second]["phase"], IncidentPhase.PIPELINE_ENGINEER.value)

    def test_two_kinds_of_stop_on_one_task_are_two_tickets(self) -> None:
        state = self._state()
        _s, ladder = self._stop(*STOPS[0], state=state)
        _s, worker = self._stop(*STOPS[1], state=state)
        self.assertNotEqual(ladder, worker)

    def test_a_ticket_without_a_task_pauses_nothing(self) -> None:
        """What to hold and where to anchor the engineer are two fields.

        A NO_SUCCESSOR ticket that named the ready tasks paused exactly the
        tasks that should have gone.
        """

        from codex_autopilot.lifecycle_reservations import tasks_paused_by_incidents

        self._stop(*STOPS[7], context_task_id="M02")
        incident = self._incidents()[0]
        self.assertEqual(incident["affected_task_ids"], [])
        self.assertEqual(incident["context_task_id"], "M02")
        plan = types.SimpleNamespace(task_map={"M01": object(), "M02": object()})
        self.assertEqual(tasks_paused_by_incidents(self.cfg, plan), set())

    def test_the_ticket_says_who_looks_first_and_what_is_hers(self) -> None:
        self._stop(*STOPS[0])
        record = str(self._incidents()[0])
        self.assertIn("on-call looks first", record)
        self.assertIn("decision that is yours", record)
        self.assertIn("exhausted the hiring ladder", record)

    def test_a_failure_to_record_is_never_silent(self) -> None:
        with mock.patch(
            "codex_autopilot.pipeline_engineer.PipelineIncidentStore.open_incident",
            side_effect=OSError("disk gone"),
        ):
            state, incident_id = self._stop(*STOPS[0])
        self.assertIsNone(incident_id)
        self.assertIn("hiring ladder", state.last_error)
        last = state.resilience_journal[-1]
        self.assertEqual(last["event"], "run_stop_unfiled")
        self.assertIn("disk gone", last["detail"]["error"])


class TheEngineerHandsOneTicketUpTests(unittest.TestCase):
    """escalate_to_owner moves one ticket; it never stops the run."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.cfg = types.SimpleNamespace(state_dir=self.state_dir)
        self.incident_id = stop_run(
            self.cfg,
            RunState(run_id="run-1"),
            stop_kind="ladder_exhausted",
            phase="BLOCKED",
            reason="M01 exhausted the hiring ladder",
            summary="s.",
            at="2026-09-23T15:02:27+00:00",
            task_ids=("M01",),
        )

    def _incident(self):
        return PipelineIncidentStore(self.state_dir).load()["incidents"][0]

    def test_the_owner_gets_the_ticket_with_what_to_decide_with(self) -> None:
        outcome = escalate_to_owner(
            self.cfg,
            self.incident_id,
            code="PRODUCT_DECISION",
            detail="the refusals are about the work",
            at="2026-09-23T15:03:00+00:00",
            escalation={"diagnosis": "I-1 repeats", "recommendation": "replan M01", "scope": "task"},
        )
        self.assertEqual(outcome, "escalated")
        incident = self._incident()
        self.assertEqual(incident["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        self.assertEqual(incident["escalation"]["recommendation"], "replan M01")
        self.assertFalse(incident["blocks_run"])

    def test_only_the_engineers_own_finding_holds_the_whole_run(self) -> None:
        escalate_to_owner(
            self.cfg,
            self.incident_id,
            code="GLOBAL_CONFIG_CHANGE",
            detail="hook trust revoked",
            at="2026-09-23T15:03:00+00:00",
            escalation={"diagnosis": "trust revoked", "scope": "run"},
        )
        self.assertTrue(self._incident()["blocks_run"])

    def test_a_handover_without_a_closed_list_code_is_refused_but_heard(self) -> None:
        """R13: the code is from the closed list. A refusal is not silence."""

        outcome = escalate_to_owner(
            self.cfg,
            self.incident_id,
            code="NOT_ON_THE_LIST",
            detail="none.",
            at="2026-09-23T15:03:00+00:00",
        )
        self.assertEqual(outcome, "refused")
        self.assertEqual(self._incident()["phase"], IncidentPhase.PIPELINE_ENGINEER.value)

    def test_her_answer_that_came_first_is_not_reopened(self) -> None:
        PipelineIncidentStore(self.state_dir).resolve_escalation_by_user(
            self.incident_id, at="2026-09-23T15:02:50+00:00", note="done"
        )
        outcome = escalate_to_owner(
            self.cfg,
            self.incident_id,
            code="PRODUCT_DECISION",
            detail="late",
            at="2026-09-23T15:03:00+00:00",
        )
        self.assertEqual(outcome, "answered")
        self.assertEqual(self._incident()["phase"], IncidentPhase.RESOLVED.value)


def _mentions_blocked(node: ast.AST) -> bool:
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "BLOCKED"
        and isinstance(node.value, ast.Name)
        and node.value.id == "TaskState"
    ):
        return True
    if isinstance(node, ast.IfExp):
        return _mentions_blocked(node.body) or _mentions_blocked(node.orelse)
    return False


def transitions_into_blocked() -> Counter:
    """Every place in src that moves a task into TaskState.BLOCKED."""

    found: Counter = Counter()
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = []
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(top)
            elif isinstance(top, ast.ClassDef):
                functions.extend(
                    item
                    for item in top.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
        for function in functions:
            for node in ast.walk(function):
                if isinstance(node, ast.Call):
                    hit = any(_mentions_blocked(item) for item in node.args) or any(
                        _mentions_blocked(item.value) for item in node.keywords
                    )
                elif isinstance(node, ast.Assign):
                    hit = _mentions_blocked(node.value)
                else:
                    hit = False
                if hit:
                    found[(path.name, function.name)] += 1
    return found


class NoStopBypassesTheDoorTests(unittest.TestCase):
    """A new stop written by hand would be a silent one again."""

    # Every transition into TaskState.BLOCKED, and why it is allowed. A new
    # one fails this test until it is added here - with its door.
    ALLOWED_TRANSITIONS_INTO_BLOCKED = {
        # a worker's own BLOCKED/ESCALATE (door: worker_blocked) and a
        # worker's plan-change request (the requester waits for its change)
        ("lifecycle_completion.py", "complete_desktop_worker"): 2,
        # the replanner's graph refused; exhausted -> door: plan_change_rejected
        ("lifecycle_completion.py", "_reject_replanner_result"): 1,
        # three unreadable verdicts -> door: verification_protocol
        ("lifecycle_completion.py", "_reject_verifier_result"): 1,
        # the proposed graph waits for plan verification (plan change)
        ("lifecycle_completion.py", "_complete_replanner"): 1,
        # no verifier can be routed -> door: verifier_routing
        ("lifecycle_reservations.py", "_reserve_followup_sessions_in_state"): 1,
        # unreadable plan verdict; exhausted -> door: plan_verification_protocol
        ("plan_verification_lifecycle.py", "reject_plan_verifier_result"): 1,
        # plan refused; exhausted -> door: plan_verification_rejected
        ("plan_verification_lifecycle.py", "complete_plan_verifier"): 1,
        # the top of the hiring ladder -> door: ladder_exhausted
        ("revision_budget.py", "block_on_exhausted_ladder"): 1,
        # v0.8 migrations: the run was already BLOCKED before this runtime;
        # the orphan sweep gives such a task its ticket
        ("run_state.py", "_migrate_v08_payload"): 1,
        ("task_state.py", "migrate_v08_task_states"): 1,
    }

    def test_every_transition_into_blocked_is_on_the_list(self) -> None:
        self.assertEqual(
            dict(transitions_into_blocked()), self.ALLOWED_TRANSITIONS_INTO_BLOCKED
        )

    def test_only_the_derivation_writes_the_runs_blocked_status(self) -> None:
        writers = {
            path.name: path.read_text(encoding="utf-8").count('state.status = "BLOCKED"')
            for path in sorted(SRC.glob("*.py"))
        }
        self.assertEqual(
            {name: count for name, count in writers.items() if count}, {"run_status.py": 1}
        )

    def test_the_door_has_no_routing_switch_in_its_source(self) -> None:
        self.assertNotIn("route=", (SRC / "blocked_runs.py").read_text(encoding="utf-8"))

    def test_the_door_is_used_by_every_module_that_stops(self) -> None:
        for name in (
            "lifecycle_completion.py",
            "plan_verification_lifecycle.py",
            "revision_budget.py",
            "lifecycle_reservations.py",
        ):
            with self.subTest(module=name):
                self.assertIn("stop_run", (SRC / name).read_text(encoding="utf-8"))


class TheDoorEndToEndTests(unittest.TestCase):
    """The door through the real completion and reservation paths."""

    def setUp(self) -> None:
        from _gates import patch_hook_trust_gates
        from _plan_contract import initialize_verified_project as initialize_project
        from codex_autopilot.config import load_config
        from codex_autopilot.run_state import StateStore
        from test_desktop_lifecycle import graph, task

        patch_hook_trust_gates(self)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        raw = graph(max_workers=1)
        raw["tasks"] = [task("A", path="src/a"), task("B", path="src/b")]
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(raw), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)

    def test_a_worker_that_stops_itself_calls_the_on_call_and_frees_its_slot(self) -> None:
        from _appserver_fakes import activate_via_app_server
        from _relay import reserve_ready_frontier
        from codex_autopilot.lifecycle import complete_desktop_worker

        first = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        self.assertEqual([item.task_id for item in first], ["A"])
        activate_via_app_server(self.cfg, self.root, first[0], "worker-A")
        from _handoff import bump_task_checkpoint

        bump_task_checkpoint(self.root, "A", "Stopped: a key is missing.")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message="нужен ключ\nAUTOPILOT_STATUS: BLOCKED MISSING_RESOURCE",
        )
        # The engineer and the neighbour, in one completion.
        self.assertEqual(
            sorted((item.task_id, item.kind) for item in outcome.descriptors),
            [("A", "pipeline_engineer"), ("B", "implementation")],
        )
        ticket = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"][-1]
        self.assertEqual(ticket["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
        self.assertEqual(ticket["affected_task_ids"], ["A"])
        self.assertEqual(ticket["system_state"]["stop_kind"], "worker_blocked")
        self.assertEqual(ticket["system_state"]["reason_code"], "MISSING_RESOURCE")
        state = self.store.load()
        self.assertEqual(state.status, "RUNNING")
        self.assertEqual(state.active_task_ids, ["B"])

    def test_a_blocked_task_nobody_holds_gets_a_ticket_of_its_own(self) -> None:
        """The safety net under the list above: an orphaned stop is found.

        A ticket closed without returning its task (or an old Resume that
        closed the ticket and kept the stop) left a BLOCKED task nobody
        would ever look at.
        """

        from _relay import reserve_ready_frontier
        from codex_autopilot.plan import load_plan
        from codex_autopilot.task_state import TaskState, transition_task

        state = self.store.load()
        plan = load_plan(self.cfg.state_dir, self.cfg.profile)
        state.task_states = transition_task(plan, state.task_states, "A", TaskState.BLOCKED)
        self.store.save(state)
        reserved = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        self.assertEqual(
            sorted((item.task_id, item.kind) for item in reserved),
            [("A", "pipeline_engineer"), ("B", "implementation")],
        )
        ticket = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"][-1]
        self.assertEqual(ticket["system_state"]["stop_kind"], "orphan_block")
        self.assertEqual(ticket["affected_task_ids"], ["A"])

    def test_a_ticket_that_names_no_task_does_not_break_the_reservation(self) -> None:
        """It used to raise "names no task of the current plan" on every pass."""

        from _relay import reserve_ready_frontier

        stop_run(
            self.cfg,
            self.store.load(),
            stop_kind="detached_dispatch",
            phase="DETACHED",
            reason="a dispatch failed and did not know its task",
            summary="s.",
            at="2026-09-23T15:02:27+00:00",
        )
        reserved = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        self.assertEqual(
            sorted((item.task_id, item.kind) for item in reserved),
            [("A", "implementation"), ("A", "pipeline_engineer")],
        )

    def test_state_that_cannot_be_is_a_ticket_not_a_crash(self) -> None:
        """A raise inside the frontier rolled back the completion that called
        it, and stood in front of the engineer's own reservation."""

        from _relay import reserve_ready_frontier

        reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        # A reads as refused by its verifier, and there is no refusal on
        # record to revise against.
        state = self.store.load()
        state.task_states["A"] = "REVISION_REQUIRED"
        state.active_task_ids = []
        self.store.save(state)
        reserved = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        self.assertIn(("A", "pipeline_engineer"), [(item.task_id, item.kind) for item in reserved])
        self.assertNotIn(("A", "revision"), [(item.task_id, item.kind) for item in reserved])
        ticket = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"][-1]
        self.assertEqual(ticket["system_state"]["stop_kind"], "inconsistent_state")
        self.assertEqual(ticket["affected_task_ids"], ["A"])
        self.assertIn("requires revision without structured issues", ticket["summary"])

    def test_an_unverified_plan_still_lets_the_on_call_come(self) -> None:
        """The engineer is reserved above the plan gate - never work."""

        from _relay import reserve_ready_frontier
        from codex_autopilot.plan_verification import PlanVerificationError

        stop_run(
            self.cfg,
            self.store.load(),
            stop_kind="worker_blocked",
            phase="BLOCKED",
            reason="r",
            summary="s.",
            at="2026-09-23T15:02:27+00:00",
            task_ids=("A",),
        )
        state = self.store.load()
        state.plan_verification = None
        self.store.save(state)
        reserved = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        self.assertEqual([item.kind for item in reserved], ["pipeline_engineer"])
        # Without a ticket the gate stands exactly as before.
        state = self.store.load()
        for item in state.worker_sessions:
            item["status"] = "COMPLETED"
        self.store.save(state)
        store = PipelineIncidentStore(self.cfg.state_dir)
        for item in store.load()["incidents"]:
            store.resolve_escalation_by_user(item["incident_id"], at="2026-09-23T15:05:00+00:00")
        with self.assertRaises(PlanVerificationError):
            reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")


if __name__ == "__main__":
    unittest.main()
