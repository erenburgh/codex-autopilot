"""Повтор по сроку поднимается сам, а не по слову человека.

Замерено на прогоне v1.0: задача упиралась в лимит, рантайм записывал
срок повтора, последний диспетчер выходил - и живого процесса не
оставалось. Прогон стоял, пока хозяйка не писала "Resume", каждые
несколько часов, ради действия, которое рантайм умел сам.

Будильник - отложенный преемник диспетчера. Проверяется и то, что он
делает, и то, чего не делает: не будит остановленный человеком прогон,
не толкается с живым диспетчером, не стреляет раньше продлённого лимита.
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
)
from test_desktop_lifecycle import graph, task

OWNER_TURN = "turn-of-the-owner"
RESET_AT = 1_800_000_000


class _Clock:
    """Часы, которые идут только когда будильник спит."""

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
        raw["tasks"] = [task("A", path="src/a")]
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

    # --- инструменты ---------------------------------------------------

    def pending_token(self, task_id: str) -> str:
        for item in self.store.load().worker_sessions:
            if item.get("task_id") == task_id and item.get("status") in PENDING_SESSION_STATUSES:
                return str(item["reservation_token"])
        raise AssertionError(f"{task_id} не зарезервирована")

    def hit_the_limit(self, *, reset_at: int = RESET_AT) -> int:
        """Задача упирается в лимит и уходит ждать повтора. Возвращает срок."""

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

    # --- что будильник делает ------------------------------------------

    def test_the_wake_sleeps_until_the_retry_and_then_dispatches(self) -> None:
        due = self.hit_the_limit()
        clock = _Clock(due - 700)
        self.wake(at_epoch=due, clock=clock)
        self.assertGreaterEqual(clock.now, due, "проснулся раньше срока")
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
        """Спать одним куском нельзя: за это время срок могли продлить."""

        due = self.hit_the_limit()
        clock = _Clock(due - 1_000)
        self.wake(at_epoch=due, clock=clock)
        self.assertTrue(clock.naps, "будильник не спал вовсе")
        self.assertLessEqual(max(clock.naps), 300)

    def test_a_later_rate_limit_delays_the_wake(self) -> None:
        """Будильник заведён на срок, а лимит продлили: стрелять рано нельзя."""

        due = self.hit_the_limit(reset_at=RESET_AT + 5_000)
        # Будильник думал, что срок раньше, чем говорит состояние.
        clock = _Clock(RESET_AT - 100)
        self.wake(at_epoch=RESET_AT, clock=clock)
        self.assertGreaterEqual(clock.now, due)
        self.assertEqual(len(self.spawned), 1)

    # --- чего будильник не делает --------------------------------------

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
        # Чужой диспетчер жив: его pid - наш собственный процесс.
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

    # --- как будильник заводится ---------------------------------------

    def test_ensure_wake_spawns_once_and_reuses_a_live_sleeper(self) -> None:
        due = self.hit_the_limit()
        calls: list[dict] = []

        def fake_spawn(cfg, **kwargs) -> int:
            calls.append(kwargs)
            return os.getpid()  # живой процесс: наш собственный

        first = ensure_wake(
            self.cfg, owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN, spawn=fake_spawn
        )
        second = ensure_wake(
            self.cfg, owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN, spawn=fake_spawn
        )
        self.assertEqual(first, os.getpid())
        self.assertEqual(second, first, "второй будильник рядом с живым не нужен")
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


class SurvivesARebootTests(WakeTests):
    """Агент обхода делает то же, что будильник, но после перезагрузки.

    Спящий процесс умирает вместе с машиной. Агент раз в пять минут
    обходит известные проекты и заводит будильник там, где повтор по
    сроку ждёт. Владельца он берёт из журнала - как диспетчер для своих
    преемников, - а не из аргументов, которых после перезагрузки нет.
    """

    def record_completed_owner_turn(self) -> None:
        """Причинный владелец записал завершённый ход - как в живом прогоне."""

        # Форма записи - та, которую требует _validate_state: сессия и
        # запись журнала со всеми обязательными полями, иначе состояние
        # не сохранится вовсе. Это и есть след, который оставляет
        # настоящий завершённый ход.
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

    def test_the_owner_is_derived_from_the_journal(self) -> None:
        self.assertIsNone(derive_owner(self.store.load()), "без завершённого хода владельца нет")
        self.record_completed_owner_turn()
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


class TheLastProcessLeavesAWakeTests(unittest.TestCase):
    """Кто уходит последним, тот заводит будильник.

    Два последних живых процесса прогона - диспетчер и Stop-хук. Если
    любой из них уйдёт молча, повтор по сроку снова будет ждать человека.
    """

    def test_the_dispatcher_schedules_a_wake_on_every_exit(self) -> None:
        source = inspect.getsource(cli._automatic_relay_loop)
        self.assertGreaterEqual(
            source.count("_ensure_wake("),
            2,
            "у цикла диспетчера два выхода, и на обоих нужен будильник",
        )

    def test_the_stop_hook_schedules_a_wake_when_no_successor_follows(self) -> None:
        source = inspect.getsource(control.handle_stop_hook)
        self.assertIn("_ensure_wake_from_hook(", source)


if __name__ == "__main__":
    unittest.main()
