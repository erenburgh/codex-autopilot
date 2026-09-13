from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.appserver import AppServerClient, AppServerError


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins/codex-autopilot-adaptive"


class CaptureClient(AppServerClient):
    def __init__(self):
        super().__init__("codex", Path(tempfile.mktemp()))
        self.calls = []
        self.sent = []
    def request(self, method, params, timeout=60):
        self.calls.append((method, params))
        return {"turn": {"id": "turn"}}
    def send(self, payload):
        self.sent.append(payload)


class AppServerTests(unittest.TestCase):
    def test_adaptive_turn_has_explicit_skill_and_effort(self):
        client = CaptureClient()
        client.start_turn(thread_id="t", prompt="p", effort="high", client_user_message_id="c", skill_name="codex-autopilot-adaptive", skill_path=Path("/skill/SKILL.md"), cwd=Path("/project"))
        params = client.calls[-1][1]
        self.assertEqual(params["effort"], "high")
        self.assertEqual(params["input"][1], {"type": "skill", "name": "codex-autopilot-adaptive", "path": "/skill/SKILL.md"})
        self.assertEqual(params["cwd"], "/project")
        self.assertEqual(params["runtimeWorkspaceRoots"], ["/project"])

    def test_host_turn_omits_effort_key(self):
        client = CaptureClient()
        client.start_turn(thread_id="t", prompt="p", effort=None, client_user_message_id="c", skill_name="codex-autopilot-host-settings", skill_path=Path("/skill/SKILL.md"), cwd=Path("/project"))
        self.assertNotIn("effort", client.calls[-1][1])

    def test_preflight_turn_has_no_worker_skill(self):
        client = CaptureClient()
        client.start_plain_turn(thread_id="t", prompt="probe", effort="medium", client_user_message_id="c", cwd=Path("/project"))
        method, params = client.calls[-1]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["input"], [{"type": "text", "text": "probe"}])
        self.assertEqual(params["effort"], "medium")
        self.assertEqual(params["cwd"], "/project")
        self.assertEqual(params["runtimeWorkspaceRoots"], ["/project"])

    def test_thread_start_only_sends_safe_allowlisted_fields(self):
        client = CaptureClient()
        client.start_thread(cwd=Path("/project"), permission_profile=":workspace", project_id=None, model=None, plugin_root=PLUGIN_ROOT)
        params = client.calls[-1][1]
        self.assertEqual(params["runtimeWorkspaceRoots"], ["/project"])
        self.assertNotIn("config", params)
        self.assertNotIn("selectedCapabilityRoots", params)
        self.assertEqual(set(params), {"cwd", "permissions", "ephemeral", "runtimeWorkspaceRoots"})

    def test_adaptive_thread_start_sends_only_resolved_model(self):
        client = CaptureClient()
        client.start_thread(cwd=Path("/project"), permission_profile=":workspace", project_id=None, model="gpt-5.6-sol", plugin_root=PLUGIN_ROOT)
        params = client.calls[-1][1]
        self.assertEqual(params["model"], "gpt-5.6-sol")
        self.assertEqual(set(params), {"cwd", "permissions", "ephemeral", "runtimeWorkspaceRoots", "model"})

    def test_preflight_thread_is_ephemeral_and_project_scoped(self):
        client = CaptureClient()
        client.start_thread(cwd=Path("/target"), permission_profile=":workspace", project_id=None, model=None, plugin_root=PLUGIN_ROOT, ephemeral=True)
        params = client.calls[-1][1]
        self.assertTrue(params["ephemeral"])
        self.assertEqual(params["runtimeWorkspaceRoots"], ["/target"])

    def test_thread_start_keeps_project_association_separate_from_cwd(self):
        client = CaptureClient()
        client.start_thread(cwd=Path("/target"), permission_profile=":workspace", project_id="project-1", model=None, plugin_root=PLUGIN_ROOT)
        params = client.calls[-1][1]
        self.assertEqual(params["cwd"], "/target")
        self.assertEqual(params["projectId"], "project-1")


    def test_archive_thread_uses_supported_app_server_method(self):
        client = CaptureClient()
        client.archive_thread("thread")
        self.assertEqual(client.calls[-1], ("thread/archive", {"threadId": "thread"}))

    def test_post_create_project_assignment_uses_metadata_update(self):
        client = CaptureClient()
        client.request = lambda method, params, timeout=60: (
            client.calls.append((method, params))
            or {"thread": {"id": "thread", "projectId": "project-1"}}
        )
        assigned = client.assign_thread_to_project("thread", "project-1")
        self.assertEqual(
            client.calls[-1],
            (
                "thread/metadata/update",
                {"threadId": "thread", "projectId": "project-1"},
            ),
        )
        self.assertEqual(assigned["projectId"], "project-1")

    def test_project_root_is_added_before_project_scoped_thread_start(self):
        client = CaptureClient()
        responses = iter([
            {
                "project": {
                    "id": "project-1",
                    "roots": [{"path": "/existing"}],
                }
            },
            {
                "project": {
                    "id": "project-1",
                    "roots": [
                        {"path": "/existing"},
                        {"path": "/target"},
                    ],
                }
            },
        ])
        client.request = lambda method, params, timeout=60: (
            client.calls.append((method, params)) or next(responses)
        )

        project = client.ensure_project_root(
            "project-1", Path("/target"), authorized=True
        )

        self.assertEqual(project["roots"][-1]["path"], "/target")
        self.assertEqual(
            client.calls,
            [
                ("project/read", {"projectId": "project-1"}),
                (
                    "project/update",
                    {
                        "projectId": "project-1",
                        "roots": [
                            {"path": "/existing"},
                            {"path": "/target"},
                        ],
                    },
                ),
            ],
        )

    def test_root_drift_fails_closed_without_authorization(self):
        """R6: рантайм не правит сохранённый проект пользователя молча.

        Прежде этот же вызов дописывал корень при каждом создании задачи.
        Расхождение корней - состояние, о котором надо сказать, а не
        починить втихую: сохранённый проект принадлежит пользователю.
        """

        from codex_autopilot.appserver import ProjectRootDrift

        client = CaptureClient()
        client.request = lambda method, params, timeout=60: (
            client.calls.append((method, params))
            or {"project": {"id": "project-1", "roots": [{"path": "/existing"}]}}
        )

        with self.assertRaises(ProjectRootDrift) as raised:
            client.ensure_project_root("project-1", Path("/target"))

        self.assertEqual(raised.exception.project_id, "project-1")
        self.assertEqual(raised.exception.root, Path("/target"))
        self.assertEqual(raised.exception.existing, (Path("/existing"),))
        # Ни одной записи: project/update не вызывался.
        self.assertEqual(
            client.calls, [("project/read", {"projectId": "project-1"})]
        )

    def test_a_canonical_root_inside_a_project_root_is_membership(self):
        """Обычный случай, а не расхождение.

        preflight выбирает проект по вложенности (``_project_contains``),
        поэтому канонический каталог сплошь и рядом лежит ВНУТРИ корня
        проекта, а не равен ему. Проверка на равенство объявила бы это
        расхождением - и прежний код именно поэтому дописывал ещё один
        корень при каждом создании задачи.
        """

        client = CaptureClient()
        client.request = lambda method, params, timeout=60: (
            client.calls.append((method, params))
            or {"project": {"id": "project-1", "roots": [{"path": "/work"}]}}
        )

        project = client.verify_project_root("project-1", Path("/work/sub/tool"))

        self.assertEqual(project["id"], "project-1")
        self.assertEqual(
            client.calls, [("project/read", {"projectId": "project-1"})]
        )

    def test_verify_project_root_cannot_write_at_all(self):
        """Отдельный read-only вход: у него нет ветки мутации вовсе."""

        from codex_autopilot.appserver import ProjectRootDrift

        client = CaptureClient()
        client.request = lambda method, params, timeout=60: (
            client.calls.append((method, params))
            or {"project": {"id": "project-1", "roots": [{"path": "/existing"}]}}
        )

        with self.assertRaises(ProjectRootDrift):
            client.verify_project_root("project-1", Path("/target"))
        self.assertEqual(
            client.calls, [("project/read", {"projectId": "project-1"})]
        )

    def test_existing_project_root_is_not_rewritten(self):
        client = CaptureClient()
        client.request = lambda method, params, timeout=60: (
            client.calls.append((method, params))
            or {
                "project": {
                    "id": "project-1",
                    "roots": [{"path": "/target"}],
                }
            }
        )

        client.ensure_project_root("project-1", Path("/target"))

        self.assertEqual(
            client.calls,
            [("project/read", {"projectId": "project-1"})],
        )

    def test_explicit_memory_approval_responds_to_exact_pending_request(self):
        client = CaptureClient()
        request = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "mcpServer/elicitation/request",
            "params": {"_meta": {"persist": ["session", "always"]}},
        }
        client.respond_project_memory_approval(request, persist="always")
        self.assertEqual(client.sent[-1], {
            "jsonrpc": "2.0",
            "id": 9,
            "result": {"action": "accept", "content": None, "_meta": {"persist": "always"}},
        })

    def test_explicit_memory_approval_refuses_unadvertised_persistence(self):
        client = CaptureClient()
        request = {"jsonrpc": "2.0", "id": 9, "method": "mcpServer/elicitation/request", "params": {"_meta": {}}}
        with self.assertRaisesRegex(AppServerError, "does not advertise"):
            client.respond_project_memory_approval(request, persist="always")
        self.assertEqual(client.sent, [])

    def test_model_list_uses_capability_metadata_api(self):
        client = CaptureClient()
        self.assertEqual(client.list_models(), [])
        self.assertEqual(client.calls[-1], ("model/list", {"includeHidden": True, "limit": 100}))

    def test_hook_list_uses_supported_cwd_inventory_method(self):
        client = CaptureClient()
        self.assertEqual(client.list_hooks(Path("/project")), [])
        self.assertEqual(client.calls[-1], ("hooks/list", {"cwds": ["/project"]}))

    def test_mcp_tool_uses_current_app_server_method(self):
        client = CaptureClient()
        client.call_mcp_tool("thread", "codex_autopilot_memory", "memory", {"operation": "current"})
        self.assertEqual(client.calls[-1], ("mcpServer/tool/call", {"threadId": "thread", "server": "codex_autopilot_memory", "tool": "memory", "arguments": {"operation": "current"}}))

    def test_turn_interrupt_uses_exact_worker_ids(self):
        client = CaptureClient()
        client.interrupt_turn("thread", "turn")
        self.assertEqual(client.calls[-1], ("turn/interrupt", {"threadId": "thread", "turnId": "turn"}))

    def test_unsubscribe_releases_exact_worker(self):
        client = CaptureClient()
        client.subscribed_thread_ids.add("thread")
        client.unsubscribe_thread("thread")
        self.assertEqual(client.calls[-1], ("thread/unsubscribe", {"threadId": "thread"}))
        self.assertNotIn("thread", client.subscribed_thread_ids)

    def test_project_slot_turn_overrides_cwd_permissions_and_runtime_roots(self):
        client = CaptureClient()
        client.start_turn(
            thread_id="t",
            prompt="p",
            effort="high",
            client_user_message_id="c",
            skill_name="codex-autopilot-adaptive",
            skill_path=Path("/skill/SKILL.md"),
            cwd=Path("/target"),
            permission_profile=":workspace",
            model="gpt-5.6-sol",
        )
        params = client.calls[-1][1]
        self.assertEqual(params["cwd"], "/target")
        self.assertEqual(params["runtimeWorkspaceRoots"], ["/target"])
        self.assertEqual(params["permissions"], ":workspace")
        self.assertEqual(params["model"], "gpt-5.6-sol")


if __name__ == "__main__": unittest.main()


class ClientVersionTests(unittest.TestCase):
    """Версия клиента берётся из пакета, а не из прибитой строки.

    Замерено на живом сервере: userAgent сообщал
    "codex-autopilot; 0.8.0-beta", когда установлена была 0.8.2. Ровно
    эта ошибка чинилась в 0.8.1 у MCP-сервера памяти; здесь она жила
    второй копией и никем не проверялась.
    """

    def test_initialize_sends_the_package_version(self) -> None:
        from codex_autopilot import __version__

        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/appserver.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"version": __version__,', source)
        self.assertNotIn('"version": "0.8', source)
        self.assertTrue(__version__)
