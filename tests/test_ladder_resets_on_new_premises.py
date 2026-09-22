"""A task rebuilt on new prerequisites does not pay for the old attempts.

The hiring ladder bounds how many times one problem may be retried: two
revisions per hire, then a re-hire at a higher effort, then a stop. That
ceiling is the point, and failing repeatedly under an unchanged contract
must still run out.

But a replanner can insert a prerequisite beneath a task. Measured on a
live run: M11 spent four revisions and a re-hire against its original
prerequisites, its worker then requested a plan change, the replanner
inserted M11A, and `depends_on` became a different set entirely - new
prerequisite, new tree, and the hole the old attempts kept falling into
now closed by M11A. M11 got exactly ONE attempt at the new problem before
the ladder, still holding the old tally, declared it exhausted.

So the tally is bound to what it was spent on.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from codex_autopilot.lifecycle_base import _reset_revision_budget_if_premises_changed
from codex_autopilot.run_state import RunState


class _Task:
    def __init__(self, task_id: str, depends_on: tuple[str, ...]) -> None:
        self.id = task_id
        self.depends_on = depends_on


class _Plan:
    def __init__(self, task: _Task) -> None:
        self.task_map = {task.id: task}


def _state(basis, revisions=4, rehires=1) -> RunState:
    state = RunState()
    state.graph_version = 5
    state.task_revisions = {"M11": revisions}
    state.task_rehires = {"M11": rehires}
    state.task_effort = {"M11": "max"}
    if basis is not None:
        state.task_revision_basis = {"M11": basis}
    return state


class TheTallyFollowsThePremisesTests(unittest.TestCase):
    def test_a_new_prerequisite_starts_the_ladder_over(self) -> None:
        state = _state({"graph_version": 4, "depends_on": ["M3"]})
        plan = _Plan(_Task("M11", ("M11A",)))
        _reset_revision_budget_if_premises_changed(plan, state, "M11", "2026-09-22T08:00:00+00:00")
        self.assertEqual(state.task_revisions["M11"], 0)
        self.assertEqual(state.task_rehires["M11"], 0)
        self.assertNotIn("M11", state.task_effort)

    def test_unchanged_premises_keep_the_ceiling(self) -> None:
        """Failing repeatedly at the same problem must still run out."""

        state = _state({"graph_version": 4, "depends_on": ["M3"]})
        plan = _Plan(_Task("M11", ("M3",)))
        _reset_revision_budget_if_premises_changed(plan, state, "M11", "2026-09-22T08:00:00+00:00")
        self.assertEqual(state.task_revisions["M11"], 4)
        self.assertEqual(state.task_rehires["M11"], 1)
        self.assertEqual(state.task_effort["M11"], "max")

    def test_a_bare_graph_bump_is_not_enough(self) -> None:
        """A replan that left this task's prerequisites alone changes nothing."""

        state = _state({"graph_version": 1, "depends_on": ["M3"]})
        plan = _Plan(_Task("M11", ("M3",)))
        _reset_revision_budget_if_premises_changed(plan, state, "M11", "2026-09-22T08:00:00+00:00")
        self.assertEqual(state.task_revisions["M11"], 4)

    def test_attempts_without_a_recorded_basis_stay_charged(self) -> None:
        """Conservative: only a change we can see lifts the ceiling."""

        state = _state(None)
        plan = _Plan(_Task("M11", ("M11A",)))
        _reset_revision_budget_if_premises_changed(plan, state, "M11", "2026-09-22T08:00:00+00:00")
        self.assertEqual(state.task_revisions["M11"], 4)

    def test_the_reset_is_recorded_not_silent(self) -> None:
        state = _state({"graph_version": 4, "depends_on": ["M3"]})
        plan = _Plan(_Task("M11", ("M11A",)))
        _reset_revision_budget_if_premises_changed(plan, state, "M11", "2026-09-22T08:00:00+00:00")
        events = [e for e in state.lifecycle_journal if e.get("event") == "revision_budget_reset_on_new_premises"]
        self.assertEqual(len(events), 1)
        detail = events[0].get("detail") or ""
        self.assertIn("M3", detail)
        self.assertIn("M11A", detail)
        self.assertIn("4", detail)

    def test_the_new_basis_replaces_the_old_one(self) -> None:
        state = _state({"graph_version": 4, "depends_on": ["M3"]})
        plan = _Plan(_Task("M11", ("M11A",)))
        _reset_revision_budget_if_premises_changed(plan, state, "M11", "2026-09-22T08:00:00+00:00")
        self.assertEqual(state.task_revision_basis["M11"]["depends_on"], ["M11A"])
        self.assertEqual(state.task_revision_basis["M11"]["graph_version"], 5)


class TheLadderAsksBeforeItJudgesTests(unittest.TestCase):
    def test_the_check_runs_before_the_tally_is_read(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/lifecycle_base.py"
        ).read_text(encoding="utf-8")
        body = source[source.index("def _rehire_or_block_on_revision_limit") :]
        reset_at = body.index("_reset_revision_budget_if_premises_changed(")
        used_at = body.index("used = int(state.task_revisions.get(task_id, 0))")
        self.assertLess(reset_at, used_at)

    def test_the_basis_is_written_where_revisions_are_counted(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_reservations.py"
        ).read_text(encoding="utf-8")
        self.assertIn("state.task_revision_basis[task.id] = basis_for(", source)
        counted = source.index("state.task_revisions[task.id] = revision_number")
        basis = source.index("state.task_revision_basis[task.id] = basis_for(")
        self.assertLess(counted, basis)


if __name__ == "__main__":
    unittest.main()
