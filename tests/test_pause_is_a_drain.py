"""Пауза доигрывает ход, а не убивает его.

`pause_desktop_run` объявляет дренаж: в журнал ложится
`semantics: drain`, статус показывает `Pause: drain`, и обещано, что
идущие ходы Desktop продолжаются и сохраняют свои блокировки.

Реализация обещание нарушала: ожидание хода, увидев паузу, слало
`turn/interrupt`. Нажатие паузы за мгновение до конца шестиминутной
приёмки убивало её целиком - задача уходила в RETRY_WAIT, попытка
терялась, поверх открывался инцидент. Пользователь при этом делал ровно
то, что написано в интерфейсе.

Тест держит настоящий `wait_for_turn`, а не копию его логики.
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
        # Транспорт ограничен: иначе ожидание крутилось бы вечно, ведь
        # срок жизни запроса живёт внутри настоящего `_get`.
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
