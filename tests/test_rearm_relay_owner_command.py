"""The DevOps re-arm command is executed, not only its tail function.

Measured gap: deleting ``**settled,`` from the answer of
``reactivate_desktop_relay_owner`` (control.py) left all 995 tests green.
``tests/test_rearm_launch_verdict.py`` calls ``_settle_rearmed_launch``
directly, and nothing at all called the command that owns it - it was
imported by the lifecycle suite and never invoked. So the whole B2 fix
lived outside the suite's reach: the command could go back to reporting
"the relay was re-armed" with no word on whether the launch was
confirmed, still in progress, or broken, and nothing would turn red.

Executing the command for the first time also showed that it could not
run at all: it reached for ``lifecycle.app_server_creation_contract``,
which the lifecycle facade did not re-export, and died with
AttributeError before any settlement. That is fixed alongside this test;
the test is what keeps the whole path executed from now on.

Only two things are substituted, because both leave the machine and
neither is under test: spawning the real dispatcher process, and the
fifteen-second launch observation window. The incident store, the run
state, the reservation and the settlement itself are real.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _appserver_fakes import FakeAppServerCreateClient, activate_via_app_server
from _gates import patch_hook_trust_gates
from _handoff import bump_task_checkpoint
from _plan_contract import (
    attested_verdict,
    TEST_OUTCOME_ID,
    canonical_verification,
    canonicalize_plan,
    initialize_verified_project as initialize_project,
)
from _relay import reserve_ready_frontier

from codex_autopilot import control
from codex_autopilot.config import load_config
from codex_autopilot.launch_gate import LaunchCheck
from codex_autopilot.lifecycle import (
    DesktopLifecycleError,
    complete_desktop_worker,
    create_desktop_thread_via_app_server,
)
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.pipeline_engineer import (
    HealthcheckResult,
    IncidentPhase,
    PipelineIncidentStore,
)
from codex_autopilot.run_state import StateStore

OWNER_THREAD = "owner-thread"
DESTINATION = "B"


def _check(check_id: str, passed: bool | None) -> LaunchCheck:
    return LaunchCheck(check_id, DESTINATION, passed, f"{check_id}={passed}")


# A just re-armed relay: the reservation exists, the dispatcher is alive,
# nothing refused, and no thread exists yet - the predecessor executes the
# turn on its NEXT Stop, so none can appear inside the waiting window.
IN_PROGRESS = (
    _check("reserved", True),
    _check("thread_bound", False),
    _check("created_in_project", False),
    _check("send_acknowledged", False),
    _check("launch_report_written", False),
    _check("visible_in_desktop", None),
    _check("dispatcher_alive", True),
    _check("no_failure_after_launch", True),
)

CONFIRMED = tuple(_check(item.id, True) for item in IN_PROGRESS)


def _task(task_id: str, *, depends_on: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Complete {task_id}.",
        "definition_of_done": [f"{task_id} is verified."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files and tests are sufficient.",
        "reasoning": "medium",
        "role": "builder",
        "depends_on": list(depends_on),
        "priority": 0,
        "verification": canonical_verification(),
        "resources": [],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
        "produces_outcomes": [TEST_OUTCOME_ID],
        "acceptance_class": "mixed",
    }


def _graph() -> dict[str, object]:
    """A -> B on one worker: B's relay owner can only be A's worker thread."""

    return canonicalize_plan({
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Exercise the DevOps re-arm of a known-failed create.",
        "user_request": "Exercise the DevOps re-arm of a known-failed create.",
        "model_strategy": "auto",
        "execution_strategy": "parallel",
        "max_parallel_workers": 1,
        "computer_use_slots": 1,
        "roles": [
            {
                "id": "builder",
                "name": "Builder",
                "responsibilities": ["Implement and verify one task."],
            }
        ],
        "tasks": [_task("A"), _task(DESTINATION, depends_on=("A",))],
    })


class RearmRelayOwnerCommandTests(unittest.TestCase):
    """Drive ``devops-rearm-relay-owner`` over a real known-failed create."""

    # Whether the engineer closes the ticket before the command runs. Both
    # states are live: the command accepts an incident in PIPELINE_ENGINEER
    # and one already RESOLVED (control.py, the phase filter).
    RESOLVE_BEFORE_REARM = True

    def setUp(self) -> None:
        # The trust gate reads the developer machine's real App Server.
        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(_graph()), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.root / ".codex-autopilot")
        self.memory = ProjectMemory(self.root)
        self.incidents = PipelineIncidentStore(self.cfg.state_dir)
        self._finish_task_a()
        self.destination = self._fail_the_create_for_destination()
        self.incident_id = self._find_the_incident()
        if self.RESOLVE_BEFORE_REARM:
            self._resolve_the_incident()

    def _evidence(self, task_id: str) -> None:
        bump_task_checkpoint(self.root, task_id, f"Completed: {task_id}")
        self.memory.record_evidence(
            kind="test",
            summary=f"{task_id} lifecycle verification passed.",
            created_by="rearm-command-test",
            milestone_id=task_id,
            command=f"verify {task_id}",
            result="PASS",
            exit_code=0,
        )

    def _finish_task_a(self) -> None:
        """A real predecessor: the command refuses an invented causal owner."""

        first = reserve_ready_frontier(
            self.cfg,
            relay_owner_thread_id="initiator-thread",
        )[0]
        activate_via_app_server(self.cfg, self.root, first, OWNER_THREAD)
        self._evidence("A")
        implementation = complete_desktop_worker(
            self.cfg,
            thread_id=OWNER_THREAD,
            turn_id="owner-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        verifier = implementation.descriptors[0]
        activate_via_app_server(self.cfg, self.root, verifier, "verifier-thread")
        self._evidence("A")
        accepted = complete_desktop_worker(
            self.cfg,
            thread_id="verifier-thread",
            turn_id="verifier-turn",
            final_message=attested_verdict(self.cfg, "A"),
        )
        self.reservation = accepted.descriptors[0]
        self.assertEqual(self.reservation.task_id, DESTINATION)

    def _fail_the_create_for_destination(self) -> str:
        """thread/start is definitively refused: the side effect is known failed.

        The incident, its payload hash and the RETRY_WAIT reservation are
        written by the runtime here, not assembled by hand - the command
        checks all three against each other and refuses on any mismatch.
        ``now_epoch=0`` puts the retry backoff in the past.
        """

        client = FakeAppServerCreateClient(
            self.root,
            [],
            thread_id="never-created",
            fail_create=True,
        )
        with mock.patch(
            "codex_autopilot.lifecycle_dispatch.installed_plugin_root",
            return_value=self.root,
        ):
            with self.assertRaises(DesktopLifecycleError):
                create_desktop_thread_via_app_server(
                    self.cfg,
                    self.reservation.reservation_token,
                    client_factory=lambda *_args: client,
                    relay_executor_thread_id=OWNER_THREAD,
                    now_epoch=0,
                )
        state = self.store.load()
        self.assertEqual(state.task_states[DESTINATION], "RETRY_WAIT")
        self.assertEqual(state.active_task_ids, [])
        return self.reservation.reservation_token

    def _find_the_incident(self) -> str:
        """The ticket the failed create opened, still held by the engineer."""

        incident = next(
            item
            for item in self.incidents.load()["incidents"]
            if item.get("operation") == "create_thread"
        )
        self.assertEqual(incident["side_effect_outcome"], "KNOWN_FAILED")
        self.assertEqual(
            incident["phase"], IncidentPhase.PIPELINE_ENGINEER.value
        )
        return str(incident["incident_id"])

    def _resolve_the_incident(self) -> str:
        """The engineer closes the ticket the way the store demands: by name."""

        incident_id = self.incident_id
        self.assertEqual(
            self.incidents.complete_pipeline_engineer(
                incident_id,
                success=True,
                at="2026-09-18T00:00:00+00:00",
                actions=("rearm_relay_owner",),
                healthcheck=HealthcheckResult(
                    name="known_failed_create_reconciled",
                    passed=True,
                    checks=("create_side_effect_known_failed_and_unbound",),
                    observed_at="2026-09-18T00:00:00+00:00",
                ),
                reason="The known-failed create was reconciled.",
            ),
            IncidentPhase.RESOLVED,
        )
        return incident_id

    def rearm(self, checks) -> dict:
        with mock.patch(
            "codex_autopilot.control.spawn_automatic_app_server_relay",
            return_value=4242,
        ) as spawn, mock.patch(
            "codex_autopilot.control.await_launch",
            return_value=checks,
        ) as observe:
            answer = control.reactivate_desktop_relay_owner(self.root)
        # Both substitutions are on the command's real tail: if it stopped
        # before them, the settlement would not have been reached either.
        self.assertEqual(
            spawn.call_args.kwargs["initiator_thread_id"], OWNER_THREAD
        )
        self.assertEqual(observe.call_args.kwargs["task_ids"], [DESTINATION])
        return answer

    def phase(self) -> str:
        return str(
            self.incidents.incident_package(self.incident_id)["incident"]["phase"]
        )

    def test_a_launch_still_in_progress_is_reported_as_in_progress(self) -> None:
        """The command must not call a re-arm done while the launch is open."""

        answer = self.rearm(IN_PROGRESS)
        self.assertEqual(answer["incident_id"], self.incident_id)
        self.assertEqual(answer["owner_thread_id"], OWNER_THREAD)
        self.assertEqual(answer["destination_task_id"], DESTINATION)
        self.assertEqual(answer["status"], "LAUNCH_IN_PROGRESS")
        self.assertEqual(answer["launch_verdict"], "IN_PROGRESS")
        self.assertIs(answer["launch_confirmed"], False)
        # R26: what is not yet observable is named, not swallowed.
        self.assertIn("thread_bound", answer["pending_checks"])
        self.assertIn("visible_in_desktop", answer["pending_checks"])
        self.assertNotIn("reserved", answer["pending_checks"])
        self.assertEqual(self.phase(), IncidentPhase.RESOLVED.value)

    def test_a_confirmed_launch_is_reported_as_rearmed(self) -> None:
        answer = self.rearm(CONFIRMED)
        self.assertEqual(answer["status"], "REARMED")
        self.assertEqual(answer["launch_verdict"], "CONFIRMED")
        self.assertIs(answer["launch_confirmed"], True)
        self.assertEqual(answer["pending_checks"], [])
        self.assertEqual(self.phase(), IncidentPhase.RESOLVED.value)


if __name__ == "__main__":
    unittest.main()


class RearmWhileTheEngineerStillHoldsTheTicketTests(RearmRelayOwnerCommandTests):
    """The phase the command exists for: the ticket is not closed yet.

    Measured on the merge seam. The re-arm closes the ticket itself when it
    finds it in PIPELINE_ENGINEER - and it closed it with no named action.
    On this branch a resolution without one is refused
    (engineer_authority.REPAIR_ACTIONS, pipeline_engineer._require_named_actions),
    a gate that main's command never had to pass. So the command raised
    PipelineIncidentError in exactly the state it was written for, while the
    already-resolved state - the one the tests above cover - went through.
    """

    RESOLVE_BEFORE_REARM = False

    def test_the_command_closes_the_ticket_naming_what_it_did(self) -> None:
        answer = self.rearm(CONFIRMED)
        self.assertEqual(answer["status"], "REARMED")
        self.assertEqual(self.phase(), IncidentPhase.RESOLVED.value)
        # The repair is recorded by name, where a repeated one is counted
        # towards a runbook - prose would be counted as nothing.
        signatures = self.incidents.load()["signatures"]
        resolutions = [
            item
            for entry in signatures.values()
            for item in entry.get("resolutions", [])
        ]
        self.assertEqual(
            [item["actions"] for item in resolutions], [["rearm_relay_owner"]]
        )


_PLAIN_GRAPH = _graph


def _staged_graph() -> dict[str, object]:
    """B writes a file through a directory resource: its work is staged."""

    graph = _PLAIN_GRAPH()
    for task in graph["tasks"]:
        if task["id"] == DESTINATION:
            task["resources"] = [{"id": "src", "kind": "directory", "target": "src", "access": "write"}]
            task["outputs"] = [{"id": "out", "description": "B's file.", "path": "src/b.txt", "required": True}]
    return graph


class RearmAStagedDestinationTests(RearmRelayOwnerCommandTests):
    """The on-call's relay repair for a staged task, under both placement contracts.

    control.py required the re-derived contract to have ``cwd == root`` and
    ``runtimeWorkspaceRoots == [root]``. A staged task's thread had its
    workspace as cwd (contract 1) and, under contract 2, the workspace as
    its only root - so the repair was refused for every staged worker,
    verifier and revision (the independent check). Mutation: the old
    condition back in control.py - both tests here raise "does not match
    canonical project metadata".
    """

    def setUp(self) -> None:
        with mock.patch(f"{__name__}._graph", _staged_graph):
            super().setUp()

    def _prove_isolation(self) -> None:
        from codex_autopilot.isolation_probe import binary_identity, write_record

        write_record(self.cfg.state_dir, {
            "root": str(self.cfg.root), "permission_profile": self.cfg.desktop.permission_profile,
            "codex_binary": binary_identity(self.cfg.desktop.binary), "outcome": "PASS",
        })

    def test_a_staged_destination_is_rearmed_under_contract_one(self) -> None:
        from codex_autopilot.lifecycle import app_server_creation_contract

        params = app_server_creation_contract(self.cfg, self.reservation)["params"]
        self.assertNotEqual(Path(self.reservation.cwd), self.cfg.root)
        self.assertEqual(params["cwd"], self.reservation.cwd)
        self.assertEqual(self.rearm(CONFIRMED)["status"], "REARMED")

    def test_a_staged_destination_is_rearmed_under_contract_two(self) -> None:
        from codex_autopilot.lifecycle import app_server_creation_contract

        self._prove_isolation()
        params = app_server_creation_contract(self.cfg, self.reservation)["params"]
        self.assertEqual((Path(params["cwd"]), params["runtimeWorkspaceRoots"]),
                         (self.cfg.root, [self.reservation.cwd]))
        self.assertEqual(self.rearm(CONFIRMED)["status"], "REARMED")
