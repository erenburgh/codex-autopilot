from __future__ import annotations

from pathlib import Path
import os
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
    def setUp(self):
        self.old_runtime = os.environ.get("CODEX_AUTOPILOT_RUNTIME")
        os.environ["CODEX_AUTOPILOT_RUNTIME"] = "/runtime/codex-autopilot"

    def tearDown(self):
        if self.old_runtime is None:
            os.environ.pop("CODEX_AUTOPILOT_RUNTIME", None)
        else:
            os.environ["CODEX_AUTOPILOT_RUNTIME"] = self.old_runtime

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
        transport = client.calls[-1][1]["config"]["mcp_servers"]["codex_autopilot_memory"]
        self.assertEqual(transport["command"], "/runtime/codex-autopilot")
        self.assertEqual(transport["args"], ["memory-mcp"])
        self.assertEqual(transport["cwd"], "/project")
        self.assertTrue(transport["enabled"])
        self.assertEqual(transport["startup_timeout_sec"], 10)
        self.assertEqual(transport["tool_timeout_sec"], 30)
        self.assertEqual(set(transport["tools"]), {"memory"})
        self.assertEqual(
            {value["approval_mode"] for value in transport["tools"].values()},
            {"prompt"},
        )
        self.assertEqual(set(client.calls[-1][1]), {"cwd", "permissions", "ephemeral", "config"})

    def test_adaptive_thread_start_sends_only_resolved_model(self):
        client = CaptureClient()
        client.start_thread(cwd=Path("/project"), permission_profile=":workspace", project_id=None, model="gpt-5.6-sol")
        params = client.calls[-1][1]
        self.assertEqual(params["model"], "gpt-5.6-sol")
        self.assertEqual(set(params), {"cwd", "permissions", "ephemeral", "config", "model"})

    def test_preflight_thread_is_ephemeral_and_project_scoped(self):
        client = CaptureClient()
        client.start_thread(cwd=Path("/target"), permission_profile=":workspace", project_id=None, model=None, ephemeral=True)
        params = client.calls[-1][1]
        self.assertTrue(params["ephemeral"])
        self.assertEqual(params["config"]["mcp_servers"]["codex_autopilot_memory"]["cwd"], "/target")

    def test_model_list_uses_capability_metadata_api(self):
        client = CaptureClient()
        self.assertEqual(client.list_models(), [])
        self.assertEqual(client.calls[-1], ("model/list", {"includeHidden": True, "limit": 100}))

    def test_mcp_tool_uses_current_app_server_method(self):
        client = CaptureClient()
        client.call_mcp_tool("thread", "codex_autopilot_memory", "memory", {"operation": "current"})
        self.assertEqual(client.calls[-1], ("mcpServer/tool/call", {"threadId": "thread", "server": "codex_autopilot_memory", "tool": "memory", "arguments": {"operation": "current"}}))

    def test_turn_interrupt_uses_exact_worker_ids(self):
        client = CaptureClient()
        client.interrupt_turn("thread", "turn")
        self.assertEqual(client.calls[-1], ("turn/interrupt", {"threadId": "thread", "turnId": "turn"}))


if __name__ == "__main__": unittest.main()
