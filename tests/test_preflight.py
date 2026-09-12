from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_autopilot.appserver import AppServerError, ApprovalRequired, TurnResult
from codex_autopilot.hook_trust import HookTrustApprovalRequired, runtime_hook_command
from codex_autopilot.models import MODEL_IDS
from codex_autopilot.preflight import MEMORY_PREFLIGHT_OK, MEMORY_PREFLIGHT_TITLE, PreflightApprovalRequired, PreflightError, ProjectMemoryApprovalRequired, REQUIRED_MEMORY_TOOLS, run_preflight
from codex_autopilot.plan import validate_plan
from codex_autopilot.project_association import ProjectAssociationError, require_desktop_project_root


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
        self.thread_args_history = []
        self.model_calls = 0
        self.named = []
        self.archived = []
        self.assigned = []
        self.thread_metadata = {}
        self.plain_turns = []
        self.approval_responses = []
        self.__class__.instances.append(self)

    def connect(self):
        return {"userAgent": "fake-app-server", "codexHome": "/tmp/fake-codex-home"}

    def close(self):
        self.closed = True

    def list_permission_profiles(self, _root):
        return [{"id": ":workspace", "allowed": True}]

    def list_hooks(self, root):
        hooks = []
        for name in ("codex-autopilot-adaptive", "codex-autopilot-host-settings"):
            hooks.append({
                "eventName": "stop",
                "handlerType": "command",
                "command": runtime_hook_command(),
                "pluginId": f"{name}@codex-autopilot-local",
                "enabled": True,
                "trustStatus": "trusted",
                "currentHash": "sha256:trusted",
            })
        return [{"cwd": str(root), "hooks": hooks, "warnings": [], "errors": []}]

    def list_projects(self):
        return []

    def start_thread(self, **kwargs):
        self.thread_args = kwargs
        self.thread_args_history.append(kwargs)
        suffix = "" if len(self.thread_args_history) == 1 else f"-{len(self.thread_args_history)}"
        thread = {
            "id": f"preflight-thread{suffix}",
            "cwd": str(kwargs["cwd"]),
            "projectId": kwargs["project_id"],
        }
        self.thread_metadata[thread["id"]] = thread
        return {
            "thread": dict(thread),
            "activePermissionProfile": {"id": kwargs["permission_profile"]},
        }

    def name_thread(self, thread_id, name):
        self.named.append((thread_id, name))

    def assign_thread_to_project(self, thread_id, project_id):
        self.assigned.append((thread_id, project_id))
        self.thread_metadata[thread_id]["projectId"] = project_id
        return dict(self.thread_metadata[thread_id])

    def read_thread(self, thread_id):
        return dict(self.thread_metadata[thread_id])

    def archive_thread(self, thread_id):
        self.archived.append(thread_id)

    def respond_project_memory_approval(self, request, *, persist):
        self.approval_responses.append((request, persist))

    def list_mcp_server_status(self, _thread_id):
        plugin_name = self.thread_args["plugin_root"].name
        return [{
            "name": "codex_autopilot_memory",
            "runtimeStatus": "connected",
            "pluginId": f"{plugin_name}@codex-autopilot-local",
            "tools": {name: {} for name in REQUIRED_MEMORY_TOOLS},
        }]

    def call_mcp_tool(self, _thread_id, _server, _tool, _args):
        return {"structuredContent": {"project_root": str(self.thread_args["cwd"]), "initialized": False}}

    def start_plain_turn(self, **kwargs):
        self.plain_turns.append(kwargs)
        return {"turn": {"id": "preflight-turn"}}

    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        turn = {
            "id": turn_id,
            "status": "completed",
            "items": [
                {"type": "mcpToolCall", "server": "codex_autopilot_memory", "tool": "memory", "status": "completed"},
                {"type": "agentMessage", "phase": "final_answer", "text": MEMORY_PREFLIGHT_OK},
            ],
        }
        return TurnResult(thread_id, turn, [])

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


class ModifiedStopHookClient(PreflightClient):
    def list_hooks(self, root):
        inventory = super().list_hooks(root)
        for hook in inventory[0]["hooks"]:
            if hook["pluginId"].startswith("codex-autopilot-adaptive@"):
                hook["trustStatus"] = "modified"
                hook["currentHash"] = "sha256:changed"
        return inventory


class UntrustedMemoryClient(PreflightClient):
    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        raise ApprovalRequired({
            "method": "mcpServer/elicitation/request",
            "id": 1,
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "serverName": "codex_autopilot_memory",
                "_meta": {"codex_approval_kind": "mcp_tool_call", "persist": ["session", "always"]},
            },
        })


class NonPersistentMemoryClient(UntrustedMemoryClient):
    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        raise ApprovalRequired({
            "method": "mcpServer/elicitation/request",
            "id": 1,
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "serverName": "codex_autopilot_memory",
                "_meta": {"codex_approval_kind": "mcp_tool_call"},
            },
        })


class ProjectPlacementClient(PreflightClient):
    initiating_root: Path

    def list_projects(self):
        return [{"id": "project-1", "name": "Codex Autopilot", "roots": [{"path": str(self.initiating_root)}]}]


class TargetProjectPlacementClient(PreflightClient):
    target_root: Path

    def list_projects(self):
        return [
            {"id": "outer", "name": "Outer", "roots": [{"path": str(self.target_root.parent)}]},
            {"id": "target", "name": "Target", "roots": [{"path": str(self.target_root)}]},
        ]


class WrongProjectPlacementClient(TargetProjectPlacementClient):
    def start_thread(self, **kwargs):
        result = super().start_thread(**kwargs)
        result["thread"]["projectId"] = "wrong-project"
        self.thread_metadata[result["thread"]["id"]]["projectId"] = "wrong-project"
        return result

    def assign_thread_to_project(self, thread_id, project_id):
        self.assigned.append((thread_id, project_id))
        return dict(self.thread_metadata[thread_id])


class DesktopSlotPreflightClient(PreflightClient):
    slots: dict[str, dict] = {}

    def read_thread(self, thread_id):
        return self.slots[thread_id]


class AlreadyDesktopArchivedClient(PreflightClient):
    def archive_thread(self, thread_id):
        if thread_id == "old-m1":
            raise AppServerError(f"no rollout found for thread id {thread_id}")
        return super().archive_thread(thread_id)


class AuthorizedMemoryClient(UntrustedMemoryClient):
    waits = 0

    def wait_for_turn(self, thread_id, turn_id, **kwargs):
        self.__class__.waits += 1
        if self.waits == 1:
            return super().wait_for_turn(thread_id, turn_id, **kwargs)
        return PreflightClient.wait_for_turn(self, thread_id, turn_id, **kwargs)


class AuthorizedSparseCompletionClient(AuthorizedMemoryClient):
    def wait_for_turn(self, thread_id, turn_id, **kwargs):
        self.__class__.waits += 1
        if self.waits == 1:
            return UntrustedMemoryClient.wait_for_turn(self, thread_id, turn_id, **kwargs)
        if self.waits == 2:
            return TurnResult(
                thread_id,
                {
                    "id": turn_id,
                    "status": "completed",
                    "items": [{"type": "agentMessage", "phase": "final_answer", "text": MEMORY_PREFLIGHT_OK}],
                },
                [],
            )
        return PreflightClient.wait_for_turn(self, thread_id, turn_id, **kwargs)

    def read_thread(self, thread_id):
        turn_id = self.plain_turns[0] and "preflight-turn"
        completed = PreflightClient.wait_for_turn(self, thread_id, turn_id)
        return {"turns": [completed.turn]}


class PreflightTests(unittest.TestCase):
    def test_desktop_root_paths_accept_the_canonical_target(self):
        codex_home = Path(tempfile.mkdtemp(prefix="codex-autopilot-codex-home-"))
        target = Path(tempfile.mkdtemp(prefix="codex-autopilot-target-"))
        (codex_home / ".codex-global-state.json").write_text(
            json.dumps(
                {
                    "local-projects": {
                        "desktop-project": {
                            "id": "desktop-project",
                            "rootPaths": [str(target.parent)],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        roots = require_desktop_project_root(
            codex_home,
            "desktop-project",
            target,
        )
        self.assertEqual(roots, (target.parent.resolve(),))

    def test_desktop_root_paths_mismatch_fails_before_false_ui_claim(self):
        codex_home = Path(tempfile.mkdtemp(prefix="codex-autopilot-codex-home-"))
        target = Path(tempfile.mkdtemp(prefix="codex-autopilot-target-"))
        unrelated = Path(tempfile.mkdtemp(prefix="codex-autopilot-unrelated-"))
        (codex_home / ".codex-global-state.json").write_text(
            json.dumps(
                {
                    "local-projects": {
                        "desktop-project": {
                            "id": "desktop-project",
                            "rootPaths": [str(unrelated)],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ProjectAssociationError,
            "rootPaths do not contain the target root",
        ):
            require_desktop_project_root(
                codex_home,
                "desktop-project",
                target,
            )

    def setUp(self):
        PreflightClient.instances.clear()

    def test_clean_first_run_checks_target_without_creating_state(self):
        root = project()
        result = run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=PreflightClient, emit=None)
        self.assertEqual(result.project, root.resolve())
        self.assertEqual(result.next_model, "GPT-5.6 Sol")
        client = PreflightClient.instances[-1]
        self.assertEqual(client.thread_args["cwd"], root.resolve())
        self.assertFalse(client.thread_args["ephemeral"])
        self.assertTrue(client.thread_args["project_memory"])
        self.assertEqual(client.named, [("preflight-thread", MEMORY_PREFLIGHT_TITLE)])
        self.assertEqual(client.archived, ["preflight-thread"])
        self.assertEqual(len(client.plain_turns), 1)
        self.assertEqual(Path(client.plain_turns[0]["cwd"]).resolve(), root.resolve())
        self.assertFalse((root / ".codex-autopilot").exists())

    def test_explicit_user_authorization_answers_pending_request_with_always(self):
        root = project()
        AuthorizedMemoryClient.waits = 0
        result = run_preflight(
            root,
            plan=plan(),
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=AuthorizedMemoryClient,
            emit=None,
            approve_project_memory_always=True,
        )
        client = PreflightClient.instances[-1]
        self.assertEqual(result.memory_preflight_thread_id, "preflight-thread")
        self.assertEqual(len(client.approval_responses), 1)
        self.assertEqual(client.approval_responses[0][1], "always")
        self.assertEqual(len(client.plain_turns), 2)
        self.assertTrue(
            all(
                Path(turn["cwd"]).resolve() == root.resolve()
                for turn in client.plain_turns
            )
        )
        self.assertEqual(
            client.named,
            [
                ("preflight-thread", MEMORY_PREFLIGHT_TITLE),
                ("preflight-thread-2", f"{MEMORY_PREFLIGHT_TITLE} · verification"),
            ],
        )
        self.assertEqual(client.archived, ["preflight-thread", "preflight-thread-2"])
        self.assertTrue(client.closed)

    def test_approved_compact_completion_is_hydrated_before_fresh_task_verification(self):
        root = project()
        AuthorizedSparseCompletionClient.waits = 0
        run_preflight(
            root,
            plan=plan(),
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=AuthorizedSparseCompletionClient,
            emit=None,
            approve_project_memory_always=True,
        )
        client = PreflightClient.instances[-1]
        self.assertEqual(len(client.plain_turns), 2)
        self.assertEqual(client.archived, ["preflight-thread", "preflight-thread-2"])

    def test_fresh_untrusted_mcp_stops_before_real_worker_or_run_state(self):
        root = project()
        with self.assertRaises(ProjectMemoryApprovalRequired) as caught:
            run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=UntrustedMemoryClient, emit=None)
        client = PreflightClient.instances[-1]
        self.assertEqual(caught.exception.thread_id, "preflight-thread")
        self.assertEqual(client.archived, ["preflight-thread"])
        self.assertEqual(len(client.plain_turns), 1)
        self.assertFalse((root / ".codex-autopilot").exists())

    def test_untrusted_raw_mcp_without_advertised_always_is_rejected(self):
        root = project()
        with self.assertRaisesRegex(PreflightError, "did not offer supported persistent approval"):
            run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=NonPersistentMemoryClient, emit=None)
        client = PreflightClient.instances[-1]
        self.assertEqual(client.approval_responses, [])
        self.assertEqual(client.archived, ["preflight-thread"])
        self.assertFalse((root / ".codex-autopilot").exists())

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
        self.assertEqual(PreflightClient.instances[-1].archived, ["preflight-thread"])
        self.assertFalse((root / ".codex-autopilot").exists())

    def test_modified_stop_hook_blocks_before_probe_thread_or_run_state(self):
        root = project()
        with self.assertRaises(HookTrustApprovalRequired) as caught:
            run_preflight(
                root,
                plan=plan(),
                profile="adaptive",
                skill_path=SKILL,
                binary="/bin/echo",
                client_factory=ModifiedStopHookClient,
                emit=None,
                desktop_project_id="desktop-project-1",
            )
        client = PreflightClient.instances[-1]
        self.assertIn("APPROVAL REQUIRED", str(caught.exception))
        self.assertIsNone(client.thread_args)
        self.assertTrue(client.closed)
        self.assertFalse((root / ".codex-autopilot").exists())

    def test_initiating_project_is_never_a_placement_fallback(self):
        """M10-REV-003: a saved project that contains only the initiating cwd
        must not receive a canonical-target task. Placement stays empty and the
        limitation is reported instead."""
        root = project()
        initiating = Path(tempfile.mkdtemp(prefix="codex-autopilot-initiating-"))
        ProjectPlacementClient.initiating_root = initiating
        result = run_preflight(
            root,
            plan=plan(),
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=ProjectPlacementClient,
            emit=None,
        )
        client = PreflightClient.instances[-1]
        self.assertIsNone(result.project_id)
        self.assertIsNone(result.project_source)
        self.assertEqual(client.thread_args["cwd"], root.resolve())
        self.assertIsNone(client.thread_args["project_id"])
        self.assertEqual(client.assigned, [])

    def test_target_longest_root_project_is_sent_and_verified_with_canonical_cwd(self):
        root = project()
        TargetProjectPlacementClient.target_root = root.resolve()
        initiating = Path(tempfile.mkdtemp(prefix="codex-autopilot-initiating-"))
        result = run_preflight(
            root,
            plan=plan(),
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=TargetProjectPlacementClient,
            emit=None,
        )
        client = PreflightClient.instances[-1]
        self.assertEqual(result.project_id, "target")
        self.assertEqual(result.project_source, "target")
        self.assertEqual(client.thread_args["cwd"], root.resolve())
        self.assertEqual(client.thread_args["project_id"], "target")

    def test_explicit_app_server_project_is_validated_and_used(self):
        root = project()
        TargetProjectPlacementClient.target_root = root.resolve()
        result = run_preflight(
            root,
            plan=plan(),
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=TargetProjectPlacementClient,
            emit=None,
            app_server_project_id="outer",
        )
        client = PreflightClient.instances[-1]
        self.assertEqual(result.project_id, "outer")
        self.assertEqual(result.project_source, "explicit target")
        self.assertEqual(client.thread_args["project_id"], "outer")

    def test_thread_start_project_metadata_mismatch_fails_closed(self):
        root = project()
        WrongProjectPlacementClient.target_root = root.resolve()
        with self.assertRaisesRegex(PreflightError, "project association"):
            run_preflight(
                root,
                plan=plan(),
                profile="adaptive",
                skill_path=SKILL,
                binary="/bin/echo",
                client_factory=WrongProjectPlacementClient,
                emit=None,
            )

    def test_desktop_project_slots_are_verified_before_memory_probe(self):
        root = project()
        DesktopSlotPreflightClient.slots = {
            "slot-1": {"id": "slot-1", "status": {"type": "idle"}, "canAcceptDirectInput": True},
        }
        result = run_preflight(
            root,
            plan=plan(),
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=DesktopSlotPreflightClient,
            emit=None,
            desktop_project_id="desktop-project-1",
            worker_thread_ids=("slot-1",),
        )
        self.assertEqual(result.desktop_project_id, "desktop-project-1")
        self.assertEqual(result.worker_thread_ids, ("slot-1",))
        self.assertIn(
            ("Desktop UI placement", "OK", "1 Desktop-created slot(s) assigned to project desktop-project-1"),
            result.checks,
        )

    def test_desktop_project_preflight_does_not_require_a_pre_created_slot(self):
        """Слоты были обходом вокруг мнимой невозможности завести видимую
        задачу. Скилл прямо запрещает создавать их заранее, поэтому
        требование слота здесь останавливало прогон целиком."""

        root = project()
        DesktopSlotPreflightClient.slots = {}
        result = run_preflight(
            root,
            plan=plan(),
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=DesktopSlotPreflightClient,
            emit=None,
            desktop_project_id="desktop-project-1",
        )
        self.assertIn(
            (
                "Desktop UI placement",
                "OK",
                "dispatcher creates its own visible task in project desktop-project-1",
            ),
            result.checks,
        )

    def test_replace_archives_each_previous_worker_once_after_trust_passes(self):
        root = project()
        state_dir = root / ".codex-autopilot"
        state_dir.mkdir()
        (state_dir / "run-state.json").write_text(
            '{"current_thread_id":"old-m1","previous_thread_ids":["old-m1"],"worker_history":[{"thread_id":"old-m1"}]}',
            encoding="utf-8",
        )
        run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=PreflightClient, emit=None, replace=True)
        client = PreflightClient.instances[-1]
        self.assertEqual(client.archived.count("old-m1"), 1)
        self.assertEqual(client.archived.count("preflight-thread"), 1)

    def test_replace_accepts_worker_already_archived_by_desktop(self):
        root = project()
        state_dir = root / ".codex-autopilot"
        state_dir.mkdir()
        (state_dir / "run-state.json").write_text(
            '{"current_thread_id":"old-m1","previous_thread_ids":["old-m1"],"worker_history":[]}',
            encoding="utf-8",
        )
        result = run_preflight(
            root,
            plan=plan(),
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=AlreadyDesktopArchivedClient,
            emit=None,
            replace=True,
        )
        self.assertIn(("Previous run", "RETIRED", "archived 1 worker task(s): old-m1"), result.checks)

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
