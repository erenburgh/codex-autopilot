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
