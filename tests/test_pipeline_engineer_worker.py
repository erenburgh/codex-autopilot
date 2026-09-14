"""Дежурный инженер создаётся как воркер, а не остаётся ярлыком в JSON.

Фаза PIPELINE_ENGINEER существовала как поле: ensure_pipeline_engineer
меняла строку и дописывала событие, а ветку инженера не создавало ничто.
Прогон, упёршийся в неё, вставал молча.

R13: DevOps решает инфраструктурные баги от имени пользователя, и
пользователь не участвует в выборе способа фикса. Поэтому эскалация - не
второй равноправный выход, а исключение с кодом причины.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from codex_autopilot.lifecycle_base import DesktopLifecycleError, SESSION_KINDS
from codex_autopilot.lifecycle_completion import (
    ESCALATION_CODES,
    parse_pipeline_engineer_status,
)
from codex_autopilot.run_state import WORKER_SESSION_KINDS
from codex_autopilot.thread_titles import pipeline_engineer_thread_title


class SessionKindTests(unittest.TestCase):
    def test_the_engineer_is_a_worker_kind(self) -> None:
        self.assertIn("pipeline_engineer", SESSION_KINDS)
        self.assertIn("pipeline_engineer", WORKER_SESSION_KINDS)


class ThreadTitleTests(unittest.TestCase):
    def test_the_title_is_readable_in_the_sidebar(self) -> None:
        title = pipeline_engineer_thread_title(
            "incident-8ea3ceca87b6c8a3", "Запуск не подтверждён чек-листом"
        )
        self.assertTrue(title.startswith("Pipeline Engineer | INC-"))
        self.assertIn("Запуск не подтверждён", title)

    def test_an_empty_identifier_is_refused(self) -> None:
        from codex_autopilot.thread_titles import ThreadTitleError

        with self.assertRaises(ThreadTitleError):
            pipeline_engineer_thread_title("incident-", "что-то")


class ExitProtocolTests(unittest.TestCase):
    def test_resolved_needs_no_code(self) -> None:
        self.assertEqual(
            parse_pipeline_engineer_status("отчёт\nPIPELINE_ENGINEER_STATUS: RESOLVED"),
            ("RESOLVED", ""),
        )

    def test_escalation_requires_a_code_from_the_closed_list(self) -> None:
        for code in sorted(ESCALATION_CODES):
            with self.subTest(code=code):
                self.assertEqual(
                    parse_pipeline_engineer_status(
                        f"отчёт\nPIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER {code}"
                    ),
                    ("ESCALATE_TO_USER", code),
                )

    def test_a_bare_escalation_is_refused(self) -> None:
        """Эскалация без причины - способ обойти R13."""

        with self.assertRaises(DesktopLifecycleError):
            parse_pipeline_engineer_status(
                "отчёт\nPIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER"
            )

    def test_an_invented_code_is_refused(self) -> None:
        with self.assertRaises(DesktopLifecycleError):
            parse_pipeline_engineer_status(
                "отчёт\nPIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER BECAUSE_HARD"
            )

    def test_the_status_must_be_the_last_line(self) -> None:
        with self.assertRaises(DesktopLifecycleError):
            parse_pipeline_engineer_status(
                "PIPELINE_ENGINEER_STATUS: RESOLVED\nещё что-то"
            )

    def test_two_statuses_are_refused(self) -> None:
        with self.assertRaises(DesktopLifecycleError):
            parse_pipeline_engineer_status(
                "PIPELINE_ENGINEER_STATUS: RESOLVED\n"
                "PIPELINE_ENGINEER_STATUS: RESOLVED"
            )


class AuthorityTests(unittest.TestCase):
    """Инженеру названы настоящие команды, а не описан несуществующий путь."""

    def test_the_prompt_names_commands_that_exist(self) -> None:
        import re
        from pathlib import Path

        from codex_autopilot.cli import parser

        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/ai_studio.py"
        ).read_text(encoding="utf-8")
        block = source[source.index("def build_pipeline_engineer_prompt") :]
        block = block[: block.index("def select_context")]
        named = set(re.findall(r"scripts/codex-autopilot (\S+)", block))
        available: set[str] = set()
        for action in parser()._subparsers._group_actions:
            available.update(action.choices)
        self.assertTrue(named, "промпт не называет ни одной команды")
        self.assertEqual(sorted(named - available), [])

    def test_the_prompt_states_full_repair_authority(self) -> None:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/ai_studio.py"
        ).read_text(encoding="utf-8")
        self.assertIn("full authority to repair", source)
        self.assertIn("The user does not choose the repair", source)


if __name__ == "__main__":
    unittest.main()


class EngineerIsActuallyReservedTests(unittest.TestCase):
    """Сквозная проверка: инцидент в фазе PIPELINE_ENGINEER даёт воркера.

    Этого теста не хватало, и цена была прямой: новый код сослался на имя,
    чей импорт сняли раньше как неиспользуемый, а набор из проверок по
    частям - заголовок, разбор статуса, текст промпта - NameError не видел.
    Поймал его только живой прогон.
    """

    def setUp(self) -> None:
        import json as _json
        import tempfile

        from _gates import patch_hook_trust_gates
        from _relay import reserve_ready_frontier
        from codex_autopilot.bootstrap import initialize_project
        from codex_autopilot.config import load_config
        from codex_autopilot.pipeline_engineer import (
            IncidentClass,
            IncidentSignal,
            PipelineIncidentStore,
            SideEffectOutcome,
        )
        from codex_autopilot.run_state import StateStore, utc_now
        from test_desktop_lifecycle import graph

        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(_json.dumps(graph()), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)
        self.reserve = reserve_ready_frontier
        first = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")[0]
        self.task_id = first.task_id

        incidents = PipelineIncidentStore(self.cfg.state_dir)
        incident = incidents.open_incident(
            IncidentSignal(
                signal_id="probe:launch",
                code="launch_not_confirmed",
                surface=IncidentClass.PIPELINE,
                summary="Запуск не подтверждён чек-листом",
                affected_task_ids=(self.task_id,),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
                system_state={},
            ),
            at=utc_now(),
        )
        self.incident_id = str(incident["incident_id"])
        incidents.route_incident(self.incident_id, at=utc_now())
        incidents.ensure_pipeline_engineer(self.incident_id, at=utc_now())

    def engineer_sessions(self) -> list[dict]:
        return [
            item
            for item in self.store.load().worker_sessions
            if item.get("kind") == "pipeline_engineer"
        ]

    def test_an_open_incident_reserves_an_engineer(self) -> None:
        descriptors = self.reserve(self.cfg, relay_owner_thread_id="owner-2")
        self.assertEqual(len(descriptors), 1)
        payload = descriptors[0].to_dict()
        self.assertEqual(payload["kind"], "pipeline_engineer")
        self.assertTrue(str(payload["title"]).startswith("Pipeline Engineer | INC-"))
        self.assertIn("AUTOPILOT_INCIDENT", payload["prompt"])

    def test_the_engineer_is_reserved_once_per_incident(self) -> None:
        self.reserve(self.cfg, relay_owner_thread_id="owner-2")
        self.reserve(self.cfg, relay_owner_thread_id="owner-3")
        self.assertEqual(len(self.engineer_sessions()), 1)

    def test_the_engineer_holds_no_resource_ownership(self) -> None:
        """Ресурсы держит сорвавшаяся сессия; чинить придёт незаблокированный."""

        self.reserve(self.cfg, relay_owner_thread_id="owner-2")
        self.assertIsNone(self.engineer_sessions()[0]["resource_ownership_token"])

    def test_the_engineer_outranks_ordinary_work(self) -> None:
        """Сломанный пайплайн старше задач: пока тикет открыт, работы нет."""

        self.reserve(self.cfg, relay_owner_thread_id="owner-2")
        self.assertEqual(self.reserve(self.cfg, relay_owner_thread_id="owner-3"), ())

    def test_the_incident_is_recorded_on_the_session(self) -> None:
        self.reserve(self.cfg, relay_owner_thread_id="owner-2")
        self.assertEqual(self.engineer_sessions()[0]["incident_id"], self.incident_id)


class ServerViewTests(unittest.TestCase):
    """Справку о ветках собирает диспетчер, а не инженер.

    Инженер, добывая её сам, выходил питоном за пределы рабочего каталога
    и упирался в запрос доступа, на который автопилот принципиально не
    отвечает. Замерено: два тикета подряд, каждый - прерванный ход на этом
    запросе. У диспетчера соединение уже открыто и разрешений не требует.
    """

    def view(self, client, sessions, affected=("M11",)):
        from types import SimpleNamespace

        from codex_autopilot.lifecycle_dispatch import server_view_for_incident

        cfg = SimpleNamespace(desktop=SimpleNamespace(project_id="proj-1"))
        state = SimpleNamespace(worker_sessions=list(sessions))
        return server_view_for_incident(
            client, cfg, state, {"affected_task_ids": list(affected)}
        )

    def test_a_live_thread_is_reported_with_its_project(self) -> None:
        from unittest import mock

        client = mock.MagicMock()
        client.read_thread.return_value = {
            "id": "t1", "name": "Worker", "projectId": "proj-1", "status": {"type": "idle"}
        }
        view = self.view(client, [{"task_id": "M11", "thread_id": "t1", "kind": "implementation", "status": "ACTIVE"}])
        self.assertEqual(view["threads"][0]["exists"], True)
        self.assertEqual(view["threads"][0]["project_id"], "proj-1")
        self.assertEqual(view["gathered_by"], "dispatcher")

    def test_a_refused_read_is_recorded_as_a_fact_not_an_exception(self) -> None:
        from unittest import mock

        client = mock.MagicMock()
        client.read_thread.side_effect = RuntimeError("thread not found: t1")
        view = self.view(client, [{"task_id": "M11", "thread_id": "t1", "kind": "replanner", "status": "PREPARED"}])
        self.assertEqual(view["threads"][0]["exists"], False)
        self.assertIn("not found", view["threads"][0]["server_error"])

    def test_threads_of_other_tasks_are_not_gathered(self) -> None:
        from unittest import mock

        client = mock.MagicMock()
        client.read_thread.return_value = {"id": "t1"}
        view = self.view(client, [{"task_id": "M9", "thread_id": "t9", "kind": "implementation", "status": "COMPLETED"}])
        self.assertEqual(view["threads"], [])

    def test_turns_are_never_requested(self) -> None:
        """Стенограммы воркеров инженеру не положены."""

        from unittest import mock

        client = mock.MagicMock()
        client.read_thread.return_value = {"id": "t1"}
        self.view(client, [{"task_id": "M11", "thread_id": "t1", "kind": "implementation", "status": "ACTIVE"}])
        for call in client.read_thread.call_args_list:
            self.assertNotIn("includeTurns", call.kwargs)

    def test_the_prompt_points_at_the_package_instead_of_probing(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/ai_studio.py"
        ).read_text(encoding="utf-8")
        self.assertIn("`server_view` carries the App Server's own record", source)
        self.assertIn("Do not run anything outside the project working directory", source)


class PlanChangeUserRequestTests(unittest.TestCase):
    """user_request переносит runtime, а не повторяет реплэннер.

    Промпт требовал сохранить его дословно. В живом прогоне M11 это 35 234
    символа: модель, переписывающая граф, такую строку не воспроизводит, и
    ЛЮБАЯ законная смена плана отклонялась целиком с "plan changes must not
    replace the original user request". Ход реплэннера при этом проходил
    успешно - отвергался результат.

    Перенос строже прежней проверки: эхо можно подделать, а поле, которое
    не читается из ответа, изменить нельзя вовсе.
    """

    def plan_data(self, **overrides):
        from test_desktop_lifecycle import graph

        data = dict(graph())
        data.update(overrides)
        return data

    def test_a_replanner_may_omit_user_request(self) -> None:
        from codex_autopilot.plan import validate_plan, validate_plan_change

        current = validate_plan(self.plan_data(user_request="и" * 35_000), "adaptive")
        data = self.plan_data(graph_version=current.graph_version + 1)
        data.pop("user_request", None)
        candidate = validate_plan_change(current, data, "adaptive")
        self.assertEqual(candidate.user_request, current.user_request)

    def test_a_returned_user_request_cannot_replace_the_original(self) -> None:
        """Поле не читается из ответа, поэтому подмена невозможна."""

        from codex_autopilot.plan import validate_plan, validate_plan_change

        current = validate_plan(self.plan_data(user_request="исходный запрос"), "adaptive")
        data = self.plan_data(
            graph_version=current.graph_version + 1,
            user_request="подменённый запрос",
        )
        candidate = validate_plan_change(current, data, "adaptive")
        self.assertEqual(candidate.user_request, "исходный запрос")

    def test_goal_stays_strict(self) -> None:
        """goal — 542 символа, модель повторяет его надёжно."""

        from codex_autopilot.plan import validate_plan, validate_plan_change

        current = validate_plan(self.plan_data(), "adaptive")
        data = self.plan_data(graph_version=current.graph_version + 1, goal="другая цель")
        with self.assertRaisesRegex(ValueError, "goal"):
            validate_plan_change(current, data, "adaptive")

    def test_the_prompt_no_longer_demands_the_impossible(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_prompts.py"
        ).read_text(encoding="utf-8")
        self.assertIn("user_request переносит runtime", source)
        self.assertIn("The runtime carries user_request over", source)
        self.assertNotIn("Дословно сохрани user_request", source)
        self.assertNotIn("Preserve user_request verbatim", source)


class ResolvedMustHandOverTests(unittest.TestCase):
    """Починка без преемника завершением не является.

    В живом прогоне инженер закрыл инцидент, его процесс штатно вышел, а
    запускать задачу стало некому: RESOLVED возвращал пустой список
    преемников, прогон уходил в READY/PREPARING и молча стоял. Причинный
    предшественник к этому моменту мёртв - именно его смерть и была
    инцидентом, - поэтому причинным звеном служит сам ход инженера.
    """

    def setUp(self) -> None:
        import json as _json
        import tempfile

        from _gates import patch_hook_trust_gates
        from _relay import reserve_ready_frontier
        from codex_autopilot.bootstrap import initialize_project
        from codex_autopilot.config import load_config
        from codex_autopilot.pipeline_engineer import (
            IncidentClass,
            IncidentSignal,
            PipelineIncidentStore,
            SideEffectOutcome,
        )
        from codex_autopilot.run_state import StateStore, utc_now
        from test_verification_lifecycle import graph, task

        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        # Граф, где B зависит от A: готовой остаётся ровно одна задача,
        # как в живом инциденте. На графе с двумя независимыми задачами
        # вторая занимает слот планировщика, и проверка меряла бы не то.
        plan_file.write_text(_json.dumps(graph(task("A"))), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)
        self.reserve = reserve_ready_frontier
        first = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")[0]
        self.task_id = first.task_id
        self.failed_token = first.reservation_token

        incidents = PipelineIncidentStore(self.cfg.state_dir)
        incident = incidents.open_incident(
            IncidentSignal(
                signal_id="probe:launch",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                summary="Worker requested approval; the dispatcher never answers",
                affected_task_ids=(self.task_id,),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
                system_state={},
            ),
            at=utc_now(),
        )
        self.incident_id = str(incident["incident_id"])
        incidents.route_incident(self.incident_id, at=utc_now())
        incidents.ensure_pipeline_engineer(self.incident_id, at=utc_now())

    def resolve_and_complete(self, final_message: str, *, dispatcher_authorized: bool = False):
        import os
        import time

        from _appserver_fakes import activate_via_app_server
        from codex_autopilot.lifecycle_completion import complete_desktop_worker
        from codex_autopilot.lifecycle_failures import record_desktop_failure
        from codex_autopilot.pipeline_engineer import (
            HealthcheckResult,
            PipelineIncidentStore,
            _expected_healthcheck,
        )
        from codex_autopilot.run_state import utc_now

        # Живая картина инцидента: сессия задачи мертва - именно её смерть
        # и была инцидентом, - а задача вернулась в READY. Провал
        # оформляется настоящим путём: он же снимает блокировки ресурсов
        # и ведёт журнал. Правка состояния руками ломала сверку замков с
        # журналом, и это правильно, что ломала.
        record_desktop_failure(
            self.cfg,
            self.failed_token,
            reason="Worker requested approval; the dispatcher never answers",
            definitive=True,
            reserve_other_ready=False,
        )

        self.future = int(time.time()) + 3_600
        descriptor = self.reserve(
            self.cfg, relay_owner_thread_id="owner-2", now_epoch=self.future
        )[0]
        activate_via_app_server(self.cfg, self.root, descriptor, "engineer-thread")
        incidents = PipelineIncidentStore(self.cfg.state_dir)
        record = next(
            item
            for item in incidents.load()["incidents"]
            if item["incident_id"] == self.incident_id
        )
        incidents.complete_pipeline_engineer(
            self.incident_id,
            success=True,
            at=utc_now(),
            healthcheck=HealthcheckResult(
                name=_expected_healthcheck(record) or "causal_predecessor_rearm_ready",
                passed=True,
                checks=("relay armed",),
                observed_at=utc_now(),
            ),
        )
        # К моменту, когда инженер закрывает инцидент, окно повтора
        # сорвавшейся задачи уже истекло - в живом прогоне M1 стояла
        # именно в READY, а не в RETRY_WAIT.
        extra = {}
        if dispatcher_authorized:
            session = next(
                item
                for item in self.store.load().worker_sessions
                if item.get("kind") == "pipeline_engineer"
            )
            session["automatic_dispatch_pid"] = os.getpid()
            session["automatic_dispatch_state"] = "RUNNING"
            state = self.store.load()
            for item in state.worker_sessions:
                if item.get("reservation_token") == session["reservation_token"]:
                    item["automatic_dispatch_pid"] = os.getpid()
                    item["automatic_dispatch_state"] = "RUNNING"
            self.store.save(state)
            extra = {
                "dispatcher_reservation_token": session["reservation_token"],
                "dispatcher_pid": os.getpid(),
            }
        return complete_desktop_worker(
            self.cfg,
            thread_id="engineer-thread",
            turn_id="engineer-turn",
            final_message=final_message,
            now_epoch=self.future,
            **extra,
        )

    def test_a_resolved_incident_hands_the_run_to_a_successor(self) -> None:
        outcome = self.resolve_and_complete(
            "инцидент закрыт\nPIPELINE_ENGINEER_STATUS: RESOLVED"
        )
        self.assertEqual(outcome.worker_status, "RESOLVED")
        # Прежде здесь был пустой кортеж, и прогон вставал навсегда.
        self.assertTrue(outcome.descriptors)
        self.assertEqual(outcome.descriptors[0].task_id, self.task_id)
        state = self.store.load()
        # Прогон не просто "не встал" - он поехал: назначенный преемник
        # переводит задачу в работу, а не оставляет её ждать.
        self.assertEqual(state.status, "RUNNING")
        self.assertEqual(state.task_states[self.task_id], "RUNNING")
        self.assertNotEqual(state.phase, "PIPELINE_ENGINEER_NO_SUCCESSOR")

    def test_the_engineer_marks_the_successor_as_its_own_transition(self) -> None:
        """Без этого учёта диспетчер отказывается вести цепочку дальше.

        В живом прогоне инженер закрыл инцидент и назначил преемника, но
        не отметил его у себя: следующий шаг ответил `current dispatcher
        does not own the completed-to-successor transition`, резервация
        повисла в CREATE_REQUESTED, и поверх закрытого инцидента
        открылся новый - уже о падении самого диспетчера.
        """

        outcome = self.resolve_and_complete(
            "инцидент закрыт\nPIPELINE_ENGINEER_STATUS: RESOLVED",
            dispatcher_authorized=True,
        )
        engineer = next(
            item
            for item in self.store.load().worker_sessions
            if item.get("kind") == "pipeline_engineer"
        )
        self.assertEqual(engineer["automatic_dispatch_state"], "ADVANCING")
        self.assertEqual(
            engineer["automatic_successor_tokens"],
            [item.reservation_token for item in outcome.descriptors],
        )

    def test_the_engineer_thread_is_the_causal_link_for_the_successor(self) -> None:
        """Релей выполняет ход инженера: другого живого предшественника нет."""

        self.resolve_and_complete("готово\nPIPELINE_ENGINEER_STATUS: RESOLVED")
        successor = next(
            item
            for item in self.store.load().worker_sessions
            if item.get("kind") != "pipeline_engineer"
            and item.get("status") not in {"COMPLETED", "FAILED", "RETRY_WAIT"}
        )
        self.assertEqual(successor["relay_owner_thread_id"], "engineer-thread")


class FailureBeforeTheRequestIsNotAmbiguousTests(unittest.TestCase):
    """Отказ до отправки запроса известен, а не неоднозначен.

    В живом прогоне `installed_plugin_root` стоял среди аргументов
    `client.start_thread`: он падал уже после `create_invoked = True`,
    хотя ни одного `thread/start` в логе диспетчера не было. Отказ
    записывался как UNKNOWN, порождал тикет AMBIGUOUS_SIDE_EFFECT, а
    такой класс по устройству запрещает и автопочинку, и дежурного
    инженера. Прогон вставал без выхода.
    """

    def test_the_plugin_root_is_resolved_before_the_flag_is_armed(self) -> None:
        import inspect

        from codex_autopilot import lifecycle_dispatch

        # Только код: в комментарии рядом обе строки упомянуты нарочно,
        # и текстовый поиск по ним ловил бы объяснение вместо реализации.
        code = "\n".join(
            line
            for line in inspect.getsource(lifecycle_dispatch).splitlines()
            if not line.lstrip().startswith("#")
        )
        resolve = code.index("plugin_root = installed_plugin_root(cfg.skill_path)")
        armed = code.index("create_invoked = True")
        self.assertLess(
            resolve,
            armed,
            "корень плагина обязан резолвиться до взведения create_invoked",
        )

    def test_the_flag_is_not_armed_from_inside_the_call_arguments(self) -> None:
        """Вызов не должен считать отправленным то, что ещё собирается."""

        import inspect

        from codex_autopilot import lifecycle_dispatch

        body = inspect.getsource(lifecycle_dispatch)
        start = body.index("started = client.start_thread(")
        args = body[start : body.index("\n            )", start)]
        self.assertNotIn("installed_plugin_root(", args)


class EscalationAlwaysHasAWayBackTests(unittest.TestCase):
    """Ответ пользователя на эскалацию не должен зависеть от фазы прогона.

    Фазу `PIPELINE_ENGINEER_ESCALATED` выставляет только завершение
    инженера. Инцидент, эскалированный маршрутизацией, оставлял прогон в
    прежней фазе - и возобновление молча ничего не закрывало.
    """

    def test_resume_does_not_gate_on_the_run_phase(self) -> None:
        import inspect

        from codex_autopilot import control

        body = inspect.getsource(control._answer_escalation)
        self.assertNotIn('state.phase != "PIPELINE_ENGINEER_ESCALATED"', body)
        self.assertIn("incident_ids_awaiting_the_user", body)


class ReplaceStartsWithoutInheritedTicketsTests(unittest.TestCase):
    """Новый прогон не наследует тикеты прежнего.

    В тикетах нет run_id, а дежурный инженер старше любой работы: два
    открытых тикета прошлого прогона вставали поперёк нового ещё до
    первой задачи. `--replace` чистил план, состояние и логи - и не
    трогал хранилище инцидентов.
    """

    def setUp(self) -> None:
        import json as _json
        import tempfile

        from _gates import patch_hook_trust_gates
        from codex_autopilot.bootstrap import initialize_project
        from codex_autopilot.pipeline_engineer import (
            IncidentClass,
            IncidentSignal,
            PipelineIncidentStore,
            SideEffectOutcome,
        )
        from codex_autopilot.run_state import utc_now
        from test_verification_lifecycle import graph, task

        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")
        self.plan_file = self.root / "input-plan.json"
        self.plan_file.write_text(_json.dumps(graph(task("A"))), encoding="utf-8")
        self.initialize = initialize_project
        self.initialize(
            self.root,
            self.plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
        )
        self.state_dir = self.root / ".codex-autopilot"
        incidents = PipelineIncidentStore(self.state_dir)
        incidents.open_incident(
            IncidentSignal(
                signal_id="probe:stale",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                summary="Тикет прошлого прогона",
                affected_task_ids=("A",),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
                system_state={},
            ),
            at=utc_now(),
        )
        self.store_cls = PipelineIncidentStore

    def replace_run(self) -> None:
        # Первый запуск потребляет файл плана, поэтому повтор пишет его
        # заново - ровно как это делает скилл на новом прогоне.
        import json as _json

        from test_verification_lifecycle import graph, task

        self.plan_file.write_text(_json.dumps(graph(task("A"))), encoding="utf-8")
        self.initialize(
            self.root,
            self.plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
            replace=True,
        )

    def test_a_replaced_run_opens_with_an_empty_incident_store(self) -> None:
        self.assertTrue(self.store_cls(self.state_dir).load()["incidents"])
        self.replace_run()
        self.assertEqual(self.store_cls(self.state_dir).load()["incidents"], [])

    def test_the_previous_tickets_are_kept_beside_the_run(self) -> None:
        """Это запись о поломке: откладывается, а не удаляется."""

        self.replace_run()
        archived = sorted(self.state_dir.glob("pipeline-incidents.*.json"))
        self.assertEqual(len(archived), 1)
        import json as _json

        kept = _json.loads(archived[0].read_text(encoding="utf-8"))
        self.assertEqual(len(kept["incidents"]), 1)


class HookTimeoutsSurviveCodexLoadTests(unittest.TestCase):
    """Codex не должен переписывать наши хуки при загрузке.

    Пользователь весь день жаловался, что доверие хукам слетает перед
    каждой новой задачей. Причина нашлась на её же экране: Codex писал
    `clamping Interrupt hook timeout to 3s` и показывал все три хука в
    состоянии Review. Мы объявляли Interrupt с таймаутом 30, Codex
    зажимал его до своего предела - определение переставало совпадать с
    доверенным, и весь файл уходил на повторный разбор при каждой
    загрузке.
    """

    MAX_INTERRUPT_TIMEOUT = 3

    def hook_files(self):
        import json as _json

        root = Path(__file__).resolve().parent.parent
        files = sorted(root.glob("plugins/*/hooks/hooks.json"))
        self.assertTrue(files, "файлы хуков не найдены")
        return [(p, _json.loads(p.read_text(encoding="utf-8"))) for p in files]

    def test_the_interrupt_timeout_is_never_above_what_codex_accepts(self) -> None:
        for path, payload in self.hook_files():
            for group in payload["hooks"].get("Interrupt", []):
                for hook in group["hooks"]:
                    self.assertLessEqual(
                        hook["timeout"],
                        self.MAX_INTERRUPT_TIMEOUT,
                        f"{path.parts[-3]}: Codex зажмёт этот таймаут и потребует "
                        "заново доверить все хуки файла",
                    )

    def test_both_profiles_declare_the_same_interrupt_timeout(self) -> None:
        """Профили отличаются составом, а не поведением хуков."""

        seen = {
            hook["timeout"]
            for _, payload in self.hook_files()
            for group in payload["hooks"].get("Interrupt", [])
            for hook in group["hooks"]
        }
        self.assertEqual(len(seen), 1, f"профили разошлись: {seen}")


class RepeatedFailureIsNotACrashTests(unittest.TestCase):
    """Второй отказ той же задачи не должен убивать диспетчер.

    В живом прогоне это открыло тикет поверх тикета: настоящая поломка
    уже ждала в RETRY_WAIT, пришла вторая запись отказа, машина
    состояний отвергла переход RETRY_WAIT -> RETRY_WAIT, релей умер, и
    появился второй инцидент - уже о падении самого диспетчера.
    """

    def setUp(self) -> None:
        ResolvedMustHandOverTests.setUp(self)

    def fail_once(self, token: str) -> None:
        from codex_autopilot.lifecycle_failures import record_desktop_failure

        record_desktop_failure(
            self.cfg,
            token,
            reason="воркер сорвался",
            definitive=True,
            reserve_other_ready=False,
        )

    def park_in_retry_wait(self) -> None:
        """Так это делает восстановление: прямым присваиванием.

        `resilience.py` ставит RETRY_WAIT в обход машины состояний, когда
        разбирает мёртвый диспетчер. Следом приходит запись отказа - и
        встречает задачу уже в том состоянии, в которое собиралась её
        перевести.
        """

        from codex_autopilot.task_state import TaskState

        state = self.store.load()
        state.task_states = {**state.task_states, self.task_id: TaskState.RETRY_WAIT.value}
        state.active_task_ids = [t for t in state.active_task_ids if t != self.task_id]
        self.store.save(state)

    def test_a_failure_meeting_an_already_waiting_task_does_not_crash(self) -> None:
        from codex_autopilot.task_state import TaskState

        self.park_in_retry_wait()
        # Прежде здесь падало IllegalTaskTransition: RETRY_WAIT -> RETRY_WAIT,
        # релей умирал, и поверх настоящей поломки открывался второй тикет.
        self.fail_once(self.failed_token)
        self.assertEqual(
            self.store.load().task_states[self.task_id], TaskState.RETRY_WAIT.value
        )

    def test_the_failure_is_still_recorded(self) -> None:
        """Идемпотентность не должна превращаться в молчание."""

        self.park_in_retry_wait()
        self.fail_once(self.failed_token)
        session = next(
            item
            for item in self.store.load().worker_sessions
            if item.get("reservation_token") == self.failed_token
        )
        self.assertEqual(session["status"], "RETRY_WAIT")
        self.assertIn("сорвался", str(session.get("failure_reason")))
