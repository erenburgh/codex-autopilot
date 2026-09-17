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


class Runtime:
    def __init__(self, required_thread_placement: str = "in_project") -> None:
        self.required_thread_placement = required_thread_placement


class Cfg:
    """Минимальная подстановка: каталог состояния и требование размещения."""

    def __init__(
        self, state_dir: Path, required_thread_placement: str = "in_project"
    ) -> None:
        self.state_dir = state_dir
        self.runtime = Runtime(required_thread_placement)


def session(**overrides) -> dict:
    base = {
        "task_id": "A",
        "reservation_token": "token-a",
        "status": "ACTIVE",
        "thread_id": "thread-a",
        "automatic_dispatch_pid": 4242,
        "desktop_placement": "INSIDE",
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
        import unittest.mock as _mock

        patcher = _mock.patch(
            "codex_autopilot.preflight.default_codex_home", return_value=self.codex_home
        )
        patcher.start()
        self.addCleanup(patcher.stop)

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
        self.assertIn("LAUNCH CONFIRMED", render_launch_checklist(checks))

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
        self.assertIn("LAUNCH FAILED", rendered)
        self.assertIn("this is a failure, not a success", rendered)


class DesktopVisibilityTests(ChecklistTests):
    """R5: успех App Server не означает, что ветка видна в сайдбаре."""

    def visibility(self, checks) -> LaunchCheck:
        return self.check(checks, "visible_in_desktop")

    def checks_now(self, placement: str = "INSIDE"):
        return launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(desktop_placement=placement)],
                events=journal(*LAUNCHED),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )

    def test_a_thread_inside_the_project_is_visible(self) -> None:
        self.assertTrue(self.visibility(self.checks_now()).passed)

    def test_a_thread_outside_the_project_is_reported(self) -> None:
        """Ровно этот случай: задача идёт, а в проекте её нет."""

        check = self.visibility(self.checks_now("OUTSIDE"))
        self.assertFalse(check.passed)
        self.assertIn("outside the project", check.detail)

    def test_a_vanished_thread_is_reported(self) -> None:
        """Ветка без хода на сервере не сохраняется - замерено на пробах."""

        check = self.visibility(self.checks_now("ABSENT"))
        self.assertFalse(check.passed)
        self.assertIn("not persisted", check.detail)

    def test_an_unmeasured_placement_is_unassessable_not_invisible(self) -> None:
        self.assertIsNone(self.visibility(self.checks_now("")).passed)

    def test_an_unmeasured_placement_never_becomes_a_ticket(self) -> None:
        """Между созданием ветки и записью размещения есть окно: отказ по
        нему снова плодил бы ложные тикеты."""

        self.assertIs(launch_verdict(self.checks_now("")), LaunchVerdict.IN_PROGRESS)

    def test_a_config_that_allows_outside_does_not_get_a_ticket(self) -> None:
        """Тикет на то, что конфиг разрешил, - ложный тикет."""

        relaxed = Cfg(self.cfg.state_dir, required_thread_placement="visible")
        checks = launch_checklist(
            relaxed,
            self.state(
                sessions=[session(desktop_placement="OUTSIDE")],
                events=journal(*LAUNCHED),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        visible = next(item for item in checks if item.id == "visible_in_desktop")
        self.assertIs(visible.passed, True)
        self.assertIsNot(launch_verdict(checks), LaunchVerdict.FAILED)

    def test_a_measured_mismatch_does_become_a_ticket(self) -> None:
        """M11-R5: измеренное расхождение - результат, а не окно.

        Прежде OUTSIDE и ABSENT не меняли вердикта вовсе: он держался в
        IN_PROGRESS, тикет не заводился, и задача, созданная мимо
        проекта, просто стояла. Окно защищено отдельно - неизмеренностью,
        а не слепотой к измерению.
        """

        self.assertIs(launch_verdict(self.checks_now("OUTSIDE")), LaunchVerdict.FAILED)
        self.assertIs(launch_verdict(self.checks_now("ABSENT")), LaunchVerdict.FAILED)


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
        self.assertIn("LAUNCH IN PROGRESS", render_launch_checklist(checks))

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

        from _plan_contract import initialize_verified_project as initialize_project
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
        # Отчёт приходит и на идущем запуске.
        self.assertIn("systemMessage", result)
        # Идущий запуск обязан отвечать continue: блокирующий ответ
        # оставляет инициирующий ход в "interrupted", а диспетчер ждёт
        # устойчивого "completed" и не создаёт ветку никогда.
        self.assertTrue(result.get("continue"))
        self.assertNotIn("decision", result)
        self.assertNotIn("Ticket", result["systemMessage"])
        self.assertEqual(PipelineIncidentStore(self.cfg.state_dir).load()["incidents"], [])

    def test_a_launch_in_progress_is_reported_without_a_ticket(self) -> None:
        from codex_autopilot.launch_gate import LaunchVerdict
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore
        from unittest import mock

        with mock.patch(
            "codex_autopilot.control.launch_verdict",
            return_value=LaunchVerdict.IN_PROGRESS,
        ):
            result = self.report()
        self.assertTrue(result.get("continue"))
        self.assertNotIn("Ticket", result["systemMessage"])
        self.assertEqual(PipelineIncidentStore(self.cfg.state_dir).load()["incidents"], [])

    def test_an_unconfirmed_launch_blocks_instead_of_claiming_success(self) -> None:
        result = self.report()
        self.assertEqual(result.get("decision"), "block")
        self.assertNotIn("continue", result)
        self.assertIn("LAUNCH FAILED", result["reason"])

    def test_an_unconfirmed_launch_opens_a_devops_ticket(self) -> None:
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        result = self.report()
        self.assertIn("Ticket", result["reason"])
        incidents = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["code"], "launch_not_confirmed")
        self.assertEqual(incidents[0]["affected_task_ids"], ["A"])

    def test_the_session_is_told_not_to_repair_the_pipeline_itself(self) -> None:
        self.assertIn("Do not repair the launch in this turn", self.report()["reason"])

    def test_the_ticket_does_not_claim_an_owner_that_does_not_exist(self) -> None:
        """Ссылка на несуществующего девопса - ложь, а не маршрутизация."""

        reason = self.report()["reason"]
        self.assertIn("No automatic executor was raised", reason)
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


class PlacementGateTests(unittest.TestCase):
    """Размещение спрашивается у сервера: из его списка рисуется сайдбар.

    Прежняя версия читала ключи .codex-global-state.json и называла OUTSIDE
    три ветки, которые человек видел в сайдбаре глазами. Прибор ни разу не
    был сверен с заведомо видимой веткой, и на его показаниях был построен
    ложный вывод, что видимую задачу через App Server завести нельзя.
    """

    def client(self, thread=None, error=None):
        from unittest import mock

        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        if error is not None:
            fake.read_thread.side_effect = error
        else:
            fake.read_thread.return_value = thread
        return fake

    def test_a_thread_in_the_expected_project_is_inside(self) -> None:
        from codex_autopilot.launch_gate import INSIDE, desktop_placement

        placement = desktop_placement(
            "t1", project_id="p1", client=self.client({"id": "t1", "projectId": "p1"})
        )
        self.assertEqual(placement, INSIDE)

    def test_a_thread_without_a_project_is_outside(self) -> None:
        from codex_autopilot.launch_gate import OUTSIDE, desktop_placement

        placement = desktop_placement(
            "t1", project_id="p1", client=self.client({"id": "t1", "projectId": None})
        )
        self.assertEqual(placement, OUTSIDE)

    def test_a_thread_in_another_project_is_outside(self) -> None:
        from codex_autopilot.launch_gate import OUTSIDE, desktop_placement

        placement = desktop_placement(
            "t1", project_id="p1", client=self.client({"id": "t1", "projectId": "p2"})
        )
        self.assertEqual(placement, OUTSIDE)

    def test_a_vanished_thread_is_absent(self) -> None:
        """Ветка без единого хода на сервере не сохраняется."""

        from codex_autopilot.launch_gate import ABSENT, desktop_placement

        placement = desktop_placement(
            "t1", project_id="p1", client=self.client(error=RuntimeError("thread not found"))
        )
        self.assertEqual(placement, ABSENT)

    def test_an_unbound_reservation_is_absent(self) -> None:
        from codex_autopilot.launch_gate import ABSENT, desktop_placement

        self.assertEqual(desktop_placement("", client=self.client()), ABSENT)

    def test_any_project_counts_when_none_is_required(self) -> None:
        from codex_autopilot.launch_gate import INSIDE, desktop_placement

        placement = desktop_placement(
            "t1", client=self.client({"id": "t1", "projectId": "p2"})
        )
        self.assertEqual(placement, INSIDE)
class OrphanedReservationTests(unittest.TestCase):
    """Резервация есть, ветки нет, диспетчер умер — прогон обязан ожить."""

    def test_a_reservation_without_a_live_dispatcher_is_revived(self) -> None:
        from unittest import mock

        from codex_autopilot.control import _orphaned_pending_descriptors

        cfg = mock.Mock(state_dir=Path('/tmp'))
        alive = mock.Mock(
            reservation_token="live", task_id="A"
        )
        orphan = mock.Mock(reservation_token="orphan", task_id="B")
        state = mock.Mock()
        state.worker_sessions = [
            {"reservation_token": "live", "automatic_dispatch_pid": 111},
            {"reservation_token": "orphan", "automatic_dispatch_pid": None},
        ]
        with mock.patch("codex_autopilot.control.StateStore") as store, mock.patch(
            "codex_autopilot.control.pending_descriptors", return_value=(alive, orphan)
        ), mock.patch(
            "codex_autopilot.control.pid_alive", side_effect=lambda pid: pid == 111
        ):
            store.return_value.load.return_value = state
            revived = _orphaned_pending_descriptors(cfg)
        self.assertEqual([item.reservation_token for item in revived], ["orphan"])

    def test_nothing_is_revived_while_a_dispatcher_is_alive(self) -> None:
        from unittest import mock

        from codex_autopilot.control import _orphaned_pending_descriptors

        alive = mock.Mock(reservation_token="live", task_id="A")
        state = mock.Mock()
        state.worker_sessions = [
            {"reservation_token": "live", "automatic_dispatch_pid": 111}
        ]
        with mock.patch("codex_autopilot.control.StateStore") as store, mock.patch(
            "codex_autopilot.control.pending_descriptors", return_value=(alive,)
        ), mock.patch("codex_autopilot.control.pid_alive", return_value=True):
            store.return_value.load.return_value = state
            self.assertEqual(_orphaned_pending_descriptors(mock.Mock(state_dir=Path('/tmp'))), ())


class CausalPredecessorTests(unittest.TestCase):
    """Завершённость хода доказывается журналом, а не статусом сессии."""

    def state(self, status: str):
        from unittest import mock

        state = mock.Mock()
        state.worker_sessions = [
            {"thread_id": "owner", "turn_id": "turn-1", "status": status}
        ]
        state.lifecycle_journal = [
            {"event": "turn_completed", "thread_id": "owner", "turn_id": "turn-1"}
        ]
        return state

    def test_a_plan_change_requester_is_a_valid_predecessor(self) -> None:
        """Задача, запросившая смену плана, свой ход завершила."""

        from codex_autopilot.control import _turn_is_completed

        self.assertTrue(
            _turn_is_completed(self.state("PLAN_CHANGE_REQUESTED"), "owner", "turn-1")
        )

    def test_a_turn_without_a_completion_record_is_not_accepted(self) -> None:
        from unittest import mock

        from codex_autopilot.control import _turn_is_completed

        state = mock.Mock()
        state.lifecycle_journal = [
            {"event": "turn_identity_bound", "thread_id": "owner", "turn_id": "turn-1"}
        ]
        # Вторым свидетельством служит закрытая сессия с тем же ходом,
        # поэтому заглушке нужен явно пустой список - иначе проверяется
        # поведение Mock, а не правила.
        state.worker_sessions = []
        self.assertFalse(_turn_is_completed(state, "owner", "turn-1"))

    def test_another_threads_completion_does_not_count(self) -> None:
        from codex_autopilot.control import _turn_is_completed

        self.assertFalse(
            _turn_is_completed(self.state("COMPLETED"), "someone-else", "turn-1")
        )


class AFastLaunchIsStillALaunchTests(ChecklistTests):
    """Быстрый воркер не должен объявляться незапущенным.

    Живой прогон получил тикет `launch_not_confirmed: dispatcher_alive,
    send_acknowledged` при том, что в том же чек-листе стояло «ход
    завершён». Оба пункта были ложны ИМЕННО потому, что работа успела
    закончиться: статус сессии ушёл дальше ACTIVE, а диспетчер штатно
    вышел. Чем быстрее веха, тем вероятнее ложный отказ - и каждый такой
    отказ требовал оператора.
    """

    FINISHED = LAUNCHED + ((6, "turn_completed"),)

    def test_a_finished_turn_needs_no_live_dispatcher(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(status="COMPLETED")], events=journal(*self.FINISHED)
            ),
            task_ids=["A"],
            pid_alive=lambda pid: False,
        )
        self.assertTrue(self.check(checks, "dispatcher_alive").passed)

    def test_the_send_is_confirmed_by_the_journal_not_the_moment(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(status="PLAN_CHANGE_REQUESTED")],
                events=journal(*self.FINISHED),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: False,
        )
        self.assertTrue(self.check(checks, "send_acknowledged").passed)

    def test_a_dead_dispatcher_without_a_finished_turn_still_fails(self) -> None:
        """Ослабление не должно прятать настоящую смерть диспетчера."""

        checks = launch_checklist(
            self.cfg,
            self.state(sessions=[session()], events=journal(*LAUNCHED)),
            task_ids=["A"],
            pid_alive=lambda pid: False,
        )
        self.assertFalse(self.check(checks, "dispatcher_alive").passed)
