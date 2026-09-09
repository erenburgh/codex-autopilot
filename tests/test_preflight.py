from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_autopilot.appserver import AppServerError
from codex_autopilot.models import MODEL_IDS
from codex_autopilot.preflight import PreflightApprovalRequired, PreflightError, REQUIRED_MEMORY_TOOLS, run_preflight
from codex_autopilot.plan import validate_plan


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md"
HOST_SKILL = ROOT / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md"


def project() -> Path:
    root = Path(tempfile.mkdtemp(prefix="codex-autopilot-preflight-test-"))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def plan(profile: str = "adaptive"):
    item = {
        "title": "Build UI",
        "objective": "Build and verify the UI",
        "definition_of_done": ["UI tests pass"],
        "execution_mode": "code",
        "execution_mode_reason": "Files and tests are sufficient.",
    }
    if profile == "adaptive":
        item["reasoning"] = "high"
    return validate_plan(
        {"goal": "Ship", "model_strategy": "auto" if profile == "adaptive" else "host-settings", "milestones": [item]},
        profile,
    )


class PreflightClient:
    instances: list["PreflightClient"] = []

    def __init__(self, *_args, **_kwargs):
        self.closed = False
        self.thread_args = None
        self.model_calls = 0
        self.__class__.instances.append(self)

    def connect(self):
        return {"userAgent": "fake-app-server", "codexHome": "/tmp/fake-codex-home"}

    def close(self):
        self.closed = True

    def list_permission_profiles(self, _root):
        return [{"id": ":workspace", "allowed": True}]

    def start_thread(self, **kwargs):
        self.thread_args = kwargs
        return {
            "thread": {"id": "preflight-thread", "cwd": str(kwargs["cwd"])},
            "activePermissionProfile": {"id": kwargs["permission_profile"]},
        }

    def list_mcp_server_status(self, _thread_id):
        return [{"name": "codex_autopilot_memory", "runtimeStatus": "connected", "tools": {name: {} for name in REQUIRED_MEMORY_TOOLS}}]

    def call_mcp_tool(self, _thread_id, _server, _tool, _args):
        return {"structuredContent": {"project_root": str(self.thread_args["cwd"]), "initialized": False}}

    def list_models(self):
        self.model_calls += 1
        efforts = [{"reasoningEffort": item} for item in ("medium", "high", "xhigh", "max")]
        return [
            {"id": MODEL_IDS["sol"], "model": MODEL_IDS["sol"], "displayName": "GPT-5.6 Sol", "supportedReasoningEfforts": efforts},
            {"id": MODEL_IDS["astra"], "model": MODEL_IDS["astra"], "displayName": "GPT-6 Astra", "supportedReasoningEfforts": efforts},
        ]


class DeniedClient(PreflightClient):
    def connect(self):
        raise AppServerError("failed to initialize sqlite state runtime under ~/.codex: Operation not permitted")


class MissingMemoryClient(PreflightClient):
    def list_mcp_server_status(self, _thread_id):
        return []


class PreflightTests(unittest.TestCase):
    def setUp(self):
        PreflightClient.instances.clear()

    def test_clean_first_run_checks_target_without_creating_state(self):
        root = project()
        result = run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=PreflightClient, emit=None)
        self.assertEqual(result.project, root.resolve())
        self.assertEqual(result.next_model, "GPT-5.6 Sol")
        client = PreflightClient.instances[-1]
        self.assertEqual(client.thread_args["cwd"], root.resolve())
        self.assertTrue(client.thread_args["ephemeral"])
        self.assertTrue(client.thread_args["project_memory"])
        self.assertFalse((root / ".codex-autopilot").exists())
        self.assertTrue(client.closed)

    def test_missing_codex_home_access_is_explicit_and_leaves_no_idle_state(self):
        root = project()
        with self.assertRaises(PreflightApprovalRequired) as caught:
            run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=DeniedClient, emit=None)
        self.assertIn("APPROVAL REQUIRED", str(caught.exception))
        self.assertIn("Codex App Server state directory", str(caught.exception))
        self.assertFalse((root / ".codex-autopilot").exists())

    def test_missing_memory_mcp_blocks_before_initialization(self):
        root = project()
        with self.assertRaisesRegex(PreflightError, "Project Memory MCP"):
            run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=MissingMemoryClient, emit=None)
        self.assertFalse((root / ".codex-autopilot").exists())

    def test_host_settings_preflight_does_not_read_model_catalog(self):
        root = project()
        run_preflight(root, plan=plan("host-settings"), profile="host-settings", skill_path=HOST_SKILL, binary="/bin/echo", client_factory=PreflightClient, emit=None)
        self.assertEqual(PreflightClient.instances[-1].model_calls, 0)

    def test_non_git_fails_before_app_server(self):
        root = Path(tempfile.mkdtemp(prefix="codex-autopilot-not-git-"))
        with self.assertRaisesRegex(PreflightError, "Git"):
            run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=PreflightClient, emit=None)
        self.assertFalse(PreflightClient.instances)


if __name__ == "__main__":
    unittest.main()
