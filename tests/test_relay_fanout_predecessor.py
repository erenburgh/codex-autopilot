"""B8: "the owner's turn is over" has one predicate, not two.

The audit found three implementations of "the turn is over", two of which
diverged. Measured on 18 Sep by going through all five candidates: three
of them answer different questions (the launch gate, retry forensics, the
R1 audit), and the differences there are deliberate. The real drift is in
one place: the fan-out branch of ``cli._automatic_relay_loop`` (several
successors) selected the predecessor inline by ``status == "COMPLETED"``,
while the single branch of the same function and the Stop hook go through
``causal_predecessor`` -> ``_turn_is_completed``, which also accepts a
turn completed with BLOCKED/ESCALATE/PLAN_CHANGE_REQUESTED, and an
interrupted one.

The scenario is reachable: on BLOCKED ``complete_desktop_worker`` keeps
``status = worker_status`` (lifecycle_completion.py) and writes
``turn_completed`` in the same transaction. The implication
COMPLETED => turn_completed holds, the converse does not by construction.
If such an owner opened two successors, the fan-out failed with a bare
``StopIteration`` - without a word about the reason.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from codex_autopilot import cli
from codex_autopilot.run_state import StateStore


def _session(token: str, task_id: str, **extra) -> dict:
    base = {
        "reservation_token": token,
        "operation_id": f"op-{token}",
        "client_user_message_id": f"client-{token}",
        "task_id": task_id,
        "kind": "implementation",
        "attempt": 1,
        "status": "CREATE_REQUESTED",
        "created_at": "2026-09-18T00:00:00Z",
        "thread_id": None,
        "turn_id": None,
        "relay_owner_thread_id": "owner",
    }
    base.update(extra)
    return base


class FakeClient:
    def __init__(self, binary, log_path, event_sink=None):
        self.proc = types.SimpleNamespace(poll=lambda: 0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class MultiSuccessorFanOutUsesTheSharedPredicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / ".codex-autopilot"
        self.cfg = types.SimpleNamespace(
            state_dir=self.state_dir,
            root=self.state_dir.parent,
            desktop=types.SimpleNamespace(binary="codex"),
        )
        self.store = StateStore(self.state_dir)
        self.descriptors = (
            types.SimpleNamespace(reservation_token="b-token", task_id="B"),
            types.SimpleNamespace(reservation_token="c-token", task_id="C"),
        )

    def save(self, *, owner_sessions: list[dict], journal: list[dict]) -> None:
        state = self.store.load()
        state.worker_sessions = [*owner_sessions, _session("b-token", "B"), _session("c-token", "C")]
        state.lifecycle_journal = journal
        state.lifecycle_journal_sequence = len(journal)
        self.store.save(state)

    def fan_out(self) -> list[dict]:
        """The real dispatcher loop; only transport and spawning are faked."""

        spawned: list[dict] = []
        with (
            mock.patch.object(cli, "AppServerClient", FakeClient),
            mock.patch.object(
                cli,
                "run_automatic_app_server_turn",
                lambda cfg, token, **kw: types.SimpleNamespace(descriptors=self.descriptors),
            ),
            mock.patch.object(cli, "_print_relay_timeline", lambda *a, **k: None),
            mock.patch.object(cli, "record_automatic_app_server_exit", lambda *a, **k: None),
            mock.patch.object(
                cli, "spawn_automatic_app_server_relay", lambda root, **kw: spawned.append(kw) or 1
            ),
        ):
            cli._automatic_relay_loop(self.cfg, token="owner-token", owner="human", owner_turn="turn-0")
        return spawned

    def test_two_successors_of_a_blocked_owner_are_spawned_from_its_finished_turn(self) -> None:
        """The owner ended the turn BLOCKED: the status is not COMPLETED,
        turn_completed is recorded."""

        self.save(
            owner_sessions=[
                _session(
                    "owner-token", "A",
                    thread_id="owner", turn_id="turn-1",
                    status="BLOCKED", final_status="BLOCKED",
                    relay_owner_thread_id="human",
                )
            ],
            journal=[
                {
                    "sequence": 1, "event": "turn_completed",
                    "operation_id": "op-owner-token", "task_id": "A", "attempt": 1,
                    "reservation_token": "owner-token", "thread_id": "owner",
                    "relay_owner_thread_id": "human", "turn_id": "turn-1",
                    "client_user_message_id": "client-owner-token",
                    "at": "2026-09-18T00:00:01Z", "detail": "BLOCKED",
                }
            ],
        )
        spawned = self.fan_out()
        self.assertEqual(
            [(i["reservation_token"], i["initiator_thread_id"], i["initiator_turn_id"]) for i in spawned],
            [("b-token", "owner", "turn-1"), ("c-token", "owner", "turn-1")],
        )

    def test_a_missing_predecessor_is_named_not_a_bare_stop_iteration(self) -> None:
        """R31: the refusal names what is missing - in the same word the
        Stop hook uses."""

        self.save(
            owner_sessions=[
                _session(
                    "owner-token", "A",
                    thread_id="owner", turn_id="turn-1",
                    status="ACTIVE", relay_owner_thread_id="human",
                )
            ],
            journal=[],
        )
        with self.assertRaisesRegex(RuntimeError, "no completed causal predecessor"):
            self.fan_out()


if __name__ == "__main__":
    unittest.main()
