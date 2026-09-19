"""R31: the check refuses where the error can still be fixed.

The completion gate asks for evidence linked to the milestone. A record
without milestone_id used to be accepted silently, and the refusal came
only after the whole turn was spent.

Measured on a live run: worker M2 recorded four pieces of evidence,
putting the milestone id into created_by ("M2-FILE-EXISTS",
"M2-EXACT-CONTENT") instead of milestone_id. Nothing landed in
milestone_evidence, completion was refused with "M2 returned completion
without new Project Memory evidence", the turn was lost entirely, and a
ticket was opened on M1.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from codex_autopilot.memory import MemoryValidationError
from codex_autopilot.memory_mcp import MemoryMcpServer


class EarlyMilestoneLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        self.state = self.root / ".codex-autopilot"
        self.state.mkdir()
        (self.state / "config.toml").write_text("", encoding="utf-8")
        self.set_active(["M2"])
        self.server = MemoryMcpServer(self.root)

    def set_active(self, task_ids: list[str]) -> None:
        (self.state / "run-state.json").write_text(
            json.dumps({"active_task_ids": task_ids}), encoding="utf-8"
        )

    def evidence(self, **extra) -> dict:
        payload = {
            "kind": "test",
            "summary": "beta file contains beta",
            "created_by": "M2-FILE-EXISTS",
            "command": "cat autopilot-080-b.txt",
            "result": "beta",
            "exit_code": 0,
        }
        payload.update(extra)
        return payload

    def test_unlinked_evidence_is_refused_while_a_milestone_is_active(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(self.evidence())
        self.assertIn("M2", str(caught.exception))

    def test_the_refusal_names_what_is_missing(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(self.evidence())
        text = str(caught.exception)
        self.assertIn("milestone_id", text)
        self.assertIn("created_by", text)

    def test_the_milestone_is_never_substituted_for_the_worker(self) -> None:
        """A link the worker did not name would be an invented one."""

        with self.assertRaises(MemoryValidationError):
            self.server._record_evidence(self.evidence())
        rows = list(
            self.server.memory.milestone_evidence("M2", after_audit_id=0, limit=10)
        )
        self.assertEqual(rows, [])

    def test_a_milestone_that_names_no_task_is_refused_too(self) -> None:
        """R31 refused the wrong half: absence, but not a wrong name.

        The gate exists so the record can reach the completion gate. It
        refused only a MISSING milestone_id and accepted any non-empty
        string - including the very label the measured worker used in
        created_by. Such a record lands under a milestone nobody will ask
        about, completion is refused for "no new evidence", and the turn is
        lost exactly as it was before the gate existed.
        """

        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(
                self.evidence(milestone_id="M2-FILE-EXISTS")
            )
        text = str(caught.exception)
        self.assertIn("M2-FILE-EXISTS", text)
        self.assertIn("M2", text)
        self.assertEqual(
            list(self.server.memory.milestone_evidence("M2", after_audit_id=0, limit=10)),
            [],
        )

    def test_linked_evidence_passes(self) -> None:
        result = self.server._record_evidence(self.evidence(milestone_id="M2"))
        self.assertTrue(result)

    def test_without_an_active_milestone_the_link_stays_optional(self) -> None:
        """Outside a milestone there is nothing to link the evidence to."""

        self.set_active([])
        self.assertTrue(self.server._record_evidence(self.evidence()))


if __name__ == "__main__":
    unittest.main()


class FailureNamesTheFailingTaskTests(unittest.TestCase):
    """The ticket names the task the failure happened on.

    The dispatcher loop moves from task to task, reassigning its own
    token. The failure handler outside the loop held the initial one, and
    a failure on a later task was attributed to the first one. Measured:
    ticket incident-78e67b38680498f1 for the M2 failure named
    affected_task_ids=['M1'], and the "on failure" ladder printed the
    steps of M1, which had already been verified.
    """

    def test_the_cursor_follows_the_loop(self) -> None:
        from codex_autopilot.cli import _RelayCursor

        cursor = _RelayCursor("token-m1")
        cursor.token = "token-m2"
        self.assertEqual(cursor.token, "token-m2")

    def test_the_failure_handler_reads_the_cursor_not_the_initial_token(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/cli.py"
        ).read_text(encoding="utf-8")
        head = source[source.index("def _run_automatic_relay_dispatch") :]
        body = head[: head.index("\ndef ", 1)]
        self.assertIn('_print_relay_timeline(cfg, cursor.token, "on failure")', body)
        self.assertIn("_record_detached_dispatch_failure(cfg, cursor.token, error)", body)

    def test_the_loop_updates_the_cursor_each_iteration(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/cli.py"
        ).read_text(encoding="utf-8")
        head = source[source.index("def _automatic_relay_loop") :]
        body = head[: head.index("\ndef ", 1)]
        self.assertIn("cursor.token = token", body)


class RefusalNamesWhatIsAcceptedTests(unittest.TestCase):
    """R31: the refusal must be enough to recover without reading sources.

    Measured on a live M1 run: nine refusals in a row. The worker went
    through evidence kind names - filesystem_verification, command_output,
    test_result, verification - each time getting only "unsupported
    evidence kind", then guessed at the path parameter, then went off to
    read the plugin sources with rg. Six minutes against twenty-three
    seconds in v0.7, where there was no memory at all.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        state = self.root / ".codex-autopilot"
        state.mkdir()
        (state / "config.toml").write_text("", encoding="utf-8")
        (state / "run-state.json").write_text(
            json.dumps({"active_task_ids": []}), encoding="utf-8"
        )
        self.server = MemoryMcpServer(self.root)

    def test_an_unsupported_kind_lists_the_supported_ones(self) -> None:
        from codex_autopilot.memory import EVIDENCE_KINDS

        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(
                {"kind": "command_output", "summary": "s", "created_by": "w"}
            )
        text = str(caught.exception)
        for kind in sorted(EVIDENCE_KINDS):
            self.assertIn(kind, text)

    def test_a_missing_path_names_the_parameter(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(
                {"kind": "file", "summary": "s", "created_by": "w"}
            )
        self.assertIn("path=", str(caught.exception))

    def test_an_unknown_argument_lists_the_accepted_ones(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.server._record_evidence(
                {
                    "kind": "file",
                    "summary": "s",
                    "created_by": "w",
                    "project_path": "x",
                }
            )
        text = str(caught.exception)
        self.assertIn("project_path", text)
        self.assertIn("milestone_id", text)
        self.assertIn("artifact_path", text)


class PlacementDeadlineTests(unittest.TestCase):
    """M11-R5: an unmeasured placement does not stay undecided forever.

    OUTSIDE and ABSENT were already decisive and went into one normalized
    ticket. But the case "nobody is left to measure it" - the dispatcher
    died between creating the thread and the placement gate - held the
    verdict in IN_PROGRESS forever: no ticket was opened, the task did not
    move, and from outside it looked as if the launch was still running.
    """

    def _check(self, session, *, now):
        from codex_autopilot.launch_gate import _desktop_visibility

        return _desktop_visibility("T1", "thread-1", session, now=lambda: now)

    def test_a_fresh_unmeasured_placement_stays_undecided(self) -> None:
        """The window between creation and recording is not a failure."""

        check = self._check(
            {"create_acknowledged_at": "2026-09-13T12:00:00+00:00"},
            now=datetime(2026, 9, 13, 12, 0, 30, tzinfo=timezone.utc).timestamp(),
        )
        self.assertIsNone(check.passed)

    def test_a_stale_unmeasured_placement_fails(self) -> None:
        from codex_autopilot.launch_gate import (
            PLACEMENT_MEASUREMENT_DEADLINE_SECONDS,
        )

        check = self._check(
            {"create_acknowledged_at": "2026-09-13T12:00:00+00:00"},
            now=(
                datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc).timestamp()
                + PLACEMENT_MEASUREMENT_DEADLINE_SECONDS
                + 1
            ),
        )
        self.assertIs(check.passed, False)
        self.assertIn("nobody is left to measure it", check.detail)

    def test_without_a_creation_stamp_nothing_is_declared_overdue(self) -> None:
        """No stamp, no deadline. One cannot stand in for the other."""

        check = self._check({}, now=datetime(2030, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(check.passed)

    def test_a_failed_placement_reaches_the_normalized_ticket(self) -> None:
        """A failed check must drop the verdict, or there is no ticket."""

        from codex_autopilot.launch_gate import (
            LaunchCheck,
            LaunchVerdict,
            launch_verdict,
        )

        checks = (
            LaunchCheck("reserved", "T1", True, ""),
            LaunchCheck("visible_in_desktop", "T1", False, "nobody is left to measure it"),
        )
        self.assertIs(launch_verdict(checks), LaunchVerdict.FAILED)


class HandoffObservationTests(unittest.TestCase):
    """M11-R5, second half: editability is observed, not gated on.

    Measured on a live server: canAcceptDirectInput comes back null both
    in thread/read of a thread that is not loaded and in all thirty rows
    of thread/list. A gate cannot be built on such a field - it does not
    tell "cannot be edited" from "nobody is holding it".
    """

    def test_the_observation_carries_what_the_server_said(self) -> None:
        from codex_autopilot.launch_gate import placement_observation

        class Fake:
            def read_thread(self, thread_id):
                return {
                    "canAcceptDirectInput": None,
                    "status": {"type": "notLoaded"},
                    "originator": "Codex Desktop",
                    "threadSource": None,
                }

        observed = placement_observation(Fake(), "t-1")
        self.assertTrue(observed["observed"])
        self.assertIsNone(observed["can_accept_direct_input"])
        self.assertEqual(observed["status_type"], "notLoaded")
        self.assertEqual(observed["originator"], "Codex Desktop")

    def test_a_failed_read_is_recorded_as_not_observed(self) -> None:
        from codex_autopilot.launch_gate import placement_observation

        class Broken:
            def read_thread(self, thread_id):
                raise RuntimeError("thread not found")

        observed = placement_observation(Broken(), "t-1")
        self.assertFalse(observed["observed"])
        self.assertIn("thread not found", observed["reason"])
