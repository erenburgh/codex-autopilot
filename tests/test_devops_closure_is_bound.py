"""Closing a stop ticket is a return, and a permission request is read against R4.

The third independent check of the on-call's powers found two open doors and
a missing basis, each reproduced on the real CLI with fakes:

- ``devops-resolve-incident`` on a stop ticket accepted any thread of the run
  and any action name. An infrastructure stop only HOLDS its task (R3), so
  the closure is the main road back to work - and a worker of another task
  closed a DEPENDENCY_DEFECT ticket naming return_stopped_task, with the
  defect untouched: no replan, no patch, no return;
- the permission check compared a request with three sentences of prose in a
  patchable module; R4 wants the durable authorization in run-state with a
  fixed, versioned list of covered operations, and a confirmation request
  for a covered one refused.

Every test drives the production entry points (the worker's completion, the
CLI, arming, the dispatcher's permission record); no live Codex, no App
Server.
"""

from __future__ import annotations

import contextlib
import io
from unittest import mock

from _appserver_fakes import activate_via_app_server
from _handoff import bump_task_checkpoint
from _relay import TEST_RELAY_OWNER, reserve_ready_frontier
from codex_autopilot.engineer_stop_actions import request_plan_change
from codex_autopilot.lifecycle import complete_desktop_worker
from codex_autopilot.pipeline_engineer import IncidentPhase
from codex_autopilot.run_authorization import authorization_record, covering_operation
from test_devops_powers import _Stopped


class _Held(_Stopped):
    def _worker_stops(self, code: str) -> str:
        """A's worker stops itself; the completion holds A and raises the on-call as eng-1."""

        worker = next(item for item in reserve_ready_frontier(self.cfg) if item.task_id == "A")
        activate_via_app_server(self.cfg, self.root, worker, "worker-A")
        bump_task_checkpoint(self.root, "A", f"Stopped: {code}.")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=f"stopped\nAUTOPILOT_STATUS: BLOCKED {code}",
        )
        engineer = next(item for item in outcome.descriptors if item.kind == "pipeline_engineer")
        activate_via_app_server(self.cfg, self.root, engineer, "eng-1")
        return str(self._session(engineer.reservation_token)["incident_id"])

    def close(self, incident_id: str, thread: str, *actions: str) -> tuple[int, str]:
        from codex_autopilot import cli

        err = io.StringIO()
        with mock.patch.object(cli, "_relay_executor_thread_id", return_value=thread), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(
                [
                    "devops-resolve-incident", "--project", str(self.root),
                    "--incident-id", incident_id,
                    "--healthcheck-name", "run_declared_healthcheck",
                    "--check", "looked",
                    *[part for action in actions for part in ("--action", action)],
                ]
            )
        return code, err.getvalue()

    def assert_still_held(self, incident_id: str) -> None:
        from codex_autopilot.engineer_reservation import tasks_paused_by_incidents
        from codex_autopilot.plan import load_plan

        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
        plan = load_plan(self.cfg.state_dir, self.cfg.profile)
        self.assertIn("A", tasks_paused_by_incidents(self.cfg, plan))


class ClosingAStopIsAReturnTests(_Held):
    def test_the_checks_probe_another_thread_cannot_close_it(self) -> None:
        """Reproduces the check: worker-B closes A's DEPENDENCY_DEFECT ticket."""

        incident_id = self._worker_stops("DEPENDENCY_DEFECT")
        self.assertEqual(self.store.load().task_states["A"], "READY")  # held, not BLOCKED

        code, err = self.close(incident_id, "worker-B", "return_stopped_task")

        self.assertEqual(code, 2)
        self.assertIn("from its own thread", err)
        self.assert_still_held(incident_id)

    def test_only_the_means_of_this_stop(self) -> None:
        """DEPENDENCY_DEFECT is re-planned or repaired in code, never simply returned."""

        incident_id = self._worker_stops("DEPENDENCY_DEFECT")

        code, err = self.close(incident_id, "eng-1", "return_stopped_task")

        self.assertEqual(code, 2)
        self.assertIn("not among the means", err)
        self.assertIn("request_plan_change", err)
        self.assert_still_held(incident_id)

    def test_a_named_repair_must_have_happened(self) -> None:
        incident_id = self._worker_stops("DEPENDENCY_DEFECT")

        code, err = self.close(incident_id, "eng-1", "repair_runtime_code")
        self.assertEqual(code, 2)
        self.assertIn("no live runtime patch", err)
        code, err = self.close(incident_id, "eng-1", "request_plan_change")
        self.assertEqual(code, 2)
        self.assertIn("asked the replanner for nothing", err)
        self.assert_still_held(incident_id)

        request_plan_change(
            self.cfg,
            incident_id=incident_id,
            task_id="A",
            reason="A needs an interface B never exports; split it",
            thread_id="eng-1",
        )
        self.assertEqual(self.close(incident_id, "eng-1", "request_plan_change")[0], 0)
        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.RESOLVED.value)

    def test_a_return_named_with_nothing_returned_is_refused(self) -> None:
        incident_id, _ = self.stopped()  # A BLOCKED under the ticket, not returned

        code, err = self.close(incident_id, "eng-1", "return_stopped_task")

        self.assertEqual(code, 2)
        self.assertIn("returned nothing", err)

    def test_a_held_infrastructure_stop_closes_as_its_return(self) -> None:
        """The legitimate road stays open: the environment repaired, the hold lifts."""

        incident_id = self._worker_stops("MISSING_RESOURCE")

        self.assertEqual(self.close(incident_id, "eng-1", "rearm_run", "return_stopped_task")[0], 0)

        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.RESOLVED.value)
        self.assertEqual(self.store.load().task_states["A"], "READY")


class TheDurableAuthorizationTests(_Held):
    """R4: run-state holds the versioned list; a covered request is a runtime defect."""

    def _payload(self, command, **params) -> dict:
        return {
            "id": 7,
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "worker-A", "itemId": "i1", "command": command, **params},
        }

    def _worker_asks(self, payload: dict) -> str:
        from codex_autopilot.approval_stops import record_approval_required

        worker = next(item for item in reserve_ready_frontier(self.cfg) if item.task_id == "A")
        activate_via_app_server(self.cfg, self.root, worker, "worker-A")
        return str(
            record_approval_required(
                self.cfg,
                worker.reservation_token,
                payload,
                thread_id="worker-A",
                turn_id="turn-A",
                owner=TEST_RELAY_OWNER,
            )
        )

    def test_arming_records_the_versioned_list(self) -> None:
        from codex_autopilot.control import arm
        from codex_autopilot.engineer_authority import (
            RUN_AUTHORIZATION_VERSION,
            RUN_AUTHORIZED_OPERATIONS,
        )

        with mock.patch("codex_autopilot.launch_registry.LaunchRegistry.add", return_value="req"), \
             mock.patch("codex_autopilot.run_state.StateStore.arm"):
            arm(self.root)

        record = self.store.load().durable_authorization
        self.assertEqual(record["version"], RUN_AUTHORIZATION_VERSION)
        self.assertEqual(
            [item["id"] for item in record["operations"]],
            [item[0] for item in RUN_AUTHORIZED_OPERATIONS],
        )
        self.assertEqual(record["project_root"], str(self.root))
        self.assertEqual(record["granted_by"], "arm")

    def test_what_a_request_proves_decides_coverage(self) -> None:
        record = authorization_record(self.cfg, at="t", granted_by="arm")
        root = str(self.root)
        cases = {
            "the plugin's CLI in the project": (
                self._payload(["codex-autopilot", "status", "--project", root], cwd=root),
                "autopilot_cli_in_project",
            ),
            "the plugin's CLI as one string": (
                self._payload(f"codex-autopilot status --project {root}", cwd=root),
                "autopilot_cli_in_project",
            ),
            "another program": (self._payload(["curl", "https://example.com"], cwd=root), None),
            "the CLI with a second command chained": (
                self._payload(f"codex-autopilot status; rm -rf {root}", cwd=root),
                None,
            ),
            "the CLI run outside the project": (
                self._payload(["codex-autopilot", "status"], cwd="/tmp"),
                None,
            ),
            "a write inside the project": (
                {"method": "item/fileChange/requestApproval", "params": {"grantRoot": f"{root}/src"}},
                "file_change_in_project",
            ),
            "a write outside the project": (
                {"method": "item/fileChange/requestApproval", "params": {"grantRoot": "/Users/x/.codex"}},
                None,
            ),
            "a write that names only where the turn stands": (
                {"method": "item/fileChange/requestApproval", "params": {"cwd": root}},
                None,
            ),
            "an MCP elicitation": ({"method": "mcpServer/elicitation/request", "params": {}}, None),
        }
        for name, (payload, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(covering_operation(record, payload), expected)

    def test_a_covered_request_is_a_runtime_defect_and_never_reaches_her(self) -> None:
        from codex_autopilot.lifecycle_reservations import pipeline_engineer_package
        from codex_autopilot.rules import violation_counts

        incident_id = self._worker_asks(
            self._payload(["codex-autopilot", "status", "--project", str(self.root)], cwd=str(self.root))
        )

        # The run was never armed here: the record is backfilled, not skipped.
        record = self.store.load().durable_authorization
        self.assertEqual(record["granted_by"], "backfill_at_first_request")
        ticket = self.ticket(incident_id)
        self.assertEqual(
            ticket["system_state"]["covered_by"],
            {"operation": "autopilot_cli_in_project", "version": record["version"]},
        )
        self.assertEqual(violation_counts(self.cfg.state_dir).get("R4"), 1)
        context = pipeline_engineer_package(self.cfg, self.store.load(), incident_id)["stop_context"]
        self.assertEqual(context["approval"]["run_authorization"], record)
        self.assertEqual(context["approval"]["covered_by"]["operation"], "autopilot_cli_in_project")

        engineer = next(item for item in reserve_ready_frontier(self.cfg) if item.kind == "pipeline_engineer")
        activate_via_app_server(self.cfg, self.root, engineer, "eng-1")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="eng-1",
            turn_id="eng-1-turn",
            final_message=(
                'AUTOPILOT_ESCALATION: {"diagnosis":"it asks for our CLI","decision_needed":"allow?",'
                '"recommendation":"allow","options":[],"scope":"task"}\n'
                "PIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER DANGEROUS_PERMISSION"
            ),
        )

        self.assertEqual(outcome.worker_status, "PROTOCOL_ERROR")
        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
        self.assertEqual(violation_counts(self.cfg.state_dir).get("R4"), 2)
        self.assertNotEqual(self.store.load().task_states["A"], "BLOCKED")

    def test_an_uncovered_request_still_goes_to_her(self) -> None:
        incident_id = self._worker_asks(self._payload(["curl", "https://example.com"], cwd=str(self.root)))
        self.assertIsNone(self.ticket(incident_id)["system_state"]["covered_by"])
        engineer = next(item for item in reserve_ready_frontier(self.cfg) if item.kind == "pipeline_engineer")
        activate_via_app_server(self.cfg, self.root, engineer, "eng-1")
        complete_desktop_worker(
            self.cfg,
            thread_id="eng-1",
            turn_id="eng-1-turn",
            final_message=(
                'AUTOPILOT_ESCALATION: {"diagnosis":"network access","decision_needed":"allow curl?",'
                '"recommendation":"replan","options":[{"code":"replan","means":"no network"}],'
                '"scope":"task"}\nPIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER DANGEROUS_PERMISSION'
            ),
        )
        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.ESCALATE_TO_USER.value)
