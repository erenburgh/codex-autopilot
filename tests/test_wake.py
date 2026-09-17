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
    wake_command,
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

    def test_the_spawned_command_is_one_the_cli_parser_accepts(self) -> None:
        """Настоящий запуск нигде не исполняется - так его и не проверяли.

        Проверяющая сломала имя флага, и всё осталось зелёным. Теперь
        аргументы, с которыми будильник порождается, прогоняются через
        настоящий парсер CLI, и каждый обязан доехать до обработчика.
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
        """Хук не падает, но и не молчит: след в логе, а не тишина."""

        from unittest import mock

        with mock.patch("codex_autopilot.wake.ensure_wake", side_effect=RuntimeError("no fork for you")):
            control._ensure_wake_from_hook(self.cfg, owner=TEST_RELAY_OWNER, owner_turn=OWNER_TURN)
        trace = (self.cfg.state_dir / "logs" / "wake-errors.log").read_text(encoding="utf-8")
        self.assertIn("no fork for you", trace)

    # --- то, что нашла проверяющая --------------------------------------

    def test_revoked_hook_trust_stops_the_wake(self) -> None:
        """Будильник проходит тот же гейт, что и запуск от хука.

        Спящий процесс не несёт доказательства доверия с собой. Если
        человек отозвал доверие хуку, пока прогон спал, повтор не
        поднимается - и это записано как причина, а не проглочено.
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
        """Запись после диспетчеризации идёт под замком координатора."""

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
            finished.wait(0.5), "запись прошла, пока замок держал другой"
        )
        released.set()
        holder.join(5)
        self.assertTrue(finished.wait(5), "запись не дождалась освобождения замка")
        writer.join(5)


class SurvivesARebootTests(WakeTests):
    """Агент обхода делает то же, что будильник, но после перезагрузки.

    Спящий процесс умирает вместе с машиной. Агент раз в пять минут
    обходит известные проекты и заводит будильник там, где повтор по
    сроку ждёт. Владельца он берёт из журнала - как диспетчер для своих
    преемников, - а не из аргументов, которых после перезагрузки нет.
    """

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


class TheLastProcessLeavesAWakeTests(WakeTests):
    """Кто уходит последним, тот заводит будильник - и это исполняется.

    Два последних живых процесса прогона - диспетчер и Stop-хук. Первая
    редакция этих тестов читала исходник и искала подстроку: проверяющая
    обернула все три вызова в ``if False:`` и всё осталось зелёным.
    Теперь оба пути исполняются, а будильник подменён и считает вызовы.
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
        """Второй выход цикла - несколько преемников - тоже заводит будильник."""

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
        # Две резервации-преемника, у которых владелец - завершённый ход.
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
