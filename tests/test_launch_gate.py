"""Гейт запуска: подтверждение вместо заявления, и тикет вместо самодеятельности."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from _gates import patch_hook_trust_gates

from codex_autopilot.launch_gate import (
    LaunchCheck,
    LaunchVerdict,
    launch_verdict,
    launch_checklist,
    launch_confirmed,
    render_launch_checklist,
)
from codex_autopilot.run_state import RunState


class Cfg:
    """Минимальная подстановка: гейт читает только каталог состояния."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir


def session(**overrides) -> dict:
    base = {
        "task_id": "A",
        "reservation_token": "token-a",
        "status": "ACTIVE",
        "thread_id": "thread-a",
        "automatic_dispatch_pid": 4242,
    }
    base.update(overrides)
    return base


def journal(*events: tuple[int, str]) -> list[dict]:
    return [
        {"sequence": number, "event": name, "reservation_token": "token-a"}
        for number, name in events
    ]


LAUNCHED = (
    (1, "reservation_created"),
    (2, "create_requested"),
    (3, "app_server_thread_created"),
    (4, "start_acknowledged"),
    (5, "visible_launch_report_ready"),
)


class ChecklistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Cfg(Path(self.temp.name))
        # Проверка видимости читает НАСТОЯЩИЙ каталог Codex. Тест обязан
        # смотреть в свой, иначе результат зависит от того, что сейчас
        # открыто у разработчика в сайдбаре.
        self.codex_home = Path(self.temp.name) / "codex-home"
        self.codex_home.mkdir()
        self.desktop_knows_thread(True)
        import unittest.mock as _mock

        patcher = _mock.patch(
            "codex_autopilot.preflight.default_codex_home", return_value=self.codex_home
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def desktop_knows_thread(self, known: bool, thread_id: str = "thread-a") -> None:
        import json as _json

        payload = {
            "thread-project-assignments": {thread_id: "project-a"} if known else {},
            "sidebar-project-thread-orders": {},
        }
        (self.codex_home / ".codex-global-state.json").write_text(
            _json.dumps(payload), encoding="utf-8"
        )

    def state(self, *, sessions, events) -> RunState:
        state = RunState(run_id="r")
        state.worker_sessions = list(sessions)
        state.lifecycle_journal = list(events)
        return state

    def check(self, checks, check_id: str) -> LaunchCheck:
        return next(item for item in checks if item.id == check_id)

    def test_a_fully_launched_task_is_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(sessions=[session()], events=journal(*LAUNCHED)),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertTrue(launch_confirmed(checks))
        self.assertIn("ЗАПУСК ПОДТВЕРЖДЁН", render_launch_checklist(checks))

    def test_a_task_that_was_never_reserved_is_not_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg, self.state(sessions=[], events=[]), task_ids=["A"]
        )
        self.assertFalse(launch_confirmed(checks))
        self.assertFalse(self.check(checks, "reserved").passed)

    def test_a_reservation_without_a_thread_is_not_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(thread_id=None, status="CREATE_REQUESTED")],
                events=journal((1, "reservation_created")),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertFalse(launch_confirmed(checks))
        self.assertFalse(self.check(checks, "thread_bound").passed)

    def test_a_thread_that_never_acknowledged_the_send_is_not_confirmed(self) -> None:
        """Именно так выглядела зависшая задача: ветка есть, работа не идёт."""

        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(status="SEND_RELAYING")],
                events=journal(*LAUNCHED[:3]),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertFalse(launch_confirmed(checks))
        self.assertFalse(self.check(checks, "send_acknowledged").passed)

    def test_a_failure_recorded_after_the_launch_is_not_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session()],
                events=journal(*LAUNCHED, (6, "interrupt_observed")),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertFalse(launch_confirmed(checks))
        self.assertIn("interrupt_observed", self.check(checks, "no_failure_after_launch").detail)

    def test_a_dead_dispatcher_is_not_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(sessions=[session()], events=journal(*LAUNCHED)),
            task_ids=["A"],
            pid_alive=lambda pid: False,
        )
        self.assertFalse(launch_confirmed(checks))

    def test_an_unassessable_check_is_not_a_pass(self) -> None:
        """"Проверить не удалось" и "проверено" - разные вещи."""

        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(automatic_dispatch_pid=None)],
                events=journal(*LAUNCHED),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertIsNone(self.check(checks, "dispatcher_alive").passed)
        self.assertFalse(launch_confirmed(checks))

    def test_an_empty_checklist_never_confirms(self) -> None:
        self.assertFalse(launch_confirmed([]))

    def test_the_rendered_verdict_names_a_failure_as_a_failure(self) -> None:
        checks = launch_checklist(
            self.cfg, self.state(sessions=[], events=[]), task_ids=["A"]
        )
        rendered = render_launch_checklist(checks)
        self.assertIn("ЗАПУСК ОТКАЗАЛ", rendered)
        self.assertIn("это отказ, а не успех", rendered)


class DesktopVisibilityTests(ChecklistTests):
    """R5: успех App Server не означает, что ветка видна в сайдбаре."""

    def visibility(self, checks) -> LaunchCheck:
        return self.check(checks, "visible_in_desktop")

    def checks_now(self):
        return launch_checklist(
            self.cfg,
            self.state(sessions=[session()], events=journal(*LAUNCHED)),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )

    def test_a_thread_desktop_knows_is_visible(self) -> None:
        self.assertTrue(self.visibility(self.checks_now()).passed)

    def test_a_thread_missing_from_desktop_records_is_reported(self) -> None:
        """Ровно этот случай: задача идёт, а в интерфейсе её нет."""

        self.desktop_knows_thread(False)
        check = self.visibility(self.checks_now())
        self.assertFalse(check.passed)
        self.assertIn("в сайдбаре", check.detail)

    def test_missing_desktop_state_is_unassessable_not_invisible(self) -> None:
        (self.codex_home / ".codex-global-state.json").unlink()
        self.assertIsNone(self.visibility(self.checks_now()).passed)

    def test_invisibility_is_reported_but_never_becomes_a_ticket(self) -> None:
        """Desktop пишет своё состояние не мгновенно: отказ по его задержке
        снова плодил бы ложные тикеты."""

        self.desktop_knows_thread(False)
        self.assertIs(launch_verdict(self.checks_now()), LaunchVerdict.IN_PROGRESS)


class VerdictTests(ChecklistTests):
    """Три состояния: подтверждён, ещё идёт, отказал."""

    def test_a_launch_still_creating_its_thread_is_in_progress(self) -> None:
        """Создание ветки занимает десятки секунд - это не отказ."""

        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(thread_id=None, status="CREATE_REQUESTED")],
                events=journal((1, "reservation_created"), (2, "create_requested")),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertIs(launch_verdict(checks), LaunchVerdict.IN_PROGRESS)
        self.assertIn("ЗАПУСК ИДЁТ", render_launch_checklist(checks))

    def test_a_dead_dispatcher_is_a_failure_not_progress(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(thread_id=None, status="CREATE_REQUESTED")],
                events=journal((1, "reservation_created")),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: False,
        )
        self.assertIs(launch_verdict(checks), LaunchVerdict.FAILED)

    def test_a_failure_event_is_a_failure_not_progress(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(status="RETRY_WAIT")],
                events=journal(*LAUNCHED, (6, "interrupt_observed")),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertIs(launch_verdict(checks), LaunchVerdict.FAILED)

    def test_a_complete_launch_is_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(sessions=[session()], events=journal(*LAUNCHED)),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertIs(launch_verdict(checks), LaunchVerdict.CONFIRMED)


class UnconfirmedLaunchGoesToDevOpsTests(unittest.TestCase):
    """Задача не поднялась - сессия не чинит сама, а заводит тикет."""

    def setUp(self) -> None:
        from unittest import mock

        from codex_autopilot.bootstrap import initialize_project
        from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
        import json

        from test_desktop_lifecycle import graph

        gate = mock.patch(
            "codex_autopilot.lifecycle_reservations.require_trusted_stop_hook_for_config"
        )
        gate.start()
        self.addCleanup(gate.stop)
        # Гейт доверия хукам читает НАСТОЯЩИЙ App Server машины. Без этой
        # подстановки набор проходил только потому, что у разработчика хуки
        # оказались доверены, и рушился сразу после переустановки плагина.
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
            worker_surface=DESKTOP_OWNED_SURFACE,
        )
        self.cfg = load_config(self.root)

    def report(self):
        from codex_autopilot.control import _launch_report

        return _launch_report(
            self.cfg, ["A"], started="Codex Autopilot dispatcher started", timeout=0.0
        )

    def test_a_launch_in_progress_does_not_open_a_ticket(self) -> None:
        """Ложный тикет на идущий запуск - тот самый шум, из-за которого
        проверки перестают читать."""

        from codex_autopilot.pipeline_engineer import PipelineIncidentStore
        from unittest import mock

        with mock.patch(
            "codex_autopilot.control.launch_verdict",
            return_value=__import__(
                "codex_autopilot.launch_gate", fromlist=["LaunchVerdict"]
            ).LaunchVerdict.IN_PROGRESS,
        ):
            result = self.report()
        self.assertTrue(result.get("continue"))
        self.assertEqual(PipelineIncidentStore(self.cfg.state_dir).load()["incidents"], [])

    def test_an_unconfirmed_launch_blocks_instead_of_claiming_success(self) -> None:
        result = self.report()
        self.assertEqual(result.get("decision"), "block")
        self.assertNotIn("continue", result)
        self.assertIn("ЗАПУСК ОТКАЗАЛ", result["reason"])

    def test_an_unconfirmed_launch_opens_a_devops_ticket(self) -> None:
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        result = self.report()
        self.assertIn("Тикет", result["reason"])
        incidents = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["code"], "launch_not_confirmed")
        self.assertEqual(incidents[0]["affected_task_ids"], ["A"])

    def test_the_session_is_told_not_to_repair_the_pipeline_itself(self) -> None:
        self.assertIn("Не чини запуск в этом ходе", self.report()["reason"])

    def test_the_ticket_does_not_claim_an_owner_that_does_not_exist(self) -> None:
        """Ссылка на несуществующего девопса - ложь, а не маршрутизация."""

        reason = self.report()["reason"]
        self.assertIn("Автоматический исполнитель не поднят", reason)
        self.assertNotIn("владелец — DevOps", reason)

    def test_the_same_failure_twice_is_one_signature(self) -> None:
        """Нормализованная подпись: повтор опознаётся как повтор."""

        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        self.report()
        self.report()
        ledger = PipelineIncidentStore(self.cfg.state_dir).signature_ledger()
        self.assertEqual(len(ledger), 1)


if __name__ == "__main__":
    unittest.main()
