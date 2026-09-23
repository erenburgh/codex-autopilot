"""The on-call engineer is created as a worker, not left as a label in JSON.

The PIPELINE_ENGINEER phase existed as a field: ensure_pipeline_engineer
changed a string and appended an event, and nothing created the
engineer's thread. A run that hit it stood silently.

R13: DevOps resolves infrastructure bugs on the user's behalf, and the
user takes no part in choosing the fix. So escalation is not a second
equal exit but an exception with a reason code.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from codex_autopilot.lifecycle_base import DesktopLifecycleError, SESSION_KINDS
from codex_autopilot.lifecycle_completion import (
    ESCALATION_CODES,
    parse_pipeline_engineer_status,
)
from codex_autopilot.run_state import WORKER_SESSION_KINDS
from codex_autopilot.thread_titles import pipeline_engineer_thread_title


class SessionKindTests(unittest.TestCase):
    def test_the_engineer_is_a_worker_kind(self) -> None:
        self.assertIn("pipeline_engineer", SESSION_KINDS)
        self.assertIn("pipeline_engineer", WORKER_SESSION_KINDS)


class ThreadTitleTests(unittest.TestCase):
    def test_the_title_is_readable_in_the_sidebar(self) -> None:
        title = pipeline_engineer_thread_title(
            "incident-8ea3ceca87b6c8a3", "Запуск не подтверждён чек-листом"
        )
        self.assertTrue(title.startswith("Pipeline Engineer | INC-"))
        self.assertIn("Запуск не подтверждён", title)

    def test_an_empty_identifier_is_refused(self) -> None:
        from codex_autopilot.thread_titles import ThreadTitleError

        with self.assertRaises(ThreadTitleError):
            pipeline_engineer_thread_title("incident-", "что-то")


class ExitProtocolTests(unittest.TestCase):
    def test_resolved_needs_no_code(self) -> None:
        self.assertEqual(
            parse_pipeline_engineer_status("отчёт\nPIPELINE_ENGINEER_STATUS: RESOLVED"),
            ("RESOLVED", ""),
        )

    def test_escalation_requires_a_code_from_the_closed_list(self) -> None:
        for code in sorted(ESCALATION_CODES):
            with self.subTest(code=code):
                self.assertEqual(
                    parse_pipeline_engineer_status(
                        f"отчёт\nPIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER {code}"
                    ),
                    ("ESCALATE_TO_USER", code),
                )

    def test_a_bare_escalation_is_refused(self) -> None:
        """An escalation without a reason is a way around R13."""

        with self.assertRaises(DesktopLifecycleError):
            parse_pipeline_engineer_status(
                "отчёт\nPIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER"
            )

    def test_an_invented_code_is_refused(self) -> None:
        with self.assertRaises(DesktopLifecycleError):
            parse_pipeline_engineer_status(
                "отчёт\nPIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER BECAUSE_HARD"
            )

    def test_the_status_must_be_the_last_line(self) -> None:
        with self.assertRaises(DesktopLifecycleError):
            parse_pipeline_engineer_status(
                "PIPELINE_ENGINEER_STATUS: RESOLVED\nещё что-то"
            )

    def test_two_statuses_are_refused(self) -> None:
        with self.assertRaises(DesktopLifecycleError):
            parse_pipeline_engineer_status(
                "PIPELINE_ENGINEER_STATUS: RESOLVED\n"
                "PIPELINE_ENGINEER_STATUS: RESOLVED"
            )


class AuthorityTests(unittest.TestCase):
    """The engineer is named real commands, not described a path that
    does not exist."""

    def test_the_prompt_names_commands_that_exist(self) -> None:
        import re
        from pathlib import Path

        from codex_autopilot.cli import parser

        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/ai_studio.py"
        ).read_text(encoding="utf-8")
        block = source[source.index("def build_pipeline_engineer_prompt") :]
        block = block[: block.index("def select_context")]
        named = set(re.findall(r"scripts/codex-autopilot (\S+)", block))
        available: set[str] = set()
        for action in parser()._subparsers._group_actions:
            available.update(action.choices)
        self.assertTrue(named, "the prompt names no command at all")
        self.assertEqual(sorted(named - available), [])

    def test_the_prompt_states_full_repair_authority(self) -> None:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/ai_studio.py"
        ).read_text(encoding="utf-8")
        self.assertIn("full authority to repair", source)
        self.assertIn("The user does not choose the repair", source)


if __name__ == "__main__":
    unittest.main()


class EngineerIsActuallyReservedTests(unittest.TestCase):
    """End-to-end check: an incident in the PIPELINE_ENGINEER phase
    gives a worker.

    This test was missing, and the price was direct: new code referred to
    a name whose import had been dropped earlier as unused, and a set of
    piecewise checks - the title, the status parsing, the prompt text -
    did not see the NameError. Only a live run caught it.
    """

    def setUp(self) -> None:
        import json as _json
        import tempfile

        from _gates import patch_hook_trust_gates
        from _relay import reserve_ready_frontier
        from _plan_contract import initialize_verified_project as initialize_project
        from codex_autopilot.config import load_config
        from codex_autopilot.pipeline_engineer import (
            IncidentClass,
            IncidentSignal,
            PipelineIncidentStore,
            SideEffectOutcome,
        )
        from codex_autopilot.run_state import StateStore, utc_now
        from test_desktop_lifecycle import graph

        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(_json.dumps(graph()), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)
        self.reserve = reserve_ready_frontier
        first = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")[0]
        self.task_id = first.task_id

        incidents = PipelineIncidentStore(self.cfg.state_dir)
        incident = incidents.open_incident(
            IncidentSignal(
                signal_id="probe:launch",
                code="launch_not_confirmed",
                surface=IncidentClass.PIPELINE,
                summary="Запуск не подтверждён чек-листом",
                affected_task_ids=(self.task_id,),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
                system_state={},
            ),
            at=utc_now(),
        )
        self.incident_id = str(incident["incident_id"])
        incidents.route_incident(self.incident_id, at=utc_now())
        incidents.ensure_pipeline_engineer(self.incident_id, at=utc_now())

    def engineer_sessions(self) -> list[dict]:
        return [
            item
            for item in self.store.load().worker_sessions
            if item.get("kind") == "pipeline_engineer"
        ]

    def test_an_open_incident_reserves_an_engineer(self) -> None:
        descriptors = self.reserve(self.cfg, relay_owner_thread_id="owner-2")
        self.assertEqual(len(descriptors), 1)
        payload = descriptors[0].to_dict()
        self.assertEqual(payload["kind"], "pipeline_engineer")
        self.assertTrue(str(payload["title"]).startswith("Pipeline Engineer | INC-"))
        self.assertIn("AUTOPILOT_INCIDENT", payload["prompt"])

    def test_the_engineer_is_reserved_once_per_incident(self) -> None:
        self.reserve(self.cfg, relay_owner_thread_id="owner-2")
        self.reserve(self.cfg, relay_owner_thread_id="owner-3")
        self.assertEqual(len(self.engineer_sessions()), 1)

    def test_the_engineer_holds_no_resource_ownership(self) -> None:
        """The failed session holds the resources; the one who comes to
        repair is not blocked."""

        self.reserve(self.cfg, relay_owner_thread_id="owner-2")
        self.assertIsNone(self.engineer_sessions()[0]["resource_ownership_token"])

    def test_the_engineer_outranks_ordinary_work(self) -> None:
        """A broken pipeline outranks tasks: while a ticket is open
        there is no work."""

        self.reserve(self.cfg, relay_owner_thread_id="owner-2")
        self.assertEqual(self.reserve(self.cfg, relay_owner_thread_id="owner-3"), ())

    def test_the_incident_is_recorded_on_the_session(self) -> None:
        self.reserve(self.cfg, relay_owner_thread_id="owner-2")
        self.assertEqual(self.engineer_sessions()[0]["incident_id"], self.incident_id)


class ServerViewTests(unittest.TestCase):
    """The dispatcher gathers the report on threads, not the engineer.

    Getting it himself, the engineer stepped outside the working directory
    with python and ran into an access request, which the autopilot on
    principle does not answer. Measured: two tickets in a row, each one a
    turn interrupted on that request. The dispatcher already has the
    connection open and needs no permissions.
    """

    def view(self, client, sessions, affected=("M11",)):
        from types import SimpleNamespace

        from codex_autopilot.lifecycle_dispatch import server_view_for_incident

        cfg = SimpleNamespace(desktop=SimpleNamespace(project_id="proj-1"))
        state = SimpleNamespace(worker_sessions=list(sessions))
        return server_view_for_incident(
            client, cfg, state, {"affected_task_ids": list(affected)}
        )

    def test_a_live_thread_is_reported_with_its_project(self) -> None:
        from unittest import mock

        client = mock.MagicMock()
        client.read_thread.return_value = {
            "id": "t1", "name": "Worker", "projectId": "proj-1", "status": {"type": "idle"}
        }
        view = self.view(client, [{"task_id": "M11", "thread_id": "t1", "kind": "implementation", "status": "ACTIVE"}])
        self.assertEqual(view["threads"][0]["exists"], True)
        self.assertEqual(view["threads"][0]["project_id"], "proj-1")
        self.assertEqual(view["gathered_by"], "dispatcher")

    def test_a_refused_read_is_recorded_as_a_fact_not_an_exception(self) -> None:
        from unittest import mock

        client = mock.MagicMock()
        client.read_thread.side_effect = RuntimeError("thread not found: t1")
        view = self.view(client, [{"task_id": "M11", "thread_id": "t1", "kind": "replanner", "status": "PREPARED"}])
        self.assertEqual(view["threads"][0]["exists"], False)
        self.assertIn("not found", view["threads"][0]["server_error"])

    def test_threads_of_other_tasks_are_not_gathered(self) -> None:
        from unittest import mock

        client = mock.MagicMock()
        client.read_thread.return_value = {"id": "t1"}
        view = self.view(client, [{"task_id": "M9", "thread_id": "t9", "kind": "implementation", "status": "COMPLETED"}])
        self.assertEqual(view["threads"], [])

    def test_turns_are_never_requested(self) -> None:
        """Worker transcripts are not the engineer's to read."""

        from unittest import mock

        client = mock.MagicMock()
        client.read_thread.return_value = {"id": "t1"}
        self.view(client, [{"task_id": "M11", "thread_id": "t1", "kind": "implementation", "status": "ACTIVE"}])
        for call in client.read_thread.call_args_list:
            self.assertNotIn("includeTurns", call.kwargs)

    def test_the_prompt_points_at_the_package_instead_of_probing(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/ai_studio.py"
        ).read_text(encoding="utf-8")
        self.assertIn("`server_view` carries the App Server's own record", source)
        self.assertIn("Do not run anything outside the project working directory", source)


class PlanChangeUserRequestTests(unittest.TestCase):
    """The runtime carries user_request over; the replanner does not
    repeat it.

    The prompt demanded it be preserved verbatim. On the live run M11 that
    is 35 234 characters: a model rewriting the graph does not reproduce
    such a string, and ANY lawful plan change was rejected whole with
    "plan changes must not replace the original user request". The
    replanner's turn passed successfully - it was the result that was
    thrown away.

    Carrying it over is stricter than the old check: an echo can be faked,
    but a field that is not read from the answer cannot be changed at all.
    """

    def plan_data(self, **overrides):
        from test_desktop_lifecycle import graph

        data = dict(graph())
        data.update(overrides)
        return data

    def test_a_replanner_may_omit_user_request(self) -> None:
        from codex_autopilot.plan import validate_plan, validate_plan_change

        current = validate_plan(self.plan_data(user_request="и" * 35_000), "adaptive")
        data = self.plan_data(graph_version=current.graph_version + 1)
        data.pop("user_request", None)
        candidate = validate_plan_change(current, data, "adaptive")
        self.assertEqual(candidate.user_request, current.user_request)

    def test_a_returned_user_request_cannot_replace_the_original(self) -> None:
        """The field is not read from the answer, so a substitution is
        impossible."""

        from codex_autopilot.plan import validate_plan, validate_plan_change

        current = validate_plan(self.plan_data(user_request="исходный запрос"), "adaptive")
        data = self.plan_data(
            graph_version=current.graph_version + 1,
            user_request="подменённый запрос",
        )
        candidate = validate_plan_change(current, data, "adaptive")
        self.assertEqual(candidate.user_request, "исходный запрос")

    def test_goal_stays_strict(self) -> None:
        """goal is 542 characters - the model repeats it reliably."""

        from codex_autopilot.plan import validate_plan, validate_plan_change

        current = validate_plan(self.plan_data(), "adaptive")
        data = self.plan_data(graph_version=current.graph_version + 1, goal="другая цель")
        with self.assertRaisesRegex(ValueError, "goal"):
            validate_plan_change(current, data, "adaptive")

    def test_the_prompt_no_longer_demands_the_impossible(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_prompts.py"
        ).read_text(encoding="utf-8")
        self.assertIn("user_request переносит runtime", source)
        self.assertIn("The runtime carries user_request over", source)
        self.assertNotIn("Дословно сохрани user_request", source)
        self.assertNotIn("Preserve user_request verbatim", source)


class ResolvedMustHandOverTests(unittest.TestCase):
    """A repair without a successor is not a completion.

    On a live run the engineer closed the incident, his process exited
    normally, and there was no one left to start the task: RESOLVED
    returned an empty list of successors, the run went to READY/PREPARING
    and stood there silently. By that moment the causal predecessor is
    dead - its death was the incident - so the engineer's own turn serves
    as the causal link.
    """

    def setUp(self) -> None:
        import json as _json
        import tempfile

        from _gates import patch_hook_trust_gates
        from _relay import reserve_ready_frontier
        from _plan_contract import initialize_verified_project as initialize_project
        from codex_autopilot.config import load_config
        from codex_autopilot.pipeline_engineer import (
            IncidentClass,
            IncidentSignal,
            PipelineIncidentStore,
            SideEffectOutcome,
        )
        from codex_autopilot.run_state import StateStore, utc_now
        from test_verification_lifecycle import graph, task

        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        # A graph where B depends on A: exactly one task stays ready,
        # as in the live incident. On a graph with two independent tasks
        # the second takes a scheduler slot, and the check would measure the wrong thing.
        plan_file.write_text(_json.dumps(graph(task("A"))), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)
        self.reserve = reserve_ready_frontier
        first = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")[0]
        self.task_id = first.task_id
        self.failed_token = first.reservation_token

        incidents = PipelineIncidentStore(self.cfg.state_dir)
        incident = incidents.open_incident(
            IncidentSignal(
                signal_id="probe:launch",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                summary="Worker requested approval; the dispatcher never answers",
                affected_task_ids=(self.task_id,),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
                system_state={},
            ),
            at=utc_now(),
        )
        self.incident_id = str(incident["incident_id"])
        incidents.route_incident(self.incident_id, at=utc_now())
        incidents.ensure_pipeline_engineer(self.incident_id, at=utc_now())

    def resolve_and_complete(
        self,
        final_message: str,
        *,
        dispatcher_authorized: bool = False,
        reserve_before_retry: bool = False,
    ):
        import os
        import time

        from _appserver_fakes import activate_via_app_server
        from codex_autopilot.lifecycle_completion import complete_desktop_worker
        from codex_autopilot.lifecycle_failures import record_desktop_failure
        from codex_autopilot.pipeline_engineer import (
            HealthcheckResult,
            PipelineIncidentStore,
            _expected_healthcheck,
        )
        from codex_autopilot.run_state import utc_now

        # The live picture of the incident: the task's session is dead - its
        # death was the incident - and the task returned to READY. The failure
        # is recorded through the real path: it also releases the resource
        # locks and keeps the journal. Editing the state by hand broke the
        # lock-journal reconciliation, and it was right that it did.
        record_desktop_failure(
            self.cfg,
            self.failed_token,
            reason="Worker requested approval; the dispatcher never answers",
            failure_code="app_server_rpc_failed",
            definitive=True,
            reserve_other_ready=False,
        )

        self.future = int(time.time()) + 3_600
        # The engineer is reserved at the moment of the failure, not an hour later:
        # the failed task's retry window is still open at that point.
        reserve_epoch = int(time.time()) if reserve_before_retry else self.future
        descriptor = self.reserve(
            self.cfg, relay_owner_thread_id="owner-2", now_epoch=reserve_epoch
        )[0]
        activate_via_app_server(self.cfg, self.root, descriptor, "engineer-thread")
        incidents = PipelineIncidentStore(self.cfg.state_dir)
        record = next(
            item
            for item in incidents.load()["incidents"]
            if item["incident_id"] == self.incident_id
        )
        incidents.complete_pipeline_engineer(
            self.incident_id,
            success=True,
            actions=("rearm_relay_owner",),
            at=utc_now(),
            healthcheck=HealthcheckResult(
                name=_expected_healthcheck(record) or "causal_predecessor_rearm_ready",
                passed=True,
                checks=("relay armed",),
                observed_at=utc_now(),
            ),
        )
        # By the time the engineer closes the incident, the failed task's
        # retry window has already expired - on the live run M1 stood
        # exactly in READY, not in RETRY_WAIT.
        extra = {}
        if dispatcher_authorized:
            session = next(
                item
                for item in self.store.load().worker_sessions
                if item.get("kind") == "pipeline_engineer"
            )
            session["automatic_dispatch_pid"] = os.getpid()
            session["automatic_dispatch_state"] = "RUNNING"
            state = self.store.load()
            for item in state.worker_sessions:
                if item.get("reservation_token") == session["reservation_token"]:
                    item["automatic_dispatch_pid"] = os.getpid()
                    item["automatic_dispatch_state"] = "RUNNING"
            self.store.save(state)
            extra = {
                "dispatcher_reservation_token": session["reservation_token"],
                "dispatcher_pid": os.getpid(),
            }
        return complete_desktop_worker(
            self.cfg,
            thread_id="engineer-thread",
            turn_id="engineer-turn",
            final_message=final_message,
            now_epoch=self.future,
            **extra,
        )

    def test_a_retry_due_while_the_engineer_worked_is_picked_up(self) -> None:
        """The retry fell due while the engineer worked - the task must
        be started.

        Measured on a live run: the engineer closed the incident and
        exited, the task's retry had fallen due twelve minutes earlier,
        and it stayed in RETRY_WAIT. Reservation saw RETRY_WAIT and parked
        the run in WAITING_RATE_LIMIT - while there was no rate-limit
        barrier at all. The dispatcher exited, there was no one left to
        wake it, and the run stood forever.
        """

        outcome = self.resolve_and_complete(
            "инцидент закрыт\nPIPELINE_ENGINEER_STATUS: RESOLVED",
            reserve_before_retry=True,
        )
        self.assertEqual(outcome.worker_status, "RESOLVED")
        self.assertTrue(outcome.descriptors)
        state = self.store.load()
        self.assertEqual(state.task_states[self.task_id], "RUNNING")
        self.assertNotIn(self.task_id, state.task_retry_at)
        self.assertNotEqual(state.phase, "WAITING_RATE_LIMIT")

    def test_a_resolved_incident_hands_the_run_to_a_successor(self) -> None:
        outcome = self.resolve_and_complete(
            "инцидент закрыт\nPIPELINE_ENGINEER_STATUS: RESOLVED"
        )
        self.assertEqual(outcome.worker_status, "RESOLVED")
        # This used to be an empty tuple, and the run stood forever.
        self.assertTrue(outcome.descriptors)
        self.assertEqual(outcome.descriptors[0].task_id, self.task_id)
        state = self.store.load()
        # The run did not merely "not stop" - it moved: the appointed successor
        # moves the task into work instead of leaving it waiting.
        self.assertEqual(state.status, "RUNNING")
        self.assertEqual(state.task_states[self.task_id], "RUNNING")
        self.assertNotEqual(state.phase, "PIPELINE_ENGINEER_NO_SUCCESSOR")

    def test_the_engineer_turn_is_visible_to_the_causal_barrier(self) -> None:
        """The barrier reads turn_completed, not the session status.

        Before this the engineer wrote only
        `pipeline_engineer_completed`: his completed turn stayed
        invisible, and there was no one to start the successor -
        `automatic relay has no completed causal predecessor`.
        """

        self.resolve_and_complete("готово\nPIPELINE_ENGINEER_STATUS: RESOLVED")
        journal = self.store.load().lifecycle_journal
        completed = [
            item
            for item in journal
            if item.get("event") == "turn_completed"
            and item.get("thread_id") == "engineer-thread"
        ]
        self.assertTrue(
            completed, "the engineer's turn is not marked completed"
        )

    def test_the_engineer_marks_the_successor_as_its_own_transition(self) -> None:
        """Without this record the dispatcher refuses to carry the chain
        further.

        On a live run the engineer closed the incident and appointed a
        successor, but did not record it on himself: the next step
        answered `current dispatcher does not own the
        completed-to-successor transition`, the reservation hung in
        CREATE_REQUESTED, and on top of the closed incident a new one
        opened - this time about the dispatcher's own crash.
        """

        outcome = self.resolve_and_complete(
            "инцидент закрыт\nPIPELINE_ENGINEER_STATUS: RESOLVED",
            dispatcher_authorized=True,
        )
        engineer = next(
            item
            for item in self.store.load().worker_sessions
            if item.get("kind") == "pipeline_engineer"
        )
        self.assertEqual(engineer["automatic_dispatch_state"], "ADVANCING")
        self.assertEqual(
            engineer["automatic_successor_tokens"],
            [item.reservation_token for item in outcome.descriptors],
        )

    def test_the_engineer_thread_is_the_causal_link_for_the_successor(self) -> None:
        """The relay runs on the engineer's turn: there is no other live
        predecessor."""

        self.resolve_and_complete("готово\nPIPELINE_ENGINEER_STATUS: RESOLVED")
        successor = next(
            item
            for item in self.store.load().worker_sessions
            if item.get("kind") != "pipeline_engineer"
            and item.get("status") not in {"COMPLETED", "FAILED", "RETRY_WAIT"}
        )
        self.assertEqual(successor["relay_owner_thread_id"], "engineer-thread")


    def test_no_successor_is_a_fresh_ticket_and_an_engineer_in_the_same_transaction(self) -> None:
        """A ready task and nobody to take it is a reservation defect.

        The old path handed the just-closed ticket to the owner without a
        code: R13 refused it, the refusal was swallowed, and the run went
        BLOCKED in silence. Now a fresh ticket holds nothing (the ready
        task is what should go, not what should wait), and the next
        engineer is reserved in the same transaction.
        """

        from unittest import mock

        from codex_autopilot import lifecycle_completion
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        real = lifecycle_completion._reserve_in_state
        calls: list[int] = []

        def first_finds_nothing(*args, **kwargs):
            calls.append(1)
            return () if len(calls) == 1 else real(*args, **kwargs)

        with mock.patch.object(lifecycle_completion, "_reserve_in_state", first_finds_nothing):
            outcome = self.resolve_and_complete(
                "инцидент закрыт\nPIPELINE_ENGINEER_STATUS: RESOLVED"
            )
        self.assertEqual(len(calls), 2, "the second reservation never ran")
        self.assertIn("pipeline_engineer", [item.kind for item in outcome.descriptors])
        incidents = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
        closed = next(item for item in incidents if item["incident_id"] == self.incident_id)
        self.assertEqual(closed["phase"], "RESOLVED")
        fresh = next(
            item for item in incidents if item["system_state"].get("stop_kind") == "no_successor"
        )
        self.assertEqual(fresh["phase"], "PIPELINE_ENGINEER")
        self.assertEqual(fresh["affected_task_ids"], [])
        self.assertEqual(fresh["context_task_id"], self.task_id)
        self.assertEqual(self.store.load().status, "RUNNING")


class FailureBeforeTheRequestIsNotAmbiguousTests(unittest.TestCase):
    """A failure before the request is sent is known, not ambiguous.

    On a live run `installed_plugin_root` sat among the arguments of
    `client.start_thread`: it failed after `create_invoked = True` was
    already set, although there was not a single `thread/start` in the
    dispatcher's log. The failure was recorded as UNKNOWN, it produced an
    AMBIGUOUS_SIDE_EFFECT ticket, and that class by design forbids both
    the automatic repair and the on-call engineer. The run stood with no
    way out.
    """

    def test_the_plugin_root_is_resolved_before_the_flag_is_armed(self) -> None:
        import inspect

        from codex_autopilot import lifecycle_dispatch

        # Code only: the nearby comment mentions both lines on purpose,
        # and a text search for them would catch the explanation instead of the implementation.
        code = "\n".join(
            line
            for line in inspect.getsource(lifecycle_dispatch).splitlines()
            if not line.lstrip().startswith("#")
        )
        resolve = code.index("plugin_root = installed_plugin_root(cfg.skill_path)")
        armed = code.index("create_invoked = True")
        self.assertLess(
            resolve,
            armed,
            "the plugin root must be resolved before create_invoked is armed",
        )

    def test_the_flag_is_not_armed_from_inside_the_call_arguments(self) -> None:
        """The call must not count as sent what is still being
        assembled."""

        import inspect

        from codex_autopilot import lifecycle_dispatch

        body = inspect.getsource(lifecycle_dispatch)
        start = body.index("started = client.start_thread(")
        args = body[start : body.index("\n            )", start)]
        self.assertNotIn("installed_plugin_root(", args)


class EscalationAlwaysHasAWayBackTests(unittest.TestCase):
    """The user's answer to an escalation must not depend on the run
    phase.

    The `PIPELINE_ENGINEER_ESCALATED` phase is set only by the engineer's
    completion. An incident escalated by routing left the run in its
    previous phase - and the resume silently closed nothing.
    """

    # The resume's independence from the run phase is checked by execution:
    # test_resume_end_to_end.test_resume_answers_the_escalation_whatever_phase_the_run_is_in.


class ReplaceStartsWithoutInheritedTicketsTests(unittest.TestCase):
    """A new run does not inherit the previous run's tickets.

    Tickets carry no run_id, and the on-call engineer outranks any work:
    two open tickets from the previous run stood across the new one before
    its first task. `--replace` cleaned the plan, the state and the logs -
    and did not touch the incident store.
    """

    def setUp(self) -> None:
        import json as _json
        import tempfile

        from _gates import patch_hook_trust_gates
        from _plan_contract import initialize_verified_project as initialize_project
        from codex_autopilot.pipeline_engineer import (
            IncidentClass,
            IncidentSignal,
            PipelineIncidentStore,
            SideEffectOutcome,
        )
        from codex_autopilot.run_state import utc_now
        from test_verification_lifecycle import graph, task

        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")
        self.plan_file = self.root / "input-plan.json"
        self.plan_file.write_text(_json.dumps(graph(task("A"))), encoding="utf-8")
        self.initialize = initialize_project
        self.initialize(
            self.root,
            self.plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
        )
        self.state_dir = self.root / ".codex-autopilot"
        incidents = PipelineIncidentStore(self.state_dir)
        incidents.open_incident(
            IncidentSignal(
                signal_id="probe:stale",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                summary="Тикет прошлого прогона",
                affected_task_ids=("A",),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
                system_state={},
            ),
            at=utc_now(),
        )
        self.store_cls = PipelineIncidentStore

    def replace_run(self) -> None:
        # The first launch consumes the plan file, so the repeat writes it
        # again - exactly as the skill does on a new run.
        import json as _json

        from test_verification_lifecycle import graph, task

        self.plan_file.write_text(_json.dumps(graph(task("A"))), encoding="utf-8")
        self.initialize(
            self.root,
            self.plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
            replace=True,
        )

    def test_a_replaced_run_opens_with_an_empty_incident_store(self) -> None:
        self.assertTrue(self.store_cls(self.state_dir).load()["incidents"])
        self.replace_run()
        self.assertEqual(self.store_cls(self.state_dir).load()["incidents"], [])

    def test_the_previous_tickets_are_kept_beside_the_run(self) -> None:
        """This is a record of a breakage: it is set aside, not
        deleted."""

        self.replace_run()
        archived = sorted(self.state_dir.glob("pipeline-incidents.*.json"))
        self.assertEqual(len(archived), 1)
        import json as _json

        kept = _json.loads(archived[0].read_text(encoding="utf-8"))
        self.assertEqual(len(kept["incidents"]), 1)


class HookTimeoutsSurviveCodexLoadTests(unittest.TestCase):
    """Codex must not rewrite our hooks while loading them.

    The user complained all day that hook trust fell off before every new
    task. The cause turned up on her own screen: Codex wrote `clamping
    Interrupt hook timeout to 3s` and showed all three hooks in the Review
    state. We declared Interrupt with a timeout of 30, Codex clamped it to
    its own limit - the definition stopped matching the trusted one, and
    the whole file went back for re-parsing on every load.
    """

    MAX_INTERRUPT_TIMEOUT = 3

    def hook_files(self):
        import json as _json

        root = Path(__file__).resolve().parent.parent
        files = sorted(root.glob("plugins/*/hooks/hooks.json"))
        self.assertTrue(files, "hook files not found")
        return [(p, _json.loads(p.read_text(encoding="utf-8"))) for p in files]

    def test_the_interrupt_timeout_is_never_above_what_codex_accepts(self) -> None:
        for path, payload in self.hook_files():
            for group in payload["hooks"].get("Interrupt", []):
                for hook in group["hooks"]:
                    self.assertLessEqual(
                        hook["timeout"],
                        self.MAX_INTERRUPT_TIMEOUT,
                        f"{path.parts[-3]}: Codex will clamp this timeout "
                        "and will demand that every hook in the file be "
                        "trusted again",
                    )

    def test_both_profiles_declare_the_same_interrupt_timeout(self) -> None:
        """The profiles differ in what they contain, not in how the
        hooks behave."""

        seen = {
            hook["timeout"]
            for _, payload in self.hook_files()
            for group in payload["hooks"].get("Interrupt", [])
            for hook in group["hooks"]
        }
        self.assertEqual(len(seen), 1, f"the profiles diverged: {seen}")


class RepeatedFailureIsNotACrashTests(unittest.TestCase):
    """A second failure of the same task must not kill the dispatcher.

    On a live run this opened a ticket on top of a ticket: the real fault
    was already waiting in RETRY_WAIT, a second failure record arrived,
    the state machine rejected the RETRY_WAIT -> RETRY_WAIT transition,
    the relay died, and a second incident appeared - this time about the
    dispatcher's own crash.
    """

    def setUp(self) -> None:
        ResolvedMustHandOverTests.setUp(self)

    def fail_once(self, token: str) -> None:
        from codex_autopilot.lifecycle_failures import record_desktop_failure

        record_desktop_failure(
            self.cfg,
            token,
            reason="воркер сорвался",
            failure_code="app_server_rpc_failed",
            definitive=True,
            reserve_other_ready=False,
        )

    def park_in_retry_wait(self) -> None:
        """This is how recovery does it: by direct assignment.

        `resilience.py` sets RETRY_WAIT around the state machine when it
        clears up a dead dispatcher. The failure record arrives next - and
        meets the task already in the state it was about to move it to.
        """

        from codex_autopilot.task_state import TaskState

        state = self.store.load()
        state.task_states = {**state.task_states, self.task_id: TaskState.RETRY_WAIT.value}
        state.active_task_ids = [t for t in state.active_task_ids if t != self.task_id]
        self.store.save(state)

    def test_a_failure_meeting_an_already_waiting_task_does_not_crash(self) -> None:
        from codex_autopilot.task_state import TaskState

        self.park_in_retry_wait()
        # This used to raise IllegalTaskTransition: RETRY_WAIT -> RETRY_WAIT,
        # the relay died, and a second ticket opened on top of the real fault.
        self.fail_once(self.failed_token)
        self.assertEqual(
            self.store.load().task_states[self.task_id], TaskState.RETRY_WAIT.value
        )

    def test_the_failure_is_still_recorded(self) -> None:
        """Idempotence must not turn into silence."""

        self.park_in_retry_wait()
        self.fail_once(self.failed_token)
        session = next(
            item
            for item in self.store.load().worker_sessions
            if item.get("reservation_token") == self.failed_token
        )
        self.assertEqual(session["status"], "RETRY_WAIT")
        self.assertIn("сорвался", str(session.get("failure_reason")))


class ResolvedIncidentResumesTheRunItselfTests(unittest.TestCase):
    """After a repair the run continues by itself, without an operator.

    This is the difference between "the pipeline gets repaired" and "the
    pipeline is automatic". A reservation created BEFORE the incident is
    not a new one, and the ordinary reserver will not return it: it hung
    in CREATE_REQUESTED until a human resumed the run by hand. Every
    repair needed an operator.
    """

    def orphan(self, **extra):
        session = {
            "task_id": "M1",
            "kind": "replanner",
            "status": "CREATE_REQUESTED",
            "thread_id": None,
            "descriptor": {
                "schema_version": 2, "surface": "desktop_owned", "run_id": "r",
                "graph_version": 1, "task_id": "M1", "task_title": "T",
                "kind": "replanner", "attempt": 1, "worker_sequence": 1,
                "reservation_token": "tok", "operation_id": "op",
                "client_user_message_id": "cid", "desktop_project_id": "p",
                "cwd": "/tmp", "title": "T", "prompt": "p", "model": None,
                "thinking": None, "execution_mode": "code",
                "created_at": "2026-09-14T00:00:00+00:00",
                "prep_app_server_exited_at": "2026-09-14T00:00:00+00:00",
                "descriptor_path": "/tmp/tok.json",
            },
        }
        session.update(extra)
        return session

    def state(self, sessions):
        from types import SimpleNamespace

        return SimpleNamespace(worker_sessions=sessions)

    def test_a_reservation_without_a_thread_is_picked_up(self) -> None:
        from codex_autopilot.lifecycle_completion import _relayable_descriptors_without_a_thread

        found = _relayable_descriptors_without_a_thread(self.state([self.orphan()]))
        self.assertEqual([item.task_id for item in found], ["M1"])

    def test_a_reservation_that_already_has_a_thread_is_left_alone(self) -> None:
        """A thread exists - the side effect happened, it must not be
        started again."""

        from codex_autopilot.lifecycle_completion import _relayable_descriptors_without_a_thread

        found = _relayable_descriptors_without_a_thread(
            self.state([self.orphan(thread_id="01a0-real")])
        )
        self.assertEqual(found, ())

    def test_a_running_session_is_not_a_leftover(self) -> None:
        from codex_autopilot.lifecycle_completion import _relayable_descriptors_without_a_thread

        found = _relayable_descriptors_without_a_thread(self.state([self.orphan(status="ACTIVE")]))
        self.assertEqual(found, ())


class EveryCompletionPathRecordsOwnershipTests(unittest.TestCase):
    """Whoever reserves the successor records the transition as its own.

    The `adopt_automatic_dispatcher_successor` barrier checks two fields
    on the completed session: automatic_successor_tokens and ADVANCING.
    Without them it refuses to carry the chain, in the words "current
    dispatcher does not own the completed-to-successor transition", the
    reservation hangs, and on top of it a ticket opens about the
    dispatcher's crash.

    There are three completion paths - the worker, the on-call engineer,
    the scheduler. I fixed them one at a time, each time after the run had
    stood. This test closes the class: any new branch that reserves a
    successor must keep the same record.
    """

    def test_no_completion_path_reserves_without_recording(self) -> None:
        import ast
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent
            / "src/codex_autopilot/lifecycle_completion.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            body = ast.dump(node)
            if "_reserve_in_state" not in body:
                continue
            if "automatic_successor_tokens" not in body:
                offenders.append(node.name)
        self.assertEqual(
            offenders,
            [],
            "these paths reserve a successor and do not record ownership "
            "of the transition",
        )

    def test_the_barrier_still_checks_both_fields(self) -> None:
        """Otherwise the test above would guard a rule that is no longer
        needed."""

        import inspect

        from codex_autopilot import lifecycle_dispatch

        body = inspect.getsource(
            lifecycle_dispatch.adopt_automatic_dispatcher_successor
        )
        self.assertIn("automatic_successor_tokens", body)
        self.assertIn("ADVANCING", body)
