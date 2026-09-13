"""Дежурный инженер создаётся как воркер, а не остаётся ярлыком в JSON.

Фаза PIPELINE_ENGINEER существовала как поле: ensure_pipeline_engineer
меняла строку и дописывала событие, а ветку инженера не создавало ничто.
Прогон, упёршийся в неё, вставал молча.

R13: DevOps решает инфраструктурные баги от имени пользователя, и
пользователь не участвует в выборе способа фикса. Поэтому эскалация - не
второй равноправный выход, а исключение с кодом причины.
"""

from __future__ import annotations

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
