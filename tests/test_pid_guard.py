"""A corrupt dispatcher_pid is a refusal, not a guess in either direction.

Both dispatcher liveness checks are guards: they prevent work while it
is alive. So an error is costly both ways.

A negative pid went into os.kill(-N, 0) - a signal to a process group. A
stray live process in the group gave "the dispatcher is alive", and the
run stood forever, exactly as that night's runs stood. A non-integer
value raised a TypeError this function does not catch.

Treating a corrupt value as dead is not allowed either: a second
dispatcher would then rise on top of a live one.
"""

from __future__ import annotations

import os
import unittest

from codex_autopilot.lifecycle_base import DesktopLifecycleError, _pid_alive


class AbsentPidTests(unittest.TestCase):
    def test_no_recorded_dispatcher_is_not_alive(self) -> None:
        self.assertFalse(_pid_alive(None))
        self.assertFalse(_pid_alive(0))


class LivePidTests(unittest.TestCase):
    def test_this_process_is_alive(self) -> None:
        self.assertTrue(_pid_alive(os.getpid()))

    def test_a_free_pid_is_not_alive(self) -> None:
        probe = 2**22
        while True:
            try:
                os.kill(probe, 0)
            except OSError:
                break
            probe += 1
        self.assertFalse(_pid_alive(probe))


class CorruptPidTests(unittest.TestCase):
    def test_a_negative_pid_is_refused_not_treated_as_a_process_group(self) -> None:
        with self.assertRaises(DesktopLifecycleError) as caught:
            _pid_alive(-os.getpid())
        self.assertIn("dispatcher_pid", str(caught.exception))

    def test_a_non_integer_pid_is_refused_not_a_TypeError(self) -> None:
        for value in ("1234", 3.5, [7]):
            with self.subTest(value=value):
                with self.assertRaises(DesktopLifecycleError):
                    _pid_alive(value)

    def test_a_boolean_is_refused(self) -> None:
        """True прошёл бы как pid 1: init жив всегда."""

        with self.assertRaises(DesktopLifecycleError):
            _pid_alive(True)

    def test_the_refusal_names_the_offending_value(self) -> None:
        with self.assertRaises(DesktopLifecycleError) as caught:
            _pid_alive("не число")
        self.assertIn("не число", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class OrphanedThreadIsLoadedBeforeTheTurnTests(unittest.TestCase):
    """Ход стартует только на ветке, загруженной ЭТИМ соединением.

    Условие прежде спрашивало "своё ли у нас соединение". Это другой
    вопрос: реле всегда передаёт готовый клиент, и ветка, созданная
    прежним - умершим - диспетчером, оставалась незагруженной.

    Замерено на живом прогоне M11: ветка реплэннера 01a0970c читается и
    резюмируется, а turn/start отвечает "thread not found". Воспроизведено
    на одноразовой ветке: создать, закрыть процесс-создатель, стартовать
    ход из нового соединения - тот же отказ. Ветка без единого хода вдобавок
    не заводит rollout, и thread/resume отвечает "no rollout found".
    """

    def test_the_condition_asks_whether_the_thread_is_loaded(self) -> None:
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_dispatch.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'if thread_id in getattr(production_client, "subscribed_thread_ids", ()):',
            source,
        )
        self.assertNotIn("if connected_client is None:\n                resumed", source)

    def test_a_thread_this_connection_created_is_not_resumed(self) -> None:
        """Лишний resume на своей ветке - лишний вызов, а не починка."""

        from codex_autopilot.appserver import AppServerClient

        client = AppServerClient.__new__(AppServerClient)
        client.subscribed_thread_ids = {"thread-a"}
        self.assertIn("thread-a", client.subscribed_thread_ids)

    def test_the_client_forgets_a_thread_it_unsubscribed(self) -> None:
        """После unsubscribe ветка снова требует загрузки."""

        from codex_autopilot.appserver import AppServerClient
        import inspect

        source = inspect.getsource(AppServerClient.unsubscribe_thread)
        self.assertIn("subscribed_thread_ids.discard", source)
