from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def git_project() -> Path:
    root = Path(tempfile.mkdtemp(prefix="codex-autopilot-mcp-test-"))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def run_server(root: Path, messages: list[dict]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "codex_autopilot.memory_mcp", "--project", str(root)],
        input="".join(json.dumps(item) + "\n" for item in messages),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


class McpTests(unittest.TestCase):
    def test_stdio_initialize_tools_and_project_binding(self):
        root = git_project()
        result = run_server(
            root,
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "memory", "arguments": {"operation": "current"}}},
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "codex-autopilot-project-memory")
        self.assertEqual(len(responses[1]["result"]["tools"]), 1)
        tools = {item["name"]: item for item in responses[1]["result"]["tools"]}
        self.assertFalse(tools["memory"]["annotations"]["readOnlyHint"])
        self.assertTrue(all(not item["annotations"]["openWorldHint"] for item in tools.values()))
        self.assertTrue(tools["memory"]["annotations"]["destructiveHint"])
        branches = tools["memory"]["inputSchema"]["oneOf"]
        actions = {branch["properties"]["operation"]["const"] for branch in branches}
        self.assertEqual(len(actions), 16)
        self.assertIn("record_verified_fact", actions)
        identity = responses[2]["result"]["structuredContent"]
        self.assertEqual(Path(identity["project_root"]), root.resolve())
        self.assertFalse(identity["initialized"])

    def test_plugin_requests_supported_persistent_approval_mode(self):
        for profile in ("codex-autopilot-adaptive", "codex-autopilot-host-settings"):
            config = json.loads((ROOT / "plugins" / profile / ".mcp.json").read_text(encoding="utf-8"))
            tool = config["mcpServers"]["codex_autopilot_memory"]["tools"]["memory"]
            self.assertEqual(tool["approval_mode"], "auto")

    def test_server_restarts_cleanly_after_process_exit(self):
        root = git_project()
        message = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        first = run_server(root, message)
        second = run_server(root, message)
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(json.loads(second.stdout)["result"]["serverInfo"]["version"], "0.8.2-beta")

    def test_unknown_arguments_are_rejected_without_sql_execution(self):
        root = git_project()
        result = run_server(
            root,
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory", "arguments": {"operation": "search", "query": "x", "raw_sql": "DROP TABLE records"}}}],
        )
        response = json.loads(result.stdout)
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("unknown argument", response["error"]["message"])


if __name__ == "__main__":
    unittest.main()
