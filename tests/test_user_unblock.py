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
from _plan_contract import initialize_verified_project as initialize_project
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

    def test_a_blank_reason_is_refused(self) -> None:
        """Пробел вместо причины - это отсутствие причины.

        Обязательность флага ловит забытый --reason, но не пустую
        строку: `--reason " "` проходила бы разбор аргументов и
        записывала решение без единого слова о том, почему.
        """

        self.block()
        result = self.run_cli(
            "unblock", "--project", str(self.root), "--task", "A", "--reason", "   "
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")

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


class R32InterventionIsRecorded(unittest.TestCase):
    """R32: вмешательство человека — записанное решение, а не реплика.

    «Я ХОЧУ ЧТОБЫ ОН РАБОТАЛ БЕЗ МЕНЯ НО И ЕСЛИ Я ЗАХОЧУ ВКЛЮЧИТЬСЯ
    НИЧЕГО НЕ ДОЛЖНО СЛОМАТЬСЯ» - 15 сентября 2026.

    Проверка на сегодняшнюю реализацию: единственный существующий путь
    вмешательства - снятие остановки - обязан записывать решение с
    причиной и отказывать без неё. По мере появления остальных путей
    (ревизия по просьбе человека, указание работающей задаче, создание
    задачи руками) проверка расширяется на них же.
    """

    def test_the_rule_is_registered_with_a_real_check(self) -> None:
        from codex_autopilot.rules import RULES

        rule = next(item for item in RULES if item.id == "R32")
        self.assertEqual(rule.mode, "CHECKED")
        self.assertTrue(rule.check.strip())
        self.assertIn("причин", rule.check)

    def test_every_recorded_intervention_carries_author_time_and_reason(self) -> None:
        import json
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        from _gates import patch_hook_trust_gates
        from _plan_contract import initialize_verified_project as initialize_project
        from codex_autopilot.config import load_config
        from codex_autopilot.run_state import StateStore
        from test_desktop_lifecycle import graph, task

        patch_hook_trust_gates(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        (root / ".git").mkdir()
        skill = root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        raw = graph(max_workers=1)
        raw["tasks"] = [task("A", path="src/a")]
        plan_file = root / "input-plan.json"
        plan_file.write_text(json.dumps(raw), encoding="utf-8")
        initialize_project(
            root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        store = StateStore(load_config(root).state_dir)
        state = store.load()
        state.task_states["A"] = "BLOCKED"
        state.status = "BLOCKED"
        state.phase = "BLOCKED"
        store.save(state)

        env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"}
        result = subprocess.run(
            [
                sys.executable, "-m", "codex_autopilot.cli", "unblock",
                "--project", str(root), "--task", "A",
                "--reason", "решение владельца: условие пересмотрено",
            ],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = store.load().user_unblocks[-1]
        self.assertEqual(record["task_id"], "A")
        self.assertIn("владельца", record["reason"])
        self.assertTrue(str(record["at"]).strip(), "решение обязано нести время")
