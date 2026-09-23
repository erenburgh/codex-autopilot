"""The on-call's powers over a stopped task, and her answer without Resume.

Every behaviour here is driven through the production entry points (the
reservation frontier, the dispatcher's completion, the CLI's own functions)
with fakes only: no live Codex and no App Server is started.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from _appserver_fakes import activate_via_app_server
from _relay import TEST_RELAY_OWNER, reserve_ready_frontier
from codex_autopilot.blocked_runs import stop_run
from codex_autopilot.engineer_authority import (
    READ_ONLY_DIAGNOSTIC_ACTIONS,
    RECOVERY_ACTIONS,
    REPAIR_ACTIONS,
)
from codex_autopilot.engineer_stop_actions import (
    EngineerStopActionError,
    request_plan_change,
    require_stop_ticket_closable,
    return_stopped_task,
)
from codex_autopilot.lifecycle import complete_desktop_worker
from codex_autopilot.owner_answers import OwnerAnswerError, answer_task
from codex_autopilot.pipeline_engineer import (
    HealthcheckResult,
    IncidentPhase,
    PipelineIncidentStore,
)
from codex_autopilot.plan import load_plan
from test_a_dead_dispatcher_strands_nothing import _Base

AT = "2026-09-24T10:00:00+00:00"


class _Stopped(_Base):
    """A task A stopped by a ticket, and the on-call holding that ticket."""

    def stopped(
        self,
        *,
        kind: str = "worker_blocked",
        reason_code: str = "MISSING_RESOURCE",
        block: bool = True,
        tasks: tuple[str, ...] = ("A",),
        extra: dict | None = None,
    ) -> tuple[str, object]:
        state = self.store.load()
        if block:
            for task_id in tasks:
                state.task_states[task_id] = "BLOCKED"
        incident_id = stop_run(
            self.cfg,
            state,
            stop_kind=kind,
            phase="BLOCKED",
            reason=f"A stopped: {reason_code}",
            summary="s.",
            at=AT,
            task_ids=tasks,
            system_state={"reason_code": reason_code, **(extra or {})},
        )
        self.store.save(state)
        reserved = reserve_ready_frontier(self.cfg)
        engineer = next(item for item in reserved if item.kind == "pipeline_engineer")
        activate_via_app_server(self.cfg, self.root, engineer, "eng-1")
        return str(incident_id), engineer

    def ticket(self, incident_id: str) -> dict:
        return next(
            item
            for item in PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
            if item["incident_id"] == incident_id
        )

    def resolve(self, incident_id: str, *actions: str):
        from codex_autopilot import cli

        err = io.StringIO()
        with mock.patch.object(cli, "_relay_executor_thread_id", return_value="eng-1"), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(
                [
                    "devops-resolve-incident", "--project", str(self.root),
                    "--incident-id", incident_id,
                    "--healthcheck-name", "run_declared_healthcheck",
                    "--check", "the key is in place",
                    *[part for action in actions for part in ("--action", action)],
                ]
            )
        return code, err.getvalue()


class TheOnCallsPackageTests(_Stopped):
    def test_an_infrastructure_stop_gets_every_repair_and_the_stop_itself(self) -> None:
        from codex_autopilot.lifecycle_reservations import pipeline_engineer_package

        incident_id, _ = self.stopped()
        package = pipeline_engineer_package(self.cfg, self.store.load(), incident_id)
        self.assertEqual(package["allowed_actions"], list(RECOVERY_ACTIONS))
        for action in ("request_plan_change", "return_stopped_task"):
            self.assertIn(action, REPAIR_ACTIONS)
            self.assertIn(action, package["allowed_actions"])
        context = package["stop_context"]
        self.assertEqual(context["stop_kind"], "worker_blocked")
        self.assertEqual(context["reason_code"], "MISSING_RESOURCE")
        self.assertIn("return_stopped_task", context["means"])
        self.assertEqual(context["tasks"]["A"]["state"], "BLOCKED")
        self.assertIn("unblock", package["owner_answer"])
        self.assertIn("--task A", package["owner_answer"])

    def test_the_prompt_of_a_stop_ticket_carries_the_brief_with_real_commands(self) -> None:
        import argparse
        import re

        from codex_autopilot.ai_studio import AIStudioRuntime
        from codex_autopilot.cli import parser
        from codex_autopilot.lifecycle_reservations import pipeline_engineer_package

        incident_id, _ = self.stopped()
        package = pipeline_engineer_package(self.cfg, self.store.load(), incident_id)
        prompt = AIStudioRuntime(
            load_plan(self.cfg.state_dir, self.cfg.profile),
            self.root,
            language="en",
            skill_path=self.cfg.skill_path,
        ).build_pipeline_engineer_prompt(package, reservation_token="t")
        self.assertIn("AUTOPILOT_ESCALATION:", prompt)
        self.assertIn("devops-return-task", prompt)
        self.assertIn("return_stopped_task", prompt)
        subparsers = {}
        for action in parser()._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparsers.update(action.choices)
        for command, arguments in re.findall(r"`scripts/codex-autopilot (\S+)([^`]*)`", prompt):
            for item in subparsers[command]._actions:
                if item.option_strings and item.required:
                    self.assertIn(max(item.option_strings, key=len), arguments, command)


class ReturningAStoppedTaskTests(_Stopped):
    def test_only_the_engineer_of_this_ticket_from_its_own_thread(self) -> None:
        incident_id, _ = self.stopped()
        with self.assertRaisesRegex(EngineerStopActionError, "own thread"):
            return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="worker-B")
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")

        result = return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")

        self.assertEqual(result["to"], "READY")
        self.assertEqual(self.store.load().task_states["A"], "READY")
        self.assertEqual(self.ticket(incident_id)["returns"][0]["task_id"], "A")

    def test_done_work_returns_to_acceptance_never_to_verified(self) -> None:
        state = self.store.load()
        state.task_revisions["A"] = 1
        self.store.save(state)
        incident_id, _ = self.stopped(kind="verification_protocol", reason_code="")
        result = return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self.assertEqual(result["to"], "IMPLEMENTED")

    def test_her_decision_is_never_lifted_by_the_on_call(self) -> None:
        incident_id, _ = self.stopped(reason_code="PRODUCT_DECISION")
        with self.assertRaisesRegex(EngineerStopActionError, "owner's"):
            return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")

    def test_a_third_return_of_the_same_stop_goes_to_her(self) -> None:
        """R23: two returns that did not hold are not followed by a third.

        The counter is the door's (stop_repeats): each return is followed by
        its ticket's closure, and the same stop back after two closures goes
        to her with what each return did.
        """

        for _ in range(2):
            incident_id, engineer = self.stopped()
            self.assertTrue(
                return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")["returned"]
            )
            self.assertEqual(self.resolve(incident_id, "return_stopped_task")[0], 0)
            state = self.store.load()
            for item in state.worker_sessions:
                if item["reservation_token"] == engineer.reservation_token:
                    item["status"] = "COMPLETED"
            self.store.save(state)
        state = self.store.load()
        state.task_states["A"] = "BLOCKED"
        third = stop_run(
            self.cfg, state, stop_kind="worker_blocked", phase="BLOCKED", reason="A stopped: MISSING_RESOURCE",
            summary="s.", at=AT, task_ids=("A",), system_state={"reason_code": "MISSING_RESOURCE"},
        )
        self.store.save(state)
        ticket = self.ticket(str(third))
        self.assertEqual(ticket["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        self.assertEqual(ticket["escalation_reason"], "RECOVERY_EXHAUSTED")
        returns = [entry for item in ticket["escalation"]["repaired"] for entry in item["returns"]]
        self.assertEqual([entry["task_id"] for entry in returns], ["A", "A"])
        self.assertFalse(
            any(item.kind == "pipeline_engineer" for item in reserve_ready_frontier(self.cfg)),
            "a third engineer was raised for the same stop",
        )


class TheTopOfTheLadderTests(_Stopped):
    def _at_the_top(self) -> int:
        plan = load_plan(self.cfg.state_dir, self.cfg.profile)
        maximum = plan.task_map["A"].verification.max_revision_attempts
        state = self.store.load()
        state.task_revisions["A"] = maximum * 4
        state.task_rehires["A"] = 3
        state.task_effort["A"] = "max"
        self.store.save(state)
        return maximum

    def _record_patch(self, incident_id: str, module: str, patch_id: str) -> None:
        PipelineIncidentStore(self.cfg.state_dir).record_runtime_patch(
            incident_id,
            patch={
                "patch_id": patch_id,
                "test_name": "test_repro",
                "at": AT,
                "changes": [{"module": module, "sha256_before": "a", "sha256_after": "b"}],
            },
            at=AT,
        )

    def _revise_again_blocks(self) -> bool:
        from codex_autopilot.lifecycle_base import _rehire_or_block_on_revision_limit

        state = self.store.load()
        plan = load_plan(self.cfg.state_dir, self.cfg.profile)
        state.task_states["A"] = "REVISION_REQUIRED"
        session = {"operation_id": "o", "task_id": "A", "attempt": 1, "reservation_token": "r"}
        return _rehire_or_block_on_revision_limit(self.cfg, plan, state, "A", session, AT)

    def test_a_harmless_patch_buys_no_fresh_budget(self) -> None:
        self._at_the_top()
        incident_id, _ = self.stopped(kind="ladder_exhausted", reason_code="")
        with self.assertRaisesRegex(EngineerStopActionError, "acceptance path"):
            return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self._record_patch(incident_id, "status.py", "patch-harmless")
        with self.assertRaisesRegex(EngineerStopActionError, "acceptance path"):
            return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")

    def test_a_patch_on_the_acceptance_path_returns_the_task_with_a_fresh_hire(self) -> None:
        self._at_the_top()
        self.assertTrue(self._revise_again_blocks(), "the fixture must start at the top")
        incident_id, _ = self.stopped(kind="ladder_exhausted", reason_code="")
        self._record_patch(incident_id, "department_acceptance.py", "patch-rubric")

        result = return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")

        self.assertEqual(result["grant"]["effort"], "max", "the top effort is kept")
        self.assertEqual(self.ticket(incident_id)["returns"][0]["grounds"]["runtime_patch_ids"], ["patch-rubric"])
        self.assertFalse(self._revise_again_blocks(), "the next REVISE stopped it again at once")

    def test_one_patch_is_good_for_one_grant(self) -> None:
        self._at_the_top()
        incident_id, _ = self.stopped(kind="ladder_exhausted", reason_code="")
        self._record_patch(incident_id, "verification.py", "patch-once")
        return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        state = self.store.load()
        state.task_states["A"] = "BLOCKED"
        self.store.save(state)
        with self.assertRaisesRegex(EngineerStopActionError, "acceptance path"):
            return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")

    def test_her_answer_at_the_top_grants_a_fresh_hire(self) -> None:
        self._at_the_top()
        incident_id, _ = self.stopped(kind="ladder_exhausted", reason_code="")
        answer_task(self.cfg, "A", "the rubric was too strict; try once more", option="retry", raise_run=False)
        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.RESOLVED.value)
        self.assertFalse(self._revise_again_blocks())
        self.assertEqual(self.store.load().task_effort["A"], "max")


class ClosingAStopTicketTests(_Stopped):
    def test_diagnostics_alone_do_not_close_a_stop(self) -> None:
        incident_id, _ = self.stopped()
        code, err = self.resolve(incident_id, "inspect_recent_events")
        self.assertEqual(code, 2)
        self.assertIn("diagnostics alone", err)
        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.PIPELINE_ENGINEER.value)

    def test_a_held_task_left_blocked_does_not_close_it(self) -> None:
        incident_id, _ = self.stopped()
        code, err = self.resolve(incident_id, "repair_runtime_code")
        self.assertEqual(code, 2)
        self.assertIn("still holds A in BLOCKED", err)
        return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self.assertEqual(self.resolve(incident_id, "return_stopped_task")[0], 0)
        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.RESOLVED.value)

    def test_a_plan_change_it_asked_for_lets_it_close(self) -> None:
        incident_id, _ = self.stopped(reason_code="DEPENDENCY_DEFECT")
        result = request_plan_change(
            self.cfg,
            incident_id=incident_id,
            task_id="A",
            reason="A depends on an interface B never exports; split the task",
            thread_id="eng-1",
        )
        state = self.store.load()
        self.assertEqual(state.active_plan_change_id, result["plan_change_id"])
        record = state.plan_changes[-1]
        self.assertEqual(record["requested_by_incident"], incident_id)
        self.assertEqual(record["requester_task_id"], "A")
        require_stop_ticket_closable(self.cfg, incident_id, ("request_plan_change",))
        self.assertEqual(self.resolve(incident_id, "request_plan_change")[0], 0)

    def test_a_plan_change_is_refused_where_the_means_table_has_none(self) -> None:
        incident_id, _ = self.stopped(kind="no_successor", reason_code="", block=False)
        with self.assertRaisesRegex(EngineerStopActionError, "not re-planned"):
            request_plan_change(
                self.cfg, incident_id=incident_id, task_id="A", reason="r", thread_id="eng-1"
            )


class TheEngineersProtocolErrorTests(_Stopped):
    def _complete(self, thread: str, message: str):
        return complete_desktop_worker(
            self.cfg, thread_id=thread, turn_id=f"{thread}-turn", final_message=message
        )

    def test_an_unreadable_answer_frees_the_lane_and_the_second_goes_to_her(self) -> None:
        from codex_autopilot.rules import violation_counts

        incident_id, first = self.stopped()
        outcome = self._complete("eng-1", "I looked around and I am done.")
        self.assertEqual(outcome.worker_status, "PROTOCOL_ERROR")
        session = self._session(first.reservation_token)
        self.assertEqual(session["status"], "COMPLETED")
        # The lane is free in the same completion: the next engineer is here.
        successor = next(item for item in outcome.descriptors if item.kind == "pipeline_engineer")
        self.assertEqual(self._engineers()[-1]["incident_id"], incident_id)
        activate_via_app_server(self.cfg, self.root, successor, "eng-2")

        outcome = self._complete("eng-2", "still no idea\nPIPELINE_ENGINEER_STATUS: RESOLVED")

        self.assertFalse(any(item.kind == "pipeline_engineer" for item in outcome.descriptors))
        ticket = self.ticket(incident_id)
        self.assertEqual(ticket["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        self.assertEqual(ticket["escalation_reason"], "RECOVERY_EXHAUSTED")
        self.assertIn("still no idea", ticket["escalation"]["diagnosis"])
        self.assertGreaterEqual(violation_counts(self.cfg.state_dir).get("R13", 0), 2)

    def test_an_escalation_without_its_report_still_reaches_her(self) -> None:
        from codex_autopilot.rules import violation_counts

        incident_id, _ = self.stopped()
        self._complete(
            "eng-1",
            "The key is hers to issue.\nPIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER DANGEROUS_PERMISSION",
        )
        ticket = self.ticket(incident_id)
        self.assertEqual(ticket["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        self.assertIn("hers to issue", ticket["escalation"]["diagnosis"])
        self.assertEqual(violation_counts(self.cfg.state_dir).get("R13"), 1)


class HerAnswerTests(_Stopped):
    def _hand_up(self) -> str:
        incident_id, _ = self.stopped()
        complete_desktop_worker(
            self.cfg,
            thread_id="eng-1",
            turn_id="eng-1-turn",
            final_message=(
                'AUTOPILOT_ESCALATION: {"diagnosis":"the key is hers","decision_needed":"issue it?",'
                '"recommendation":"issue a scoped key","options":[{"code":"issued","means":"I issued it"}],'
                '"scope":"task"}\nPIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER DANGEROUS_PERMISSION'
            ),
        )
        return incident_id

    def test_unblock_closes_her_tickets_lifts_the_stop_and_raises_the_run(self) -> None:
        from codex_autopilot.lifecycle_reservations import tasks_paused_by_incidents

        incident_id = self._hand_up()
        spawned = []

        # The owner is derived from the journal, exactly as the launchd sweep
        # derives it; here the journal has none of its own, so the derivation
        # is stood in for and the call into it is what is checked.
        with mock.patch(
            "codex_autopilot.wake.derive_owner", return_value=("owner-thread", "owner-turn")
        ) as derive:
            result = answer_task(
                self.cfg,
                "A",
                "I issued a scoped key",
                option="issued",
                spawn=lambda cfg, **kwargs: spawned.append(kwargs) or 4242,
            )
        derive.assert_called_once()

        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.RESOLVED.value)
        state = self.store.load()
        self.assertEqual(state.task_states["A"], "READY")
        plan = load_plan(self.cfg.state_dir, self.cfg.profile)
        self.assertNotIn("A", tasks_paused_by_incidents(self.cfg, plan))
        self.assertEqual(state.user_unblocks[-1]["option"], "issued")
        self.assertIn("I issued it", state.user_unblocks[-1]["reason"])
        self.assertTrue(result["raised"])
        self.assertEqual(spawned[0]["owner"], "owner-thread")
        self.assertEqual(spawned[0]["owner_turn"], "owner-turn")

    def test_without_a_causal_owner_it_says_who_raises_the_run(self) -> None:
        self._hand_up()
        result = answer_task(self.cfg, "A", "go on", option="issued", spawn=lambda cfg, **kw: 1)
        self.assertFalse(result["raised"])
        self.assertIn("sweep raises the run", result["why"])

    def test_an_option_outside_the_offer_is_refused(self) -> None:
        self._hand_up()
        with self.assertRaisesRegex(OwnerAnswerError, "not among the options"):
            answer_task(self.cfg, "A", "whatever", option="ignore", raise_run=False)
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")

    def test_the_cli_prints_that_the_run_goes_on_and_never_asks_for_resume(self) -> None:
        from codex_autopilot import cli

        self._hand_up()
        out = io.StringIO()
        with mock.patch("codex_autopilot.wake._spawn_wake", return_value=4242), \
             contextlib.redirect_stdout(out):
            code = cli.main(
                ["unblock", "--project", str(self.root), "--task", "A", "--reason", "go on"]
            )
        self.assertEqual(code, 0)
        self.assertIn("continues by itself", out.getvalue())
        self.assertNotIn("Resume", out.getvalue())

    def test_the_status_card_carries_what_she_decides_with(self) -> None:
        from codex_autopilot.status import render_short_status

        self._hand_up()
        card = render_short_status(
            self.cfg,
            self.store.load(),
            load_plan(self.cfg.state_dir, self.cfg.profile),
            dispatcher_running=False,
        )
        self.assertIn("the key is hers", card)
        self.assertIn("issue a scoped key", card)
        self.assertIn("issued", card)
        self.assertIn("unblock --project", card)

    def test_resume_answers_what_was_handed_up_and_routes_what_was_not(self) -> None:
        from codex_autopilot.control import _answer_escalation
        from codex_autopilot.engineer_authority import IncidentClass, SideEffectOutcome
        from codex_autopilot.pipeline_engineer import IncidentSignal

        handed = self._hand_up()
        store = PipelineIncidentStore(self.cfg.state_dir)
        fresh = store.open_incident(
            IncidentSignal(
                signal_id="unexamined",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                summary="nobody looked yet",
                affected_task_ids=("B",),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
            ),
            at=AT,
        )

        closed = _answer_escalation(self.cfg, self.store.load())

        self.assertEqual(closed, (handed,))
        self.assertEqual(self.store.load().task_states["A"], "READY")
        self.assertEqual(self.ticket(fresh["incident_id"])["phase"], IncidentPhase.PIPELINE_ENGINEER.value)


class ThePermissionRequestTests(_Stopped):
    PAYLOAD = {
        "id": 7,
        "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "worker-A", "itemId": "i1", "command": ["curl", "https://example.com"]},
    }

    def _worker_asks(self) -> str:
        from codex_autopilot.approval_stops import record_approval_required

        reserved = reserve_ready_frontier(self.cfg)
        worker = next(item for item in reserved if item.task_id == "A")
        activate_via_app_server(self.cfg, self.root, worker, "worker-A")
        return str(
            record_approval_required(
                self.cfg,
                worker.reservation_token,
                self.PAYLOAD,
                thread_id="worker-A",
                turn_id="turn-A",
                owner=TEST_RELAY_OWNER,
            )
        )

    def test_it_is_not_retried_and_goes_to_the_on_call_with_the_request(self) -> None:
        incident_id = self._worker_asks()
        state = self.store.load()
        self.assertEqual(int(state.failure_signature_attempts.get("approval_required", 0)), 0)
        ticket = self.ticket(incident_id)
        self.assertEqual(ticket["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
        self.assertEqual(ticket["affected_task_ids"], ["A"])
        self.assertEqual(ticket["system_state"]["approval"]["method"], self.PAYLOAD["method"])
        # Held: the next pass reserves the on-call, never a retry of A.
        state.task_retry_at["A"] = 0
        self.store.save(state)
        kinds = {(item.task_id, item.kind) for item in reserve_ready_frontier(self.cfg)}
        self.assertIn(("A", "pipeline_engineer"), kinds)
        self.assertNotIn(("A", "implementation"), kinds)

    def test_her_retry_is_allowed_once_per_request(self) -> None:
        from codex_autopilot.stop_holds import block_escalated_tasks

        first = self._worker_asks()
        state = self.store.load()
        block_escalated_tasks(self.cfg, state, first)
        self.store.save(state)
        PipelineIncidentStore(self.cfg.state_dir).escalate_incident_to_user(
            first, reason_code="DANGEROUS_PERMISSION", at=AT
        )
        with self.assertRaisesRegex(OwnerAnswerError, "--option replan"):
            answer_task(self.cfg, "A", "ok", raise_run=False)
        answer_task(self.cfg, "A", "I allowed curl", option="retry", raise_run=False)
        # The same request comes back after her retry.
        state = self.store.load()
        for item in state.worker_sessions:
            if item["task_id"] == "A" and item["kind"] != "pipeline_engineer":
                item["status"] = "COMPLETED"
        state.task_states["A"] = "READY"
        state.task_retry_at.pop("A", None)
        self.store.save(state)
        second = self._worker_asks()
        state = self.store.load()
        block_escalated_tasks(self.cfg, state, second)
        self.store.save(state)
        PipelineIncidentStore(self.cfg.state_dir).escalate_incident_to_user(
            second, reason_code="DANGEROUS_PERMISSION", at=AT
        )
        with self.assertRaisesRegex(OwnerAnswerError, "earlier retry"):
            answer_task(self.cfg, "A", "again", option="retry", raise_run=False)
        answer_task(self.cfg, "A", "do it without curl", option="replan", raise_run=False)
        state = self.store.load()
        self.assertEqual(state.plan_changes[-1]["requester_task_id"], "A")
        self.assertTrue(state.plan_changes[-1]["requested_by_owner"])


class ArmingTests(_Stopped):
    def test_arm_refuses_beside_live_sessions_but_not_its_own(self) -> None:
        from codex_autopilot.control import arm
        from codex_autopilot.run_arming import ArmRefused

        self.stopped()
        # B's worker is pending next to the engineer.
        with self.assertRaisesRegex(ArmRefused, "B/implementation"):
            arm(self.root, caller_thread="eng-1")
        state = self.store.load()
        for item in state.worker_sessions:
            if item["kind"] != "pipeline_engineer":
                item["status"] = "COMPLETED"
        self.store.save(state)
        with mock.patch("codex_autopilot.launch_registry.LaunchRegistry.add", return_value="req"), \
             mock.patch("codex_autopilot.run_state.StateStore.arm"):
            arm(self.root, caller_thread="eng-1")
        self.assertEqual(self.store.load().phase, "ARMED")


class AdvisoryTicketsPassThroughTheLaneTests(_Base):
    def test_an_ambiguous_create_reaches_the_on_call_with_diagnostics_only(self) -> None:
        from codex_autopilot.ai_studio import AIStudioRuntime
        from codex_autopilot.engineer_authority import IncidentClass, SideEffectOutcome
        from codex_autopilot.lifecycle_reservations import pipeline_engineer_package
        from codex_autopilot.pipeline_engineer import IncidentSignal, PipelineIncidentError

        store = PipelineIncidentStore(self.cfg.state_dir)
        incident = store.open_incident(
            IncidentSignal(
                signal_id="ambiguous-1",
                code="create_thread_ambiguous",
                surface=IncidentClass.PIPELINE,
                summary="create outcome unknown",
                affected_task_ids=("A",),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.UNKNOWN,
            ),
            at=AT,
        )
        self.assertEqual(incident["classification"], "AMBIGUOUS_SIDE_EFFECT")
        self.assertEqual(store.route_incident(incident["incident_id"], at=AT), IncidentPhase.PIPELINE_ENGINEER)
        reserved = reserve_ready_frontier(self.cfg)
        self.assertIn(("A", "pipeline_engineer"), {(item.task_id, item.kind) for item in reserved})
        package = pipeline_engineer_package(self.cfg, self.store.load(), incident["incident_id"])
        self.assertEqual(package["allowed_actions"], list(READ_ONLY_DIAGNOSTIC_ACTIONS))
        prompt = AIStudioRuntime(
            load_plan(self.cfg.state_dir, self.cfg.profile),
            self.root,
            language="en",
            skill_path=self.cfg.skill_path,
        ).build_pipeline_engineer_prompt(package, reservation_token="t")
        self.assertIn("not yours to repair or to close", prompt)
        with self.assertRaisesRegex(PipelineIncidentError, "not the on-call's to close"):
            store.complete_pipeline_engineer(
                incident["incident_id"],
                success=True,
                at=AT,
                healthcheck=HealthcheckResult(
                    name="run_declared_healthcheck", passed=True, observed_at=AT, checks=("x",)
                ),
                actions=("inspect_recent_events",),
            )


class TheHookTrustSignalTests(_Base):
    def test_revoked_trust_reaches_her_card_with_what_to_do(self) -> None:
        """The one signal that goes to her without the on-call (her boundary)."""

        from codex_autopilot.hook_trust import HookPreflightError
        from codex_autopilot.status import render_short_status

        self._ticket_holding_everything()

        def refused(*_args, **_kwargs):
            raise HookPreflightError("the Stop hook definition is not trusted")

        from codex_autopilot.wake import run_wake

        run_wake(
            self.cfg,
            at_epoch=0,
            owner=TEST_RELAY_OWNER,
            owner_turn="turn",
            now=__import__("time").time,
            sleep=lambda _s: None,
            reserve=refused,
            spawn_relay=lambda *_a, **_k: 1,
        )
        card = render_short_status(
            self.cfg,
            self.store.load(),
            load_plan(self.cfg.state_dir, self.cfg.profile),
            dispatcher_running=False,
        )
        self.assertIn("restore trust in the Codex Autopilot hook", card)


class BannersForHerDecisionTests(unittest.TestCase):
    def test_escalation_banners_alone_show_decisions_and_nothing_else(self) -> None:
        from types import SimpleNamespace

        from codex_autopilot import notify
        from codex_autopilot.blocked_runs import _tell_owner

        cfg = SimpleNamespace(
            runtime=SimpleNamespace(desktop_notifications=False, escalation_notifications=True),
            root=Path("/tmp/project"),
        )
        with mock.patch.object(notify.shutil, "which", return_value="/usr/bin/osascript"), \
             mock.patch.object(notify.subprocess, "run") as run:
            _tell_owner(cfg, "A waits for your decision: PRODUCT_DECISION")
            self.assertEqual(run.call_count, 1)
            self.assertFalse(notify.notify(cfg, "Codex Autopilot", "p", "A verified"))
            self.assertEqual(run.call_count, 1)
        cfg.runtime.escalation_notifications = False
        with mock.patch.object(notify.shutil, "which", return_value="/usr/bin/osascript"), \
             mock.patch.object(notify.subprocess, "run") as run:
            _tell_owner(cfg, "A waits for your decision")
            self.assertEqual(run.call_count, 0, "off by default: hers to turn on")


if __name__ == "__main__":
    unittest.main()
