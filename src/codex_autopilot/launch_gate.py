"""Гейт запуска: подтвердить, что задача поднялась, а не сообщить об этом.

Отказ, ради которого это написано, выглядел так: сессия отвечала
"диспетчер запущен, pid такой-то", завершалась, и ничего не происходило.
Сообщение было заявлением о намерении, а не наблюдением результата:
`wait_for_dispatcher` дожидается только того, что процесс диспетчера
дошёл до какой-то фазы, и ничего не говорит о самой задаче.

Здесь проверяется цепочка целиком и по записям прогона, без обращения к
App Server: резервирование, привязанная ветка, создание в проекте,
подтверждённая отправка, отчёт о видимом запуске, живой диспетчер и
отсутствие отказа уже после запуска.

Проверка, для которой нет данных, помечается как непроверенная и НЕ
считается пройденной. "Не удалось подтвердить" и "подтверждено" - разные
вещи, и подмена второго первым и есть тот самый дефект.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import Config
from .run_state import RunState, StateStore

# События, которые пишет только фактически прошедший шаг живого пути.
CREATED_EVENTS = frozenset(
    {
        "app_server_thread_created",
        "app_server_project_scoped_create",
        "create_acknowledged",
    }
)
ACKNOWLEDGED_EVENTS = frozenset({"start_acknowledged", "wait_registered"})
VISIBLE_EVENTS = frozenset({"visible_launch_report_ready"})
FAILURE_EVENTS = frozenset(
    {
        "create_failed",
        "start_failed",
        "prep_failed",
        "interrupt_observed",
        "retry_scheduled",
        "slot_history_rejected",
    }
)

ACTIVE_STATUS = "ACTIVE"

__all__ = [
    "LaunchCheck",
    "await_launch",
    "launch_checklist",
    "launch_confirmed",
    "render_launch_checklist",
]


@dataclass(frozen=True, slots=True)
class LaunchCheck:
    """Один пункт чек-листа.

    ``passed=None`` означает, что проверить было нечем. Это не успех.
    """

    id: str
    task_id: str
    passed: bool | None
    detail: str

    @property
    def mark(self) -> str:
        if self.passed is True:
            return "+"
        if self.passed is False:
            return "-"
        return "?"


def launch_checklist(
    cfg: Config,
    state: RunState,
    *,
    task_ids: Sequence[str],
    pid_alive: Callable[[Any], bool] | None = None,
) -> tuple[LaunchCheck, ...]:
    """Проверить по записям прогона, что названные задачи действительно подняты."""

    alive = pid_alive or _pid_alive
    checks: list[LaunchCheck] = []
    for task_id in task_ids:
        session = _latest_session(state, task_id)
        if session is None:
            checks.append(
                LaunchCheck(
                    "reserved",
                    task_id,
                    False,
                    "резервирование не найдено: задача не бралась в работу",
                )
            )
            continue
        checks.append(LaunchCheck("reserved", task_id, True, "резервирование есть"))

        thread_id = str(session.get("thread_id") or "")
        checks.append(
            LaunchCheck(
                "thread_bound",
                task_id,
                bool(thread_id),
                f"ветка {thread_id}" if thread_id else "ветка не привязана",
            )
        )

        token = str(session.get("reservation_token") or "")
        events = _events_for(state, token)
        checks.append(
            _event_check(
                "created_in_project", task_id, events, CREATED_EVENTS,
                ok="ветка создана через App Server в проекте прогона",
                bad="нет записи о создании ветки",
            )
        )
        checks.append(
            LaunchCheck(
                "send_acknowledged",
                task_id,
                session.get("status") == ACTIVE_STATUS,
                f"статус сессии {session.get('status')!r}"
                + ("" if session.get("status") == ACTIVE_STATUS else "; отправка не подтверждена"),
            )
        )
        checks.append(
            _event_check(
                "visible_launch", task_id, events, VISIBLE_EVENTS,
                ok="отчёт о видимом запуске записан",
                bad="отчёта о видимом запуске нет",
            )
        )

        pid = session.get("automatic_dispatch_pid")
        if pid is None:
            pid = state.dispatcher_pid
        checks.append(
            LaunchCheck(
                "dispatcher_alive",
                task_id,
                alive(pid) if pid is not None else None,
                f"диспетчер pid {pid}" if pid is not None else "pid диспетчера не записан",
            )
        )

        failure = _failure_after_launch(events)
        checks.append(
            LaunchCheck(
                "no_failure_after_launch",
                task_id,
                failure is None,
                "отказов после запуска нет"
                if failure is None
                else f"после запуска записан отказ: {failure}",
            )
        )
    return tuple(checks)


def launch_confirmed(checks: Iterable[LaunchCheck]) -> bool:
    """Запуск подтверждён, только если каждый пункт прошёл.

    Непроверенный пункт подтверждением не является.
    """

    items = list(checks)
    return bool(items) and all(item.passed is True for item in items)


def render_launch_checklist(checks: Sequence[LaunchCheck]) -> str:
    if not checks:
        return "Чек-лист запуска: проверять нечего — ни одна задача не названа."
    lines: list[str] = []
    for task_id in dict.fromkeys(item.task_id for item in checks):
        lines.append(f"{task_id}:")
        for item in checks:
            if item.task_id == task_id:
                lines.append(f"  [{item.mark}] {item.id}: {item.detail}")
    verdict = (
        "ЗАПУСК ПОДТВЕРЖДЁН"
        if launch_confirmed(checks)
        else "ЗАПУСК НЕ ПОДТВЕРЖДЁН — это отказ запуска, а не успех"
    )
    return verdict + "\n" + "\n".join(lines)


def await_launch(
    cfg: Config,
    *,
    task_ids: Sequence[str],
    timeout: float = 20.0,
    interval: float = 0.25,
    pid_alive: Callable[[Any], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> tuple[LaunchCheck, ...]:
    """Дождаться подтверждения запуска или срока, и вернуть чек-лист как есть.

    Ограничение по времени обязательно: хук живёт 30 секунд, и гейт не
    вправе висеть дольше. Истёкший срок - это отрицательный результат,
    который возвращается честным чек-листом, а не исключением.
    """

    rest = sleep or time.sleep
    now = monotonic or time.monotonic
    store = StateStore(cfg.state_dir)
    deadline = now() + timeout
    checks = launch_checklist(cfg, store.load(), task_ids=task_ids, pid_alive=pid_alive)
    while not launch_confirmed(checks) and now() < deadline:
        rest(interval)
        checks = launch_checklist(
            cfg, store.load(), task_ids=task_ids, pid_alive=pid_alive
        )
    return checks


def _latest_session(state: RunState, task_id: str) -> Mapping[str, Any] | None:
    matches = [
        item for item in state.worker_sessions if str(item.get("task_id") or "") == task_id
    ]
    return matches[-1] if matches else None


def _events_for(state: RunState, token: str) -> list[Mapping[str, Any]]:
    if not token:
        return []
    return sorted(
        (
            item
            for item in state.lifecycle_journal
            if str(item.get("reservation_token") or "") == token
        ),
        key=lambda item: int(item.get("sequence") or 0),
    )


def _event_check(
    check_id: str,
    task_id: str,
    events: Sequence[Mapping[str, Any]],
    wanted: frozenset[str],
    *,
    ok: str,
    bad: str,
) -> LaunchCheck:
    found = next((item for item in events if str(item.get("event") or "") in wanted), None)
    if found is None:
        return LaunchCheck(check_id, task_id, False, bad)
    return LaunchCheck(check_id, task_id, True, f"{ok} (#{found.get('sequence')})")


def _failure_after_launch(events: Sequence[Mapping[str, Any]]) -> str | None:
    """Отказ, записанный уже после того, как ветка была создана."""

    launched_at = next(
        (
            int(item.get("sequence") or 0)
            for item in events
            if str(item.get("event") or "") in CREATED_EVENTS
        ),
        None,
    )
    if launched_at is None:
        return None
    for item in events:
        if int(item.get("sequence") or 0) <= launched_at:
            continue
        name = str(item.get("event") or "")
        if name in FAILURE_EVENTS:
            return f"{name} (#{item.get('sequence')})"
    return None


def _pid_alive(pid: Any) -> bool:
    from .control import pid_alive as control_pid_alive

    return control_pid_alive(pid)
