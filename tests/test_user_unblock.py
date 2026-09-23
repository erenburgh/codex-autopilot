"""A stop under a rule is lifted by a human - and only with a named reason.

An escalation with no way back is a dead end, not an exception. The run
stopped on a rule violation, "resume" deliberately did not lift it (and
rightly: it is not a button that erases an unexamined fault), and no
other path existed at all. The task hung BLOCKED forever.

Measured: task M0 stopped with BLOCKED DANGEROUS_PERMISSION, refusing to
overwrite the runtime that was executing it. The decision here is a
human one, but there was nothing to make it with.
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
        # The status is derived now (run_status), not written by the answer:
        # what matters is that the run no longer waits for her. It used to
        # be set to READY/PREPARING by hand and then wait for her Resume.
        self.assertNotEqual(state.status, "BLOCKED")
        self.assertIsNone(state.last_error)
        self.assertEqual(state.user_unblocks[-1]["task_id"], "A")
        self.assertIn("оператором", state.user_unblocks[-1]["reason"])
        self.assertIn("continues by itself", result.stdout)
        self.assertNotIn("Resume", result.stdout)

    def test_a_blank_reason_is_refused(self) -> None:
        """A blank instead of a reason is the absence of a reason.

        A required flag catches a forgotten --reason, but not an empty
        string: `--reason " "` would pass argument parsing and record a
        decision without a single word about why.
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
        self.assertIn("is not stopped", result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()


class R32InterventionIsRecorded(unittest.TestCase):
    """R32: a human intervention is a recorded decision, not a remark.

    "I WANT IT TO WORK WITHOUT ME BUT ALSO IF I WANT TO STEP IN NOTHING
    SHOULD BREAK" - 15 September 2026.

    A check against today's implementation: the only existing path of
    intervention - lifting a stop - must record the decision with a
    reason and refuse without one. As the other paths appear (a review
    at a person's request, an instruction to a running task, creating a
    task by hand) the check extends to them too.
    """

    def test_the_rule_is_registered_with_a_real_check(self) -> None:
        from codex_autopilot.rules import RULES

        rule = next(item for item in RULES if item.id == "R32")
        self.assertEqual(rule.mode, "CHECKED")
        self.assertTrue(rule.check.strip())
        self.assertIn("reason", rule.check)

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
        self.assertTrue(
            str(record["at"]).strip(), "the decision must carry a time"
        )
