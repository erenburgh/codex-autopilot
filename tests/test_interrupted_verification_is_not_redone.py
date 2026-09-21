"""An interrupted verification is re-judged, not re-done.

The state machine says it at TaskState.VERIFYING: "A verifier whose reply
cannot be read verified nothing. The work stays done and still awaits
acceptance, so the task returns to IMPLEMENTED rather than being redone."
Only one path ever took it - the one where a verdict arrived and could not
be parsed. A verifier that answered nothing at all, because a pause drained
it, a reboot killed it or a rate limit took it, fell into the generic
failure branch: RETRY_WAIT, whose only exit is READY, which means a fresh
implementation.

So the better-informed failure was handled gently and the less-informed one
threw the work away, although nothing had judged it.

Measured on a live run of 21 Sep 2026: pausing the run to install a new
version retired M20's verifier mid-flight, and the task spent a whole
implementation turn redoing work that was finished and merely unjudged.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from codex_autopilot.task_state import TaskState, transition_task


class TheStateMachineAllowsTheGentlePathTests(unittest.TestCase):
    def test_verifying_may_return_to_implemented(self) -> None:
        self.assertIn(
            TaskState.IMPLEMENTED,
            {TaskState.IMPLEMENTED}
            & set(_allowed_from(TaskState.VERIFYING)),
        )

    def test_retry_wait_can_only_lead_to_a_fresh_start(self) -> None:
        """Which is why sending a verifier there redoes the work."""

        exits = set(_allowed_from(TaskState.RETRY_WAIT))
        self.assertIn(TaskState.READY, exits)
        self.assertNotIn(TaskState.IMPLEMENTED, exits)
        self.assertNotIn(TaskState.VERIFYING, exits)

    def test_the_transition_really_is_legal(self) -> None:
        states = {"T1": TaskState.VERIFYING.value}
        moved = transition_task(_plan(), states, "T1", TaskState.IMPLEMENTED)
        self.assertEqual(moved["T1"], TaskState.IMPLEMENTED.value)


class TheFailurePathTakesItTests(unittest.TestCase):
    """The branch is one shared `else`; only the verifier case changes."""

    def _source(self) -> str:
        return (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_failures.py"
        ).read_text(encoding="utf-8")

    def test_a_lost_verifier_is_recognised_by_kind_and_state(self) -> None:
        source = self._source()
        self.assertIn('str(session.get("kind") or "") == "verifier"', source)
        self.assertIn(
            'state.task_states.get(task_id) == TaskState.VERIFYING.value', source
        )

    def test_it_returns_the_task_to_implemented(self) -> None:
        source = self._source()
        lost = source.index("verifier_lost_mid_flight = (")
        after = source[lost:]
        self.assertIn("TaskState.IMPLEMENTED", after[: after.index("elif state.task_states")])

    def test_everything_else_still_retries(self) -> None:
        """An implementation that failed must still be redone."""

        source = self._source()
        self.assertIn(
            "elif state.task_states.get(task_id) != TaskState.RETRY_WAIT.value:", source
        )
        self.assertIn("TaskState.RETRY_WAIT\n                )", source)

    def test_the_return_is_recorded_rather_than_silent(self) -> None:
        self.assertIn("verification_returned_for_reverification", self._source())


def _allowed_from(state: TaskState):
    from codex_autopilot.task_state import TASK_TRANSITIONS

    return TASK_TRANSITIONS[state]


def _plan():
    """Enough plan for the transition check, and nothing more."""

    class _Task:
        id = "T1"
        depends_on: tuple[str, ...] = ()

    class _Plan:
        tasks = (_Task(),)
        task_map = {"T1": _Task()}

    return _Plan()


if __name__ == "__main__":
    unittest.main()
