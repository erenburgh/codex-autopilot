"""Сквозной путь возобновления: от фразы пользователя до начала работы.

Каждый шаг этой цепочки был сломан по отдельности, и каждый вскрывался
только живым запуском - по одному за круг, с участием человека и
переустановкой рантайма. Ни один не был покрыт тестом.

Цепочка целиком:

    фраза -> вооружение -> Stop-хук изымает запрос -> резервация
    -> создание ветки -> размещение в проекте -> взятие хода -> старт

Каждый тест ниже закрывает один из семи найденных дефектов, чтобы
следующий такой же ловился здесь, а не на живом прогоне.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _appserver_fakes import activate_via_app_server
from _gates import patch_hook_trust_gates
from _relay import TEST_RELAY_OWNER, reserve_ready_frontier
from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
from codex_autopilot.control import handle_prompt_hook, handle_stop_hook
from codex_autopilot.run_state import StateStore
from test_desktop_lifecycle import graph


class ResumeChainTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(graph()), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        # Реестр взведённых стартов один на пользователя и живёт в TMPDIR.
        # Без изоляции этот набор оставлял в нём запись про свой временный
        # каталог, и живой Stop-хук потом отказывался запускать что-либо:
        # "multiple Autopilot starts are armed".
        registry = Path(tempfile.mkdtemp(prefix="codex-autopilot-launch-registry-")) / "requests"
        launch_dir = mock.patch.dict(
            os.environ, {"CODEX_AUTOPILOT_LAUNCH_DIR": str(registry)}
        )
        launch_dir.start()
        self.addCleanup(launch_dir.stop)
        self.store = StateStore(self.cfg.state_dir)

        self.spawned: list[str] = []
        spawn = mock.patch(
            "codex_autopilot.control.spawn_automatic_app_server_relay",
            side_effect=lambda root, *, reservation_token, **_kw: (
                self.spawned.append(reservation_token) or 4242
            ),
        )
        spawn.start()
        self.addCleanup(spawn.stop)

        # Гейт размещения читает настоящий каталог Codex: в тесте он обязан
        # смотреть в свой, иначе результат зависит от машины.
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.write_desktop_state()
        home = mock.patch(
            "codex_autopilot.preflight.default_codex_home", return_value=self.codex_home
        )
        home.start()
        self.addCleanup(home.stop)

    def write_desktop_state(self, threads: dict | None = None) -> None:
        (self.codex_home / ".codex-global-state.json").write_text(
            json.dumps({"thread-project-assignments": threads or {}}),
            encoding="utf-8",
        )

    def resume(self) -> dict:
        return handle_prompt_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "продолжи кодекс автопайлот",
                "cwd": str(self.root),
            }
        )

    def stop(self, **overrides) -> dict:
        payload = {
            "hook_event_name": "Stop",
            "cwd": str(self.root),
            # Хук приходит от того же владельца, что держит резервацию:
            # именно так это выглядит на живом пути.
            "session_id": TEST_RELAY_OWNER,
            "turn_id": "owner-turn",
            "last_assistant_message": "готово",
            "stop_hook_active": False,
        }
        payload.update(overrides)
        return handle_stop_hook(payload)

    # --- 1. фраза доходит до вооружения -------------------------------

    def test_the_russian_phrase_arms_the_resume(self) -> None:
        """Кириллица в названии продукта не должна ломать команду."""

        self.resume()
        state = self.store.load()
        self.assertEqual((state.status, state.phase), ("READY", "ARMED"))

    # --- 2. Stop-хук изымает запрос и заводит работу ------------------

    def test_stop_hook_reserves_and_spawns_after_arming(self) -> None:
        self.resume()
        result = self.stop()
        self.assertTrue(self.spawned, "ни одна резервация не поднята")
        self.assertIn("reason", result)

    # --- 3. изъятый запрос не исчезает, если состояние ушло вперёд ----

    def test_an_advanced_state_does_not_swallow_the_armed_request(self) -> None:
        """Дефект, из-за которого возобновление пропадало без следа.

        Хук изымает запрос, потом видит, что состояние уже не READY/ARMED,
        и возвращает пустоту: ни процесса, ни журнала, ни сообщения.
        """

        descriptor = reserve_ready_frontier(self.cfg)[0]
        self.resume()
        state = self.store.load()
        state.status = "RUNNING"
        state.phase = "PLAN_CHANGE_DRAINING"
        self.store.save(state)

        result = self.stop()
        self.assertIn(descriptor.reservation_token, self.spawned)
        self.assertIn("reason", result)

    # --- 4. висящая резервация без живого диспетчера поднимается ------

    def test_a_reservation_whose_dispatcher_died_is_revived(self) -> None:
        descriptor = reserve_ready_frontier(self.cfg)[0]
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        session["automatic_dispatch_pid"] = 999999  # заведомо мёртвый
        self.store.save(state)

        self.resume()
        self.stop()
        self.assertIn(descriptor.reservation_token, self.spawned)

    # --- 5. создание, размещение и старт ------------------------------

    def test_dispatcher_creates_places_and_starts_the_task(self) -> None:
        """Вторая половина цепочки: то, что делает отсоединённый процесс."""

        descriptor = reserve_ready_frontier(self.cfg)[0]
        self.write_desktop_state({"thread-a": {"projectId": "desktop-project"}})
        activate_via_app_server(self.cfg, self.root, descriptor, "thread-a")
        session = next(
            item
            for item in self.store.load().worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        self.assertEqual(session["status"], "ACTIVE")
        self.assertEqual(session["thread_id"], "thread-a")

    # --- 6. отчёт содержит лестницу шагов -----------------------------

    def test_the_report_shows_the_ladder_of_steps(self) -> None:
        self.resume()
        result = self.stop()
        report = result.get("reason") or result.get("systemMessage") or ""
        self.assertIn("слот зарезервирован", report)
        self.assertIn("ЗАПУСК", report)


if __name__ == "__main__":
    unittest.main()


class DeadRelayWithoutAThreadIsNotADeadEndTests(unittest.TestCase):
    """Релей, умерший до создания ветки, не должен запирать прогон.

    В живом прогоне сессия осталась в RELAYING с пустым thread_id: процесс
    умер между «начал» и «создал». Запуск отвечал
    `automatic relay cannot spawn from 'RELAYING'`, а разобрать эту сессию
    не мог никто - наблюдать со стороны App Server тоже нечего, ветки не
    существует. Прогон становился неоживимым, хотя не было создано ничего.
    """

    def test_a_relaying_session_without_a_thread_can_respawn(self) -> None:
        from codex_autopilot import control

        session = {
            "reservation_token": "t1",
            "relay_owner_thread_id": "owner",
            "status": "RELAYING",
            "thread_id": None,
            "automatic_dispatch_pid": 999_999_999,
            "automatic_dispatch_state": "RUNNING",
        }
        control._revive_dead_relay_session(session)
        self.assertEqual(session["status"], "CREATE_REQUESTED")
        self.assertIsNone(session["automatic_dispatch_pid"])

    def test_a_relaying_session_with_a_thread_is_left_alone(self) -> None:
        """Ветка есть - побочный эффект был, догадываться нельзя."""

        from codex_autopilot import control

        session = {
            "reservation_token": "t1",
            "status": "RELAYING",
            "thread_id": "01a0-real",
            "automatic_dispatch_pid": 999_999_999,
        }
        self.assertFalse(control._revive_dead_relay_session(session))
        self.assertEqual(session["status"], "RELAYING")
