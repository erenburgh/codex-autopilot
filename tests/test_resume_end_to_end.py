"""The end-to-end resume path: from the user's phrase to the start of work.

Every step of this chain was broken separately, and each surfaced only
on a live launch - one per lap, with a human involved and the runtime
reinstalled. None was covered by a test.

The whole chain:

    phrase -> arming -> the Stop hook takes the request -> reservation
    -> thread creation -> project placement -> taking the turn -> start

Each test below closes one of the seven defects found, so the next one
like it is caught here and not on a live run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _appserver_fakes import activate_via_app_server
from _gates import patch_hook_trust_gates
from _relay import TEST_RELAY_OWNER, reserve_ready_frontier
from _plan_contract import initialize_verified_project as initialize_project
from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
from codex_autopilot.control import handle_prompt_hook, handle_stop_hook
from codex_autopilot.run_state import StateStore
from test_desktop_lifecycle import graph


class ResumeChainTests(unittest.TestCase):
    def setUp(self) -> None:
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
        # The registry of armed starts is one per user and lives in TMPDIR.
        # Without isolation this suite left a record about its temporary
        # directory in it, and the live Stop hook then refused to launch anything:
        # "multiple Autopilot starts are armed".
        registry = Path(tempfile.mkdtemp(prefix="codex-autopilot-launch-registry-")) / "requests"
        launch_dir = mock.patch.dict(
            os.environ, {"CODEX_AUTOPILOT_LAUNCH_DIR": str(registry)}
        )
        launch_dir.start()
        self.addCleanup(launch_dir.stop)
        self.store = StateStore(self.cfg.state_dir)

        self.spawned: list[str] = []
        spawn = mock.patch(
            "codex_autopilot.control.spawn_automatic_app_server_relay",
            side_effect=lambda root, *, reservation_token, **_kw: (
                self.spawned.append(reservation_token) or 4242
            ),
        )
        spawn.start()
        self.addCleanup(spawn.stop)

        # The placement gate reads the real Codex directory: in a test it must
        # look at its own, otherwise the result depends on the machine.
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.write_desktop_state()
        home = mock.patch(
            "codex_autopilot.preflight.default_codex_home", return_value=self.codex_home
        )
        home.start()
        self.addCleanup(home.stop)

    def write_desktop_state(self, threads: dict | None = None) -> None:
        (self.codex_home / ".codex-global-state.json").write_text(
            json.dumps({"thread-project-assignments": threads or {}}),
            encoding="utf-8",
        )

    def resume(self) -> dict:
        return handle_prompt_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "продолжи кодекс автопайлот",
                "cwd": str(self.root),
            }
        )

    def stop(self, **overrides) -> dict:
        payload = {
            "hook_event_name": "Stop",
            "cwd": str(self.root),
            # The hook comes from the same owner that holds the reservation:
            # that is exactly how it looks on the live path.
            "session_id": TEST_RELAY_OWNER,
            "turn_id": "owner-turn",
            "last_assistant_message": "готово",
            "stop_hook_active": False,
        }
        payload.update(overrides)
        return handle_stop_hook(payload)

    # --- 1. the phrase reaches arming ---------------------------------

    def test_the_russian_phrase_arms_the_resume(self) -> None:
        """Cyrillic in the product name must not break the command."""

        self.resume()
        state = self.store.load()
        self.assertEqual((state.status, state.phase), ("READY", "ARMED"))

    # --- 2. the Stop hook takes the request and starts the work -------

    def test_stop_hook_reserves_and_spawns_after_arming(self) -> None:
        self.resume()
        result = self.stop()
        self.assertTrue(self.spawned, "not a single reservation was raised")
        self.assertIn("reason", result)

    # --- 3. a taken request does not vanish when the state moved on ---

    def test_an_advanced_state_does_not_swallow_the_armed_request(self) -> None:
        """The defect that made a resume vanish without a trace.

        The hook takes the request, then sees the state is no longer
        READY/ARMED, and returns nothing: no process, no journal, no
        message.
        """

        descriptor = reserve_ready_frontier(self.cfg)[0]
        self.resume()
        state = self.store.load()
        state.status = "RUNNING"
        state.phase = "PLAN_CHANGE_DRAINING"
        self.store.save(state)

        result = self.stop()
        self.assertIn(descriptor.reservation_token, self.spawned)
        self.assertIn("reason", result)

    # --- 4. a hanging reservation with no live dispatcher is raised ----

    def test_a_reservation_whose_dispatcher_died_is_revived(self) -> None:
        descriptor = reserve_ready_frontier(self.cfg)[0]
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        session["automatic_dispatch_pid"] = 999999  # known to be dead
        self.store.save(state)

        self.resume()
        self.stop()
        self.assertIn(descriptor.reservation_token, self.spawned)

    # --- 5. creation, placement and start -----------------------------

    def test_dispatcher_creates_places_and_starts_the_task(self) -> None:
        """The second half of the chain: what the detached process does."""

        descriptor = reserve_ready_frontier(self.cfg)[0]
        self.write_desktop_state({"thread-a": {"projectId": "desktop-project"}})
        activate_via_app_server(self.cfg, self.root, descriptor, "thread-a")
        session = next(
            item
            for item in self.store.load().worker_sessions
            if item["reservation_token"] == descriptor.reservation_token
        )
        self.assertEqual(session["status"], "ACTIVE")
        self.assertEqual(session["thread_id"], "thread-a")

    # --- 6. the report contains the ladder of steps -------------------

    def test_the_report_shows_the_ladder_of_steps(self) -> None:
        self.resume()
        result = self.stop()
        report = result.get("reason") or result.get("systemMessage") or ""
        self.assertIn("slot reserved", report)
        self.assertIn("LAUNCH", report)

    # --- 7. a cleared stop reason does not come back from disk --------

    def _escalate(self) -> str:
        """Open a ticket waiting on the user and stop the run on it."""

        from codex_autopilot.pipeline_engineer import (
            IncidentClass,
            IncidentSignal,
            PipelineIncidentStore,
            SideEffectOutcome,
            _atomic_json,
        )

        store = PipelineIncidentStore(self.cfg.state_dir)
        incident = store.open_incident(
            IncidentSignal(
                signal_id="sig-b4",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                operation="create_thread",
                summary="вердикт не разобран",
                affected_task_ids=("M4",),
                system_state={},
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
            ),
            at="2026-09-15T17:00:00+00:00",
        )
        incident_id = str(incident["incident_id"])
        raw = store.load()
        for item in raw["incidents"]:
            if item["incident_id"] == incident_id:
                item["phase"] = "ESCALATE_TO_USER"
        _atomic_json(store.path, raw)
        return incident_id

    def test_resume_answers_the_escalation_whatever_phase_the_run_is_in(self) -> None:
        """The answer to an escalation ignores the run phase - by execution.

        This used to be checked by reading the source of
        ``_answer_escalation``: that its body holds no comparison with
        ``PIPELINE_ENGINEER_ESCALATED`` and does reach for
        ``incident_ids_awaiting_the_user``. Such a test stays green even
        when the line it needs sits under ``if False:``. Here the run is
        stopped by an escalation in a phase that is NOT set by the
        engineer finishing - and the resume must close the ticket anyway.
        """

        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        incident_id = self._escalate()
        state = self.store.load()
        state.status = "BLOCKED"
        state.phase = "DESKTOP_WORKERS_ACTIVE"
        self.store.save(state)

        self.resume()

        after = self.store.load()
        self.assertEqual((after.status, after.phase), ("READY", "ARMED"))
        phase = PipelineIncidentStore(self.cfg.state_dir).incident_package(incident_id)["incident"]["phase"]
        self.assertEqual(phase, "RESOLVED", "the ticket stayed open because of the run phase")

    def test_answering_the_escalation_clears_the_blocked_reason(self) -> None:
        """The stop reason was cleared in memory and came back from disk.

        The resume set ``last_error = None`` on the object, and right
        after re-read the state (``state = store.load()``) - for the sake
        of the tasks reconciliation returned. The re-read state carried
        the old reason, and that is what got saved. Measured: after
        answering an escalation the run goes to READY/ARMED, while the
        detailed status still prints the ``last_error`` of a stop that no
        longer exists.
        """

        self._escalate()
        state = self.store.load()
        state.status = "BLOCKED"
        state.last_error = "M0 DANGEROUS_PERMISSION: перезапись живого рантайма"
        self.store.save(state)

        self.resume()

        after = self.store.load()
        self.assertEqual((after.status, after.phase), ("READY", "ARMED"))
        self.assertIsNone(
            after.last_error,
            "the stop reason survived the answer to the escalation",
        )


if __name__ == "__main__":
    unittest.main()


class DeadRelayWithoutAThreadIsNotADeadEndTests(unittest.TestCase):
    """A relay that died before creating the thread must not lock the run.

    On a live run the session stayed in RELAYING with an empty thread_id:
    the process died between "started" and "created". The launch answered
    `automatic relay cannot spawn from 'RELAYING'`, and nobody could take
    that session apart - there is nothing to observe from the App Server
    side either, the thread does not exist. The run became unrevivable,
    although nothing had been created.
    """

    def test_a_relaying_session_without_a_thread_can_respawn(self) -> None:
        from codex_autopilot import control

        session = {
            "reservation_token": "t1",
            "relay_owner_thread_id": "owner",
            "status": "RELAYING",
            "thread_id": None,
            "automatic_dispatch_pid": 999_999_999,
            "automatic_dispatch_state": "RUNNING",
        }
        control._revive_dead_relay_session(session)
        self.assertEqual(session["status"], "CREATE_REQUESTED")
        self.assertIsNone(session["automatic_dispatch_pid"])

    def test_a_relaying_session_with_a_thread_is_left_alone(self) -> None:
        """The thread exists - the side effect happened; do not guess."""

        from codex_autopilot import control

        session = {
            "reservation_token": "t1",
            "status": "RELAYING",
            "thread_id": "01a0-real",
            "automatic_dispatch_pid": 999_999_999,
        }
        self.assertFalse(control._revive_dead_relay_session(session))
        self.assertEqual(session["status"], "RELAYING")


class ACompletedSessionIsAlsoAWitnessTests(unittest.TestCase):
    """A completed turn is proved by more than a journal entry.

    The on-call engineer only started writing `turn_completed` now. Runs
    created before that have a completed engineer turn and no event: the
    chain stopped at `automatic relay has no completed causal
    predecessor`, and the only fix was editing the journal by hand - that
    is, forging a record of something the system never observed.
    """

    def state(self, *, journal, sessions):
        from types import SimpleNamespace

        return SimpleNamespace(lifecycle_journal=journal, worker_sessions=sessions)

    def test_the_journal_entry_is_enough(self) -> None:
        from codex_autopilot.control import _turn_is_completed

        state = self.state(
            journal=[{"event": "turn_completed", "thread_id": "t", "turn_id": "u"}],
            sessions=[],
        )
        self.assertTrue(_turn_is_completed(state, "t", "u"))

    def test_a_completed_session_is_enough(self) -> None:
        from codex_autopilot.control import _turn_is_completed

        state = self.state(
            journal=[],
            sessions=[{"thread_id": "t", "turn_id": "u", "status": "COMPLETED"}],
        )
        self.assertTrue(_turn_is_completed(state, "t", "u"))

    def test_an_unfinished_session_is_not_a_witness(self) -> None:
        from codex_autopilot.control import _turn_is_completed

        state = self.state(
            journal=[],
            sessions=[{"thread_id": "t", "turn_id": "u", "status": "ACTIVE"}],
        )
        self.assertFalse(_turn_is_completed(state, "t", "u"))


class PlanChangePredecessorIsAcceptedTests(unittest.TestCase):
    """A task that requested a plan change is a lawful predecessor.

    It finished its turn and wrote turn_completed, but its session stays
    in PLAN_CHANGE_REQUESTED. control has accounted for this for a long
    time; lifecycle_dispatch held a second copy of the status check, and
    there was nobody to raise the scheduler such a task had reserved:
    `automatic successor has no completed causal predecessor turn`.
    """

    def state(self, status: str, *, journal: bool):
        from types import SimpleNamespace

        session = {"thread_id": "owner", "turn_id": "turn-1", "status": status}
        entries = (
            [{"event": "turn_completed", "thread_id": "owner", "turn_id": "turn-1"}]
            if journal
            else []
        )
        return SimpleNamespace(worker_sessions=[session], lifecycle_journal=entries)

    def accepts(self, state) -> bool:
        # The real function from the runtime, not a copy in the test: the first
        # version of these checks repeated the logic locally and caught no mutation.
        from codex_autopilot.lifecycle_dispatch import causal_predecessor

        return causal_predecessor(state, "owner") is not None

    def test_plan_change_requested_with_a_completed_turn_is_accepted(self) -> None:
        self.assertTrue(self.accepts(self.state("PLAN_CHANGE_REQUESTED", journal=True)))

    def test_a_completed_session_is_accepted_without_the_journal(self) -> None:
        self.assertTrue(self.accepts(self.state("COMPLETED", journal=False)))

    def test_an_unfinished_turn_is_still_refused(self) -> None:
        self.assertFalse(self.accepts(self.state("ACTIVE", journal=False)))

    # "The dispatcher barrier uses the shared predicate" is now checked by
    # execution in test_desktop_lifecycle: a substituted causal_predecessor
    # makes adopt_automatic_dispatcher_successor fail with that very refusal.
