"""A pause plays the turn out, it does not kill it.

`pause_desktop_run` declares a drain: `semantics: drain` goes into the
journal, status shows `Pause: drain`, and it is promised that running
Desktop turns continue and keep their locks.

The implementation broke the promise: the turn wait, on seeing the
pause, sent `turn/interrupt`. Pressing pause a moment before the end of
a six-minute acceptance killed it entirely - the task went to
RETRY_WAIT, the attempt was lost, an incident opened on top. The user
was doing exactly what the interface said.

The test holds the real `wait_for_turn`, not a copy of its logic.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.appserver import AppServerClient, PauseRequested


class _TransportExhausted(Exception):
    """Признак того, что ожидание продолжилось, а не оборвалось паузой."""


class _RecordingClient(AppServerClient):
    """Настоящий клиент без процесса: транспорт и запросы записываются."""

    def __init__(self, log_path: Path) -> None:
        super().__init__("codex", log_path)
        self.requests: list[str] = []
        self.polls = 0

    def request(self, method, params=None, *, timeout=None):  # type: ignore[override]
        self.requests.append(method)
        return {}

    def _turn_status(self, thread_id: str, turn_id: str) -> str:  # type: ignore[override]
        return "in_progress"

    def _get(self, deadline, maximum_wait=1):  # type: ignore[override]
        # The transport is bounded: otherwise the wait would spin forever, since
        # the request lifetime lives inside the real `_get`.
        self.polls += 1
        if self.polls > 2:
            raise _TransportExhausted
        raise TimeoutError


class PauseIsADrainTests(unittest.TestCase):
    def _client(self) -> _RecordingClient:
        self.addCleanup(self._directory.cleanup)
        return _RecordingClient(Path(self._directory.name) / "app-server.log")

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()

    def test_a_pause_never_interrupts_the_running_turn(self) -> None:
        client = self._client()
        with self.assertRaises(PauseRequested):
            client.wait_for_turn(
                "thread-1",
                "turn-1",
                timeout=30,
                pause_requested=lambda: True,
            )
        self.assertNotIn(
            "turn/interrupt",
            client.requests,
            "пауза объявлена дренажной и не имеет права прерывать идущий ход",
        )
        self.assertEqual(client.requests, [], "пауза не шлёт серверу ничего")

    def test_without_a_pause_the_wait_keeps_going(self) -> None:
        client = self._client()
        with self.assertRaises(_TransportExhausted):
            client.wait_for_turn(
                "thread-1",
                "turn-1",
                timeout=30,
                pause_requested=lambda: False,
            )
        self.assertGreater(client.polls, 1, "ожидание обязано продолжаться без паузы")
        self.assertNotIn("turn/interrupt", client.requests)


if __name__ == "__main__":
    unittest.main()
