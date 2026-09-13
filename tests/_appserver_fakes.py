"""Активация резервации по ЖИВОМУ пути.

Прежде тесты доводили резервацию до ACTIVE выведенным слот-релеем.
Этого пути в продакшене нет, и держать на нём тесты означало проверять
то, чем система не пользуется. Здесь то же состояние достигается так,
как это делает диспетчер: create_desktop_thread_via_app_server,
затем claim_automatic_app_server_turn.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

from codex_autopilot.appserver import AppServerRpcError
from codex_autopilot.run_state import StateStore
from codex_autopilot.lifecycle import (
    acknowledge_desktop_send,
    claim_automatic_app_server_turn,
    create_desktop_thread_via_app_server,
)


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
        # Настоящий клиент ведёт учёт загруженных им веток: ход стартует
        # только на загруженной, и по этому множеству диспетчер решает,
        # нужен ли resume. Подделка без него моделировала соединение,
        # которое якобы загрузило всё на свете.
        self.subscribed_thread_ids: set[str] = set()

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

    def ensure_project_root(self, project_id, root, *, authorized=False):
        # Подделка повторяет контракт настоящего клиента: членство
        # проверяется, а корень дописывается только по разрешению.
        from codex_autopilot.appserver import ProjectRootDrift

        self.events.append("app-server-project-root-ensured")
        known = self.read_project(project_id)
        existing = tuple(
            Path(str(item["path"])).expanduser().resolve()
            for item in known.get("roots") or []
        )
        canonical = Path(str(root)).expanduser().resolve()
        if any(_within(canonical, item) for item in existing):
            return known
        if not authorized:
            raise ProjectRootDrift(project_id, canonical, existing)
        self.events.append("app-server-project-root-added")
        return {
            "id": project_id,
            "roots": [{"path": str(item)} for item in (*existing, canonical)],
        }

    def resume_thread(self, thread_id):
        self.events.append("thread-resumed")
        self.subscribed_thread_ids.add(thread_id)
        return {"thread": self.read_thread(thread_id)}

    def start_thread(self, **kwargs):
        self.events.append("thread-start-called")
        self.start_kwargs = kwargs
        if self.fail_create:
            raise AppServerRpcError("thread/start", {"message": "known failure"})
        self.project_id = kwargs["project_id"]
        self.subscribed_thread_ids.add(self.thread_id)
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


def activate_via_app_server(cfg, root, descriptor, thread_id, *, owner=None):
    """Довести резервацию до ACTIVE тем же путём, что и продакшен."""
    if owner is None:
        # Владелец берётся из самой резервации: тесты создают её
        # с разными идентификаторами, и угадывать его нельзя.
        state = StateStore(cfg.state_dir).load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        owner = str(session.get("relay_owner_thread_id") or "")
    events: list[str] = []
    client = FakeAppServerCreateClient(Path(root), events, thread_id=thread_id)
    with mock.patch(
        "codex_autopilot.lifecycle_dispatch.installed_plugin_root",
        return_value=Path(root),
    ):
        create_desktop_thread_via_app_server(
            cfg,
            descriptor.reservation_token,
            client_factory=lambda *_args: client,
            relay_executor_thread_id=owner,
        )
    claim_automatic_app_server_turn(
        cfg,
        descriptor.reservation_token,
        relay_executor_thread_id=owner,
    )
    # SEND_RELAYING -> ACTIVE: на живом пути это делает
    # run_automatic_app_server_turn после старта production-хода.
    acknowledge_desktop_send(cfg, descriptor.reservation_token, thread_id=thread_id)
    return client, events


def _within(target, root) -> bool:
    try:
        Path(str(target)).relative_to(Path(str(root)))
    except ValueError:
        return False
    return True
