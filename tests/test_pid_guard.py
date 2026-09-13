"""Битый dispatcher_pid - отказ, а не догадка в любую сторону.

Обе проверки живости диспетчера охранные: они не дают работать, пока он
жив. Поэтому ошибка дорога в обе стороны.

Отрицательный pid уходил в os.kill(-N, 0) - сигнал группе процессов.
Посторонний живой процесс в группе давал "диспетчер жив", и прогон вставал
навсегда, ровно как вставали прогоны этой ночи. Нецелое значение роняло
TypeError, который в этой функции не ловится.

Считать испорченное значение мёртвым тоже нельзя: тогда поверх живого
диспетчера поднялся бы второй.
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
