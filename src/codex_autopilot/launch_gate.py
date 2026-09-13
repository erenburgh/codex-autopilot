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
from enum import Enum
import json
from pathlib import Path
import tempfile
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
    "desktop_placement",
    "render_launch_timeline",
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
        checks.append(_desktop_visibility(task_id, thread_id, session))

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


# Шаги запуска человеческим языком. Порядок берётся из журнала, а не
# отсюда: журнал и есть настоящая последовательность.
TIMELINE_STEPS = {
    "reservation_created": "слот зарезервирован",
    "create_requested": "запрошено создание ветки",
    "app_server_create_claimed": "создание начато",
    "app_server_thread_created": "ветка создана",
    "app_server_project_scoped_create": "создана в пространстве проекта",
    "prep_completed": "рабочий каталог подготовлен",
    "automatic_turn_claimed": "ход взят",
    "start_acknowledged": "работа начата",
    "turn_completed": "ход завершён",
    "implementation_completed": "реализация завершена",
    "verification_started": "проверка начата",
    "verification_passed": "проверка пройдена",
}

TIMELINE_FAILURES = {
    "create_failed": "создание ветки не удалось",
    "start_failed": "старт не удался",
    "prep_failed": "подготовка каталога не удалась",
    "interrupt_observed": "работа прервана",
    "retry_scheduled": "назначен повтор",
    "scope_violation_recorded": "выход за объявленную область",
    "rule_declaration_missing": "в отчёте нет применённых правил",
    "scope_not_observed": "область проверить не удалось",
}


def render_launch_timeline(state: RunState, task_ids: Sequence[str]) -> str:
    """Лента шагов запуска с починками, а не снимок конечного состояния.

    Снимок умалчивает о самом важном: по нему нельзя понять, понадобилась
    ли починка. Гейт размещения может увидеть ABSENT, перенести ветку в
    проект и увидеть INSIDE - в снимке это одна галочка, и кажется, что
    всё прошло само.
    """

    lines: list[str] = []
    for task_id in task_ids:
        session = _latest_session(state, task_id)
        if session is None:
            lines.append(f"{task_id}:")
            lines.append("  [✗] слот не зарезервирован — задача не бралась в работу")
            continue
        lines.append(f"{task_id}:")
        for event in _current_attempt(
            _events_for(state, str(session.get("reservation_token") or ""))
        ):
            lines.extend(_timeline_line(event))
    return "\n".join(lines)


def _current_attempt(
    events: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Оставить по одной - последней - записи каждого шага.

    Резервация переживает несколько попыток, и её журнал копит их все. В
    ленте это выглядело противоречием: рядом стояли "перенос не помог" от
    первой попытки и "размещение подтверждено" от второй, и прочесть, где
    задача сейчас, было нельзя. Показываем текущее положение дел, а не
    историю: у каждого шага последнее наблюдение.
    """

    latest: dict[str, Mapping[str, Any]] = {}
    for event in events:
        latest[str(event.get("event") or "")] = event
    return sorted(latest.values(), key=lambda item: int(item.get("sequence") or 0))


def _timeline_line(event: Mapping[str, Any]) -> list[str]:
    name = str(event.get("event") or "")
    detail = str(event.get("detail") or "")
    if name == "desktop_placement_verified":
        return _placement_lines(detail)
    if name in TIMELINE_FAILURES:
        text = TIMELINE_FAILURES[name]
        return [f"  [✗] {text}" + (f" — {detail[:90]}" if detail else "")]
    if name in TIMELINE_STEPS:
        suffix = ""
        if name == "app_server_thread_created" and detail:
            suffix = f" — {detail[:40]}"
        return [f"  [✓] {TIMELINE_STEPS[name]}{suffix}"]
    return []


def _placement_lines(detail: str) -> list[str]:
    """Размещение в Desktop: показать и проверку, и починку."""

    before, _, after = detail.partition(" -> ")
    before, after = before.strip(), after.strip()
    names = {
        INSIDE: "в проекте",
        OUTSIDE: "видна, но вне проекта",
        ABSENT: "Desktop о ней не знает",
    }
    if before == after == INSIDE:
        return ["  [✓] размещение в проекте подтверждено"]
    lines = [f"  [✗] размещение: {names.get(before, before)}"]
    if after == INSIDE:
        lines.append("  [→] перенесена в проект")
        lines.append("  [✓] размещение в проекте подтверждено")
    else:
        lines.append(f"  [✗] перенос не помог: {names.get(after, after)}")
    return lines


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
    task_id: str, thread_id: str, session: Mapping[str, Any]
) -> LaunchCheck:
    """Видна ли ветка, по измерению, сделанному при размещении.

    Само измерение делает путь создания: у него есть живое соединение с
    сервером, и спрашивать размещение заново на каждый опрос ленты значило
    бы поднимать app-server по разу в секунду. Здесь читается записанный
    результат.

    Пункт намеренно не решающий: между созданием ветки и записью
    размещения есть окно, и объявлять отказ по нему значило бы снова
    плодить ложные тикеты.
    """

    if not thread_id:
        return LaunchCheck(
            "visible_in_desktop", task_id, None, "нечего искать: ветка не привязана"
        )
    placement = str(session.get("desktop_placement") or "")
    if placement == INSIDE:
        return LaunchCheck(
            "visible_in_desktop", task_id, True, "ветка в проекте и видна в сайдбаре"
        )
    if placement == OUTSIDE:
        return LaunchCheck(
            "visible_in_desktop", task_id, False, "сервер знает ветку, но она вне проекта"
        )
    if placement == ABSENT:
        return LaunchCheck(
            "visible_in_desktop", task_id, False, "сервер ветку не знает: она не сохранилась"
        )
    return LaunchCheck(
        "visible_in_desktop", task_id, None, "размещение ещё не измерено"
    )


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


# Где ветка по мнению самого сервера. Именно из его списка Desktop рисует
# сайдбар: замерено на ветках, которые человек видит глазами, - записи
# приложения в .codex-global-state.json про них молчат, а сервер их знает.
ABSENT = "ABSENT"      # сервер ветки не знает: она не сохранилась
OUTSIDE = "OUTSIDE"    # сервер знает, но вне нужного проекта
INSIDE = "INSIDE"      # в проекте


def desktop_placement(
    thread_id: str,
    *,
    project_id: str | None = None,
    client: Any = None,
    binary: str = "codex",
    log_path: Path | None = None,
) -> str:
    """Спросить у сервера, где ветка.

    Прежняя проверка читала ключи .codex-global-state.json. Замерено: три
    ветки, которые человек видел в сайдбаре проекта, лежат только в
    electron-persisted-atom-state, а в thread-project-assignments их нет
    вовсе - та проверка называла их OUTSIDE. На её показаниях был построен
    ложный вывод, что видимую задачу через App Server завести нельзя.

    Ветка без единого хода на сервере не сохраняется: четыре пробы,
    созданные пустыми, исчезли из thread/list полностью. Поэтому ABSENT
    означает не "невидима", а "её больше нет".
    """

    if not thread_id:
        return ABSENT
    if client is not None:
        return _placement_via(client, thread_id, project_id)

    from .appserver import AppServerClient

    destination = log_path or Path(tempfile.gettempdir()) / "codex-autopilot-placement.jsonl"
    try:
        with AppServerClient(binary, destination) as fresh:
            return _placement_via(fresh, thread_id, project_id)
    except Exception:
        return ABSENT


def _placement_via(client: Any, thread_id: str, project_id: str | None) -> str:
    try:
        thread = client.read_thread(thread_id)
    except Exception:
        # Исчезнувшая ветка отвечает "thread not found"; связь могла и
        # просто оборваться, но в обоих случаях размещения у нас нет.
        return ABSENT
    if not thread:
        return ABSENT
    assigned = str(thread.get("projectId") or "")
    if not assigned:
        return OUTSIDE
    if project_id and assigned != str(project_id):
        return OUTSIDE
    return INSIDE
