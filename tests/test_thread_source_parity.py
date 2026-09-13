"""Ветка создаётся как обычная, а не как созданная агентом.

v0.7 не передавала threadSource вовсе, и её задачи появлялись в сайдбаре
проекта обычными ветками - пользователь мог их открыть и продолжить.

v0.8 стала передавать "agent_created_thread". Приложение знает это
значение и обращается с такой веткой иначе: показывает её как созданную
в другом приложении и требует ручного перехвата. Автоматический перехват
недоступен, поэтому задача оставалась недостижимой, сколько бы раз её ни
привязывали к проекту на стороне App Server.

Этот параметр и был единственным отличием создания от работавшей версии.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _gates import patch_hook_trust_gates
from _relay import reserve_ready_frontier
from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
from codex_autopilot.lifecycle_dispatch import app_server_creation_contract
from test_desktop_lifecycle import graph


class ThreadSourceParityTests(unittest.TestCase):
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

    def contract_params(self) -> dict:
        descriptor = reserve_ready_frontier(self.cfg)[0]
        return app_server_creation_contract(self.cfg, descriptor)["params"]

    def test_the_create_contract_does_not_mark_the_thread_as_agent_created(self) -> None:
        params = self.contract_params()
        self.assertNotIn("threadSource", params)

    def test_production_never_passes_an_agent_thread_source(self) -> None:
        """Сторож на возврат параметра в живой путь создания."""

        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "codex_autopilot"
            / "lifecycle_dispatch.py"
        ).read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in source.splitlines()
            if "agent_created_thread" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(offenders, [])

    def test_the_contract_still_carries_project_and_workspace(self) -> None:
        """Убрали только пометку об агенте: остальное создание не трогали."""

        params = self.contract_params()
        self.assertEqual(params["cwd"], str(self.root))
        self.assertEqual(params["runtimeWorkspaceRoots"], [str(self.root)])
        self.assertIs(params["ephemeral"], False)


if __name__ == "__main__":
    unittest.main()
