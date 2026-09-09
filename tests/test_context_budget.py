from __future__ import annotations

import json
from pathlib import Path
import unittest

from codex_autopilot.bootstrap import select_milestone
from codex_autopilot.config import load_config
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.orchestrator import build_worker_prompt
from codex_autopilot.plan import load_plan
from codex_autopilot.run_state import StateStore

from test_core import make_project


class ContextBudgetTests(unittest.TestCase):
    def test_initial_context_does_not_grow_linearly_through_twenty_milestones(self):
        root = make_project("adaptive", 20)
        cfg = load_config(root)
        plan = load_plan(cfg.state_dir, cfg.profile)
        memory = ProjectMemory(root)
        state_store = StateStore(cfg.state_dir)
        sizes: dict[int, int] = {}
        simulated_v07_sizes: dict[int, int] = {}
        history: list[str] = []
        previous = 0
        for milestone in (1, 5, 10, 20):
            for index in range(previous, milestone * 5):
                statement = f"Historic unrelated observation {index}: " + ("context that older workers might have summarized " * 8)
                history.append(statement)
                memory.add_observation(statement=statement, created_by=f"worker-{max(1, milestone - 1)}")
                memory.add_constraint(statement=f"Constraint {index}: retain deterministic behavior", origin="agent", created_by="test")
            previous = milestone * 5
            state = state_store.load()
            state.milestone_index = milestone - 1
            state.milestone_id = f"M{milestone}"
            state.worker_sequence = milestone
            state_store.save(state)
            select_milestone(cfg.state_dir, plan, milestone - 1)
            (cfg.state_dir / "HANDOFF.md").write_text("# Handoff (advisory)\n\nNext: current milestone.\n", encoding="utf-8")
            prompt = build_worker_prompt(cfg, state, plan)
            sizes[milestone] = len(prompt)
            simulated_v07_sizes[milestone] = 2_500 + len("\n".join(history))

        self.assertLess(max(sizes.values()) - min(sizes.values()), 6_000)
        self.assertGreater(simulated_v07_sizes[20] - simulated_v07_sizes[1], 20_000)
        self.assertLess(sizes[20], simulated_v07_sizes[20])
        returned = memory.search(query="historic", categories=["observation"], limit=8)
        self.assertEqual(len(returned.records), 8)
        self.assertLess(len(json.dumps(returned.records)), 12_000)


if __name__ == "__main__":
    unittest.main()
