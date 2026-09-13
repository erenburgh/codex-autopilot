from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _gates import patch_hook_trust_gates

from codex_autopilot.appserver import DESKTOP_ORIGINATOR, AppServerRpcError, TurnResult
from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.cli import (
    _run_automatic_relay_dispatch,
    parser as cli_parser,
)
from codex_autopilot.config import (
    DESKTOP_OWNED_SURFACE,
    load_config,
)
from codex_autopilot.control import (
    arm,
    handle_post_tool_hook,
    handle_stop_hook,
    reactivate_desktop_relay_owner,
    spawn_automatic_app_server_relay,
    spawn_dispatcher,
)
from codex_autopilot.hook_trust import HookTrustApprovalRequired
from _handoff import bump_task_checkpoint
from codex_autopilot.lifecycle import task_checkpoint_path
from _appserver_fakes import activate_via_app_server
from _relay import reserve_ready_frontier  # R21: без зависимости от окружения
from codex_autopilot.lifecycle import (
    pending_descriptors,

    reconcile_desktop_runtime,
    DESKTOP_SLOT_READY,
    WORKSPACE_HANDOFF_OK,
    DesktopLifecycleError,
    acknowledge_desktop_send,
    adopt_automatic_dispatcher_successor,
    complete_desktop_worker,
    create_desktop_thread_via_app_server,
    pause_desktop_run,
    record_automatic_app_server_exit,
    record_desktop_interrupt,
    record_desktop_failure,
    reconcile_desktop_thread_identity,
    relay_session_status,
    run_automatic_app_server_turn,
)
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.pipeline_engineer import (
    PipelineIncidentStore,
)
from codex_autopilot.run_state import StateStore
from codex_autopilot.task_state import TaskState


def task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    path: str | None = None,
    verification_required: bool = True,
    verification_policy: str = "independent",
) -> dict[str, object]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Complete {task_id}.",
        "definition_of_done": [f"{task_id} is verified."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files and tests are sufficient.",
        "reasoning": "medium",
        "role": "builder",
        "depends_on": list(depends_on),
        "priority": 0,
        "verification": {
            "policy": verification_policy,
            "required": verification_required,
            "max_revision_attempts": 1,
        },
        "resources": (
            [
                {
                    "id": "tree",
                    "kind": "directory",
                    "target": path,
                    "access": "write",
                }
            ]
            if path
            else []
        ),
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
    }


def graph(*, max_workers: int = 2) -> dict[str, object]:
    return {
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Exercise the Desktop-owned JIT lifecycle.",
        "user_request": "Exercise role-aware Desktop lifecycle behavior.",
        "model_strategy": "auto",
        "execution_strategy": "parallel",
        "max_parallel_workers": max_workers,
        "computer_use_slots": 1,
        "roles": [
            {
                "id": "builder",
                "name": "Builder",
                "responsibilities": ["Implement and verify one task."],
            }
        ],
        "tasks": [
            task("A", path="src/a"),
            task("B", path="src/b", verification_required=False),
            task("C", depends_on=("A", "B"), path="src/c"),
        ],
    }


def legacy_graph() -> dict[str, object]:
    return {
        "schema_version": 2,
        "goal": "Recover a migrated serial run.",
        "model_strategy": "auto",
        "milestones": [
            {
                "id": task_id,
                "title": f"Task {task_id}",
                "objective": f"Complete {task_id}.",
                "definition_of_done": [f"{task_id} is verified."],
                "execution_mode": "code",
                "execution_mode_reason": "Repository files and tests are sufficient.",
                "reasoning": "medium",
            }
            for task_id in ("M6", "M7", "M8")
        ],
    }


class FakePrepClient:
    def __init__(
        self,
        canonical_cwd: Path,
        events: list[str],
        *,
        slot_turn_id: str,
        slot_prompt: str,
        extra_turns: list[dict[str, object]] | None = None,
        app_server_redaction: bool = False,
    ) -> None:
        self.canonical_cwd = canonical_cwd
        self.cwd = canonical_cwd.parent / "saved-desktop-project"
        self.events = events
        self.process_exited = False
        self.handoff_prompt = ""
        self.name: str | None = None
        self.turns: list[dict[str, object]] = [
            {
                "id": slot_turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "functionCallOutput",
                        "name": "create_thread",
                        "namespace": "codex_app",
                        "output": (
                            "<codex_delegation><source_thread_id>source</source_thread_id>"
                            f"<input>{slot_prompt}</input></codex_delegation>"
                        ),
                    },
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": (
                            f"<redacted chars={len(DESKTOP_SLOT_READY)} sha256="
                            f"{hashlib.sha256(DESKTOP_SLOT_READY.encode()).hexdigest()}>"
                            if app_server_redaction
                            else DESKTOP_SLOT_READY
                        ),
                    },
                ],
            },
            *(extra_turns or []),
        ]

    def __enter__(self):
        self.events.append("app-server-connected")
        return self

    def __exit__(self, *_args):
        self.process_exited = True
        self.events.append("app-server-exited")

    def resume_thread(self, thread_id):
        self.events.append("thread-resumed-for-prep")
        return {"thread": {"id": thread_id, "cwd": str(self.cwd)}}

    def start_plain_turn(self, *, thread_id, prompt, cwd, **_kwargs):
        self.events.append("workspace-handoff-started")
        self.handoff_prompt = prompt
        self.cwd = cwd
        return {"turn": {"id": f"prep-{thread_id}"}}

    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        self.events.append("workspace-handoff-completed")
        return TurnResult(
            thread_id,
            {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": WORKSPACE_HANDOFF_OK,
                    }
                ],
            },
            [],
        )

    def name_thread(self, thread_id, name):
        self.events.append(("thread-name-set", thread_id, name))
        self.name = name

    def read_thread(self, thread_id):
        self.events.append("canonical-cwd-verified")
        return {
            "id": thread_id,
            "cwd": str(self.cwd),
            "name": self.name,
            "projectId": None,
            "turns": self.turns,
        }


class FakeAppServerCreateClient:
    def __init__(
        self,
        canonical_cwd: Path,
        events: list[str],
        *,
        thread_id: str,
        project_id: str | None = None,
        fail_create: bool = False,
    ) -> None:
        self.canonical_cwd = canonical_cwd
        self.events = events
        self.thread_id = thread_id
        self.project_id = project_id
        self.fail_create = fail_create
        self.process_exited = False
        self.name: str | None = None

    def __enter__(self):
        self.events.append("app-server-connected")
        return self

    def __exit__(self, *_args):
        self.process_exited = True
        self.events.append("app-server-exited")

    def list_permission_profiles(self, cwd):
        self.events.append("permission-profile-verified")
        return [{"id": ":workspace", "allowed": True}]

    def read_project(self, project_id):
        self.events.append("app-server-project-verified")
        return {
            "id": project_id,
            "roots": [{"path": str(self.canonical_cwd)}],
        }

    def ensure_project_root(self, project_id, root):
        self.events.append("app-server-project-root-ensured")
        return {
            "id": project_id,
            "roots": [{"path": str(root)}],
        }

    def start_thread(self, **kwargs):
        self.events.append("thread-start-called")
        self.start_kwargs = kwargs
        if self.fail_create:
            raise AppServerRpcError("thread/start", {"message": "known failure"})
        self.project_id = kwargs["project_id"]
        return {
            "thread": {
                "id": self.thread_id,
                "cwd": str(self.canonical_cwd),
                "projectId": self.project_id,
            },
            "activePermissionProfile": {"id": ":workspace"},
        }

    def name_thread(self, thread_id, name):
        self.events.append("thread-name-set")
        self.name = name

    def assign_thread_to_project(self, thread_id, project_id):
        self.events.append("thread-project-assigned")
        self.project_id = project_id
        return {
            "id": thread_id,
            "cwd": str(self.canonical_cwd),
            "name": self.name,
            "projectId": project_id,
        }

    def read_thread(self, thread_id):
        self.events.append("thread-metadata-read")
        return {
            "id": thread_id,
            "cwd": str(self.canonical_cwd),
            "name": self.name,
            "projectId": self.project_id,
            "turns": [],
        }


class FakeAutomaticWaitClient:
    def __init__(self, events: list[str], owner: str, owner_turn: str) -> None:
        self.events = events
        self.owner = owner
        self.owner_turn = owner_turn
        self.process_exited = False

    def __enter__(self):
        self.events.append("wait-app-server-connected")
        return self

    def __exit__(self, *_args):
        self.process_exited = True
        self.events.append("wait-app-server-exited")

    def read_thread(self, thread_id):
        self.events.append("causal-predecessor-read")
        return {
            "id": thread_id,
            "turns": [{"id": self.owner_turn, "status": "completed", "items": []}],
        }


class FakeAutomaticProductionClient:
    def __init__(
        self,
        root: Path,
        events: list[str],
        *,
        thread_id: str,
        on_complete,
    ) -> None:
        self.root = root
        self.events = events
        self.thread_id = thread_id
        self.on_complete = on_complete
        self.process_exited = False
        self.prompt = ""

    def __enter__(self):
        self.events.append("production-app-server-connected")
        return self

    def __exit__(self, *_args):
        self.process_exited = True
        self.events.append("production-app-server-exited")

    def resume_thread(self, thread_id):
        self.events.append("production-thread-resumed")
        return {
            "thread": {
                "id": thread_id,
                "cwd": str(self.root),
                "projectId": None,
            }
        }

    def start_turn(self, *, thread_id, prompt, **_kwargs):
        self.events.append("production-turn-started")
        self.prompt = prompt
        return {"turn": {"id": "turn-a"}}

    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        self.events.append("production-turn-completed")
        self.on_complete()
        return TurnResult(
            thread_id,
            {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "AUTOPILOT_STATUS: ROTATE",
                    }
                ],
            },
            [],
        )


class FakeV07DispatcherClient(FakeAppServerCreateClient):
    """One App Server connection owns create, production, and completion."""

    def __init__(
        self,
        root: Path,
        events: list[str],
        *,
        owner: str,
        owner_turn: str,
        thread_id: str,
        on_complete,
    ) -> None:
        super().__init__(root, events, thread_id=thread_id)
        self.owner = owner
        self.owner_turn = owner_turn
        self.on_complete = on_complete
        self.prompt = ""

    def read_thread(self, thread_id):
        if thread_id == self.owner:
            self.events.append("causal-predecessor-read")
            return {
                "id": thread_id,
                "turns": [
                    {"id": self.owner_turn, "status": "completed", "items": []}
                ],
            }
        return super().read_thread(thread_id)

    def start_turn(self, *, thread_id, prompt, **_kwargs):
        self.events.append("production-turn-started")
        self.prompt = prompt
        return {"turn": {"id": "turn-a"}}

    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        self.events.append("production-turn-completed")
        self.on_complete()
        return TurnResult(
            thread_id,
            {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "AUTOPILOT_STATUS: ROTATE",
                    }
                ],
            },
            [],
        )


class NoNamePrepClient(FakePrepClient):
    def read_thread(self, thread_id):
        metadata = super().read_thread(thread_id)
        metadata.pop("name", None)
        return metadata


class WrongNamePrepClient(FakePrepClient):
    def name_thread(self, thread_id, name):
        self.events.append(("thread-name-set", thread_id, name))
        self.name = f"{name} (changed)"


class DesktopLifecycleTests(unittest.TestCase):

    def activate(self, descriptor, thread_id: str):
        """Живой путь: так задачу поднимает продакшен-диспетчер."""
        return activate_via_app_server(self.cfg, self.root, descriptor, thread_id)

    def bypass_launch_gate(self):
        """Эти тесты проверяют резервирование, а не подтверждение запуска.

        Диспетчер здесь замокан, поэтому сессия никогда не станет ACTIVE и
        гейт честно ответит "запуск не подтверждён". Сам гейт покрыт
        отдельно в test_launch_gate.py.
        """

        return mock.patch(
            "codex_autopilot.control._launch_report",
            side_effect=lambda cfg, task_ids, *, started, timeout: {
                "continue": True,
                "systemMessage": started,
            },
        )

    def setUp(self) -> None:
        # Гейт доверия хукам читает НАСТОЯЩИЙ App Server машины. Без этой
        # подстановки набор проходил только потому, что у разработчика хуки
        # оказались доверены, и рушился сразу после переустановки плагина.
        # Патч ровно один на точку вызова: второй поверх первого сделал бы
        # проверки мока бессмысленными.
        self.hook_gate_mock = patch_hook_trust_gates(self)["lifecycle_reservations"]
        self.automatic_dispatch = mock.patch(
            "codex_autopilot.control.spawn_automatic_app_server_relay",
            return_value=4242,
        )
        self.automatic_dispatch_mock = self.automatic_dispatch.start()
        self.addCleanup(self.automatic_dispatch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(graph()), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
            worker_surface=DESKTOP_OWNED_SURFACE,
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.root / ".codex-autopilot")
        self.memory = ProjectMemory(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def legacy_retry_fixture(self):
        root = self.root / "legacy"
        (root / ".git").mkdir(parents=True)
        skill = root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = root / "input-plan.json"
        plan_file.write_text(json.dumps(legacy_graph()), encoding="utf-8")
        initialize_project(
            root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
            worker_surface=DESKTOP_OWNED_SURFACE,
        )
        cfg = load_config(root)
        store = StateStore(root / ".codex-autopilot")
        memory = ProjectMemory(root)

        def activate(descriptor, thread_id: str) -> None:
            # Живой путь вместо выведенного слот-релея.
            activate_via_app_server(cfg, root, descriptor, thread_id)

        def complete(descriptor, thread_id: str):
            bump_task_checkpoint(root, descriptor.task_id, f"Completed: {descriptor.task_id}")
            memory.record_evidence(
                kind="test",
                summary=f"{descriptor.task_id} passed.",
                created_by="desktop-lifecycle-test",
                milestone_id=descriptor.task_id,
                command=f"verify {descriptor.task_id}",
                result="PASS",
                exit_code=0,
            )
            implementation = complete_desktop_worker(
                cfg,
                thread_id=thread_id,
                turn_id=f"turn-{thread_id}",
                final_message="AUTOPILOT_STATUS: ROTATE",
            )
            verifier = implementation.descriptors[0]
            verifier_thread = f"verify-{thread_id}"
            activate(verifier, verifier_thread)
            bump_task_checkpoint(
                root, descriptor.task_id, f"Independently verified: {descriptor.task_id}"
            )
            memory.record_evidence(
                kind="test",
                summary=f"{descriptor.task_id} independently passed.",
                created_by="independent-desktop-lifecycle-test",
                milestone_id=descriptor.task_id,
                role="independent_verification",
                command=f"independently verify {descriptor.task_id}",
                result="PASS",
                exit_code=0,
            )
            return complete_desktop_worker(
                cfg,
                thread_id=verifier_thread,
                turn_id=f"turn-{verifier_thread}",
                final_message=(
                    'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}'
                ),
            )

        m6 = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="initiator-thread",
        )[0]
        activate(m6, "M6-thread")
        m7 = complete(m6, "M6-thread").descriptors[0]
        activate(m7, "M7-thread")
        m8 = complete(m7, "M7-thread").descriptors[0]
        record_desktop_failure(
            cfg,
            m8.reservation_token,
            reason="definitive create failure",
            definitive=True,
            now_epoch=100,
            reserve_other_ready=False,
            relay_executor_thread_id="M7-thread",
        )
        state = store.load()
        failed = next(
            item
            for item in state.worker_sessions
            if item.get("reservation_token") == m8.reservation_token
        )
        failed["relay_owner_thread_id"] = None
        for event in state.lifecycle_journal:
            if event.get("reservation_token") == m8.reservation_token:
                event["relay_owner_thread_id"] = None
        store.save(state)
        return root, cfg, store, m8

    def test_cli_exposes_no_model_mediated_transport_commands(self) -> None:
        obsolete = (
            "relay-reserve",
            "relay-payload",
            "relay-create-payload",
            "relay-app-server-create",
            "relay-ack",
            "relay-create-ack",
            "relay-release-ack",
            "relay-prep",
            "relay-send-payload",
            "relay-send-ack",
        )
        for command in obsolete:
            with self.subTest(command=command), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    cli_parser().parse_args([command])

    def test_automatic_dispatch_spawn_is_idempotent_for_one_reservation(self) -> None:
        descriptor = reserve_ready_frontier(
            self.cfg,
            relay_owner_thread_id="M8-thread",
        )[0]
        process = mock.Mock(pid=4321)
        with mock.patch(
            "codex_autopilot.control.subprocess.Popen",
            return_value=process,
        ) as popen, mock.patch(
            "codex_autopilot.control.pid_alive",
            return_value=True,
        ):
            first = spawn_automatic_app_server_relay(
                self.root,
                reservation_token=descriptor.reservation_token,
                initiator_thread_id="M8-thread",
                initiator_turn_id="M8-turn",
            )
            second = spawn_automatic_app_server_relay(
                self.root,
                reservation_token=descriptor.reservation_token,
                initiator_thread_id="M8-thread",
                initiator_turn_id="M8-turn",
            )

        self.assertEqual((first, second), (4321, 4321))
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("_relay_dispatch", command)
        self.assertIn(descriptor.reservation_token, command)
        self.assertNotIn("relay-send-payload", command)
        self.assertIn(
            str(Path(__file__).resolve().parents[1] / "src"),
            popen.call_args.kwargs["env"]["PYTHONPATH"].split(os.pathsep),
        )

    def test_one_dispatcher_uses_a_fresh_app_server_for_each_successor(self) -> None:
        clients = []

        class ClosedClient:
            def __init__(inner_self, binary, log_path, **kwargs):
                inner_self.binary = binary
                inner_self.log_path = log_path
                inner_self.kwargs = kwargs
                inner_self.closed = False
                inner_self.proc = mock.Mock()
                inner_self.proc.poll.side_effect = (
                    lambda: 0 if inner_self.closed else None
                )
                clients.append(inner_self)

            def __enter__(inner_self):
                return inner_self

            def __exit__(inner_self, *_args):
                inner_self.closed = True

        first_successor = mock.Mock(reservation_token="token-b")
        outcomes = [
            mock.Mock(descriptors=(first_successor,)),
            mock.Mock(descriptors=()),
        ]
        with mock.patch(
            "codex_autopilot.cli.AppServerClient",
            ClosedClient,
        ), mock.patch(
            "codex_autopilot.cli.run_automatic_app_server_turn",
            side_effect=outcomes,
        ) as run_turn, mock.patch(
            "codex_autopilot.cli.record_automatic_app_server_exit",
        ) as record_exit, mock.patch(
            "codex_autopilot.cli.adopt_automatic_dispatcher_successor",
            return_value=("thread-a", "turn-a"),
        ) as adopt:
            result = _run_automatic_relay_dispatch(
                self.cfg,
                token="token-a",
                owner="initiator",
                owner_turn="initiator-turn",
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(client.closed for client in clients))
        self.assertEqual(
            [client.log_path.name for client in clients],
            [
                "app-server-dispatcher-token-a.jsonl",
                "app-server-dispatcher-token-b.jsonl",
            ],
        )
        self.assertTrue(
            all(
                client.kwargs.get("originator", DESKTOP_ORIGINATOR) == DESKTOP_ORIGINATOR
                for client in clients
            )
        )
        self.assertEqual(
            [call.args[1] for call in run_turn.call_args_list],
            ["token-a", "token-b"],
        )
        self.assertEqual(record_exit.call_count, 2)
        adopt.assert_called_once_with(
            self.cfg,
            completed_reservation_token="token-a",
            successor_reservation_token="token-b",
        )

    def test_live_automatic_worker_stop_is_observational(self) -> None:
        descriptor = reserve_ready_frontier(
            self.cfg,
            relay_owner_thread_id="initiator",
        )[0]
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        session["thread_id"] = "thread-a"
        session["status"] = "ACTIVE"
        session["automatic_dispatch_state"] = "RUNNING"
        session["automatic_dispatch_pid"] = 31337
        self.store.save(state)
        self.automatic_dispatch_mock.reset_mock()

        with mock.patch(
            "codex_autopilot.control.pid_alive",
            return_value=True,
        ), mock.patch(
            "codex_autopilot.control.complete_desktop_worker",
        ) as complete:
            result = handle_stop_hook(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(self.root),
                    "session_id": "thread-a",
                    "turn_id": "turn-a",
                    "last_assistant_message": "AUTOPILOT_STATUS: ROTATE",
                }
            )

        self.assertEqual(result, {})
        complete.assert_not_called()
        self.automatic_dispatch_mock.assert_not_called()

    def test_automatic_failure_releases_stale_dispatcher_identity(self) -> None:
        descriptor = reserve_ready_frontier(
            self.cfg,
            relay_owner_thread_id="initiator",
        )[0]
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        session["automatic_dispatch_state"] = "RUNNING"
        session["automatic_dispatch_pid"] = 31337
        session["automatic_dispatch_connection_pid"] = 31337
        self.store.save(state)

        record_desktop_failure(
            self.cfg,
            descriptor.reservation_token,
            reason="definitive automatic create failure",
            definitive=True,
            now_epoch=100,
            reserve_other_ready=False,
            relay_executor_thread_id="initiator",
        )

        failed = next(
            item
            for item in self.store.load().worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        self.assertEqual(failed["status"], "RETRY_WAIT")
        self.assertEqual(failed["automatic_dispatch_state"], "RETRY_WAIT")
        self.assertIsNone(failed["automatic_dispatch_pid"])
        self.assertIsNone(failed["automatic_dispatch_connection_pid"])

    def test_resume_reconciles_dead_dispatcher_identity_from_older_runtime(self) -> None:
        descriptor = reserve_ready_frontier(
            self.cfg,
            relay_owner_thread_id="initiator",
        )[0]
        record_desktop_failure(
            self.cfg,
            descriptor.reservation_token,
            reason="interrupted by the older runtime",
            definitive=True,
            now_epoch=100,
            reserve_other_ready=False,
            relay_executor_thread_id="initiator",
        )
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        session["automatic_dispatch_state"] = "RUNNING"
        session["automatic_dispatch_pid"] = 99_999_999
        session["automatic_dispatch_connection_pid"] = 99_999_999
        self.store.save(state)

        reconcile_desktop_runtime(self.cfg, now_epoch=100)

        reconciled = next(
            item
            for item in self.store.load().worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        self.assertEqual(reconciled["automatic_dispatch_state"], "RETRY_WAIT")
        self.assertIsNone(reconciled["automatic_dispatch_pid"])
        self.assertIsNone(reconciled["automatic_dispatch_connection_pid"])

    def evidence_and_handoff(self, task_id: str) -> None:
        bump_task_checkpoint(self.root, task_id, f"Completed: {task_id}")
        self.memory.record_evidence(
            kind="test",
            summary=f"{task_id} lifecycle verification passed.",
            created_by="desktop-lifecycle-test",
            milestone_id=task_id,
            command=f"verify {task_id}",
            result="PASS",
            exit_code=0,
        )


    def test_created_thread_is_explicitly_assigned_to_the_saved_project(self) -> None:
        """Создание с projectId и явная привязка - разные вызовы.

        Второй существовал в живом прогоне (событие
        app_server_project_assigned 11.09), но кода не осталось ни в одном
        коммите и ни в одной установленной версии: работа была потеряна, и
        ветки перестали доходить до проекта.
        """

        from dataclasses import replace

        cfg = replace(
            self.cfg,
            desktop=replace(self.cfg.desktop, project_id="app-server-project"),
        )
        descriptor = reserve_ready_frontier(cfg)[0]
        client, events = activate_via_app_server(
            cfg, self.root, descriptor, "thread-a"
        )
        self.assertIn("thread-project-assigned", events)
        self.assertEqual(client.project_id, "app-server-project")
        assigned = [
            item
            for item in self.store.load().lifecycle_journal
            if item["event"] == "app_server_project_assigned"
            and item["reservation_token"] == descriptor.reservation_token
        ]
        self.assertEqual(len(assigned), 1)
        self.assertIn("thread-a", assigned[0]["detail"])

    def test_without_a_saved_project_nothing_is_assigned(self) -> None:
        descriptor = reserve_ready_frontier(self.cfg)[0]
        _, events = activate_via_app_server(
            self.cfg, self.root, descriptor, "thread-a"
        )
        self.assertNotIn("thread-project-assigned", events)

    def test_dispatcher_preserves_app_server_project_metadata_before_turn(self) -> None:
        self.cfg = replace(
            self.cfg,
            desktop=replace(self.cfg.desktop, project_id="app-server-ui-project"),
        )
        state = self.store.load()
        state.project_id = "app-server-ui-project"
        self.store.save(state)
        descriptor = reserve_ready_frontier(
            self.cfg,
            relay_owner_thread_id="M8-thread",
        )[0]
        events: list[str] = []
        client = FakeAppServerCreateClient(
            self.root,
            events,
            thread_id="M9-thread",
        )
        with mock.patch(
            "codex_autopilot.lifecycle_dispatch.installed_plugin_root",
            return_value=self.root,
        ):
            created = create_desktop_thread_via_app_server(
                self.cfg,
                descriptor.reservation_token,
                client_factory=lambda *_args: client,
                relay_executor_thread_id="M8-thread",
            )

        self.assertEqual(
            client.start_kwargs["project_id"],
            "app-server-ui-project",
        )
        self.assertEqual(created["app_server_project_id"], "app-server-ui-project")
        self.assertLess(
            events.index("app-server-project-root-ensured"),
            events.index("thread-start-called"),
        )
        self.assertLess(
            events.index("thread-start-called"),
            events.index("thread-metadata-read"),
        )
        session = next(
            item
            for item in self.store.load().worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        self.assertEqual(session["actual_project_id"], "app-server-ui-project")
        self.assertIn(
            "project-scoped thread/start",
            session["project_association_verification"],
        )

    def test_authorized_dispatcher_survives_modified_hook_without_chat_relay(self) -> None:
        descriptor = reserve_ready_frontier(
            self.cfg,
            relay_owner_thread_id="M8-thread",
        )[0]
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        session["automatic_dispatch_state"] = "RUNNING"
        session["automatic_dispatch_pid"] = os.getpid()
        self.store.save(state)
        self.hook_gate_mock.reset_mock()
        self.hook_gate_mock.side_effect = HookTrustApprovalRequired(
            plugin_id="codex-autopilot-adaptive@codex-autopilot-local",
            trust_status="modified",
            current_hash="sha256:new",
        )
        events: list[str] = []
        dispatcher_client = FakeV07DispatcherClient(
            self.root,
            events,
            owner="M8-thread",
            owner_turn="M8-turn",
            thread_id="thread-a",
            on_complete=lambda: self.evidence_and_handoff("A"),
        )

        with mock.patch(
            "codex_autopilot.lifecycle_dispatch.installed_plugin_root",
            return_value=self.root,
        ):
            outcome = run_automatic_app_server_turn(
                self.cfg,
                descriptor.reservation_token,
                initiator_thread_id="M8-thread",
                initiator_turn_id="M8-turn",
                connected_client=dispatcher_client,
            )

        self.assertTrue(outcome.matched)
        self.assertEqual(outcome.worker_status, "ROTATE")
        self.assertEqual(outcome.descriptors[0].kind, "verifier")
        self.assertEqual(dispatcher_client.prompt, descriptor.prompt)
        self.assertLess(
            events.index("causal-predecessor-read"),
            events.index("thread-start-called"),
        )
        self.assertLess(
            events.index("thread-start-called"),
            events.index("production-turn-started"),
        )
        self.assertNotIn("codex-app-create-thread", events)
        self.assertNotIn("codex-app-send-message", events)
        self.assertNotIn("app-server-exited", events)
        self.hook_gate_mock.assert_not_called()
        state = self.store.load()
        completed = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(completed["thread_id"], "thread-a")
        record_automatic_app_server_exit(
            self.cfg,
            descriptor.reservation_token,
            dispatcher_pid=os.getpid(),
        )
        exited = next(
            item
            for item in self.store.load().worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        self.assertTrue(exited["app_server_worker_exited_at"])
        self.assertIsNone(exited["automatic_dispatch_connection_pid"])
        self.assertIn(
            "app_server_worker_process_exited",
            [
                item["event"]
                for item in self.store.load().lifecycle_journal
                if item.get("reservation_token")
                == descriptor.reservation_token
            ],
        )
        owner, owner_turn = adopt_automatic_dispatcher_successor(
            self.cfg,
            completed_reservation_token=descriptor.reservation_token,
            successor_reservation_token=outcome.descriptors[0].reservation_token,
        )
        self.assertEqual((owner, owner_turn), ("thread-a", "turn-a"))
        successor = next(
            item
            for item in self.store.load().worker_sessions
            if item["reservation_token"]
            == outcome.descriptors[0].reservation_token
        )
        self.assertEqual(successor["automatic_dispatch_state"], "RUNNING")
        self.assertEqual(successor["automatic_dispatch_pid"], os.getpid())


    def test_reserves_only_two_parallel_sol_workers_after_trust_gate(self) -> None:
        self.assertEqual(self.cfg.runtime.worker_surface, DESKTOP_OWNED_SURFACE)
        descriptors = reserve_ready_frontier(self.cfg)
        self.hook_gate_mock.assert_called_once_with(self.cfg)
        self.assertEqual([item.task_id for item in descriptors], ["A", "B"])
        self.assertEqual({item.model for item in descriptors}, {"gpt-5.6-sol"})
        self.assertTrue(all(item.surface == DESKTOP_OWNED_SURFACE for item in descriptors))
        self.assertEqual(
            [item.title for item in descriptors],
            ["Builder | A | Task A", "Builder | B | Task B"],
        )
        self.assertTrue(all(item.run_id not in item.title for item in descriptors))
        self.assertEqual(reserve_ready_frontier(self.cfg), ())
        state = self.store.load()
        self.assertEqual(state.active_task_ids, ["A", "B"])
        self.assertEqual(len(state.worker_sessions), 2)
        self.assertEqual(len({item["reservation_token"] for item in state.worker_sessions}), 2)

    def test_parallel_workers_cannot_satisfy_each_others_checkpoint(self) -> None:
        """M10-REV-005: чекпойнт задачный, не общий.

        Раньше reserve_ready_frontier считал ОДИН хэш общего HANDOFF.md
        и штамповал его всем зарезервированным задачам. Гейт завершения
        проверял только "хэш общего файла изменился", поэтому первый
        записавший воркер закрывал гейт всем остальным, а параллельная
        запись в один файл теряла правки при непересекающихся ресурсах.
        """
        descriptors = reserve_ready_frontier(self.cfg)
        self.assertEqual([item.task_id for item in descriptors], ["A", "B"])
        first, second = descriptors

        state = self.store.load()
        sessions = {
            item["task_id"]: item
            for item in state.worker_sessions
            if item["reservation_token"] in {first.reservation_token, second.reservation_token}
        }
        # Каждая задача несёт СВОЙ чекпойнт, а не общий хэш на всех.
        self.assertEqual(sessions["A"]["checkpoint_before"], "")
        self.assertEqual(sessions["B"]["checkpoint_before"], "")
        self.assertNotEqual(
            task_checkpoint_path(self.cfg.state_dir, "A"),
            task_checkpoint_path(self.cfg.state_dir, "B"),
        )

        # Работает только A.
        self.activate(first, "thread-a")
        self.activate(second, "thread-b")
        self.evidence_and_handoff("A")

        # B не может завершиться за счёт записи A: своё evidence есть,
        # своего чекпойнта нет.
        self.memory.record_evidence(
            kind="test",
            summary="B lifecycle verification passed.",
            created_by="desktop-lifecycle-test",
            milestone_id="B",
            command="verify B",
            result="PASS",
            exit_code=0,
        )
        with self.assertRaises(DesktopLifecycleError) as caught:
            complete_desktop_worker(
                self.cfg,
                thread_id="thread-b",
                turn_id="turn-thread-b",
                final_message="AUTOPILOT_STATUS: ROTATE",
            )
        self.assertIn("its own checkpoint file", str(caught.exception))
        self.assertIn("B.md", str(caught.exception))

        # A завершается штатно: его собственный файл изменился.
        complete_desktop_worker(
            self.cfg,
            thread_id="thread-a",
            turn_id="turn-thread-a",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )

        # Запись A не попала в файл B: подмена невозможна.
        self.assertFalse(task_checkpoint_path(self.cfg.state_dir, "B").is_file())

        # B завершается только после собственной записи.
        self.evidence_and_handoff("B")
        complete_desktop_worker(
            self.cfg,
            thread_id="thread-b",
            turn_id="turn-thread-b",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        self.assertIn("Completed: B", task_checkpoint_path(self.cfg.state_dir, "B").read_text(encoding="utf-8"))
        self.assertNotIn("Completed: B", task_checkpoint_path(self.cfg.state_dir, "A").read_text(encoding="utf-8"))

    def test_superseded_desktop_task_fails_closed_beside_its_replacement(self) -> None:
        """M10-REV-006: детерминированный повтор наблюдённой последовательности.

        На самом аудите M10 исходная резервация оставалась в RETRY_WAIT,
        новая становилась ACTIVE, а прерванная Desktop-задача оставалась
        адресуемой и продолжала менять то же рабочее дерево: у исходников
        и тестов менялись mtime во время аудита.
        """
        first = reserve_ready_frontier(self.cfg)[0]
        self.assertEqual(first.task_id, "A")
        self.activate(first, "thread-a-attempt-1")

        state = self.store.load()
        original = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == first.reservation_token
        )
        self.assertEqual(original["status"], "ACTIVE")
        self.assertEqual(original["thread_id"], "thread-a-attempt-1")

        # Прерывание: задача уходит в RETRY_WAIT, тред остаётся живым.
        record_desktop_interrupt(
            self.cfg,
            thread_id="thread-a-attempt-1",
            turn_id="turn-a-attempt-1",
        )

        # Замена берёт ту же задачу.
        replacement = next(
            item
            for item in reserve_ready_frontier(self.cfg, now_epoch=2_000_000_000)
            if item.task_id == "A"
        )
        self.assertNotEqual(replacement.reservation_token, first.reservation_token)

        state = self.store.load()
        original = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == first.reservation_token
        )
        self.assertEqual(original["status"], "RETIRED_SUPERSEDED")
        self.assertIn("replacement reservation", original["retired_reason"])
        self.assertIn(
            "session_retired_superseded",
            [event["event"] for event in state.lifecycle_journal],
        )

        # Прерванная Desktop-задача получает ввод и обязана упасть закрыто,
        # а не продолжить производство рядом с активной попыткой.
        with self.assertRaises(DesktopLifecycleError) as caught:
            complete_desktop_worker(
                self.cfg,
                thread_id="thread-a-attempt-1",
                turn_id="turn-a-attempt-1-late",
                final_message="AUTOPILOT_STATUS: ROTATE",
            )
        self.assertIn("superseded", str(caught.exception))
        self.assertIn("must not continue production", str(caught.exception))


    def test_failed_trust_gate_cannot_reserve_frontier(self) -> None:
        self.hook_gate_mock.side_effect = HookTrustApprovalRequired(
            plugin_id="codex-autopilot-adaptive@codex-autopilot-local",
            trust_status="untrusted",
            current_hash="sha256:new",
        )
        state_path = self.root / ".codex-autopilot" / "run-state.json"
        before = state_path.read_bytes()
        with self.assertRaises(HookTrustApprovalRequired):
            reserve_ready_frontier(self.cfg)
        self.assertEqual(state_path.read_bytes(), before)


    def test_atomic_reservation_prevents_duplicate_concurrent_creation(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(pool.map(lambda _: reserve_ready_frontier(self.cfg), range(2)))
        descriptors = [item for batch in batches for item in batch]
        self.assertEqual([item.task_id for item in descriptors], ["A", "B"])
        self.assertEqual(len({item.reservation_token for item in descriptors}), 2)
        self.assertEqual(len(pending_descriptors(self.cfg)), 2)

    def test_conflicting_ready_tasks_wait_even_when_worker_capacity_is_open(self) -> None:
        plan_file = self.root / "conflict-plan.json"
        conflict_graph = graph()
        conflict_graph["tasks"] = [
            task("A", path="src/shared"),
            task("B", path="src/shared"),
            task("C", depends_on=("A", "B"), path="src/c"),
        ]
        plan_file.write_text(json.dumps(conflict_graph), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            replace=True,
            desktop_project_id="desktop-project",
            worker_surface=DESKTOP_OWNED_SURFACE,
        )
        descriptors = reserve_ready_frontier(load_config(self.root))
        self.assertEqual([item.task_id for item in descriptors], ["A"])
        self.assertEqual(self.store.load().task_states["B"], TaskState.READY.value)

    def test_authoritative_completion_unlocks_dependency_just_in_time(self) -> None:
        a, b = reserve_ready_frontier(self.cfg)
        self.activate(a, "thread-a")
        self.activate(b, "thread-b")

        self.evidence_and_handoff("A")
        first = complete_desktop_worker(
            self.cfg,
            thread_id="thread-a",
            turn_id="turn-a",
            final_message="A complete.\nAUTOPILOT_STATUS: ROTATE",
        )
        self.assertEqual(first.descriptors[0].kind, "verifier")
        self.activate(first.descriptors[0], "verifier-a")
        self.evidence_and_handoff("A")
        accepted_a = complete_desktop_worker(
            self.cfg,
            thread_id="verifier-a",
            turn_id="verifier-turn-a",
            final_message='AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}',
        )
        self.assertEqual(accepted_a.descriptors, ())
        self.assertEqual(self.store.load().task_states["C"], TaskState.WAITING.value)

        self.evidence_and_handoff("B")
        second = complete_desktop_worker(
            self.cfg,
            thread_id="thread-b",
            turn_id="turn-b",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        self.assertEqual(second.descriptors[0].kind, "verifier")
        self.activate(second.descriptors[0], "verifier-b")
        self.evidence_and_handoff("B")
        accepted_b = complete_desktop_worker(
            self.cfg,
            thread_id="verifier-b",
            turn_id="verifier-turn-b",
            final_message='AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}',
        )
        self.assertEqual([item.task_id for item in accepted_b.descriptors], ["C"])
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.VERIFIED.value)
        self.assertEqual(state.task_states["B"], TaskState.VERIFIED.value)
        self.assertEqual(state.task_states["C"], TaskState.RUNNING.value)
        events = [item["event"] for item in state.lifecycle_journal]
        self.assertIn("turn_identity_bound", events)
        self.assertIn("turn_completed", events)
        self.assertIn("wait_registered", events)

    def test_non_self_policy_records_implemented_then_starts_fresh_verifier(self) -> None:
        plan_file = self.root / "independent-plan.json"
        independent_graph = graph(max_workers=1)
        independent_graph["tasks"] = [
            task("A", verification_policy="independent"),
            task("B", depends_on=("A",)),
        ]
        plan_file.write_text(json.dumps(independent_graph), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            replace=True,
            desktop_project_id="desktop-project",
            worker_surface=DESKTOP_OWNED_SURFACE,
        )
        cfg = load_config(self.root)
        descriptor = reserve_ready_frontier(cfg)[0]
        self.cfg = cfg
        self.activate(descriptor, "thread-a")
        self.memory = ProjectMemory(self.root)
        self.evidence_and_handoff("A")
        outcome = complete_desktop_worker(
            cfg,
            thread_id="thread-a",
            turn_id="turn-a",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        self.assertEqual(len(outcome.descriptors), 1)
        self.assertEqual(outcome.descriptors[0].kind, "verifier")
        verifier_prompt = outcome.descriptors[0].prompt
        self.assertIn(independent_graph["user_request"], verifier_prompt)
        self.assertIn(independent_graph["goal"], verifier_prompt)
        self.assertIn(
            "Independently compare the result with acceptance_gate.original_user_request",
            verifier_prompt,
        )
        self.assertIn("Implementer-authored tests are evidence only", verifier_prompt)
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.VERIFYING.value)
        self.assertEqual(state.task_states["B"], TaskState.WAITING.value)
        implemented = next(
            item for item in state.lifecycle_journal if item["event"] == "implementation_completed"
        )
        self.assertEqual(implemented["detail"], TaskState.IMPLEMENTED.value)

    def test_crash_recovery_keeps_one_ambiguous_reservation_and_dependencies_locked(self) -> None:
        first = reserve_ready_frontier(self.cfg)
        reloaded = load_config(self.root)
        self.assertEqual(reserve_ready_frontier(reloaded), ())
        record_desktop_failure(
            reloaded,
            first[0].reservation_token,
            reason="create result connection lost",
            definitive=False,
        )
        self.assertEqual(reserve_ready_frontier(reloaded), ())
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.RUNNING.value)
        self.assertEqual(state.task_states["C"], TaskState.WAITING.value)
        ambiguous = next(
            item for item in state.worker_sessions if item["reservation_token"] == first[0].reservation_token
        )
        self.assertEqual(ambiguous["status"], "AMBIGUOUS")


    def test_platform_handoff_identity_reconciliation_preserves_completion(self) -> None:
        descriptor = reserve_ready_frontier(self.cfg)[0]
        self.activate(descriptor, "original-thread")
        self.evidence_and_handoff("A")

        unmatched = complete_desktop_worker(
            self.cfg,
            thread_id="continued-thread",
            turn_id="continued-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        self.assertFalse(unmatched.matched)

        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="continued-thread",
            source_thread_id="original-thread",
            turn_id="continued-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )

        self.assertTrue(outcome.matched)
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        self.assertEqual(session["thread_id"], "continued-thread")
        self.assertEqual(session["thread_identity_history"], ["original-thread"])
        events = [item["event"] for item in state.lifecycle_journal]
        self.assertLess(
            events.index("thread_identity_reconciled"),
            events.index("turn_completed"),
        )
        event = next(
            item
            for item in state.lifecycle_journal
            if item["event"] == "thread_identity_reconciled"
        )
        detail = json.loads(event["detail"])
        self.assertEqual(detail["previous_thread_id"], "original-thread")
        self.assertEqual(detail["current_thread_id"], "continued-thread")

    def test_identity_reconciliation_requires_exact_active_reservation(self) -> None:
        descriptor = reserve_ready_frontier(self.cfg)[0]
        self.activate(descriptor, "original-thread")

        with self.assertRaisesRegex(DesktopLifecycleError, "source does not match"):
            reconcile_desktop_thread_identity(
                self.cfg,
                descriptor.reservation_token,
                previous_thread_id="wrong-thread",
                current_thread_id="continued-thread",
                expected_task_id="A",
            )
        with self.assertRaisesRegex(DesktopLifecycleError, "another task"):
            reconcile_desktop_thread_identity(
                self.cfg,
                descriptor.reservation_token,
                previous_thread_id="original-thread",
                current_thread_id="continued-thread",
                expected_task_id="B",
            )


    def test_one_rate_limit_preserves_other_worker_and_retries_deterministically(self) -> None:
        a, b = reserve_ready_frontier(self.cfg, now_epoch=100)
        self.activate(b, "thread-b")
        self.assertEqual(
            record_desktop_failure(
                self.cfg,
                a.reservation_token,
                reason="rate limited",
                definitive=True,
                rate_limited=True,
                now_epoch=100,
            ),
            (),
        )
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.RETRY_WAIT.value)
        self.assertEqual(state.task_states["B"], TaskState.RUNNING.value)
        self.assertEqual(state.active_task_ids, ["B"])
        retry_at = state.task_retry_at["A"]
        self.assertEqual(reserve_ready_frontier(self.cfg, now_epoch=retry_at - 1), ())
        retry = reserve_ready_frontier(self.cfg, now_epoch=retry_at)
        self.assertEqual([item.task_id for item in retry], ["A"])
        self.assertEqual(retry[0].attempt, 2)

    def test_interrupt_is_journaled_with_exact_desktop_thread_and_turn(self) -> None:
        a, _b = reserve_ready_frontier(self.cfg)
        self.activate(a, "thread-a")
        self.assertTrue(
            record_desktop_interrupt(
                self.cfg,
                thread_id="thread-a",
                turn_id="turn-interrupted",
                now_epoch=100,
            )
        )
        state = self.store.load()
        event = next(
            item for item in state.lifecycle_journal if item["event"] == "interrupt_observed"
        )
        self.assertEqual(event["thread_id"], "thread-a")
        self.assertEqual(event["turn_id"], "turn-interrupted")
        self.assertEqual(state.task_states["A"], TaskState.RETRY_WAIT.value)

    def test_prep_exit_is_required_and_detached_dispatcher_is_forbidden(self) -> None:
        state = self.store.load()
        state.prep_app_server_exited_at = None
        self.store.save(state)
        with self.assertRaisesRegex(DesktopLifecycleError, "full process exit"):
            reserve_ready_frontier(self.cfg)
        with self.assertRaisesRegex(RuntimeError, "cannot start through"):
            spawn_dispatcher(self.root)

    def test_stop_hook_reserves_and_requests_same_task_relay(self) -> None:
        self.enterContext(self.bypass_launch_gate())
        arm(self.root)
        with mock.patch("codex_autopilot.control.spawn_dispatcher") as spawn, mock.patch(
            "codex_autopilot.appserver.AppServerClient.resume_thread"
        ) as resume, mock.patch(
            "codex_autopilot.control.spawn_automatic_app_server_relay",
            return_value=42,
        ) as automatic_dispatch:
            result = handle_stop_hook(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(self.root),
                    "session_id": "initiator",
                    "turn_id": "initiator-turn",
                    "last_assistant_message": "initialized",
                    "stop_hook_active": False,
                }
            )
        spawn.assert_not_called()
        resume.assert_not_called()
        self.assertTrue(result.get("continue"))
        self.assertIn("automatic dispatcher started", result.get("systemMessage", ""))
        self.assertEqual(automatic_dispatch.call_count, 2)
        self.assertEqual(
            [item["status"] for item in self.store.load().worker_sessions],
            ["CREATE_REQUESTED", "CREATE_REQUESTED"],
        )


    def test_m7_stop_alone_recovers_due_legacy_m8_retry(self) -> None:
        self.enterContext(self.bypass_launch_gate())
        root, cfg, store, first_m8 = self.legacy_retry_fixture()
        state_path = root / ".codex-autopilot" / "run-state.json"
        retry_at = store.load().task_retry_at["M8"]

        self.hook_gate_mock.reset_mock()
        before = state_path.read_bytes()
        with mock.patch("codex_autopilot.lifecycle_reservations.time.time", return_value=retry_at - 1):
            self.assertEqual(
                handle_stop_hook(
                    {
                        "hook_event_name": "Stop",
                        "cwd": str(root),
                        "session_id": "M7-thread",
                        "turn_id": "too-early-turn",
                        "last_assistant_message": "relay recovery ping",
                    }
                ),
                {},
            )
        self.assertEqual(state_path.read_bytes(), before)

        with mock.patch("codex_autopilot.lifecycle_reservations.time.time", return_value=retry_at):
            with self.assertRaisesRegex(
                DesktopLifecycleError,
                "belongs to owner thread M7-thread",
            ):
                reserve_ready_frontier(
                    cfg,
                    relay_owner_thread_id="root-thread",
                )
        self.assertEqual(state_path.read_bytes(), before)

        for foreign_thread in ("root-thread", "M6-thread"):
            with mock.patch(
                "codex_autopilot.lifecycle_reservations.time.time", return_value=retry_at
            ):
                self.assertEqual(
                    handle_stop_hook(
                        {
                            "hook_event_name": "Stop",
                            "cwd": str(root),
                            "session_id": foreign_thread,
                            "turn_id": f"turn-{foreign_thread}",
                            "last_assistant_message": "relay recovery ping",
                        }
                    ),
                    {},
                )
            self.assertEqual(state_path.read_bytes(), before)

        self.hook_gate_mock.reset_mock()
        with mock.patch("codex_autopilot.lifecycle_reservations.time.time", return_value=retry_at):
            recovered = handle_stop_hook(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(root),
                    "session_id": "M7-thread",
                    "turn_id": "M7-recovery-turn",
                    "last_assistant_message": "relay recovery ping",
                }
            )
        self.assertTrue(recovered.get("continue"))
        self.assertIn("automatic retry dispatcher", recovered.get("systemMessage", ""))
        state = store.load()
        m8_sessions = [
            item for item in state.worker_sessions if item.get("task_id") == "M8"
        ]
        self.assertEqual(len(m8_sessions), 2)
        self.assertEqual(m8_sessions[0]["reservation_token"], first_m8.reservation_token)
        # M10-REV-006 сюда не применяется: у этой сессии создание не
        # состоялось и Desktop-треда нет, продолжать производство нечему.
        # Ограждается только адресуемая задача - см.
        # test_superseded_desktop_task_fails_closed_beside_its_replacement.
        self.assertEqual(m8_sessions[0]["status"], "RETRY_WAIT")
        self.assertFalse(str(m8_sessions[0].get("thread_id") or "").strip())
        self.assertEqual(m8_sessions[0]["relay_owner_thread_id"], "M7-thread")
        self.assertEqual(m8_sessions[1]["attempt"], 2)
        self.assertEqual(m8_sessions[1]["status"], "CREATE_REQUESTED")
        self.assertEqual(m8_sessions[1]["relay_owner_thread_id"], "M7-thread")
        self.assertEqual(state.task_states["M8"], TaskState.RUNNING.value)
        self.assertEqual(state.active_task_ids, ["M8"])
        self.hook_gate_mock.assert_called_once_with(cfg)

        after_recovery = state_path.read_bytes()
        repeated = handle_stop_hook(
            {
                "hook_event_name": "Stop",
                "cwd": str(root),
                "session_id": "M7-thread",
                "turn_id": "M7-repeated-turn",
                "last_assistant_message": "relay recovery ping",
            }
        )
        self.assertTrue(repeated.get("continue"))
        self.assertIn("automatic dispatcher", repeated.get("systemMessage", ""))
        self.assertEqual(state_path.read_bytes(), after_recovery)

        record_desktop_failure(
            cfg,
            m8_sessions[1]["reservation_token"],
            reason="second definitive create failure",
            definitive=True,
            now_epoch=retry_at,
            reserve_other_ready=False,
            relay_executor_thread_id="M7-thread",
        )
        second_retry_at = store.load().task_retry_at["M8"]
        with mock.patch(
            "codex_autopilot.lifecycle_reservations.time.time", return_value=second_retry_at
        ):
            third = handle_stop_hook(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(root),
                    "session_id": "M7-thread",
                    "turn_id": "M7-second-recovery-turn",
                    "last_assistant_message": "relay recovery ping",
                }
            )
        self.assertTrue(third.get("continue"))
        self.assertIn("automatic retry dispatcher", third.get("systemMessage", ""))
        state = store.load()
        m8_sessions = [
            item for item in state.worker_sessions if item.get("task_id") == "M8"
        ]
        self.assertEqual([item["attempt"] for item in m8_sessions], [1, 2, 3])
        self.assertEqual(m8_sessions[-1]["status"], "CREATE_REQUESTED")
        self.assertEqual(m8_sessions[-1]["relay_owner_thread_id"], "M7-thread")

    def test_retry_derived_ready_state_keeps_m7_owner_requirement(self) -> None:
        self.enterContext(self.bypass_launch_gate())
        root, cfg, store, _first_m8 = self.legacy_retry_fixture()
        state = store.load()
        retry_at = state.task_retry_at.pop("M8")
        state.task_states["M8"] = TaskState.READY.value
        store.save(state)
        state_path = root / ".codex-autopilot" / "run-state.json"
        before = state_path.read_bytes()

        with mock.patch("codex_autopilot.lifecycle_reservations.time.time", return_value=retry_at):
            with self.assertRaisesRegex(
                DesktopLifecycleError,
                "belongs to owner thread M7-thread",
            ):
                reserve_ready_frontier(
                    cfg,
                    relay_owner_thread_id="root-thread",
                )
        self.assertEqual(state_path.read_bytes(), before)

        with mock.patch("codex_autopilot.lifecycle_reservations.time.time", return_value=retry_at):
            recovered = handle_stop_hook(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(root),
                    "session_id": "M7-thread",
                    "turn_id": "M7-ready-recovery-turn",
                    "last_assistant_message": "relay recovery ping",
                }
            )
        self.assertTrue(recovered.get("continue"))
        self.assertIn("automatic retry dispatcher", recovered.get("systemMessage", ""))
        latest = [
            item
            for item in store.load().worker_sessions
            if item.get("task_id") == "M8"
        ][-1]
        self.assertEqual(latest["attempt"], 2)
        self.assertEqual(latest["relay_owner_thread_id"], "M7-thread")

    def test_nonnull_foreign_retry_owner_without_causal_worker_fails_closed(self) -> None:
        root, cfg, store, first_m8 = self.legacy_retry_fixture()
        state = store.load()
        retry_at = state.task_retry_at["M8"]
        failed = next(
            item
            for item in state.worker_sessions
            if item.get("reservation_token") == first_m8.reservation_token
        )
        failed["relay_owner_thread_id"] = "root-thread"
        reservation = next(
            item
            for item in state.lifecycle_journal
            if item.get("event") == "reservation_created"
            and item.get("reservation_token") == first_m8.reservation_token
        )
        reservation["relay_owner_thread_id"] = "root-thread"
        store.save(state)
        state_path = root / ".codex-autopilot" / "run-state.json"
        before = state_path.read_bytes()

        with mock.patch("codex_autopilot.lifecycle_reservations.time.time", return_value=retry_at):
            self.assertEqual(
                handle_stop_hook(
                    {
                        "hook_event_name": "Stop",
                        "cwd": str(root),
                        "session_id": "root-thread",
                        "turn_id": "root-recovery-turn",
                        "last_assistant_message": "relay recovery ping",
                    }
                ),
                {},
            )
            with self.assertRaisesRegex(
                DesktopLifecycleError,
                "no unique authoritative predecessor owner",
            ):
                reserve_ready_frontier(
                    cfg,
                    relay_owner_thread_id="root-thread",
                )
        self.assertEqual(state_path.read_bytes(), before)

    def test_first_legacy_task_retry_preserves_its_bound_initiator_owner(self) -> None:
        root = self.root / "legacy-first"
        (root / ".git").mkdir(parents=True)
        skill = root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_data = legacy_graph()
        plan_data["milestones"] = plan_data["milestones"][:1]
        plan_file = root / "input-plan.json"
        plan_file.write_text(json.dumps(plan_data), encoding="utf-8")
        initialize_project(
            root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
            worker_surface=DESKTOP_OWNED_SURFACE,
        )
        cfg = load_config(root)
        first = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="initiator-thread",
        )[0]
        record_desktop_failure(
            cfg,
            first.reservation_token,
            reason="definitive create failure",
            definitive=True,
            now_epoch=100,
            reserve_other_ready=False,
            relay_executor_thread_id="initiator-thread",
        )
        retry_at = StateStore(root / ".codex-autopilot").load().task_retry_at["M6"]
        with self.assertRaisesRegex(
            DesktopLifecycleError,
            "belongs to owner thread initiator-thread",
        ):
            reserve_ready_frontier(
                cfg,
                now_epoch=retry_at,
                relay_owner_thread_id="root-thread",
            )
        retry = reserve_ready_frontier(
            cfg,
            now_epoch=retry_at,
            relay_owner_thread_id="initiator-thread",
        )
        self.assertEqual(retry[0].attempt, 2)
        state = StateStore(root / ".codex-autopilot").load()
        latest = [item for item in state.worker_sessions if item["task_id"] == "M6"][-1]
        self.assertEqual(latest["relay_owner_thread_id"], "initiator-thread")

    def test_worker_completion_binds_new_frontier_to_that_worker_thread(self) -> None:
        a, b = reserve_ready_frontier(
            self.cfg,
            relay_owner_thread_id="initiator-thread",
        )
        self.activate(a, "M7-thread")
        self.activate(b, "parallel-thread")
        self.evidence_and_handoff("B")
        complete_desktop_worker(
            self.cfg,
            thread_id="parallel-thread",
            turn_id="parallel-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        ).descriptors[0]
        verifier_b = next(
            item
            for item in pending_descriptors(self.cfg)
            if item.task_id == "B" and item.kind == "verifier"
        )
        self.activate(verifier_b, "parallel-verifier")
        self.evidence_and_handoff("B")
        complete_desktop_worker(
            self.cfg,
            thread_id="parallel-verifier",
            turn_id="parallel-verifier-turn",
            final_message='AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}',
        )
        self.evidence_and_handoff("A")
        implementation_outcome = complete_desktop_worker(
            self.cfg,
            thread_id="M7-thread",
            turn_id="M7-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        verifier_a = implementation_outcome.descriptors[0]
        self.activate(verifier_a, "M7-verifier-thread")
        self.evidence_and_handoff("A")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="M7-verifier-thread",
            turn_id="M7-verifier-turn",
            final_message='AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}',
        )
        self.assertEqual([item.task_id for item in outcome.descriptors], ["C"])
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == outcome.descriptors[0].reservation_token
        )
        self.assertEqual(session["relay_owner_thread_id"], "M7-thread")


if __name__ == "__main__":
    unittest.main()
