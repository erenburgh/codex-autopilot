from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from _plan_contract import canonicalize_plan, canonical_verification
from _plan_contract import initialize_verified_project as initialize_project
from codex_autopilot.memory import MemoryValidationError
from codex_autopilot.memory_mcp import MemoryMcpServer


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
        self.assertEqual(len(actions), 17)
        self.assertIn("record_verified_fact", actions)
        self.assertIn("store_department_rubric", actions)
        identity = responses[2]["result"]["structuredContent"]
        self.assertEqual(Path(identity["project_root"]), root.resolve())
        self.assertFalse(identity["initialized"])

    # The approval-mode check moved to test_memory_tool_approval.py.
    # Here it asserted the value "auto" - the very one absent from the
    # Codex enumeration - and kept the typo in place.

    def test_server_restarts_cleanly_after_process_exit(self):
        root = git_project()
        message = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        first = run_server(root, message)
        second = run_server(root, message)
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        from codex_autopilot import __version__

        self.assertEqual(
            json.loads(second.stdout)["result"]["serverInfo"]["version"], __version__
        )

    def test_unknown_arguments_are_rejected_without_sql_execution(self):
        root = git_project()
        result = run_server(
            root,
            [{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "memory", "arguments": {"operation": "search", "query": "x", "raw_sql": "DROP TABLE records"}}}],
        )
        response = json.loads(result.stdout)
        self.assertEqual(response["error"]["code"], -32602)
        self.assertIn("unknown argument", response["error"]["message"])

    def _project_with_two_tasks(self):
        """Проект с двумя задачами и запущенным сервером памяти."""
        root = git_project()
        skill = root / "SKILL.md"
        skill.write_text("test skill\n", encoding="utf-8")
        plan_file = root / "plan.json"
        plan_file.write_text(
            json.dumps(
                canonicalize_plan({
                    "schema_version": 3,
                    "graph_version": 1,
                    "goal": "Exercise task-addressed Project Memory retrieval.",
                    "user_request": "canonical original request",
                    "model_strategy": "auto",
                    "execution_strategy": "auto",
                    "max_parallel_workers": 2,
                    "computer_use_slots": 1,
                    "roles": [
                        {
                            "id": "builder",
                            "name": "Builder",
                            "responsibilities": ["Build."],
                        }
                    ],
                    "tasks": [
                        {
                            "id": task_id,
                            "title": f"Task {task_id}",
                            "objective": f"Produce {task_id}.",
                            "definition_of_done": [f"{task_id} is done."],
                            "execution_mode": "code",
                            "execution_mode_reason": "Repository work.",
                            "reasoning": "medium",
                            "role": "builder",
                            "depends_on": [],
                            "priority": 0,
                            "verification": canonical_verification(),
                            "resources": [],
                            "required_capabilities": [],
                            "context": {},
                            "outputs": [],
                            "tags": [],
                        }
                        for task_id in ("A", "B")
                    ],
                })
            ),
            encoding="utf-8",
        )
        initialize_project(
            root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
        )
        server = MemoryMcpServer(root)
        return root, server

    def test_current_can_retrieve_the_canonical_request_for_an_exact_task(self):
        root, server = self._project_with_two_tasks()

        current = server.actions["current"]({"task_id": "B"})

        self.assertEqual(current["user_request"], "canonical original request")
        self.assertEqual(current["milestone"]["id"], "B")
        with self.assertRaisesRegex(MemoryValidationError, "unknown task_id"):
            server.actions["current"]({"task_id": "missing"})

    def test_the_runtime_verifies_the_request_so_the_worker_never_hashes_it(self):
        """Заверение делает сервер, а не тот, кого проверяют.

        Прежде воркер должен был сам сверить длину и sha256 полученного
        текста с теми, что в его промпте. Это не работало дважды. Изолят
        постобработки Codex не имеет ни `crypto`, ни `TextEncoder` -
        посчитать хэш нечем, - а контракт разрешает ровно один вызов и
        повторить его нельзя: задача честно вставала с
        ENVIRONMENT_FAILURE, и так встали M4 и M11 живого прогона. И
        отдельно: проверку, которую делает сам проверяемый, можно молча
        не сделать.
        """

        root, server = self._project_with_two_tasks()
        import hashlib

        request = "canonical original request"
        digest = hashlib.sha256(request.encode("utf-8")).hexdigest()

        current = server.actions["current"](
            {"task_id": "B", "expect_user_request_sha256": digest}
        )
        self.assertEqual(current["user_request"], request)
        self.assertEqual(current["user_request_sha256"], digest)
        self.assertEqual(current["user_request_chars"], len(request))
        self.assertTrue(current["user_request_verified"])

    def test_a_request_that_changed_underneath_the_task_fails_closed(self):
        """Расхождение - причина остановиться, а не продолжить."""

        root, server = self._project_with_two_tasks()
        with self.assertRaisesRegex(MemoryValidationError, "does not match the digest"):
            server.actions["current"](
                {"task_id": "B", "expect_user_request_sha256": "0" * 64}
            )

    def test_the_digest_is_still_reported_without_an_expectation(self):
        """Без ожидания текст отдаётся, но заверенным не объявляется."""

        root, server = self._project_with_two_tasks()
        current = server.actions["current"]({"task_id": "B"})
        self.assertFalse(current["user_request_verified"])
        self.assertIn("user_request_sha256", current)


if __name__ == "__main__":
    unittest.main()
