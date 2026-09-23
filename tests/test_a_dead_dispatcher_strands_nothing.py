"""A session whose dispatcher died is somebody's to move, whatever its status.

The independent check reproduced it on the fakes. The on-call's completion
raised before its transaction - a status line the runtime could not read,
RESOLVED declared without devops-resolve-incident - the dispatcher, which
catches only WorkerProtocolError, died, and the engineer's session stayed
ACTIVE with a dead automatic_dispatch_pid. One engineer per run then kept
every later engineer out, the status read RUNNING, and the wake-up said
"not stranded": only reservations that could be raised again counted. Even
had it said stranded, it could not lift such a session - the frontier
reserves nothing while an engineer is pending, and the revival raised only
reservations never created. A silent stop, which her requirement forbids.

Now the wake-up asks the server (thread/read, the same observation and
reconciliation as her Resume): a turn that is over is retired, and whatever
that frees - the on-call's lane above all - is reserved in the same wake.
Two engineers lost on one ticket send that ticket to her with what happened,
not to a third engineer (R23). A create in doubt has no thread to ask about;
the reservation pass settles it - a worker's gets a ticket, an engineer's,
which did no work, is retired.

Only fakes: no live Codex and no App Server is started here.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from _gates import patch_hook_trust_gates
from _plan_contract import initialize_verified_project as initialize_project
from _relay import TEST_RELAY_OWNER, reserve_ready_frontier
from codex_autopilot.blocked_runs import stop_run
from codex_autopilot.config import load_config
from codex_autopilot.lifecycle_failures import record_desktop_failure
from codex_autopilot.pipeline_engineer import IncidentPhase, PipelineIncidentStore
from codex_autopilot.run_state import StateStore
from codex_autopilot.wake import is_stranded, run_wake
from test_desktop_lifecycle import graph, task

DEAD_PID = 999_999_999


class _Base(unittest.TestCase):
    def setUp(self) -> None:
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
        self.incidents = PipelineIncidentStore(self.cfg.state_dir)
        self.spawned: list[str] = []

    def _ticket_holding_everything(self) -> str:
        """A stop that holds both tasks, so only the engineer is reserved."""

        return str(
            stop_run(
                self.cfg,
                self.store.load(),
                stop_kind="worker_blocked",
                phase="BLOCKED",
                reason="A and B wait for a repair",
                summary="s.",
                at="2026-09-23T15:02:27+00:00",
                task_ids=("A", "B"),
            )
        )

    def _engineer_dies_mid_turn(self, thread_id: str) -> str:
        """Reserve the on-call and leave it as the check found it: ACTIVE, dead pid."""

        reserved = reserve_ready_frontier(self.cfg)
        self.assertEqual([item.kind for item in reserved], ["pipeline_engineer"])
        token = reserved[0].reservation_token
        self._mark(token, status="ACTIVE", thread_id=thread_id)
        return token

    def _mark(self, token: str, **fields) -> None:
        state = self.store.load()
        session = next(item for item in state.worker_sessions if item["reservation_token"] == token)
        session.update(fields)
        session["automatic_dispatch_state"] = "RUNNING"
        session["automatic_dispatch_pid"] = DEAD_PID
        self.store.save(state)

    def _session(self, token: str) -> dict:
        return next(
            item for item in self.store.load().worker_sessions if item["reservation_token"] == token
        )

    def _engineers(self) -> list[dict]:
        return [
            item for item in self.store.load().worker_sessions if item["kind"] == "pipeline_engineer"
        ]

    def _wake(self, observed: str) -> None:
        run_wake(
            self.cfg,
            at_epoch=0,
            owner=TEST_RELAY_OWNER,
            owner_turn="turn-of-the-owner",
            now=time.time,
            sleep=lambda _seconds: None,
            spawn_relay=lambda _root, **kwargs: self.spawned.append(kwargs["reservation_token"]) or 4242,
            revive=lambda _cfg: (),
            observe=lambda _cfg: {
                str(item.get("resource_ownership_token") or item.get("reservation_token")): observed
                for item in self.store.load().worker_sessions
                if item.get("thread_id")
            },
        )


class TheWakeLiftsADeadSessionTests(_Base):
    def test_a_dead_engineer_is_retired_and_the_next_one_comes_in_the_same_wake(self) -> None:
        incident_id = self._ticket_holding_everything()
        first = self._engineer_dies_mid_turn("engineer-1")
        self.assertTrue(is_stranded(self.cfg, self.store.load()))

        self._wake("terminal")

        self.assertEqual(self._session(first)["status"], "RETRY_WAIT")
        engineers = self._engineers()
        self.assertEqual(len(engineers), 2)
        successor = engineers[-1]
        self.assertEqual(successor["incident_id"], incident_id)
        self.assertEqual(successor["status"], "CREATE_REQUESTED")
        self.assertEqual(self.spawned, [successor["reservation_token"]])

    def test_a_turn_still_running_is_not_cut_short(self) -> None:
        """"active" and "unknown" are retained, exactly as Resume retains them."""

        self._ticket_holding_everything()
        for observed in ("active", "unknown"):
            with self.subTest(observed=observed):
                first = self._engineer_dies_mid_turn("engineer-1") if observed == "active" else first
                self._wake(observed)
                self.assertEqual(self._session(first)["status"], "ACTIVE")
                self.assertEqual(len(self._engineers()), 1)
                self.assertEqual(self.spawned, [])

    def test_a_worker_whose_dispatcher_died_goes_back_to_retry(self) -> None:
        """No ticket at all: only its dead dispatcher consumed its turn."""

        worker = reserve_ready_frontier(self.cfg)[0]
        self.assertEqual(worker.kind, "implementation")
        self._mark(worker.reservation_token, status="ACTIVE", thread_id="worker-A")
        self.assertTrue(is_stranded(self.cfg, self.store.load()))

        self._wake("terminal")

        state = self.store.load()
        self.assertEqual(self._session(worker.reservation_token)["status"], "RETRY_WAIT")
        self.assertEqual(state.task_states[worker.task_id], "RETRY_WAIT")

    def test_two_lost_engineers_send_the_ticket_to_her_not_to_a_third(self) -> None:
        """R23: each round would burn her limits on the same finding."""

        incident_id = self._ticket_holding_everything()
        first = self._engineer_dies_mid_turn("engineer-1")
        self._wake("terminal")
        second = self._engineers()[-1]["reservation_token"]
        self._mark(second, status="ACTIVE", thread_id="engineer-2")
        self.spawned.clear()

        self._wake("terminal")

        self.assertEqual(
            [item["status"] for item in self._engineers()], ["RETRY_WAIT", "RETRY_WAIT"]
        )
        self.assertEqual(self.spawned, [])
        ticket = next(
            item for item in self.incidents.load()["incidents"] if item["incident_id"] == incident_id
        )
        self.assertEqual(ticket["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        report = ticket["escalation"]
        self.assertEqual(
            [item["session"] for item in report["repaired"]], [first, second]
        )
        self.assertTrue(report["recommendation"])
        self.assertFalse(is_stranded(self.cfg, self.store.load()))


class ACreateInDoubtIsSettledTests(_Base):
    def test_a_workers_create_in_doubt_gets_a_ticket_and_the_on_call(self) -> None:
        """Below the retry ceiling nothing was filed: A waited for her Resume."""

        worker = reserve_ready_frontier(self.cfg)[0]
        record_desktop_failure(
            self.cfg,
            worker.reservation_token,
            reason="create result connection lost",
            failure_code="app_server_rpc_failed",
            definitive=False,
        )
        self.assertEqual(self.incidents.load().get("incidents") or [], [])
        self.assertTrue(is_stranded(self.cfg, self.store.load()))

        reserved = reserve_ready_frontier(self.cfg)

        self.assertEqual([(item.task_id, item.kind) for item in reserved], [("A", "pipeline_engineer")])
        ticket = self.incidents.load()["incidents"][-1]
        self.assertEqual(ticket["system_state"]["stop_kind"], "lost_create")
        self.assertEqual(ticket["affected_task_ids"], ["A"])
        self.assertEqual(self._session(worker.reservation_token)["status"], "AMBIGUOUS")
        # A second pass files nothing new: the ticket holds A now.
        reserve_ready_frontier(self.cfg)
        self.assertEqual(len(self.incidents.load()["incidents"]), 1)

    def test_an_engineers_create_in_doubt_is_retired_and_frees_the_lane(self) -> None:
        """It started no turn - no thread to start one on - so it did no work."""

        incident_id = self._ticket_holding_everything()
        engineer = reserve_ready_frontier(self.cfg)[0]
        record_desktop_failure(
            self.cfg,
            engineer.reservation_token,
            reason="create result connection lost",
            failure_code="app_server_rpc_failed",
            definitive=False,
        )
        self.assertEqual(self._session(engineer.reservation_token)["status"], "AMBIGUOUS")
        self.assertTrue(is_stranded(self.cfg, self.store.load()))

        reserved = reserve_ready_frontier(self.cfg)

        self.assertEqual([item.kind for item in reserved], ["pipeline_engineer"])
        lost = self._session(engineer.reservation_token)
        self.assertEqual(lost["status"], "RETRY_WAIT")
        self.assertIn("create in doubt", lost["failure_reason"])
        self.assertEqual(self._engineers()[-1]["incident_id"], incident_id)



class TheEngineersOwnFailureTests(_Base):
    def test_a_failed_engineer_dispatch_does_not_pause_its_anchor(self) -> None:
        """Devops correction 2: the anchor is context, not a held task.

        The engineer's task is only where its thread opens; it did no work
        there. Its failed dispatch named that task as affected and paused a
        neighbour for as long as the ticket stayed open.
        """

        import contextlib
        import io

        from codex_autopilot import cli

        stop_run(
            self.cfg,
            self.store.load(),
            stop_kind="worker_blocked",
            phase="BLOCKED",
            reason="B waits for a repair",
            summary="s.",
            at="2026-09-23T15:02:27+00:00",
            task_ids=("B",),
        )
        reserved = reserve_ready_frontier(self.cfg)
        engineer = next(item for item in reserved if item.kind == "pipeline_engineer")
        worker = next(item for item in reserved if item.kind == "implementation")
        with contextlib.redirect_stdout(io.StringIO()):
            cli._record_detached_dispatch_failure(self.cfg, engineer.reservation_token, RuntimeError("boom"))
            cli._record_detached_dispatch_failure(self.cfg, worker.reservation_token, RuntimeError("boom"))
        tickets = {
            item["system_state"]["reservation_token"]: item
            for item in self.incidents.load()["incidents"]
            if item["code"] == "detached_dispatch_failed"
        }
        on_call = tickets[engineer.reservation_token]
        self.assertEqual(on_call["affected_task_ids"], [])
        self.assertEqual(on_call["context_task_id"], engineer.task_id)
        self.assertEqual(tickets[worker.reservation_token]["affected_task_ids"], [worker.task_id])


if __name__ == "__main__":
    unittest.main()
