"""A malformed model reply is a failed attempt, not a machine breakdown.

The difference is not cosmetic; it was measured on the live run of
16 Sep 2026.

Worker M8 finished its turn and got the final line's format wrong. On
the hook path such a refusal returns to the worker as `decision: block`
and is fixed in the same turn - for free. On the automatic path the
exception went up: the dispatcher crashed, a PIPELINE-class ticket was
opened, the on-call engineer was raised. Nobody was left to accept the
turn's completion, the session stayed hanging active, and **the whole
run stopped** - a human was needed to send `Resume`.

The on-call engineer, analysing this, opened an R31 rule conflict: the
runtime rejected an **already completed** worker at a late final-status
check, that is, threw away done work at a format gate.

So protocol errors carry a separate class: the automatic path tells
"the model formatted its reply wrong" from "the transport broke" and
treats the former as a failed task attempt.
"""

from __future__ import annotations

import unittest

from codex_autopilot.lifecycle_base import (
    DesktopLifecycleError,
    WorkerProtocolError,
    parse_desktop_worker_status,
)


class ProtocolRetryOutcomeTests(unittest.TestCase):
    """The outcome of the protocol branch must be constructible.

    The first version of this branch built `CompletionOutcome` without
    the required `run_done` field. The classification was right, the
    tests green - and on the first real firing the branch failed with
    `TypeError`, and the run stopped out of nowhere. No test covered
    that branch then: running it end to end needs a large fake of the
    App Server client, and I knew that and shipped it anyway.

    This test is cheap and closes exactly that miss: it builds the same
    outcome the branch builds, and fails if the type gains a new
    required field.
    """

    def test_a_protocol_retry_outcome_is_constructible(self) -> None:
        from codex_autopilot.lifecycle_base import CompletionOutcome

        outcome = CompletionOutcome(
            matched=True,
            worker_status="PROTOCOL_RETRY",
            descriptors=(),
            run_done=False,
        )
        self.assertTrue(outcome.matched)
        self.assertEqual(outcome.worker_status, "PROTOCOL_RETRY")
        self.assertFalse(outcome.run_done)

    def test_the_dispatch_branch_builds_that_exact_outcome(self) -> None:
        """Compare the build in the branch with the real type, not memory."""

        import ast
        import inspect

        from codex_autopilot import lifecycle_base, lifecycle_dispatch

        source = inspect.getsource(lifecycle_dispatch.run_automatic_app_server_turn)
        tree = ast.parse(source.lstrip())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "CompletionOutcome"
            and any(
                isinstance(kw.value, ast.Constant)
                and kw.value.value == "PROTOCOL_RETRY"
                for kw in node.keywords
            )
        ]
        self.assertEqual(
            len(calls), 1, "the protocol branch must build exactly one outcome"
        )
        supplied = {kw.arg for kw in calls[0].keywords}
        required = {
            name
            for name, field in lifecycle_base.CompletionOutcome.__dataclass_fields__.items()
            if field.default is field.default_factory is __import__("dataclasses").MISSING
        }
        self.assertEqual(
            required - supplied,
            set(),
            "the branch does not fill the required fields",
        )


class ProtocolErrorClassTests(unittest.TestCase):
    def test_a_missing_status_line_is_a_protocol_error(self) -> None:
        with self.assertRaises(WorkerProtocolError):
            parse_desktop_worker_status("работа сделана, отчёт выше")

    def test_a_status_line_that_is_not_last_is_a_protocol_error(self) -> None:
        with self.assertRaises(WorkerProtocolError):
            parse_desktop_worker_status(
                "AUTOPILOT_STATUS: ROTATE\nещё одна строка после статуса"
            )

    def test_two_status_lines_are_a_protocol_error(self) -> None:
        with self.assertRaises(WorkerProtocolError):
            parse_desktop_worker_status(
                "AUTOPILOT_STATUS: ROTATE\nAUTOPILOT_STATUS: ROTATE"
            )

    def test_a_success_status_with_a_reason_code_is_a_protocol_error(self) -> None:
        with self.assertRaises(WorkerProtocolError):
            parse_desktop_worker_status("AUTOPILOT_STATUS: ROTATE MISSING_RESOURCE")

    def test_the_hook_path_still_blocks_because_the_class_is_a_lifecycle_error(
        self,
    ) -> None:
        """The hook path catches DesktopLifecycleError and answers `block`.

        The new class must stay its subclass: otherwise the worker would
        stop getting the reason in the same turn - that is, repairing
        one path would break the other.
        """

        self.assertTrue(issubclass(WorkerProtocolError, DesktopLifecycleError))

    def test_a_well_formed_status_still_parses(self) -> None:
        status, code = parse_desktop_worker_status("AUTOPILOT_STATUS: ROTATE")
        self.assertEqual(status, "ROTATE")
        self.assertFalse(code)


if __name__ == "__main__":
    unittest.main()
