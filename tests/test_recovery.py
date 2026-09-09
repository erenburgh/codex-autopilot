from __future__ import annotations

import json
from pathlib import Path
import unittest

from codex_autopilot.appserver import PauseRequested, TurnResult
from codex_autopilot.config import load_config
from codex_autopilot.orchestrator import DesktopOrchestrator, checkpoint_signature
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.run_state import StateStore

from test_core import FakeClient, completed_turn, make_project, update_handoff


class PauseClient(FakeClient):
    def wait_for_turn(self, *_args, **_kwargs):
        self.active = 0
        raise PauseRequested("test")


class RecoveryClient(FakeClient):
    recovered_turn = None
    def read_thread(self, _thread_id):
        return {"turns": [self.recovered_turn]}
    def start_thread(self, **_kwargs):
        raise AssertionError("completed recovered milestone must not be dispatched again")


class InitiatorClient(FakeClient):
    reads = 0
    def read_thread(self, _thread_id):
        self.reads += 1
        status = "interrupted" if self.reads == 1 else "completed"
        return {"turns": [{"id": "initiator-turn", "status": status}]}


class RecoveryTests(unittest.TestCase):
    def tearDown(self): FakeClient.instances.clear()

    def test_pause_is_recoverable_and_keeps_milestone(self):
        root = make_project("adaptive", 1)
        PauseClient.root = root
        PauseClient.statuses = []
        code = DesktopOrchestrator(load_config(root), client_factory=PauseClient).run()
        state = StateStore(root / ".codex-autopilot").load()
        self.assertEqual(code, 0)
        self.assertEqual(state.status, "PAUSED")
        self.assertEqual(state.milestone_index, 0)
        self.assertEqual(len(state.previous_thread_ids), 1)

    def test_crash_after_completed_turn_processes_without_duplicate(self):
        root = make_project("adaptive", 1)
        cfg = load_config(root)
        store = StateStore(cfg.state_dir)
        state = store.load()
        before = checkpoint_signature(cfg)
        update_handoff(root, "completed before crash")
        state.status = "RUNNING"
        state.phase = "RUNNING_TURN"
        state.worker_sequence = 1
        state.attempt = 1
        state.current_thread_id = "existing-thread"
        state.current_turn_id = "existing-turn"
        state.client_user_message_id = "client-id"
        state.checkpoint_before = before
        state.memory_audit_before = ProjectMemory(root).audit_highwater()
        from codex_autopilot.orchestrator import build_worker_prompt
        from codex_autopilot.plan import load_plan
        import hashlib
        state.selected_reasoning = "medium"
        state.prompt_sha256 = hashlib.sha256(build_worker_prompt(cfg, state, load_plan(cfg.state_dir, cfg.profile)).encode()).hexdigest()
        store.save(state)
        ProjectMemory(root).record_evidence(kind="file", summary="Recovered worker verified ROADMAP.md", path="ROADMAP.md", milestone_id="M1", created_by="worker-recovery")
        turn = completed_turn("DONE", "existing-turn")
        turn["items"].insert(0, {"type": "userMessage", "clientId": "client-id"})
        RecoveryClient.recovered_turn = turn
        RecoveryClient.root = root
        self.assertEqual(DesktopOrchestrator(cfg, client_factory=RecoveryClient).run(), 0)
        final = store.load()
        self.assertEqual(final.status, "DONE")
        self.assertEqual(final.previous_thread_ids, ["existing-thread"])

    def test_worker_starts_only_after_initiator_completed(self):
        root = make_project("adaptive", 1)
        InitiatorClient.root = root
        InitiatorClient.statuses = ["DONE"]
        InitiatorClient.reads = 0
        code = DesktopOrchestrator(load_config(root), client_factory=InitiatorClient, sleep_fn=lambda _seconds: None).run("initiator-thread", "initiator-turn")
        self.assertEqual(code, 0)
        self.assertGreaterEqual(InitiatorClient.instances[-1].reads, 2)
        self.assertEqual(InitiatorClient.instances[-1].events[0][0], "thread/start")


if __name__ == "__main__": unittest.main()
