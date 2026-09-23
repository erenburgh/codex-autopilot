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

    def _engineer_closes(self, incident_id: str) -> None:
        from codex_autopilot.pipeline_engineer import HealthcheckResult

        PipelineIncidentStore(self.state_dir).complete_pipeline_engineer(
            incident_id,
            success=True,
            actions=("repair_runtime_code",),
            at="2026-09-23T15:05:00+00:00",
            healthcheck=HealthcheckResult(
                name="stop_repaired", passed=True, checks=("ok",), observed_at="2026-09-23T15:05:00+00:00"
            ),
            note="rewired the environment",
        )

    def test_the_same_stop_closed_twice_goes_to_the_owner_the_third_time(self) -> None:
        """R23 lives in the door: every kind of stop is bounded, once.

        The hold of R3 and a NO_SUCCESSOR ticket had no bound at all; the
        orphan sweep and the plan gate each carried a copy.
        """

        for stop in STOPS:
            with self.subTest(stop_kind=stop[0]):
                self.setUp()
                state = self._state()
                phases = []
                for _ in range(3):
                    _state, incident_id = self._stop(*stop, state=state)
                    phases.append(
                        next(i for i in self._incidents() if i["incident_id"] == incident_id)["phase"]
                    )
                    if phases[-1] == IncidentPhase.PIPELINE_ENGINEER.value:
                        self._engineer_closes(incident_id)
                self.assertEqual(
                    phases,
                    [IncidentPhase.PIPELINE_ENGINEER.value] * 2 + [IncidentPhase.ESCALATE_TO_USER.value],
                )
                last = self._incidents()[-1]
                self.assertEqual(last["escalation_reason"], "RECOVERY_EXHAUSTED")
                self.assertEqual(
                    [row["note"] for row in last["escalation"]["repaired"]],
                    ["rewired the environment"] * 2,
                )

    def test_her_answer_to_a_ticket_handed_to_her_starts_the_count_again(self) -> None:
        state = self._state()
        for _ in range(2):
            _state, incident_id = self._stop(*STOPS[1], state=state)
            self._engineer_closes(incident_id)
        _state, exhausted = self._stop(*STOPS[1], state=state)
        self._resolve(exhausted)
        _state, again = self._stop(*STOPS[1], state=state)
        self.assertEqual(self._incidents()[-1]["phase"], IncidentPhase.PIPELINE_ENGINEER.value)

    def test_a_ticket_she_swept_shut_unasked_still_counts(self) -> None:
        """Resume closes the on-call's lane too; that answered no question."""

        state = self._state()
        for _ in range(2):
            _state, incident_id = self._stop(*STOPS[1], state=state)
            self._resolve(incident_id)
        self._stop(*STOPS[1], state=state)
        self.assertEqual(self._incidents()[-1]["phase"], IncidentPhase.ESCALATE_TO_USER.value)

    def test_another_reason_code_is_another_signature(self) -> None:
        state = self._state()
        for code in ("MISSING_RESOURCE", "ENVIRONMENT_FAILURE", "MISSING_RESOURCE"):
            _state, incident_id = self._stop(
                "worker_blocked", "BLOCKED", f"M01 {code}", ("M01",), state=state,
                system_state={"reason_code": code},
            )
            self.assertEqual(self._incidents()[-1]["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
            self._engineer_closes(incident_id)


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
    #
    # R3 is read off this list too: an infrastructure stop may not appear
    # here. Three used to - three unreadable verdicts (_reject_verifier_result),
    # a verifier nobody could route (_reserve_followup_sessions_in_state) and
    # a worker's ENVIRONMENT_FAILURE (inside complete_desktop_worker). They
    # now only hold their task by its ticket; BLOCKED waits for the
    # on-call's escalation (stop_holds.block_escalated_tasks).
    ALLOWED_TRANSITIONS_INTO_BLOCKED = {
        # a worker's plan-change request (the requester waits for its change)
        ("lifecycle_completion.py", "complete_desktop_worker"): 1,
        # a worker's own BLOCKED/ESCALATE with a product or policy code
        # (door: worker_blocked); infrastructure codes are only held
        ("stop_holds.py", "stop_worker_task"): 1,
        # the on-call handed a ticket up, or the door found the same stop
        # closed twice and back (R23, stop_repeats): held tasks wait for her
        ("stop_holds.py", "block_escalated_tasks"): 1,
        # the replanner's graph refused; exhausted -> door: plan_change_rejected
        ("lifecycle_completion.py", "_reject_replanner_result"): 1,
        # the proposed graph waits for plan verification (plan change)
        ("lifecycle_completion.py", "_complete_replanner"): 1,
        # unreadable plan verdict; exhausted -> door: plan_verification_protocol
        ("plan_verification_lifecycle.py", "reject_plan_verifier_result"): 1,
        # plan refused; exhausted -> door: plan_verification_rejected
        ("plan_verification_lifecycle.py", "complete_plan_verifier"): 1,
        # the top of the hiring ladder -> door: ladder_exhausted
        ("revision_budget.py", "block_on_exhausted_ladder"): 1,
        # a plan change the on-call asked for on a stopped task's behalf: the
        # requester waits for the replanner, like a worker's own request
        ("engineer_stop_actions.py", "request_plan_change"): 1,
        # a fresh hire whose runtime patch was withdrawn, refused or
        # reverted is revoked: the task goes back to the stop it came from,
        # held by its open stop ticket or by a new one (door: hold_revoked /
        # runtime_patch_refused)
        ("ladder_grants.py", "revoke_grants"): 1,
        # her answer "replan": the same plan-change wait, on her decision
        ("owner_answers.py", "_owner_plan_change"): 1,
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

    def _worker_stops(self, code: str):
        from _appserver_fakes import activate_via_app_server
        from _handoff import bump_task_checkpoint
        from _relay import reserve_ready_frontier
        from codex_autopilot.lifecycle import complete_desktop_worker

        first = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        activate_via_app_server(self.cfg, self.root, first[0], "worker-A")
        bump_task_checkpoint(self.root, "A", f"Stopped: {code}.")
        return complete_desktop_worker(
            self.cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=f"итог\nAUTOPILOT_STATUS: BLOCKED {code}",
        )

    def _paused(self) -> set:
        from codex_autopilot.lifecycle_reservations import tasks_paused_by_incidents
        from codex_autopilot.plan import load_plan

        return tasks_paused_by_incidents(self.cfg, load_plan(self.cfg.state_dir, self.cfg.profile))

    def test_an_environment_stop_holds_the_task_until_the_on_call_hands_it_up(self) -> None:
        """R3: an infrastructure cause is not BLOCKED while DevOps can still look.

        It used to be BLOCKED at once: the engineer repaired the environment,
        closed its ticket, and the task stayed stopped - a closed ticket does
        not lift a stop - until the orphan sweep sent it to the owner.
        """

        from _appserver_fakes import activate_via_app_server
        from codex_autopilot.lifecycle import complete_desktop_worker

        outcome = self._worker_stops("ENVIRONMENT_FAILURE")
        self.assertEqual(
            sorted((item.task_id, item.kind) for item in outcome.descriptors),
            [("A", "pipeline_engineer"), ("B", "implementation")],
        )
        state = self.store.load()
        self.assertEqual(state.task_states["A"], "READY")
        self.assertEqual(self._paused(), {"A"})
        ticket = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"][-1]
        self.assertTrue(ticket["system_state"]["held"])
        # The on-call hands it up: only now is A BLOCKED - hers to decide.
        engineer = next(item for item in outcome.descriptors if item.kind == "pipeline_engineer")
        activate_via_app_server(self.cfg, self.root, engineer, "engineer-1")
        complete_desktop_worker(
            self.cfg,
            thread_id="engineer-1",
            turn_id="engineer-1-turn",
            final_message=self.ENGINEER_HANDS_UP,
        )
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")

    def test_a_held_task_goes_back_to_work_when_its_ticket_closes(self) -> None:
        """The same action runs again - R3's "DevOps returns control"."""

        from _relay import reserve_ready_frontier

        self._worker_stops("MISSING_RESOURCE")
        store = PipelineIncidentStore(self.cfg.state_dir)
        ticket = store.load()["incidents"][-1]
        store.resolve_escalation_by_user(ticket["incident_id"], at="2026-09-23T15:05:00+00:00")
        self.assertEqual(self._paused(), set())
        reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        # Nothing is left stopped: no orphan ticket about A, A waits for a slot.
        self.assertEqual(self.store.load().task_states["A"], "READY")
        self.assertEqual(
            [item["system_state"]["stop_kind"] for item in store.load()["incidents"]],
            ["worker_blocked"],
        )

    def test_a_defect_upstream_or_in_the_contract_is_held_too(self) -> None:
        """R3 names what is hers: PRODUCTION and POLICY. Nothing else blocks.

        61e80a6 listed the infrastructure codes instead and missed two: a
        DEPENDENCY_DEFECT or CONTRADICTORY_CONTRACT went BLOCKED at once,
        although the door files them as RUNTIME and the on-call repairs both
        with a plan change or a runtime repair.
        """

        for code in ("DEPENDENCY_DEFECT", "CONTRADICTORY_CONTRACT", "RECOVERY_EXHAUSTED", "UNSPECIFIED"):
            with self.subTest(code=code):
                self.setUp()
                self._worker_stops(code)
                self.assertEqual(self.store.load().task_states["A"], "READY")
                self.assertEqual(self._paused(), {"A"})
                ticket = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"][-1]
                self.assertTrue(ticket["system_state"]["held"])

    def test_her_decisions_and_her_approvals_block_at_once(self) -> None:
        for code in ("PRODUCT_DECISION", "ARCHITECTURE_DECISION", "DANGEROUS_PERMISSION"):
            with self.subTest(code=code):
                self.setUp()
                self._worker_stops(code)
                self.assertEqual(self.store.load().task_states["A"], "BLOCKED")

    def test_a_product_stop_still_blocks_at_once(self) -> None:
        """PRODUCT and POLICY are hers by R3 - no hold, no wait for DevOps."""

        outcome = self._worker_stops("PRODUCT_DECISION")
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")
        self.assertIn(("A", "pipeline_engineer"), [(item.task_id, item.kind) for item in outcome.descriptors])
        ticket = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"][-1]
        self.assertFalse(ticket["system_state"]["held"])

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

    def test_task_states_that_do_not_fit_the_graph_are_a_stop_not_a_raise(self) -> None:
        """``_prepare_state`` raised inside the frontier, in front of the engineer.

        The same inconsistent-state character as the scheduler race and the
        pending producer; the independent check named it once those had
        become stops. Nothing is scheduled from such a map - only the
        on-call comes, and one ticket holds the graph while it is open.
        """

        from _relay import reserve_ready_frontier

        # A restore, or a plan change applied halfway: the map names a task
        # the graph does not have.
        state = self.store.load()
        state.task_states["Z"] = "READY"
        self.store.save(state)
        for _pass in range(2):
            reserved = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
            self.assertEqual([item.kind for item in reserved], ["pipeline_engineer"] if not _pass else [])
        tickets = [
            item
            for item in PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
            if item["system_state"].get("stop_kind") == "task_states_mismatch"
        ]
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["affected_task_ids"], ["A", "B"])
        self.assertIn("unknown ['Z'], missing []", tickets[0]["summary"])
        self.assertEqual(
            [item["kind"] for item in self.store.load().worker_sessions], ["pipeline_engineer"]
        )

    def test_the_engineer_reads_no_routing_from_a_refused_graph(self) -> None:
        """The invariant held only as a comment, the independent check found.

        Under a refused graph ``_build_descriptor`` still took the model from
        its ``model_strategy`` and the effort from the anchor task's
        ``reasoning`` - the graph's word. The engineer now runs on her own
        Codex settings then, as under ``host-settings``; under a verified
        graph it is routed as before.
        """

        from _relay import reserve_ready_frontier

        stop_run(
            self.cfg,
            self.store.load(),
            stop_kind="worker_blocked",
            phase="BLOCKED",
            reason="r",
            summary="s.",
            at="2026-09-23T15:02:27+00:00",
            task_ids=("A", "B"),
        )
        verified = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")[0]
        self.assertEqual(verified.kind, "pipeline_engineer")
        self.assertIsNotNone(verified.model)
        self.assertEqual(verified.thinking, "medium")
        state = self.store.load()
        for item in state.worker_sessions:
            item["status"] = "COMPLETED"
        self.store.save(state)
        self._unverify()
        refused = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")[0]
        self.assertEqual(refused.kind, "pipeline_engineer")
        self.assertIsNone(refused.model)
        self.assertIsNone(refused.thinking)
        self.assertEqual(refused.cwd, str(self.root))

    ENGINEER_HANDS_UP = (
        'AUTOPILOT_ESCALATION: {"diagnosis":"d","recommendation":"r","scope":"task"}\n'
        "PIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER PRODUCT_DECISION"
    )

    def _unverify(self) -> None:
        state = self.store.load()
        state.plan_verification = None
        self.store.save(state)

    def _plan_gate_tickets(self) -> list[dict]:
        return [
            item
            for item in PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
            if item["system_state"].get("stop_kind") == "plan_unverified"
        ]

    def test_an_unverified_plan_still_lets_the_on_call_come(self) -> None:
        """The engineer is reserved above the plan gate - never work."""

        from _relay import reserve_ready_frontier

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
        self._unverify()
        reserved = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        self.assertEqual([item.kind for item in reserved], ["pipeline_engineer"])
        self.assertEqual(
            [item["kind"] for item in self.store.load().worker_sessions], ["pipeline_engineer"]
        )

    def test_the_on_call_finishes_under_an_unverified_plan(self) -> None:
        """Its completion used to be rolled back by the plan gate.

        Measured by the independent check: the engineer escalated, the
        incident journal moved, and run-state kept the engineer ACTIVE and
        the run RUNNING forever - one engineer per run then kept every later
        one out, and the wake-up saw nothing to raise.
        """

        from _appserver_fakes import activate_via_app_server
        from _relay import reserve_ready_frontier
        from codex_autopilot.lifecycle import complete_desktop_worker

        first = stop_run(
            self.cfg,
            self.store.load(),
            stop_kind="worker_blocked",
            phase="BLOCKED",
            reason="r",
            summary="s.",
            at="2026-09-23T15:02:27+00:00",
            task_ids=("A",),
        )
        self._unverify()
        engineer = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")[0]
        activate_via_app_server(self.cfg, self.root, engineer, "engineer-1")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="engineer-1",
            turn_id="engineer-1-turn",
            final_message=self.ENGINEER_HANDS_UP,
        )
        state = self.store.load()
        session = next(
            item for item in state.worker_sessions
            if item["reservation_token"] == engineer.reservation_token
        )
        self.assertEqual(session["status"], "COMPLETED")
        tickets = {
            item["incident_id"]: item
            for item in PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
        }
        self.assertEqual(tickets[first]["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        # The refusal itself is a ticket, and the next engineer comes for it
        # in the same completion - never a worker built from the graph.
        gate = self._plan_gate_tickets()
        self.assertEqual(len(gate), 1)
        self.assertEqual(gate[0]["affected_task_ids"], ["A", "B"])
        self.assertEqual([item.kind for item in outcome.descriptors], ["pipeline_engineer"])
        successor = next(
            item for item in state.worker_sessions
            if item["reservation_token"] == outcome.descriptors[0].reservation_token
        )
        self.assertEqual(successor["incident_id"], gate[0]["incident_id"])
        self.assertEqual(state.status, "RUNNING")

    def test_a_worker_completion_survives_the_plan_gate(self) -> None:
        """A finished turn is recorded; only what would be built next waits."""

        from _appserver_fakes import activate_via_app_server
        from _handoff import bump_task_checkpoint
        from _relay import reserve_ready_frontier
        from codex_autopilot.lifecycle import complete_desktop_worker

        first = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        activate_via_app_server(self.cfg, self.root, first[0], "worker-A")
        from codex_autopilot.memory import ProjectMemory

        bump_task_checkpoint(self.root, "A", "Done.")
        ProjectMemory(self.root).record_evidence(
            kind="test",
            summary="Evidence for A.",
            created_by="plan-gate-test",
            milestone_id="A",
            role="verification",
            command="verify A",
            result="PASS",
            exit_code=0,
        )
        self._unverify()
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        state = self.store.load()
        self.assertEqual(state.task_states["A"], "IMPLEMENTED")
        self.assertEqual([item.kind for item in outcome.descriptors], ["pipeline_engineer"])
        self.assertEqual(len(self._plan_gate_tickets()), 1)

    def test_the_plan_gate_alone_calls_the_on_call_and_then_the_owner(self) -> None:
        """No ticket at all used to mean a bare raise: a stop nobody heard.

        Now the refusal is its own ticket. A repair that does not take is
        bounded like the orphan sweep: the third ticket goes to the owner,
        and with nothing else to do the run derives BLOCKED.
        """

        from _relay import reserve_ready_frontier
        from codex_autopilot.run_status import _finish_global_state
        from codex_autopilot.plan import load_plan

        self._unverify()
        store = PipelineIncidentStore(self.cfg.state_dir)
        for lap in range(3):
            reserved = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
            gate = self._plan_gate_tickets()
            self.assertEqual(len(gate), lap + 1)
            self.assertIn("PLAN_PROPOSED", gate[-1]["summary"])
            if lap < 2:
                self.assertEqual([item.kind for item in reserved], ["pipeline_engineer"])
                # The on-call closes it without a fix.
                state = self.store.load()
                for item in state.worker_sessions:
                    item["status"] = "COMPLETED"
                self.store.save(state)
                store.resolve_escalation_by_user(gate[-1]["incident_id"], at="2026-09-23T15:05:00+00:00")
        self.assertEqual(gate[-1]["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        self.assertEqual(reserved, ())
        state = self.store.load()
        self.assertNotIn("implementation", [item["kind"] for item in state.worker_sessions])
        _finish_global_state(load_plan(self.cfg.state_dir, self.cfg.profile), state, (), cfg=self.cfg)
        self.assertEqual((state.status, state.phase), ("BLOCKED", "AWAITING_OWNER"))


if __name__ == "__main__":
    unittest.main()
