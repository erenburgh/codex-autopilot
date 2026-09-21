"""A rubric the task cannot have teaches the next verifier, or it repeats.

Two doors lead to the same class of failure. Behind the first the verdict
cannot be parsed at all; that one already goes through
`_reject_verifier_result`, which writes the reason into
`verification_rejections`, and `lifecycle_prompts` hands it to the next
verifier with the corrective that fits exactly: return
AUTOPILOT_VERIFICATION with exactly two top-level fields.

Behind the second the verdict parses perfectly and carries `rubric` - a
legal field, just not for a task without a department binding. That door
raised straight to WorkerProtocolError, recording nothing. The next
verifier was told nothing, did the same thing, and its turn was
interrupted again.

Measured three times on one live run - twice on M6, once on M11A - each
one costing a worker turn and leaving the task in VERIFYING until the
on-call engineer picked it up.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src/codex_autopilot/lifecycle_completion.py"


def _completion_source() -> str:
    return SOURCE.read_text(encoding="utf-8")


class BothDoorsReachTheRecorderTests(unittest.TestCase):
    def test_the_rubric_case_records_instead_of_raising(self) -> None:
        source = _completion_source()
        marker = "elif verdict.rubric is not None:"
        self.assertIn(marker, source)
        branch = source[source.index(marker) :]
        branch = branch[: branch.index("except (DepartmentAcceptanceError")]
        self.assertIn("_reject_verifier_result(", branch)
        self.assertNotIn("raise DepartmentAcceptanceError(", branch)

    def test_the_reason_still_names_what_was_wrong(self) -> None:
        source = _completion_source()
        self.assertIn(
            "verifier attested a department rubric for a task without", source
        )

    def test_the_recorder_now_has_more_than_one_caller(self) -> None:
        """One caller was the whole defect: the other door bypassed it."""

        tree = ast.parse(_completion_source())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_reject_verifier_result"
        ]
        self.assertGreaterEqual(len(calls), 2)

    def test_the_note_the_recorder_reaches_says_the_right_thing(self) -> None:
        prompts = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_prompts.py"
        ).read_text(encoding="utf-8")
        self.assertIn("verification_rejections", prompts)
        self.assertIn("exactly two top-level fields", prompts)

    def test_the_recorder_writes_where_the_prompt_reads(self) -> None:
        source = _completion_source()
        self.assertIn("state.verification_rejections[task_id] = rejections", source)


class OtherDepartmentFailuresStillRaiseTests(unittest.TestCase):
    """The fix is narrow: only the verifier's own inventable mistake."""

    def test_a_mismatched_department_title_is_still_an_error(self) -> None:
        source = _completion_source()
        self.assertIn(
            "department verifier title does not identify the pinned Lead Role", source
        )
        title_branch = source[source.index("department verifier title does not") :]
        self.assertIn("raise DepartmentAcceptanceError", source[: source.index("department verifier title does not")] + title_branch[:200])


if __name__ == "__main__":
    unittest.main()
