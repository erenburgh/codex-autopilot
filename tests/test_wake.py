"""A timed retry rises by itself, not on a human's word.

Measured on the v1.0 run: a task hit the rate limit, the runtime recorded
the retry time, the last dispatcher exited - and no live process was
left. The run stood until the owner typed "Resume", every few hours, for
an action the runtime could do itself.

The alarm is the dispatcher's deferred successor. Both what it does and
what it does not do are checked: it does not wake a run stopped by a
human, does not jostle a live dispatcher, does not fire before an
extended limit.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import tempfile
import unittest

from _gates import patch_hook_trust_gates
from _plan_contract import initialize_verified_project as initialize_project
from _relay import TEST_RELAY_OWNER, reserve_ready_frontier
from codex_autopilot import cli, control
from codex_autopilot.config import load_config
from codex_autopilot.lifecycle_base import PENDING_SESSION_STATUSES
from codex_autopilot.lifecycle_failures import record_desktop_failure
from codex_autopilot.run_state import StateStore
from codex_autopilot.wake import (
    derive_owner,
    due_wake_epoch,
    ensure_wake,
    register_project,
    registered_projects,
    run_wake,
    sweep,
    wake_command,
)
from test_desktop_lifecycle import graph, task

OWNER_TURN = "turn-of-the-owner"
RESET_AT = 1_800_000_000


class _Clock:
    """A clock that moves only while the alarm sleeps."""

    def __init__(self, start: int) -> None:
        self.now = float(start)
        self.naps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.naps.append(seconds)
        self.now += seconds


class WakeTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        raw = graph(max_workers=1)
        raw["tasks"] = [task("A", path="src/a"), task("B", path="src/b")]
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
        self.spawned: list[dict] = []

    # --- tools ---------------------------------------------------------

    def pending_token(self, task_id: str) -> str:
        for item in self.store.load().worker_sessions:
            if item.get("task_id") == task_id and item.get("status") in PENDING_SESSION_STATUSES:
                return str(item["reservation_token"])
        raise AssertionError(f"{task_id} не зарезервирована")

    def hit_the_limit(self, *, reset_at: int = RESET_AT) -> int:
        """A task hits the limit and goes off to wait for a retry.
        Returns the due time."""

        reserve_ready_frontier(self.cfg, now_epoch=reset_at - 10_000)
        record_desktop_failure(
            self.cfg,
            self.pending_token("A"),
            reason="лимит окна исчерпан",
            failure_code="app_server_rpc_failed",
            definitive=True,
            rate_limited=True,
            reset_at=reset_at,
            now_epoch=reset_at - 10_000,
            reserve_other_ready=False,
        )
        state = self.store.load()
        self.assertEqual(state.task_states["A"], "RETRY_WAIT")
        due = due_wake_epoch(state)
        self.assertIsNotNone(due)
        return int(due)

    def fake_spawn_relay(self, root, **kwargs) -> int:
        self.spawned.append(kwargs)
        return 4242

    def events(self) -> list[str]:
        return [item["event"] for item in self.store.load().resilience_journal]

    def wake(self, *, at_epoch: int, clock: _Clock) -> int:
        return run_wake(
            self.cfg,
            at_epoch=at_epoch,
            owner=TEST_RELAY_OWNER,
            owner_turn=OWNER_TURN,
            now=clock,
            sleep=clock.sleep,
            spawn_relay=self.fake_spawn_relay,
        )

    def record_completed_owner_turn(self) -> None:
        """The causal owner recorded a completed turn, as in a live run."""

        # The record shape is the one _validate_state requires: a session and
        # a journal entry with every mandatory field, otherwise the state
        # is not saved at all. That is exactly the trace a real
        # completed turn leaves.
        state = self.store.load()
        state.worker_sessions.append(
            {
                "task_id": "A",
                "kind": "worker",
                "thread_id": TEST_RELAY_OWNER,
                "turn_id": OWNER_TURN,
                "relay_owner_thread_id": TEST_RELAY_OWNER,
                "status": "COMPLETED",
                "reservation_token": "owner-reservation",
                "operation_id": "owner-operation",
                "client_user_message_id": "owner-message",
                "created_at": "2026-09-17T00:00:00+00:00",
                "attempt": 1,
            }
        )
        state.lifecycle_journal_sequence += 1
        state.lifecycle_journal.append(
            {
                "sequence": state.lifecycle_journal_sequence,
                "event": "turn_completed",
                "task_id": "A",
                "attempt": 1,
                "reservation_token": "owner-reservation",
                "operation_id": "owner-operation",
                "thread_id": TEST_RELAY_OWNER,
                "turn_id": OWNER_TURN,
                "at": "2026-09-17T00:00:00+00:00",
            }
        )
        self.store.save(state)

    def record_interrupted_owner_turn(self) -> None:
        """The owner's turn ended on an interrupt - it ended all the same.

        No turn_completed will ever come for an interrupted turn, so waiting
        for one means waiting forever. The dispatcher has accepted that proof
        for a long time (control._turn_is_completed); the alarm kept a
        narrower copy of the same question.
        """

        state = self.store.load()
        state.worker_sessions.append(
            {
                "task_id": "A",
                "kind": "worker",
                "thread_id": TEST_RELAY_OWNER,
                "turn_id": OWNER_TURN,
                "relay_owner_thread_id": TEST_RELAY_OWNER,
                "status": "INTERRUPTED",
                "reservation_token": "owner-reservation",
                "operation_id": "owner-operation",
                "client_user_message_id": "owner-message",
                "created_at": "2026-09-17T00:00:00+00:00",
                "attempt": 1,
            }
        )
        state.lifecycle_journal_sequence += 1
        state.lifecycle_journal.append(
            {
                "sequence": state.lifecycle_journal_sequence,
                "event": "interrupt_observed",
                "task_id": "A",
                "attempt": 1,
                "reservation_token": "owner-reservation",
                "operation_id": "owner-operation",
                "thread_id": TEST_RELAY_OWNER,
                "turn_id": OWNER_TURN,
                "at": "2026-09-17T00:00:00+00:00",
            }
        )
        self.store.save(state)

    # --- what the alarm does -------------------------------------------

    def test_the_wake_sleeps_until_the_retry_and_then_dispatches(self) -> None:
        due = self.hit_the_limit()
        clock = _Clock(due - 700)
        self.wake(at_epoch=due, clock=clock)
        self.assertGreaterEqual(clock.now, due, "woke up before the due time")
        self.assertEqual(len(self.spawned), 1)
        launch = self.spawned[0]
        self.assertEqual(launch["initiator_thread_id"], TEST_RELAY_OWNER)
        self.assertEqual(launch["initiator_turn_id"], OWNER_TURN)
        state = self.store.load()
        session = next(
            item
            for item in state.worker_sessions
            if item.get("reservation_token") == launch["reservation_token"]
        )
        self.assertEqual(session["task_id"], "A")
        self.assertIn("wake_dispatched", self.events())
        self.assertIsNone(state.wake_pid)
        self.assertIsNone(state.wake_at)

    def test_naps_are_bounded_so_a_moved_deadline_is_noticed(self) -> None:
        """Sleeping in one piece is not allowed: the deadline could have
        been extended in that time."""

        due = self.hit_the_limit()
        clock = _Clock(due - 1_000)
        self.wake(at_epoch=due, clock=clock)
        self.assertTrue(clock.naps, "the alarm did not sleep at all")
        self.assertLessEqual(max(clock.naps), 300)

    def test_a_later_rate_limit_delays_the_wake(self) -> None:
        """The alarm was set for a due time and then the limit was
        extended: it must not fire early."""

        due = self.hit_the_limit(reset_at=RESET_AT + 5_000)
        # The alarm thought the time was earlier than the state says.
        clock = _Clock(RESET_AT - 100)
        self.wake(at_epoch=RESET_AT, clock=clock)
        self.assertGreaterEqual(clock.now, due)
        self.assertEqual(len(self.spawned), 1)

    # --- what the alarm does not do ------------------------------------

    def test_a_paused_run_is_left_alone(self) -> None:
        due = self.hit_the_limit()
        self.store.request_pause()
        clock = _Clock(due + 1)
        self.wake(at_epoch=due, clock=clock)
        self.assertEqual(self.spawned, [])
        self.assertIn("wake_skipped", self.events())

    def test_a_live_dispatcher_is_not_raced(self) -> None:
        due = self.hit_the_limit()
        state = self.store.load()
        # Someone else's dispatcher is alive: its pid is our own process.
        state.dispatcher_pid = os.getpid()
        self.store.save(state)
        clock = _Clock(due + 1)
        self.wake(at_epoch=due, clock=clock)
        self.assertEqual(self.spawned, [])
        self.assertIn("wake_skipped", self.events())

    def test_nothing_waiting_means_nothing_to_wake(self) -> None:
        clock = _Clock(RESET_AT)
        self.wake(at_epoch=RESET_AT, clock=clock)
        self.assertEqual(self.spawned, [])
        self.assertEqual(clock.naps, [])

    # --- how the alarm is armed ----------------------------------------

    def test_ensure_wake_spawns_once_and_reuses_a_live_sleeper(self) -> None:
        due = self.hit_the_limit()
        calls: list[dict] = []

        def fake_spawn(cfg, **kwargs) -> int:
            calls.append(kwargs)
            return os.getpid()  # a live process: our own

        first = ensure_wake(
            self.cfg, owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN, spawn=fake_spawn
        )
        second = ensure_wake(
            self.cfg, owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN, spawn=fake_spawn
        )
        self.assertEqual(first, os.getpid())
        self.assertEqual(
            second, first, "a second alarm beside a live one is not needed"
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["at_epoch"], due)
        state = self.store.load()
        self.assertEqual(state.wake_pid, os.getpid())
        self.assertEqual(state.wake_at, due)

    def test_ensure_wake_does_nothing_when_nobody_waits(self) -> None:
        calls: list[dict] = []
        result = ensure_wake(
            self.cfg,
            owner=TEST_RELAY_OWNER,
            owner_turn=OWNER_TURN,
            spawn=lambda cfg, **kw: calls.append(kw) or 1,
        )
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_the_spawned_command_is_one_the_cli_parser_accepts(self) -> None:
        """The real launch is executed nowhere - so it was never checked.

        The mutation check broke the name of a flag and everything stayed
        green. Now the arguments the alarm is spawned with are run through
        the real CLI parser, and every one of them has to reach the
        handler.
        """

        from codex_autopilot.cli import parser

        command = wake_command(self.cfg, owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN, at_epoch=RESET_AT)
        self.assertEqual(command[1:4], ["-m", "codex_autopilot.cli", "_wake"])
        args = parser().parse_args(command[3:])
        self.assertEqual(args.command, "_wake")
        self.assertEqual(args.project, self.cfg.root)
        self.assertEqual(args.at, RESET_AT)
        self.assertEqual(args.owner, TEST_RELAY_OWNER)
        self.assertEqual(args.owner_turn, OWNER_TURN)

    def test_a_wake_that_fails_to_schedule_leaves_a_trace(self) -> None:
        """The hook does not fall over, and it does not go quiet either:
        a trace in the log, not silence."""

        from unittest import mock

        with mock.patch("codex_autopilot.wake.ensure_wake", side_effect=RuntimeError("no fork for you")):
            control._ensure_wake_from_hook(self.cfg, owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN)
        trace = (self.cfg.state_dir / "logs" / "wake-errors.log").read_text(encoding="utf-8")
        self.assertIn("no fork for you", trace)

    # --- what the reviewer found ---------------------------------------

    def test_revoked_hook_trust_stops_the_wake(self) -> None:
        """The alarm passes the same gate as a launch from the hook.

        A sleeping process does not carry the proof of trust with it. If
        the person revoked trust in the hook while the run slept, the
        retry is not raised - and that is written down as a reason, not
        swallowed.
        """

        from unittest import mock

        from codex_autopilot.hook_trust import HookPreflightError

        due = self.hit_the_limit()
        clock = _Clock(due + 1)
        with mock.patch(
            "codex_autopilot.lifecycle_reservations.require_trusted_stop_hook_for_config",
            side_effect=HookPreflightError("trust revoked"),
        ):
            self.wake(at_epoch=due, clock=clock)
        self.assertEqual(self.spawned, [])
        journal = self.store.load().resilience_journal
        skipped = [item for item in journal if item["event"] == "wake_skipped"]
        self.assertTrue(skipped)
        self.assertIn("hook trust", json.dumps(skipped[-1], ensure_ascii=False))

    def test_the_final_record_waits_for_the_lock(self) -> None:
        """The record written after dispatch goes under the
        coordinator's lock."""

        import threading

        from codex_autopilot.resources import ResourceLockCoordinator
        from codex_autopilot.wake import _finish

        coordinator = ResourceLockCoordinator(self.store, self.cfg.root)
        released = threading.Event()
        holder_ready = threading.Event()

        def hold() -> None:
            with coordinator.transaction():
                holder_ready.set()
                released.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        holder_ready.wait(5)
        finished = threading.Event()

        def finish() -> None:
            _finish(self.cfg, self.store, "wake_skipped", detail={"why": "test"})
            finished.set()

        writer = threading.Thread(target=finish)
        writer.start()
        self.assertFalse(
            finished.wait(0.5),
            "the record went through while someone else held the lock",
        )
        released.set()
        holder.join(5)
        self.assertTrue(
            finished.wait(5),
            "the record did not wait for the lock to be released",
        )
        writer.join(5)


class SurvivesARebootTests(WakeTests):
    """The sweep agent does what the alarm does, but after a reboot.

    A sleeping process dies together with the machine. Once every five
    minutes the agent walks the known projects and arms an alarm wherever
    a timed retry waits. It takes the owner from the journal - as the
    dispatcher does for its own successors - and not from arguments,
    which do not exist after a reboot.
    """

    def test_the_owner_is_derived_from_the_journal(self) -> None:
        self.assertIsNone(
            derive_owner(self.store.load()),
            "without a completed turn there is no owner",
        )
        self.record_completed_owner_turn()
        self.assertEqual(derive_owner(self.store.load()), (TEST_RELAY_OWNER, OWNER_TURN))

    def test_the_owner_of_an_interrupted_turn_is_still_an_owner(self) -> None:
        """One question, one answer - the alarm asks the dispatcher's predicate.

        derive_owner accepted only a turn_completed event. The dispatcher
        accepts three proofs that a turn ended, an interrupt among them -
        and an interrupted turn is exactly the case that leaves nobody to
        raise the successor. So a run whose owner was interrupted had no
        owner for the alarm: after a reboot the sweep skipped the project
        quietly and the retry waited for a human, which is the one thing the
        alarm exists to prevent.
        """

        self.record_interrupted_owner_turn()
        self.assertEqual(derive_owner(self.store.load()), (TEST_RELAY_OWNER, OWNER_TURN))

    def test_a_sweep_arms_a_wake_where_a_retry_waits(self) -> None:
        self.hit_the_limit()
        self.record_completed_owner_turn()
        calls: list[dict] = []
        outcome = sweep(
            roots=[str(self.root)],
            spawn=lambda cfg, **kw: calls.append(kw) or os.getpid(),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["owner"], TEST_RELAY_OWNER)
        self.assertEqual(calls[0]["owner_turn"], OWNER_TURN)
        self.assertTrue(outcome[str(self.root)].startswith("wake "))

    def test_a_sweep_leaves_alone_what_needs_no_wake(self) -> None:
        calls: list[dict] = []
        spawn = lambda cfg, **kw: calls.append(kw) or 1  # noqa: E731
        gone = str(self.root / "no-such-project")
        outcome = sweep(roots=[str(self.root), gone], spawn=spawn)
        self.assertEqual(outcome[str(self.root)], "nothing due")
        self.assertEqual(outcome[gone], "gone")
        self.hit_the_limit()
        self.store.request_pause()
        self.assertEqual(sweep(roots=[str(self.root)], spawn=spawn)[str(self.root)], "stopped")
        self.assertEqual(calls, [])

    def test_a_retry_without_a_completed_owner_is_not_woken_on_anyones_behalf(self) -> None:
        self.hit_the_limit()
        calls: list[dict] = []
        outcome = sweep(roots=[str(self.root)], spawn=lambda cfg, **kw: calls.append(kw) or 1)
        self.assertEqual(outcome[str(self.root)], "no completed owner")
        self.assertEqual(calls, [])

    def test_projects_are_registered_once_and_survive_rereading(self) -> None:
        registry = self.root / "projects.json"
        register_project(self.root, path=registry)
        register_project(self.root, path=registry)
        register_project(self.root / "other", path=registry)
        self.assertEqual(
            registered_projects(path=registry),
            sorted({str(self.root), str((self.root / "other").resolve())}),
        )


class TheLastProcessLeavesAWakeTests(WakeTests):
    """Whoever leaves last arms the alarm - and that is executed.

    The last two live processes of a run are the dispatcher and the Stop
    hook. The first edition of these tests read the source and looked for
    a substring: the mutation check wrapped all three calls in
    ``if False:`` and everything stayed green. Now both paths are
    executed, and the alarm is patched and counts the calls.
    """

    def test_the_dispatcher_arms_a_wake_when_it_exits_with_no_successor(self) -> None:
        from unittest import mock

        from codex_autopilot import cli

        class ClosedClient:
            def __init__(inner, binary, log_path, **kwargs):
                inner.closed = False
                inner.proc = mock.Mock()
                inner.proc.poll.side_effect = lambda: 0 if inner.closed else None

            def __enter__(inner):
                return inner

            def __exit__(inner, *_args):
                inner.closed = True

        with mock.patch("codex_autopilot.cli.AppServerClient", ClosedClient), mock.patch(
            "codex_autopilot.cli.run_automatic_app_server_turn",
            return_value=mock.Mock(descriptors=()),
        ), mock.patch("codex_autopilot.cli.record_automatic_app_server_exit"), mock.patch(
            "codex_autopilot.cli._ensure_wake"
        ) as ensure:
            result = cli._run_automatic_relay_dispatch(
                self.cfg, token="token-a", owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN
            )
        self.assertEqual(result, 0)
        ensure.assert_called_once_with(self.cfg, owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN)

    def test_the_dispatcher_arms_a_wake_after_fanning_out_successors(self) -> None:
        """The second exit from the loop - several successors - arms the
        alarm too."""

        from unittest import mock

        from codex_autopilot import cli

        class ClosedClient:
            def __init__(inner, binary, log_path, **kwargs):
                inner.closed = False
                inner.proc = mock.Mock()
                inner.proc.poll.side_effect = lambda: 0 if inner.closed else None

            def __enter__(inner):
                return inner

            def __exit__(inner, *_args):
                inner.closed = True

        self.record_completed_owner_turn()
        # Two successor reservations whose owner is a completed turn.
        state = self.store.load()
        for token, task_id in (("succ-1", "A"), ("succ-2", "B")):
            state.worker_sessions.append(
                {
                    "task_id": task_id,
                    "kind": "worker",
                    "status": "CREATE_REQUESTED",
                    "reservation_token": token,
                    "operation_id": f"op-{token}",
                    "client_user_message_id": f"msg-{token}",
                    "relay_owner_thread_id": TEST_RELAY_OWNER,
                    "created_at": "2026-09-17T00:00:00+00:00",
                    "attempt": 1,
                }
            )
        self.store.save(state)
        successors = (mock.Mock(reservation_token="succ-1"), mock.Mock(reservation_token="succ-2"))
        with mock.patch("codex_autopilot.cli.AppServerClient", ClosedClient), mock.patch(
            "codex_autopilot.cli.run_automatic_app_server_turn",
            return_value=mock.Mock(descriptors=successors),
        ), mock.patch("codex_autopilot.cli.record_automatic_app_server_exit"), mock.patch(
            "codex_autopilot.cli.spawn_automatic_app_server_relay", return_value=1
        ), mock.patch("codex_autopilot.cli._ensure_wake") as ensure:
            result = cli._run_automatic_relay_dispatch(
                self.cfg, token="token-a", owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN
            )
        self.assertEqual(result, 0)
        ensure.assert_called_once()

    def test_the_stop_hook_arms_a_wake_when_no_successor_follows(self) -> None:
        from unittest import mock

        with mock.patch(
            "codex_autopilot.control.complete_desktop_worker",
            return_value=mock.Mock(matched=True, descriptors=()),
        ), mock.patch("codex_autopilot.control._ensure_wake_from_hook") as ensure:
            result = control.handle_stop_hook(
                {
                    "hook_event_name": "Stop",
                    "cwd": str(self.root),
                    "session_id": TEST_RELAY_OWNER,
                    "turn_id": OWNER_TURN,
                    "last_assistant_message": "AUTOPILOT_STATUS: ROTATE",
                }
            )
        self.assertEqual(result, {})
        ensure.assert_called_once_with(self.cfg, owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN)


if __name__ == "__main__":
    unittest.main()
