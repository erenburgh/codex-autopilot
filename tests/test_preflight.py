from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_autopilot.appserver import AppServerError, ApprovalRequired, TurnResult
from codex_autopilot.hook_trust import HookTrustApprovalRequired, runtime_hook_command
from codex_autopilot.models import MODEL_IDS
from codex_autopilot.appserver import AppServerClient, TurnTimeout
from codex_autopilot.models import PUBLIC_REASONING
from codex_autopilot.preflight import PROBE_ATTEMPTS, PROBE_REASONING
from codex_autopilot.preflight import MEMORY_PREFLIGHT_OK, MEMORY_PREFLIGHT_TITLE, PreflightApprovalRequired, PreflightError, ProjectMemoryApprovalRequired, REQUIRED_MEMORY_TOOLS, run_preflight
from codex_autopilot.plan import validate_plan
from codex_autopilot.project_association import ProjectAssociationError, require_desktop_project_root


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_PROJECT = "desktop-project-for-tests"
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

    def test_the_printed_report_runs_end_to_end(self) -> None:
        """Печать отчёта - тоже код, и он должен исполняться в тестах.

        Все прочие проверки звали preflight с emit=None, и весь блок
        отчёта не исполнялся ни разу. В нём уехал NameError: строка про
        ёмкость обращалась к DEFAULT_MAX_PARALLEL_WORKERS, которого в
        модуле не было. Падение случилось у пользователя, в самом конце
        успешного preflight, после выданного разрешения.
        """

        root = project()
        lines: list[str] = []
        run_preflight(
            root,
            plan=plan(),
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=PreflightClient,
            desktop_project_id=DESKTOP_PROJECT,
            emit=lines.append,
        )
        report = "\n".join(lines)
        self.assertIn("Routing:", report)
        self.assertIn("Next worker:", report)
        self.assertIn("Ёмкость:", report)
        self.assertIn("Preflight: PASS", report)

    def test_clean_first_run_checks_target_without_creating_state(self):
        root = project()
        result = run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=PreflightClient, desktop_project_id=DESKTOP_PROJECT, emit=None)
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
            desktop_project_id=DESKTOP_PROJECT, emit=None,
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
            desktop_project_id=DESKTOP_PROJECT, emit=None,
            approve_project_memory_always=True,
        )
        client = PreflightClient.instances[-1]
        self.assertEqual(len(client.plain_turns), 2)
        self.assertEqual(client.archived, ["preflight-thread", "preflight-thread-2"])

    def test_fresh_untrusted_mcp_stops_before_real_worker_or_run_state(self):
        root = project()
        with self.assertRaises(ProjectMemoryApprovalRequired) as caught:
            run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=UntrustedMemoryClient, desktop_project_id=DESKTOP_PROJECT, emit=None)
        client = PreflightClient.instances[-1]
        self.assertEqual(caught.exception.thread_id, "preflight-thread")
        self.assertEqual(client.archived, ["preflight-thread"])
        self.assertEqual(len(client.plain_turns), 1)
        self.assertFalse((root / ".codex-autopilot").exists())
        # Проводка, а не помощник: мутационная проверка показала, что
        # тесты на сам approval_command проходят и с оборванной связкой.
        message = str(caught.exception)
        self.assertIn("--approve-project-memory-always", message)
        self.assertIn(str(root.resolve()), message)
        self.assertIsNotNone(caught.exception.command)
        # Команда без идентификаторов проекта падает раньше разрешения -
        # на проверке размещения. Первая выданная пользователю команда
        # была именно такой.
        self.assertIn("--desktop-project-id", caught.exception.command)

    def test_untrusted_raw_mcp_without_advertised_always_is_rejected(self):
        root = project()
        with self.assertRaisesRegex(PreflightError, "did not offer supported persistent approval"):
            run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=NonPersistentMemoryClient, desktop_project_id=DESKTOP_PROJECT, emit=None)
        client = PreflightClient.instances[-1]
        self.assertEqual(client.approval_responses, [])
        self.assertEqual(client.archived, ["preflight-thread"])
        self.assertFalse((root / ".codex-autopilot").exists())

    def test_missing_codex_home_access_is_explicit_and_leaves_no_idle_state(self):
        root = project()
        with self.assertRaises(PreflightApprovalRequired) as caught:
            run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=DeniedClient, desktop_project_id=DESKTOP_PROJECT, emit=None)
        self.assertIn("APPROVAL REQUIRED", str(caught.exception))
        self.assertIn("Codex App Server state directory", str(caught.exception))
        self.assertFalse((root / ".codex-autopilot").exists())

    def test_missing_memory_mcp_blocks_before_initialization(self):
        root = project()
        with self.assertRaisesRegex(PreflightError, "Project Memory MCP"):
            run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=MissingMemoryClient, desktop_project_id=DESKTOP_PROJECT, emit=None)
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
            desktop_project_id=DESKTOP_PROJECT, emit=None,
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
            desktop_project_id=DESKTOP_PROJECT, emit=None,
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
            desktop_project_id=DESKTOP_PROJECT, emit=None,
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
                desktop_project_id=DESKTOP_PROJECT, emit=None,
            )


    def test_desktop_project_preflight_reports_self_created_placement(self):
        """Заранее созданных слотов больше нет как понятия.

        Они были обходом вокруг мнимой невозможности завести видимую
        задачу, а требование слота в префлайте противоречило скиллу,
        который запрещал их создавать, и останавливало прогон целиком.
        Механизм снят в 0.8.1; ветку заводит сам диспетчер."""

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
        run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=PreflightClient, desktop_project_id=DESKTOP_PROJECT, emit=None, replace=True)
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
            desktop_project_id=DESKTOP_PROJECT, emit=None,
            replace=True,
        )
        self.assertIn(("Previous run", "RETIRED", "archived 1 worker task(s): old-m1"), result.checks)

    def test_host_settings_preflight_does_not_read_model_catalog(self):
        root = project()
        run_preflight(root, plan=plan("host-settings"), profile="host-settings", skill_path=HOST_SKILL, binary="/bin/echo", client_factory=PreflightClient, desktop_project_id=DESKTOP_PROJECT, emit=None)
        self.assertEqual(PreflightClient.instances[-1].model_calls, 0)

    def test_non_git_fails_before_app_server(self):
        root = Path(tempfile.mkdtemp(prefix="codex-autopilot-not-git-"))
        with self.assertRaisesRegex(PreflightError, "Git"):
            run_preflight(root, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo", client_factory=PreflightClient, desktop_project_id=DESKTOP_PROJECT, emit=None)
        self.assertFalse(PreflightClient.instances)


if __name__ == "__main__":
    unittest.main()


class TargetMustBelongToAProjectTests(unittest.TestCase):
    """Прогон без проекта Codex отказывает до создания состояния.

    Прежде отсутствие проекта всплывало посреди прогона -
    "desktop_owned requires desktop.desktop_project_id" - уже после
    планирования, и человека просили добавить проект руками в
    середине работы. Весь жизненный цикл требует проект: в нём
    создаётся каждая задача и в нём же проверяется размещение.
    Значит отказ обязан наступать на входе и называть действие.
    """

    def test_missing_project_fails_before_any_state(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / ".git").mkdir()
            with self.assertRaises(PreflightError) as caught:
                run_preflight(
                    root,
                    plan=plan(),
                    profile="adaptive",
                    skill_path=SKILL,
                    binary="/bin/echo",
                    client_factory=PreflightClient,
                    emit=None,
                )
            message = str(caught.exception)
            self.assertIn("не принадлежит ни одному проекту Codex", message)
            self.assertIn("Открой проект Codex", message)
            # Состояния нет: отказ наступил до его создания.
            self.assertFalse((root / ".codex-autopilot").exists())


class TrustProbeTests(unittest.TestCase):
    """Проба доверия — последний шаг preflight и единственный, где он падал.

    На прогоне v1.0 preflight прошёл все десять проверок и дважды умер
    здесь ровно по 302 секунды, а на третий заход прошёл меньше чем за
    минуту. Причина — ход, которому нечего обдумывать, шёл на усилии
    рабочего воркера.
    """

    def run_preflight(self, client_factory):
        return run_preflight(
            project(),
            plan=plan(),
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=client_factory,
            desktop_project_id=DESKTOP_PROJECT,
            emit=None,
        )

    def test_the_probe_does_not_run_at_the_worker_effort(self):
        self.run_preflight(PreflightClient)
        turns = PreflightClient.instances[-1].plain_turns
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["effort"], PROBE_REASONING)
        # Лестница воркеров начинается с medium. Проба воркером не
        # является, и её усилие не должно в эту лестницу попадать:
        # иначе правка маршрутизации молча вернёт xhigh.
        self.assertNotIn(PROBE_REASONING, PUBLIC_REASONING)

    def test_a_timed_out_probe_is_retried_instead_of_failing_the_launch(self):
        class FlakyProbeClient(PreflightClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.attempts = 0
                self.interrupted = []

            def interrupt_turn(self, thread_id, turn_id):
                self.interrupted.append((thread_id, turn_id))

            def wait_for_turn(self, thread_id, turn_id, **kwargs):
                self.attempts += 1
                if self.attempts == 1:
                    raise TurnTimeout("probe did not finish within 300s")
                return super().wait_for_turn(thread_id, turn_id, **kwargs)

        result = self.run_preflight(FlakyProbeClient)
        self.assertEqual(result.next_model, "GPT-5.6 Sol")
        client = FlakyProbeClient.instances[-1]
        self.assertEqual(client.attempts, 2)
        self.assertEqual(len(client.plain_turns), 2)
        # Зависший ход прерывается, иначе он продолжает занимать тред.
        self.assertEqual(len(client.interrupted), 1)

    def test_exhausted_attempts_name_the_model_turn_and_not_the_transport(self):
        class StuckProbeClient(PreflightClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.interrupted = []

            def interrupt_turn(self, thread_id, turn_id):
                self.interrupted.append((thread_id, turn_id))

            def wait_for_turn(self, thread_id, turn_id, **kwargs):
                raise TurnTimeout(
                    "Project Memory trust probe did not finish within 300s on thread "
                    f"{thread_id}. App Server answered throughout, so this is the "
                    "model turn and not the transport."
                )

        with self.assertRaises(PreflightError) as caught:
            self.run_preflight(StuckProbeClient)
        message = str(caught.exception)
        self.assertIn("model turn and not the transport", message)
        client = StuckProbeClient.instances[-1]
        self.assertEqual(len(client.plain_turns), PROBE_ATTEMPTS)
        self.assertEqual(len(client.interrupted), PROBE_ATTEMPTS)

    def test_the_real_timeout_message_blames_the_turn_not_the_server(self):
        """Сообщение строится в appserver, а не в подделке теста.

        Прежнее «Timed out waiting for App Server» отправляло чинить
        транспорт и права, хотя App Server всё это время отвечал.
        """

        client = AppServerClient("/bin/true", Path(os.devnull))
        with self.assertRaises(TurnTimeout) as caught:
            client.wait_for_turn(
                "thread-1", "turn-1", timeout=0.05, what="Project Memory trust probe"
            )
        message = str(caught.exception)
        self.assertIn("Project Memory trust probe", message)
        self.assertIn("did not finish within 0.05s", message)
        self.assertIn("model turn and not the transport", message)


class ApprovalArrivesAsACommandTests(unittest.TestCase):
    """Человеку нужна строка, которую можно запустить, а не инструкция.

    Всплывающего окна нет и быть не может: запрос инструмента памяти
    уходит на соединение диспетчера, а тот на approvals не отвечает по
    правилу. Прежде preflight писал "повторите ту же команду с флагом" -
    собрать её предлагалось модели, и до человека она не доходила ни
    разу за весь день.
    """

    def test_the_message_carries_a_runnable_command(self) -> None:
        from codex_autopilot.preflight import ProjectMemoryApprovalRequired, approval_command

        command = approval_command(
            Path("/tmp/проект"), Path("/tmp/проект/.codex-autopilot/plan.json"), "adaptive"
        )
        message = str(ProjectMemoryApprovalRequired("thread-1", "Заголовок", command))
        self.assertIn("preflight", message)
        self.assertIn("--approve-project-memory-always", message)
        self.assertIn("/tmp/проект", message)

    def test_the_command_quotes_paths_with_spaces(self) -> None:
        """Каталог проекта у пользователя называется через пробелы."""

        from codex_autopilot.preflight import approval_command

        command = approval_command(
            Path("/Users/x/Autopilot Studio | Test"),
            Path("/Users/x/Autopilot Studio | Test/.codex-autopilot/plan.json"),
            "adaptive",
        )
        self.assertIn('--project "/Users/x/Autopilot Studio | Test"', command)
        self.assertIn('"/Users/x/Autopilot Studio | Test/.codex-autopilot/plan.json"', command)

    def test_the_message_says_no_dialog_is_coming(self) -> None:
        """Иначе человек ждёт окна, которого не будет."""

        from codex_autopilot.preflight import ProjectMemoryApprovalRequired

        message = str(ProjectMemoryApprovalRequired("t", "T", "cmd"))
        self.assertIn("окна не будет", message)


class TheCommandPointsAtTheRealRuntimeTests(unittest.TestCase):
    """Команда обязана указывать туда, откуда скилл запускается у ЭТОГО
    пользователя, а не туда, где он лежит у меня.

    Запускатель плагина уважает CODEX_AUTOPILOT_RUNTIME. Первая версия
    генератора прошивала `~/Library/Application Support/...` наглухо:
    у любого, кто поставил рантайм иначе, выданная строка указывала бы
    в пустоту - и это ровно тот класс "работает только у автора".
    """

    def test_an_override_wins(self) -> None:
        from unittest import mock

        from codex_autopilot.preflight import runtime_command_path

        with mock.patch.dict(
            "os.environ", {"CODEX_AUTOPILOT_RUNTIME": "/opt/ap/bin/codex-autopilot"}
        ):
            self.assertEqual(
                str(runtime_command_path()), "/opt/ap/bin/codex-autopilot"
            )

    def test_the_command_uses_it(self) -> None:
        from unittest import mock

        from codex_autopilot.preflight import approval_command

        with mock.patch.dict(
            "os.environ", {"CODEX_AUTOPILOT_RUNTIME": "/opt/ap/bin/codex-autopilot"}
        ):
            command = approval_command(Path("/p"), Path("/p/plan.json"), "adaptive")
        self.assertIn('"/opt/ap/bin/codex-autopilot"', command)
