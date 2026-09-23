"""An unreadable verdict goes back to the verifier instead of killing the dispatcher.

Measured on a live run: the verifier attached a `rubric` field to the
verdict - the department rubric the previous task itself had created.
The turn completed successfully, 107 items, and the dispatcher died
parsing the reply. The work stayed done, acceptance was not recorded, a
ticket about the dispatcher crash opened on top, and the run stood for
an hour and a half.

The same class is already closed for the replanner: an invalid model
reply is a model error, not a runtime breakdown.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from _gates import patch_hook_trust_gates
from _relay import reserve_ready_frontier
from _handoff import bump_task_checkpoint
from _plan_contract import initialize_verified_project as initialize_project
from codex_autopilot.config import load_config
from codex_autopilot.lifecycle_completion import complete_desktop_worker
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.run_state import StateStore
from test_desktop_lifecycle import graph, task


VERDICT = 'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[],"rubric":"runtime"}'


class VerifierProtocolRejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        raw = graph(max_workers=1)
        raw["tasks"] = [task("A", path="src/a")]
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
        self.memory = ProjectMemory(self.root)

    def evidence(self, task_id: str, label: str, role: str) -> None:
        self.memory.record_evidence(
            kind="test",
            summary=f"Свидетельство: {label}.",
            created_by="verifier-protocol-test",
            milestone_id=task_id,
            role=role,
            command=f"check {label}",
            result="PASS",
            exit_code=0,
        )

    def mark_active(self, token: str, thread_id: str) -> None:
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == token
        )
        session["thread_id"] = thread_id
        session["status"] = "ACTIVE"
        self.store.save(state)

    def reach_verification(self) -> str:
        descriptor = reserve_ready_frontier(
            self.cfg, relay_owner_thread_id="owner", hook_gate=lambda _cfg: None
        )[0]
        self.mark_active(descriptor.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, "A", "Работа сделана.")
        self.evidence("A", "реализация", "implementation")
        verifier = complete_desktop_worker(
            self.cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message="итог\nAUTOPILOT_STATUS: DONE",
            hook_gate=lambda _cfg: None,
        ).descriptors[0]
        self.mark_active(verifier.reservation_token, "verifier-A")
        return verifier.reservation_token

    def test_an_unreadable_verdict_returns_to_a_fresh_verifier(self) -> None:
        self.reach_verification()
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="verifier-A",
            turn_id="turn-V",
            final_message="разбор\n" + VERDICT,
            hook_gate=lambda _cfg: None,
        )
        self.assertEqual(outcome.worker_status, "VERIFICATION_REJECTED")
        state = self.store.load()
        # Acceptance counted in no direction: the task is neither accepted nor
        # sent for revision. A fresh verifier rose at once, so the state
        # is VERIFYING again - but with a new session.
        self.assertNotEqual(state.task_states["A"], "VERIFIED")
        self.assertNotEqual(state.task_states["A"], "REVISION_REQUIRED")
        self.assertEqual(state.task_states["A"], "VERIFYING")
        self.assertEqual(
            self.store.load().worker_sessions[-1]["kind"], "verifier"
        )
        self.assertIn("rubric", state.verification_rejections["A"][-1]["reason"])
        self.assertTrue(outcome.descriptors, "a fresh verifier must come up")

    def test_the_reason_reaches_the_next_verifier(self) -> None:
        self.reach_verification()
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="verifier-A",
            turn_id="turn-V",
            final_message="разбор\n" + VERDICT,
            hook_gate=lambda _cfg: None,
        )
        prompt = outcome.descriptors[0].prompt
        self.assertIn("rubric", prompt)
        self.assertIn("verdict", prompt)

    def test_three_unreadable_verdicts_stop_the_run_loudly(self) -> None:
        token = self.reach_verification()
        outcome = None
        for attempt in range(3):
            if attempt:
                self.mark_active(token, f"verifier-{attempt}")
            outcome = complete_desktop_worker(
                self.cfg,
                thread_id="verifier-A" if not attempt else f"verifier-{attempt}",
                turn_id=f"turn-V{attempt}",
                final_message="разбор\n" + VERDICT,
                hook_gate=lambda _cfg: None,
            )
            if outcome.descriptors[0].kind != "verifier":
                break
            token = outcome.descriptors[0].reservation_token
        # The task stops and the on-call is reserved in the same completion.
        # It used to return nothing and set the run BLOCKED, which the
        # wake-up skipped: nobody ever came.
        self.assertEqual([item.kind for item in outcome.descriptors], ["pipeline_engineer"])
        state = self.store.load()
        self.assertEqual(state.status, "RUNNING")
        self.assertEqual(state.task_states["A"], "BLOCKED")
        self.assertIn("rubric", str(state.last_error))
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        ticket = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"][-1]
        self.assertEqual(ticket["code"], "run_stopped:VERIFICATION_PROTOCOL_BLOCKED")
        self.assertEqual(ticket["phase"], "PIPELINE_ENGINEER")
        self.assertEqual(ticket["system_state"]["stop_kind"], "verification_protocol")


if __name__ == "__main__":
    unittest.main()
