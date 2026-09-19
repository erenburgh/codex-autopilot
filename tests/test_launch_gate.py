"""The launch gate: confirmation instead of a claim, and a ticket instead of improvisation."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from _gates import patch_hook_trust_gates

from codex_autopilot.launch_gate import (
    LaunchCheck,
    LaunchVerdict,
    launch_verdict,
    launch_checklist,
    launch_confirmed,
    render_launch_checklist,
)
from codex_autopilot.run_state import RunState


class Runtime:
    def __init__(self, required_thread_placement: str = "in_project") -> None:
        self.required_thread_placement = required_thread_placement


class Cfg:
    """Minimal stand-in: the state directory and the placement rule."""

    def __init__(
        self, state_dir: Path, required_thread_placement: str = "in_project"
    ) -> None:
        self.state_dir = state_dir
        self.runtime = Runtime(required_thread_placement)


def session(**overrides) -> dict:
    base = {
        "task_id": "A",
        "reservation_token": "token-a",
        "status": "ACTIVE",
        "thread_id": "thread-a",
        "automatic_dispatch_pid": 4242,
        "desktop_placement": "INSIDE",
    }
    base.update(overrides)
    return base


def journal(*events: tuple[int, str]) -> list[dict]:
    return [
        {"sequence": number, "event": name, "reservation_token": "token-a"}
        for number, name in events
    ]


LAUNCHED = (
    (1, "reservation_created"),
    (2, "create_requested"),
    (3, "app_server_thread_created"),
    (4, "start_acknowledged"),
    (5, "visible_launch_report_ready"),
)


class ChecklistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Cfg(Path(self.temp.name))
        # The visibility check reads the REAL Codex directory. The test must
        # look at its own, otherwise the result depends on what the developer
        # currently has open in the sidebar.
        self.codex_home = Path(self.temp.name) / "codex-home"
        self.codex_home.mkdir()
        import unittest.mock as _mock

        patcher = _mock.patch(
            "codex_autopilot.preflight.default_codex_home", return_value=self.codex_home
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self, *, sessions, events) -> RunState:
        state = RunState(run_id="r")
        state.worker_sessions = list(sessions)
        state.lifecycle_journal = list(events)
        return state

    def check(self, checks, check_id: str) -> LaunchCheck:
        return next(item for item in checks if item.id == check_id)

    def test_a_fully_launched_task_is_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(sessions=[session()], events=journal(*LAUNCHED)),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertTrue(launch_confirmed(checks))
        self.assertIn("LAUNCH CONFIRMED", render_launch_checklist(checks))

    def test_a_task_that_was_never_reserved_is_not_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg, self.state(sessions=[], events=[]), task_ids=["A"]
        )
        self.assertFalse(launch_confirmed(checks))
        self.assertFalse(self.check(checks, "reserved").passed)

    def test_a_reservation_without_a_thread_is_not_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(thread_id=None, status="CREATE_REQUESTED")],
                events=journal((1, "reservation_created")),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertFalse(launch_confirmed(checks))
        self.assertFalse(self.check(checks, "thread_bound").passed)

    def test_a_thread_that_never_acknowledged_the_send_is_not_confirmed(self) -> None:
        """This is how the stuck task looked: a thread exists, no work runs."""

        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(status="SEND_RELAYING")],
                events=journal(*LAUNCHED[:3]),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertFalse(launch_confirmed(checks))
        self.assertFalse(self.check(checks, "send_acknowledged").passed)

    def test_a_failure_recorded_after_the_launch_is_not_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session()],
                events=journal(*LAUNCHED, (6, "interrupt_observed")),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertFalse(launch_confirmed(checks))
        self.assertIn("interrupt_observed", self.check(checks, "no_failure_after_launch").detail)

    def test_a_dead_dispatcher_is_not_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(sessions=[session()], events=journal(*LAUNCHED)),
            task_ids=["A"],
            pid_alive=lambda pid: False,
        )
        self.assertFalse(launch_confirmed(checks))

    def test_an_unassessable_check_is_not_a_pass(self) -> None:
        """"Could not be checked" and "checked" are different things."""

        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(automatic_dispatch_pid=None)],
                events=journal(*LAUNCHED),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertIsNone(self.check(checks, "dispatcher_alive").passed)
        self.assertFalse(launch_confirmed(checks))

    def test_an_empty_checklist_never_confirms(self) -> None:
        self.assertFalse(launch_confirmed([]))

    def test_the_rendered_verdict_names_a_failure_as_a_failure(self) -> None:
        checks = launch_checklist(
            self.cfg, self.state(sessions=[], events=[]), task_ids=["A"]
        )
        rendered = render_launch_checklist(checks)
        self.assertIn("LAUNCH FAILED", rendered)
        self.assertIn("this is a failure, not a success", rendered)


class DesktopVisibilityTests(ChecklistTests):
    """R5: an App Server success does not mean the sidebar shows the
    thread."""

    def visibility(self, checks) -> LaunchCheck:
        return self.check(checks, "visible_in_desktop")

    def checks_now(self, placement: str = "INSIDE"):
        return launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(desktop_placement=placement)],
                events=journal(*LAUNCHED),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )

    def test_a_thread_inside_the_project_is_visible(self) -> None:
        self.assertTrue(self.visibility(self.checks_now()).passed)

    def test_a_thread_outside_the_project_is_reported(self) -> None:
        """Exactly this case: the task runs, but it is not in the project."""

        check = self.visibility(self.checks_now("OUTSIDE"))
        self.assertFalse(check.passed)
        self.assertIn("outside the project", check.detail)

    def test_a_vanished_thread_is_reported(self) -> None:
        """A thread with no turn on the server is not persisted -
        measured on probes."""

        check = self.visibility(self.checks_now("ABSENT"))
        self.assertFalse(check.passed)
        self.assertIn("not persisted", check.detail)

    def test_an_unmeasured_placement_is_unassessable_not_invisible(self) -> None:
        self.assertIsNone(self.visibility(self.checks_now("")).passed)

    def test_an_unmeasured_placement_never_becomes_a_ticket(self) -> None:
        """There is a window between creating the thread and recording
        the placement: failing on it would breed false tickets again."""

        self.assertIs(launch_verdict(self.checks_now("")), LaunchVerdict.IN_PROGRESS)

    def test_a_config_that_allows_outside_does_not_get_a_ticket(self) -> None:
        """A ticket for what the config allowed is a false ticket."""

        relaxed = Cfg(self.cfg.state_dir, required_thread_placement="visible")
        checks = launch_checklist(
            relaxed,
            self.state(
                sessions=[session(desktop_placement="OUTSIDE")],
                events=journal(*LAUNCHED),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        visible = next(item for item in checks if item.id == "visible_in_desktop")
        self.assertIs(visible.passed, True)
        self.assertIsNot(launch_verdict(checks), LaunchVerdict.FAILED)

    def test_a_measured_mismatch_does_become_a_ticket(self) -> None:
        """M11-R5: a measured mismatch is a result, not a window.

        Before this, OUTSIDE and ABSENT did not change the verdict at
        all: it held at IN_PROGRESS, no ticket was opened, and a task
        created outside the project simply sat there. The window is
        guarded separately - by being unmeasured, not by blindness to
        the measurement.
        """

        self.assertIs(launch_verdict(self.checks_now("OUTSIDE")), LaunchVerdict.FAILED)
        self.assertIs(launch_verdict(self.checks_now("ABSENT")), LaunchVerdict.FAILED)


class VerdictTests(ChecklistTests):
    """Three states: confirmed, still in progress, failed."""

    def test_a_launch_still_creating_its_thread_is_in_progress(self) -> None:
        """Creating a thread takes tens of seconds - that is not a
        failure."""

        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(thread_id=None, status="CREATE_REQUESTED")],
                events=journal((1, "reservation_created"), (2, "create_requested")),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertIs(launch_verdict(checks), LaunchVerdict.IN_PROGRESS)
        self.assertIn("LAUNCH IN PROGRESS", render_launch_checklist(checks))

    def test_a_dead_dispatcher_is_a_failure_not_progress(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(thread_id=None, status="CREATE_REQUESTED")],
                events=journal((1, "reservation_created")),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: False,
        )
        self.assertIs(launch_verdict(checks), LaunchVerdict.FAILED)

    def test_a_failure_event_is_a_failure_not_progress(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(status="RETRY_WAIT")],
                events=journal(*LAUNCHED, (6, "interrupt_observed")),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertIs(launch_verdict(checks), LaunchVerdict.FAILED)

    def test_a_complete_launch_is_confirmed(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(sessions=[session()], events=journal(*LAUNCHED)),
            task_ids=["A"],
            pid_alive=lambda pid: True,
        )
        self.assertIs(launch_verdict(checks), LaunchVerdict.CONFIRMED)


class UnconfirmedLaunchGoesToDevOpsTests(unittest.TestCase):
    """The task did not come up - the session does not repair it
    itself, it opens a ticket."""

    def setUp(self) -> None:
        from unittest import mock

        from _plan_contract import initialize_verified_project as initialize_project
        from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
        import json

        from test_desktop_lifecycle import graph

        gate = mock.patch(
            "codex_autopilot.lifecycle_reservations.require_trusted_stop_hook_for_config"
        )
        gate.start()
        self.addCleanup(gate.stop)
        # The hook-trust gate reads the machine's REAL App Server. Without this
        # substitution the suite passed only because the developer's hooks
        # happened to be trusted, and it collapsed right after reinstalling the plugin.
        patch_hook_trust_gates(self)

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(graph()), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)

    def report(self):
        from codex_autopilot.control import _launch_report

        return _launch_report(
            self.cfg, ["A"], started="Codex Autopilot dispatcher started", timeout=0.0
        )

    def test_a_launch_in_progress_does_not_open_a_ticket(self) -> None:
        """A false ticket on a launch in progress is exactly the noise
        that makes checks stop being read."""

        from codex_autopilot.pipeline_engineer import PipelineIncidentStore
        from unittest import mock

        with mock.patch(
            "codex_autopilot.control.launch_verdict",
            return_value=__import__(
                "codex_autopilot.launch_gate", fromlist=["LaunchVerdict"]
            ).LaunchVerdict.IN_PROGRESS,
        ):
            result = self.report()
        # The report arrives on a running launch too.
        self.assertIn("systemMessage", result)
        # A running launch must answer continue: a blocking answer
        # leaves the initiating turn "interrupted", while the dispatcher waits
        # for a stable "completed" and never creates the thread.
        self.assertTrue(result.get("continue"))
        self.assertNotIn("decision", result)
        self.assertNotIn("Ticket", result["systemMessage"])
        self.assertEqual(PipelineIncidentStore(self.cfg.state_dir).load()["incidents"], [])

    def test_a_launch_in_progress_is_reported_without_a_ticket(self) -> None:
        from codex_autopilot.launch_gate import LaunchVerdict
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore
        from unittest import mock

        with mock.patch(
            "codex_autopilot.control.launch_verdict",
            return_value=LaunchVerdict.IN_PROGRESS,
        ):
            result = self.report()
        self.assertTrue(result.get("continue"))
        self.assertNotIn("Ticket", result["systemMessage"])
        self.assertEqual(PipelineIncidentStore(self.cfg.state_dir).load()["incidents"], [])

    def test_an_unconfirmed_launch_blocks_instead_of_claiming_success(self) -> None:
        result = self.report()
        self.assertEqual(result.get("decision"), "block")
        self.assertNotIn("continue", result)
        self.assertIn("LAUNCH FAILED", result["reason"])

    def test_an_unconfirmed_launch_opens_a_devops_ticket(self) -> None:
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        result = self.report()
        self.assertIn("Ticket", result["reason"])
        incidents = PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["code"], "launch_not_confirmed")
        self.assertEqual(incidents[0]["affected_task_ids"], ["A"])

    def test_the_session_is_told_not_to_repair_the_pipeline_itself(self) -> None:
        self.assertIn("Do not repair the launch in this turn", self.report()["reason"])

    def test_the_ticket_does_not_claim_an_owner_that_does_not_exist(self) -> None:
        """Pointing at a devops who does not exist is a lie, not
        routing."""

        reason = self.report()["reason"]
        self.assertIn("No automatic executor was raised", reason)
        self.assertNotIn("владелец — DevOps", reason)

    def test_the_same_failure_twice_is_one_signature(self) -> None:
        """A normalized signature: a repeat is recognized as a repeat."""

        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        self.report()
        self.report()
        ledger = PipelineIncidentStore(self.cfg.state_dir).signature_ledger()
        self.assertEqual(len(ledger), 1)


if __name__ == "__main__":
    unittest.main()


class PlacementGateTests(unittest.TestCase):
    """Placement is asked of the server: the sidebar is drawn from its
    list.

    The previous version read keys out of .codex-global-state.json and
    called OUTSIDE three threads that a human saw in the sidebar with his
    own eyes. The instrument was never checked against a thread known to
    be visible, and on its readings a false conclusion was built: that a
    visible task cannot be created through the App Server.
    """

    def client(self, thread=None, error=None):
        from unittest import mock

        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        if error is not None:
            fake.read_thread.side_effect = error
        else:
            fake.read_thread.return_value = thread
        return fake

    def test_a_thread_in_the_expected_project_is_inside(self) -> None:
        from codex_autopilot.launch_gate import INSIDE, desktop_placement

        placement = desktop_placement(
            "t1", project_id="p1", client=self.client({"id": "t1", "projectId": "p1"})
        )
        self.assertEqual(placement, INSIDE)

    def test_a_thread_without_a_project_is_outside(self) -> None:
        from codex_autopilot.launch_gate import OUTSIDE, desktop_placement

        placement = desktop_placement(
            "t1", project_id="p1", client=self.client({"id": "t1", "projectId": None})
        )
        self.assertEqual(placement, OUTSIDE)

    def test_a_thread_in_another_project_is_outside(self) -> None:
        from codex_autopilot.launch_gate import OUTSIDE, desktop_placement

        placement = desktop_placement(
            "t1", project_id="p1", client=self.client({"id": "t1", "projectId": "p2"})
        )
        self.assertEqual(placement, OUTSIDE)

    def test_a_vanished_thread_is_absent(self) -> None:
        """A thread without a single turn is not persisted by the
        server."""

        from codex_autopilot.launch_gate import ABSENT, desktop_placement

        placement = desktop_placement(
            "t1", project_id="p1", client=self.client(error=RuntimeError("thread not found"))
        )
        self.assertEqual(placement, ABSENT)

    def test_an_unbound_reservation_is_absent(self) -> None:
        from codex_autopilot.launch_gate import ABSENT, desktop_placement

        self.assertEqual(desktop_placement("", client=self.client()), ABSENT)

    def test_any_project_counts_when_none_is_required(self) -> None:
        from codex_autopilot.launch_gate import INSIDE, desktop_placement

        placement = desktop_placement(
            "t1", client=self.client({"id": "t1", "projectId": "p2"})
        )
        self.assertEqual(placement, INSIDE)
class OrphanedReservationTests(unittest.TestCase):
    """A reservation exists, no thread, the dispatcher died - the run
    must come back to life."""

    def test_a_reservation_without_a_live_dispatcher_is_revived(self) -> None:
        from unittest import mock

        from codex_autopilot.control import _reservations_without_a_live_dispatcher

        cfg = mock.Mock(state_dir=Path('/tmp'))
        alive = mock.Mock(
            reservation_token="live", task_id="A"
        )
        orphan = mock.Mock(reservation_token="orphan", task_id="B")
        state = mock.Mock()
        state.worker_sessions = [
            {"reservation_token": "live", "automatic_dispatch_pid": 111},
            {"reservation_token": "orphan", "automatic_dispatch_pid": None},
        ]
        with mock.patch("codex_autopilot.control.StateStore") as store, mock.patch(
            "codex_autopilot.control.pending_descriptors", return_value=(alive, orphan)
        ), mock.patch(
            "codex_autopilot.control.pid_alive", side_effect=lambda pid: pid == 111
        ):
            store.return_value.load.return_value = state
            revived = _reservations_without_a_live_dispatcher(cfg)
        self.assertEqual([item.reservation_token for item in revived], ["orphan"])

    def test_nothing_is_revived_while_a_dispatcher_is_alive(self) -> None:
        from unittest import mock

        from codex_autopilot.control import _reservations_without_a_live_dispatcher

        alive = mock.Mock(reservation_token="live", task_id="A")
        state = mock.Mock()
        state.worker_sessions = [
            {"reservation_token": "live", "automatic_dispatch_pid": 111}
        ]
        with mock.patch("codex_autopilot.control.StateStore") as store, mock.patch(
            "codex_autopilot.control.pending_descriptors", return_value=(alive,)
        ), mock.patch("codex_autopilot.control.pid_alive", return_value=True):
            store.return_value.load.return_value = state
            self.assertEqual(_reservations_without_a_live_dispatcher(mock.Mock(state_dir=Path('/tmp'))), ())


class CausalPredecessorTests(unittest.TestCase):
    """A turn is proved completed by the journal, not by the session
    status."""

    def state(self, status: str):
        from unittest import mock

        state = mock.Mock()
        state.worker_sessions = [
            {"thread_id": "owner", "turn_id": "turn-1", "status": status}
        ]
        state.lifecycle_journal = [
            {"event": "turn_completed", "thread_id": "owner", "turn_id": "turn-1"}
        ]
        return state

    def test_a_plan_change_requester_is_a_valid_predecessor(self) -> None:
        """A task that requested a plan change has completed its turn."""

        from codex_autopilot.control import _turn_is_completed

        self.assertTrue(
            _turn_is_completed(self.state("PLAN_CHANGE_REQUESTED"), "owner", "turn-1")
        )

    def test_a_turn_without_a_completion_record_is_not_accepted(self) -> None:
        from unittest import mock

        from codex_autopilot.control import _turn_is_completed

        state = mock.Mock()
        state.lifecycle_journal = [
            {"event": "turn_identity_bound", "thread_id": "owner", "turn_id": "turn-1"}
        ]
        # The second piece of evidence is a closed session with the same turn,
        # so the stub needs an explicitly empty list - otherwise Mock's
        # behaviour is tested, not the rules.
        state.worker_sessions = []
        self.assertFalse(_turn_is_completed(state, "owner", "turn-1"))

    def test_another_threads_completion_does_not_count(self) -> None:
        from codex_autopilot.control import _turn_is_completed

        self.assertFalse(
            _turn_is_completed(self.state("COMPLETED"), "someone-else", "turn-1")
        )


class AFastLaunchIsStillALaunchTests(ChecklistTests):
    """A fast worker must not be declared unlaunched.

    A live run got the ticket `launch_not_confirmed: dispatcher_alive,
    send_acknowledged` while the same checklist said "turn completed".
    Both items were false EXACTLY because the work had managed to finish:
    the session status had moved past ACTIVE, and the dispatcher exited
    normally. The faster the milestone, the more likely the false
    failure - and every such failure needed an operator.
    """

    FINISHED = LAUNCHED + ((6, "turn_completed"),)

    def test_a_finished_turn_needs_no_live_dispatcher(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(status="COMPLETED")], events=journal(*self.FINISHED)
            ),
            task_ids=["A"],
            pid_alive=lambda pid: False,
        )
        self.assertTrue(self.check(checks, "dispatcher_alive").passed)

    def test_the_send_is_confirmed_by_the_journal_not_the_moment(self) -> None:
        checks = launch_checklist(
            self.cfg,
            self.state(
                sessions=[session(status="PLAN_CHANGE_REQUESTED")],
                events=journal(*self.FINISHED),
            ),
            task_ids=["A"],
            pid_alive=lambda pid: False,
        )
        self.assertTrue(self.check(checks, "send_acknowledged").passed)

    def test_a_dead_dispatcher_without_a_finished_turn_still_fails(self) -> None:
        """The relaxation must not hide a real dispatcher death."""

        checks = launch_checklist(
            self.cfg,
            self.state(sessions=[session()], events=journal(*LAUNCHED)),
            task_ids=["A"],
            pid_alive=lambda pid: False,
        )
        self.assertFalse(self.check(checks, "dispatcher_alive").passed)
