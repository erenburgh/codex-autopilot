"""The rate-limit snapshot must reach the state during the run.

The scheduler computes capacity from the last snapshot. While the
`account/rateLimits/updated` event was recorded nowhere, the scheduler
saw only what was known at start, and narrowing during the work never
kicked in.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import types
import unittest

from codex_autopilot import cli
from codex_autopilot.run_state import StateStore
from codex_autopilot.usage import worker_budget


SNAPSHOT = {
    "primary": {"usedPercent": 96, "windowDurationMins": 10080},
    "credits": {"hasCredits": False, "unlimited": False},
    "planType": "prolite",
}


class RateLimitCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = Path(tempfile.mkdtemp()) / ".codex-autopilot"
        self.cfg = types.SimpleNamespace(
            state_dir=self.state_dir,
            desktop=types.SimpleNamespace(binary="codex"),
        )

    def test_updated_event_reaches_run_state(self) -> None:
        cli._record_rate_limits(
            self.cfg, "account/rateLimits/updated", {"rateLimits": SNAPSHOT}
        )
        self.assertEqual(StateStore(self.state_dir).load().rate_limits, SNAPSHOT)

    def test_unrelated_events_are_ignored(self) -> None:
        cli._record_rate_limits(self.cfg, "thread/started", {"rateLimits": SNAPSHOT})
        self.assertIsNone(StateStore(self.state_dir).load().rate_limits)

    def test_snapshot_never_rewrites_the_run_journal(self) -> None:
        """Событие приходит из читающего потока, параллельно диспетчеру.

        Запись снимка не должна проходить через run-state.json: загрузка
        и сохранение целого журнала из чужого потока отменила бы переход
        сессии, сделанный в ту же миллисекунду.
        """

        store = StateStore(self.state_dir)
        state = store.load()
        state.status = "RUNNING"
        store.save(state)
        journal = (self.state_dir / "run-state.json").read_bytes()
        cli._record_rate_limits(
            self.cfg, "account/rateLimits/updated", {"rateLimits": SNAPSHOT}
        )
        self.assertEqual((self.state_dir / "run-state.json").read_bytes(), journal)
        self.assertEqual(store.load().rate_limits, SNAPSHOT)

    def test_captured_snapshot_narrows_the_worker_budget(self) -> None:
        """Ради этого всё и делается: ёмкость считается по свежим данным."""

        cli._record_rate_limits(
            self.cfg, "account/rateLimits/updated", {"rateLimits": SNAPSHOT}
        )
        limits = StateStore(self.state_dir).load().rate_limits
        self.assertEqual(worker_budget(10, limits).workers, 1)
        self.assertEqual(worker_budget(10, None).workers, 10)

    def test_dispatcher_client_is_built_with_the_capture(self) -> None:
        """Перехват должен стоять на живом клиенте диспетчера.

        Функция с тестами, но без вызова в продакшене - не реализация.
        """

        captured: dict[str, object] = {}

        class FakeClient:
            def __init__(self, binary, log_path, event_sink=None):
                captured["sink"] = event_sink
                self.proc = types.SimpleNamespace(poll=lambda: 0)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_turn(cfg, token, **kwargs):
            return types.SimpleNamespace(descriptors=())

        original_client = cli.AppServerClient
        original_turn = cli.run_automatic_app_server_turn
        original_timeline = cli._print_relay_timeline
        original_exit = cli.record_automatic_app_server_exit
        cli.AppServerClient = FakeClient
        cli.run_automatic_app_server_turn = fake_turn
        cli._print_relay_timeline = lambda *a, **k: None
        cli.record_automatic_app_server_exit = lambda *a, **k: None
        try:
            cli._automatic_relay_loop(
                self.cfg, token="t", owner="owner", owner_turn="turn"
            )
        finally:
            cli.AppServerClient = original_client
            cli.run_automatic_app_server_turn = original_turn
            cli._print_relay_timeline = original_timeline
            cli.record_automatic_app_server_exit = original_exit

        sink = captured.get("sink")
        self.assertIsNotNone(sink)
        sink("account/rateLimits/updated", {"rateLimits": SNAPSHOT})
        self.assertEqual(StateStore(self.state_dir).load().rate_limits, SNAPSHOT)


if __name__ == "__main__":
    unittest.main()
