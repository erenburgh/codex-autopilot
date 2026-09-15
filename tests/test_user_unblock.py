"""Остановку по правилу снимает человек - и только с названной причиной.

Эскалация без обратного пути - тупик, а не исключение. Прогон вставал по
нарушению правила, «продолжи» его намеренно не снимало (и правильно: это
не кнопка, стирающая неразобранную поломку), а другого пути не
существовало вовсе. Задача висела BLOCKED навсегда.

Замерено: задача M0 остановилась с BLOCKED DANGEROUS_PERMISSION, отказавшись
перезаписать рантайм, который её же и исполнял. Решение тут человеческое,
но принять его было нечем.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from _gates import patch_hook_trust_gates
from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.config import load_config
from codex_autopilot.run_state import StateStore
from test_desktop_lifecycle import graph, task


ROOT = Path(__file__).resolve().parents[1]


class UserUnblockTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        raw = graph(max_workers=1)
        raw["tasks"] = [task("A", path="src/a")]
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(raw), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)

    def block(self) -> None:
        state = self.store.load()
        state.task_states["A"] = "BLOCKED"
        state.status = "BLOCKED"
        state.phase = "BLOCKED"
        state.last_error = "A BLOCKED DANGEROUS_PERMISSION"
        self.store.save(state)

    def run_cli(self, *args: str):
        return subprocess.run(
            [sys.executable, "-m", "codex_autopilot.cli", *args],
            cwd=ROOT,
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )

    def test_the_user_decision_lifts_the_block_and_is_recorded(self) -> None:
        self.block()
        result = self.run_cli(
            "unblock", "--project", str(self.root), "--task", "A",
            "--reason", "смена рантайма выполняется оператором отдельным шагом",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.store.load()
        self.assertEqual(state.task_states["A"], "READY")
        self.assertEqual(state.status, "READY")
        self.assertIsNone(state.last_error)
        self.assertEqual(state.user_unblocks[-1]["task_id"], "A")
        self.assertIn("оператором", state.user_unblocks[-1]["reason"])

    def test_a_reason_is_mandatory(self) -> None:
        self.block()
        result = self.run_cli(
            "unblock", "--project", str(self.root), "--task", "A"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")

    def test_a_task_that_is_not_blocked_is_refused(self) -> None:
        result = self.run_cli(
            "unblock", "--project", str(self.root), "--task", "A",
            "--reason", "просто так",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("не остановлена", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
