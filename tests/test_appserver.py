from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.appserver import AppServerClient


class CaptureClient(AppServerClient):
    def __init__(self):
        super().__init__("codex", Path(tempfile.mktemp()))
        self.calls = []
    def request(self, method, params, timeout=60):
        self.calls.append((method, params))
        return {"turn": {"id": "turn"}}


class AppServerTests(unittest.TestCase):
    def test_adaptive_turn_has_explicit_skill_and_effort(self):
        client = CaptureClient()
        client.start_turn(thread_id="t", prompt="p", effort="high", client_user_message_id="c", skill_name="codex-autopilot-adaptive", skill_path=Path("/skill/SKILL.md"))
        params = client.calls[-1][1]
        self.assertEqual(params["effort"], "high")
        self.assertEqual(params["input"][1], {"type": "skill", "name": "codex-autopilot-adaptive", "path": "/skill/SKILL.md"})

    def test_host_turn_omits_effort_key(self):
        client = CaptureClient()
        client.start_turn(thread_id="t", prompt="p", effort=None, client_user_message_id="c", skill_name="codex-autopilot-host-settings", skill_path=Path("/skill/SKILL.md"))
        self.assertNotIn("effort", client.calls[-1][1])

    def test_thread_start_only_sends_safe_allowlisted_fields(self):
        client = CaptureClient()
        client.start_thread(cwd=Path("/project"), permission_profile=":workspace", project_id=None, model=None)
        self.assertEqual(client.calls[-1], ("thread/start", {"cwd": "/project", "permissions": ":workspace", "ephemeral": False}))

    def test_adaptive_thread_start_sends_only_resolved_model(self):
        client = CaptureClient()
        client.start_thread(cwd=Path("/project"), permission_profile=":workspace", project_id=None, model="gpt-5.6-sol")
        self.assertEqual(client.calls[-1], ("thread/start", {"cwd": "/project", "permissions": ":workspace", "ephemeral": False, "model": "gpt-5.6-sol"}))

    def test_model_list_uses_capability_metadata_api(self):
        client = CaptureClient()
        self.assertEqual(client.list_models(), [])
        self.assertEqual(client.calls[-1], ("model/list", {"includeHidden": True, "limit": 100}))

    def test_turn_interrupt_uses_exact_worker_ids(self):
        client = CaptureClient()
        client.interrupt_turn("thread", "turn")
        self.assertEqual(client.calls[-1], ("turn/interrupt", {"threadId": "thread", "turnId": "turn"}))


if __name__ == "__main__": unittest.main()
