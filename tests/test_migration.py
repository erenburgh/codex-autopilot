from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from _plan_contract import initialize_verified_project as initialize_project
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.run_state import StateStore


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md"


def milestone(index: int) -> dict:
    return {
        "id": f"M{index}",
        "title": f"Step {index}",
        "objective": f"Do step {index}",
        "definition_of_done": [f"Step {index} verified"],
        "execution_mode": "code",
        "execution_mode_reason": "Files and tests are sufficient.",
        "reasoning": "medium",
    }


class MigrationTests(unittest.TestCase):
    def test_v07_migration_backs_up_and_never_promotes_prose_to_truth(self):
        root = Path(tempfile.mkdtemp(prefix="codex-autopilot-v07-migration-"))
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        state_dir = root / ".codex-autopilot"
        state_dir.mkdir()
        old_plan = {"schema_version": 2, "goal": "Ship", "model_strategy": "auto", "milestones": [milestone(1), milestone(2)]}
        (state_dir / "plan.json").write_text(json.dumps(old_plan), encoding="utf-8")
        (state_dir / "run-state.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "run_id": "old-run",
                    "status": "PAUSED",
                    "phase": "PAUSED",
                    "milestone_index": 1,
                    "worker_sequence": 1,
                    "worker_history": [{"milestone_id": "M1", "status": "ROTATE"}],
                }
            ),
            encoding="utf-8",
        )
        (state_dir / "DECISIONS.md").write_text("# Decisions\n\n- Use SQLite for persistence.\n", encoding="utf-8")
        (state_dir / "HANDOFF.md").write_text("# Handoff\n\nSQLite is definitely perfect.\n", encoding="utf-8")
        (state_dir / "PROJECT_STATE.md").write_text("# State\n\nEverything is complete.\n", encoding="utf-8")
        new_plan_file = state_dir / "bootstrap-plan.json"
        new_plan_file.write_text(json.dumps({"goal": "Ship", "model_strategy": "auto", "milestones": [milestone(1), milestone(2)]}), encoding="utf-8")

        initialize_project(root, new_plan_file, profile="adaptive", skill_path=SKILL, replace=True)

        migrations = list((state_dir / "migrations").glob("v0.7-to-v0.8-*"))
        self.assertEqual(len(migrations), 1)
        backup = migrations[0] / "backup"
        self.assertEqual(json.loads((backup / "run-state.json").read_text())["schema_version"], 3)
        report = (migrations[0] / "MIGRATION_REPORT.md").read_text()
        self.assertIn("Truth records imported from agent prose: 0", report)

        memory = ProjectMemory(root)
        self.assertEqual(memory.list_records(categories=["truth"], limit=8).records, [])
        decisions = memory.list_records(categories=["decision"], limit=8).records
        self.assertEqual((decisions[0]["origin"], decisions[0]["status"]), ("agent", "proposed"))
        observations = memory.list_records(categories=["observation"], limit=8).records
        self.assertEqual(len(observations), 2)
        self.assertTrue(all(item["status"] == "unverified" for item in observations))

        state = StateStore(state_dir).load()
        self.assertEqual(state.schema_version, 5)
        self.assertEqual(state.milestone_index, 1)
        self.assertEqual(state.milestone_id, "M2")
        self.assertIn("- [x] M1", (root / "ROADMAP.md").read_text())
        self.assertIn("M1: 1 evidence record(s) (v0.7-checkpoint-migration)", (state_dir / "PROJECT_STATE.md").read_text())


if __name__ == "__main__":
    unittest.main()
