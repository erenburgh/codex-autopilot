"""The same stop, repeated, ends at the owner with a report - never a loop.

R3 made an infrastructure stop HOLD its task instead of blocking it: the
on-call repairs, closes its ticket, and the task takes up the same action
again. That removed the only bound the old path had. Before, the task went
BLOCKED, the closed ticket left it an orphan, and the orphan sweep sent the
third ticket to the owner. After, the independent check drove the real path
six rounds - a worker answering ``BLOCKED ENVIRONMENT_FAILURE``, the on-call
closing its ticket through devops-resolve-incident - and got six tickets,
six engineers, the task back in READY each time, and not one signal to her.

R23: N identical attempts stop the loop and produce a report instead of the
next attempt. So the door now counts: the same stop of the same task with
the same reason, closed twice by the on-call without going away, is not a
third engineer's job. The third ticket goes to her as RECOVERY_EXHAUSTED
with the signature, the count and what each closure did; its tasks are
BLOCKED. Her own answer is a change of premises and starts the count again.

The plan gate had the mirror image of that loop: after its ticket went to
her, every engineer completion saw "a READY task and no session" - the
tasks her ticket holds - filed a NO_SUCCESSOR ticket and called another
engineer, forever, with the run reading RUNNING.

Every test here drives production: complete_desktop_worker for the worker
and the engineer, devops-resolve-incident's store call for the closure.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_autopilot.pipeline_engineer import (
    HealthcheckResult,
    IncidentPhase,
    PipelineIncidentStore,
    _expected_healthcheck,
)
from codex_autopilot.run_state import utc_now


class _Run(unittest.TestCase):
    TASKS = ("A",)

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
        raw["tasks"] = [task(name, path=f"src/{name.lower()}") for name in self.TASKS]
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
        self._threads = 0

    def _thread(self, prefix: str) -> str:
        self._threads += 1
        return f"{prefix}-{self._threads}"

    def _run(self, descriptor, final_message: str):
        from _appserver_fakes import activate_via_app_server
        from _handoff import bump_task_checkpoint
        from codex_autopilot.lifecycle import complete_desktop_worker

        thread = self._thread(descriptor.kind)
        activate_via_app_server(self.cfg, self.root, descriptor, thread)
        if descriptor.kind != "pipeline_engineer":
            bump_task_checkpoint(self.root, descriptor.task_id, f"Turn of {thread}.")
        return complete_desktop_worker(
            self.cfg, thread_id=thread, turn_id=f"{thread}-turn", final_message=final_message
        )

    def _engineer_closes(self, descriptor):
        """devops-resolve-incident, then the engineer's own final line."""

        session = next(
            item
            for item in self.store.load().worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        incident_id = session["incident_id"]
        record = next(
            item for item in self.incidents.load()["incidents"] if item["incident_id"] == incident_id
        )
        self.incidents.complete_pipeline_engineer(
            incident_id,
            success=True,
            # Not a replayable action: a learned runbook would change the
            # route, and the loop under test is the engineer's.
            actions=("repair_runtime_code",),
            at=utc_now(),
            healthcheck=HealthcheckResult(
                name=_expected_healthcheck(record) or "stop_repaired",
                passed=True,
                checks=("the environment answers",),
                observed_at=utc_now(),
            ),
            note=f"repaired round for {incident_id}",
        )
        return self._run(descriptor, "PIPELINE_ENGINEER_STATUS: RESOLVED")

    @staticmethod
    def _pick(outcome, kind: str):
        return next((item for item in outcome.descriptors if item.kind == kind), None)

    def _tickets(self, stop_kind: str | None = None) -> list[dict]:
        return [
            item
            for item in self.incidents.load()["incidents"]
            if stop_kind is None or item["system_state"].get("stop_kind") == stop_kind
        ]


class TheSameHeldStopIsBoundedTests(_Run):
    def _loop(self, rounds: int, code: str = "ENVIRONMENT_FAILURE"):
        from _relay import reserve_ready_frontier

        worker = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")[0]
        seen = []
        for _ in range(rounds):
            self.assertEqual((worker.task_id, worker.kind), ("A", "implementation"))
            stopped = self._run(worker, f"итог\nAUTOPILOT_STATUS: BLOCKED {code}")
            seen.append(stopped)
            engineer = self._pick(stopped, "pipeline_engineer")
            if engineer is None:
                return seen
            worker = self._pick(self._engineer_closes(engineer), "implementation")
            if worker is None:
                return seen
        return seen

    def test_the_third_identical_stop_goes_to_the_owner_with_a_report(self) -> None:
        """Measured before the fix: 6 rounds, 6 RESOLVED tickets, A READY, no signal."""

        seen = self._loop(6)
        self.assertEqual(len(seen), 3, "the loop did not stop at the third identical stop")
        tickets = self._tickets("worker_blocked")
        self.assertEqual(
            [item["phase"] for item in tickets],
            [IncidentPhase.RESOLVED.value, IncidentPhase.RESOLVED.value, IncidentPhase.ESCALATE_TO_USER.value],
        )
        last = tickets[-1]
        self.assertEqual(last["escalation_reason"], "RECOVERY_EXHAUSTED")
        # The report R23 asks for: the signature, the count, what changed.
        report = last["escalation"]
        self.assertIn("ENVIRONMENT_FAILURE", report["diagnosis"])
        self.assertIn("2", report["diagnosis"])
        self.assertIn("repaired round for", json.dumps(report["repaired"]))
        self.assertTrue(report["decision_needed"])
        self.assertTrue(report["recommendation"])
        state = self.store.load()
        self.assertEqual(state.task_states["A"], "BLOCKED")
        self.assertEqual((state.status, state.phase), ("BLOCKED", "AWAITING_OWNER"))
        self.assertNotIn(
            "pipeline_engineer",
            [item.kind for item in seen[-1].descriptors],
            "a third engineer was called for the same finding",
        )

    def test_her_answer_starts_the_count_again(self) -> None:
        """Her decision is a change of premises (R23): the on-call comes again."""

        import contextlib
        import io

        from _relay import reserve_ready_frontier
        from codex_autopilot.cli import main
        from codex_autopilot.control import _answer_escalation

        self._loop(6)
        # Her answer as production takes it: unblock the task, then Resume
        # closes what waits for her. The wake-up her answer raises is a real
        # detached process; left unpatched it wrote its log into the state
        # directory while the temporary root was being removed, and the
        # test failed at cleanup about one run in five ("Directory not
        # empty"), at HEAD as well.
        with mock.patch("codex_autopilot.wake._spawn_wake", return_value=4242), \
             contextlib.redirect_stdout(io.StringIO()):
            main(["unblock", "--project", str(self.root), "--task", "A", "--reason", "the key is in place"])
        _answer_escalation(self.cfg, self.store.load())
        worker = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-2")[0]
        stopped = self._run(worker, "итог\nAUTOPILOT_STATUS: BLOCKED ENVIRONMENT_FAILURE")
        self.assertIsNotNone(self._pick(stopped, "pipeline_engineer"))
        self.assertEqual(self._tickets("worker_blocked")[-1]["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
        self.assertEqual(self.store.load().task_states["A"], "READY")

    def test_a_different_reason_is_a_different_signature(self) -> None:
        from _relay import reserve_ready_frontier

        worker = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")[0]
        for code in ("ENVIRONMENT_FAILURE", "MISSING_RESOURCE", "ENVIRONMENT_FAILURE"):
            stopped = self._run(worker, f"итог\nAUTOPILOT_STATUS: BLOCKED {code}")
            engineer = self._pick(stopped, "pipeline_engineer")
            self.assertIsNotNone(engineer, f"{code} did not call the on-call")
            worker = self._pick(self._engineer_closes(engineer), "implementation")
        # Three stops, but no signature closed twice: A is back at work.
        self.assertEqual((worker.task_id, worker.kind), ("A", "implementation"))
        self.assertEqual(
            {item["phase"] for item in self._tickets("worker_blocked")}, {IncidentPhase.RESOLVED.value}
        )


class TheOwnersTicketIsNotANoSuccessorTests(_Run):
    TASKS = ("A", "B")

    def test_the_engineer_does_not_spin_while_the_plan_waits_for_her(self) -> None:
        """Measured before the fix: plan_unverified x2, x1 to her, then
        no_successor RESOLVED x5 and one more engineer, status RUNNING."""

        from _relay import reserve_ready_frontier

        state = self.store.load()
        state.plan_verification = None
        self.store.save(state)
        engineer = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")[0]
        rounds = 0
        while engineer is not None and rounds < 7:
            rounds += 1
            self.assertEqual(engineer.kind, "pipeline_engineer")
            engineer = self._pick(self._engineer_closes(engineer), "pipeline_engineer")
        self.assertEqual(rounds, 2)
        self.assertEqual(self._tickets("no_successor"), [])
        gate = self._tickets("plan_unverified")
        self.assertEqual(
            [item["phase"] for item in gate],
            [IncidentPhase.RESOLVED.value, IncidentPhase.RESOLVED.value, IncidentPhase.ESCALATE_TO_USER.value],
        )
        state = self.store.load()
        self.assertEqual((state.status, state.phase), ("BLOCKED", "AWAITING_OWNER"))
        self.assertFalse(
            [item for item in state.worker_sessions if item["status"] in {"CREATE_REQUESTED", "ACTIVE"}]
        )

    def test_a_ready_task_nobody_holds_is_still_a_no_successor(self) -> None:
        """The NO_SUCCESSOR ticket stays for what it was made for."""

        from codex_autopilot.engineer_escalation import _would_idle_forever

        state = self.store.load()
        self.assertTrue(any(value == "READY" for value in state.task_states.values()))
        self.assertTrue(_would_idle_forever(self.cfg, state))


if __name__ == "__main__":
    unittest.main()
