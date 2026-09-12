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
from datetime import datetime, timezone
from enum import Enum
import json
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
REPORT_EVENTS = frozenset({"visible_launch_report_ready"})

# Где Desktop держит собственные записи об интерфейсе. Успех на стороне
# App Server их не меняет, поэтому видимость проверяется только здесь.
DESKTOP_UI_KEYS = ("thread-project-assignments", "sidebar-project-thread-orders")
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
    "ABSENT",
    "INSIDE",
    "OUTSIDE",
    "adopt_into_desktop_project",
    "desktop_placement",
    "promote_into_project",
    "LaunchCheck",
    "LaunchVerdict",
    "launch_verdict",
    "await_launch",
    "launch_checklist",
    "launch_confirmed",
    "render_launch_checklist",
]


class LaunchVerdict(str, Enum):
    """Три состояния запуска, а не два.

    Создание ветки через App Server занимает десятки секунд, а хук живёт
    тридцать. Пока шагов не хватает, но диспетчер жив и отказов не
    записано, это ИДЁТ, а не СЛОМАЛОСЬ. Смешение этих двух состояний
    превращает нормальный запуск в ложный тикет - тот самый шум, из-за
    которого проверки перестают читать.
    """

    CONFIRMED = "CONFIRMED"
    IN_PROGRESS = "IN_PROGRESS"
    FAILED = "FAILED"


# Пункты, недостижимость которых означает поломку, а не незавершённость.
DECISIVE_CHECKS = frozenset({"reserved", "dispatcher_alive", "no_failure_after_launch"})


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
                "launch_report_written", task_id, events, REPORT_EVENTS,
                ok="рантайм дошёл до отчёта о запуске",
                bad="рантайм до отчёта о запуске не дошёл",
            )
        )
        checks.append(_desktop_visibility(task_id, thread_id, events))

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


def launch_verdict(checks: Iterable[LaunchCheck]) -> LaunchVerdict:
    """Подтверждён, ещё идёт или отказ - по наличию признаков поломки."""

    items = list(checks)
    if not items:
        return LaunchVerdict.FAILED
    if all(item.passed is True for item in items):
        return LaunchVerdict.CONFIRMED
    broken = [
        item
        for item in items
        if item.id in DECISIVE_CHECKS and item.passed is not True
    ]
    return LaunchVerdict.FAILED if broken else LaunchVerdict.IN_PROGRESS


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
    verdict = {
        LaunchVerdict.CONFIRMED: "ЗАПУСК ПОДТВЕРЖДЁН",
        LaunchVerdict.IN_PROGRESS: (
            "ЗАПУСК ИДЁТ — диспетчер жив, отказов нет, часть шагов ещё впереди"
        ),
        LaunchVerdict.FAILED: "ЗАПУСК ОТКАЗАЛ — это отказ, а не успех",
    }[launch_verdict(checks)]
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
    while launch_verdict(checks) is LaunchVerdict.IN_PROGRESS and now() < deadline:
        rest(interval)
        checks = launch_checklist(
            cfg, store.load(), task_ids=task_ids, pid_alive=pid_alive
        )
    return checks


def _desktop_visibility(
    task_id: str, thread_id: str, events: Sequence[Mapping[str, Any]]
) -> LaunchCheck:
    """Видна ли ветка в интерфейсе Desktop.

    Проверяется по собственным записям Desktop, а не по успеху App Server:
    project/update и thread/metadata/update проходят в пространстве имён
    App Server, не меняя метаданных сайдбара, и принимать их успех за
    размещение в интерфейсе - ложное срабатывание. Ровно так задача
    "создавалась просто так" и оказывалась невидимой.

    Пункт намеренно не решающий: Desktop пишет своё состояние не мгновенно,
    и объявлять отказ по его задержке значило бы снова плодить ложные
    тикеты. Расхождение видно в чек-листе, но тикета не открывает.
    """

    from .preflight import default_codex_home

    if not thread_id:
        return LaunchCheck(
            "visible_in_desktop", task_id, None, "нечего искать: ветка не привязана"
        )
    path = default_codex_home().expanduser().resolve() / ".codex-global-state.json"
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        return LaunchCheck(
            "visible_in_desktop", task_id, None, f"состояние Desktop не прочитано: {error}"
        )
    for key in DESKTOP_UI_KEYS:
        if thread_id in json.dumps(payload.get(key), ensure_ascii=False):
            return LaunchCheck(
                "visible_in_desktop", task_id, True, f"ветка есть в записи {key}"
            )
    created = _created_at(events)
    written = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if created is not None and written < created:
        return LaunchCheck(
            "visible_in_desktop",
            task_id,
            None,
            "Desktop ещё не переписывал своё состояние после создания ветки",
        )
    return LaunchCheck(
        "visible_in_desktop",
        task_id,
        False,
        "ветки нет в записях интерфейса Desktop: в сайдбаре она не появится",
    )


def _created_at(events: Sequence[Mapping[str, Any]]):
    for item in events:
        if str(item.get("event") or "") in CREATED_EVENTS:
            try:
                return datetime.fromisoformat(str(item.get("at") or ""))
            except ValueError:
                return None
    return None


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


# Где ветка по мнению самого Desktop. Успех вызова App Server сюда не
# входит: project/update и thread/metadata/update проходят в пространстве
# имён App Server, не меняя метаданных сайдбара Electron.
ABSENT = "ABSENT"      # Desktop о ветке не знает - её не видно вообще
OUTSIDE = "OUTSIDE"    # видна, но вне проекта (Recents)
INSIDE = "INSIDE"      # в проекте: привязка и порядок сайдбара

_PROJECT_KEYS = ("thread-project-assignments", "sidebar-project-thread-orders")
_KNOWN_KEYS = _PROJECT_KEYS + ("projectless-thread-ids", "electron-persisted-atom-state")


def desktop_placement(thread_id: str) -> str:
    """Прочитать размещение ветки из собственных записей Desktop."""

    from .preflight import default_codex_home

    path = default_codex_home().expanduser().resolve() / ".codex-global-state.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ABSENT
    hits = [
        key
        for key in _KNOWN_KEYS
        if thread_id in json.dumps(payload.get(key), ensure_ascii=False)
    ]
    if any(key in _PROJECT_KEYS for key in hits):
        return INSIDE
    return OUTSIDE if hits else ABSENT


def promote_into_project(
    thread_id: str,
    project_id: str,
    *,
    client: Any,
    settle: float = 3.0,
    sleep: Callable[[float], None] | None = None,
) -> tuple[str, str]:
    """Довести ветку до проекта и вернуть (состояние до, состояние после).

    Ветка, созданная через App Server, Desktop о себе не сообщает. Явная
    привязка - единственный доступный рычаг; выполняется, только если
    ветка ещё не в проекте, и результат перечитывается из записей Desktop,
    а не берётся из ответа вызова.
    """

    rest = sleep or time.sleep
    before = desktop_placement(thread_id)
    if before == INSIDE:
        return before, before
    client.assign_thread_to_project(thread_id, project_id)
    rest(max(0.0, settle))
    after = desktop_placement(thread_id)
    if after == INSIDE:
        return before, after
    # Привязка на стороне App Server прошла, а Desktop о ветке не узнал:
    # замерено на живом прогоне, ABSENT -> ABSENT. Его собственная очередь
    # переноса застревает - обход падает на первой же сбойной ветке, и флаг
    # завершения не пишется никогда. Делаем ту же запись, что делает adopt
    # внутри самого приложения.
    adopt_into_desktop_project(thread_id, project_id)
    return before, desktop_placement(thread_id)


ASSIGNMENTS_KEY = "thread-project-assignments"
ORDERS_KEY = "sidebar-project-thread-orders"
PROJECTLESS_KEY = "projectless-thread-ids"
MAPPING_KEY = "app-server-project-id-by-legacy-project-id-by-host"


def adopt_into_desktop_project(thread_id: str, app_server_project_id: str) -> bool:
    """Внести ветку в проект записью, которую делает сам Desktop.

    Форма взята из приложения: adopt пишет в thread-project-assignments
    пару projectKind/projectId, убирает ветку из projectless-thread-ids и
    добавляет её в порядок сайдбара проекта.

    Трогаются только эти три ключа, файл переписывается целиком и
    атомарно: приложение не должно увидеть половину записи. Возвращает
    True, если запись сделана.
    """

    from .preflight import default_codex_home

    path = default_codex_home().expanduser().resolve() / ".codex-global-state.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    legacy = _legacy_project_id(payload, app_server_project_id)
    if legacy is None:
        return False

    assignments = dict(payload.get(ASSIGNMENTS_KEY) or {})
    if assignments.get(thread_id, {}).get("projectId") == legacy:
        return False
    assignments[thread_id] = {"projectKind": "local", "projectId": legacy}
    projectless = [
        item for item in (payload.get(PROJECTLESS_KEY) or []) if item != thread_id
    ]
    orders = dict(payload.get(ORDERS_KEY) or {})
    order = list((orders.get(legacy) or {}).get("threadIds") or [])
    if thread_id not in order:
        order.append(thread_id)
    orders[legacy] = {"threadIds": order}

    payload[ASSIGNMENTS_KEY] = assignments
    payload[PROJECTLESS_KEY] = projectless
    payload[ORDERS_KEY] = orders
    temporary = path.with_name(path.name + ".codex-autopilot.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        return False
    return True


def _legacy_project_id(
    payload: Mapping[str, Any], app_server_project_id: str
) -> str | None:
    for mapping in (payload.get(MAPPING_KEY) or {}).values():
        for legacy, server in (mapping or {}).items():
            if server == app_server_project_id:
                return str(legacy)
    return None
