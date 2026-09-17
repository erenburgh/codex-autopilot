"""Гейт запуска: подтвердить, что задача поднялась, а не сообщить об этом.

Отказ, ради которого это написано, выглядел так: сессия отвечала
"диспетчер запущен, pid такой-то", завершалась, и ничего не происходило.
Сообщение было заявлением о намерении, а не наблюдением результата:
ожидание диспетчера подтверждало лишь то, что его процесс дошёл до
какой-то фазы, и ничего не говорило о самой задаче. Та функция
(`wait_for_dispatcher`) снята в 0.8.1 вместе с headless-путём; замер,
ради которого написан этот гейт, от этого не устарел.

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

# M11-R5: сколько ждать измерения размещения, прежде чем считать его
# несостоявшимся. Размещение меряется сразу после создания, в том же
# проходе диспетчера, поэтому запас здесь велик намеренно: срок нужен не
# для нормального хода, а для случая, когда мерить стало некому.
PLACEMENT_MEASUREMENT_DEADLINE_SECONDS = 180.0

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

# M11-R5. Пункты, которые роняют вердикт только на явном False, но не на
# "проверить было нечем". Размещение именно таково: между созданием ветки
# и записью измерения есть окно, и отказ по неизмеренности плодил бы
# ложные тикеты - ровно поэтому пункт и был сделан нерешающим целиком.
# Но измеренное OUTSIDE или ABSENT - это не окно, а результат, и прежде
# он не менял ничего: вердикт держался в IN_PROGRESS, тикет не заводился.
# Просроченная неизмеренность превращается в False отдельно, по сроку.
DECISIVE_ON_FAILURE = frozenset({"visible_in_desktop"})


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
    now: Callable[[], float] | None = None,
) -> tuple[LaunchCheck, ...]:
    """Проверить по записям прогона, что названные задачи действительно подняты.

    ``now`` отдаёт время эпохи и нужен только сроку измерения размещения;
    он отделён от монотонных часов ожидания, потому что сравнивается с
    отметкой создания ветки, а она записана стенными часами.
    """

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
                    "no reservation found: the task was never taken up",
                )
            )
            continue
        checks.append(LaunchCheck("reserved", task_id, True, "reservation present"))

        thread_id = str(session.get("thread_id") or "")
        checks.append(
            LaunchCheck(
                "thread_bound",
                task_id,
                bool(thread_id),
                f"thread {thread_id}" if thread_id else "no thread bound",
            )
        )

        token = str(session.get("reservation_token") or "")
        events = _events_for(state, token)
        checks.append(
            _event_check(
                "created_in_project", task_id, events, CREATED_EVENTS,
                ok="thread created through App Server in the run's project",
                bad="no record of the thread being created",
            )
        )
        # Отправка подтверждается durable-записью, а не мгновенным
        # статусом. Ход, успевший завершиться до проверки, уводит статус
        # дальше ACTIVE - и быстрый воркер объявлялся незапущенным.
        acknowledged = session.get("status") == ACTIVE_STATUS or any(
            str(item.get("event") or "") in ACKNOWLEDGED_EVENTS for item in events
        )
        checks.append(
            LaunchCheck(
                "send_acknowledged",
                task_id,
                acknowledged,
                f"session status {session.get('status')!r}"
                + ("" if acknowledged else "; the send was not acknowledged"),
            )
        )
        checks.append(
            _event_check(
                "launch_report_written", task_id, events, REPORT_EVENTS,
                ok="the runtime reached the launch report",
                bad="the runtime did not reach the launch report",
            )
        )
        checks.append(
            _desktop_visibility(
                task_id,
                thread_id,
                session,
                now=now,
                required=cfg.runtime.required_thread_placement,
            )
        )

        pid = session.get("automatic_dispatch_pid")
        if pid is None:
            pid = state.dispatcher_pid
        # Завершённому ходу живой диспетчер не нужен: он выходит штатно,
        # закончив работу. Прежде быстрый воркер получал здесь False и
        # тикет launch_not_confirmed - при том что в том же чек-листе
        # стояло "ход завершён".
        finished = any(str(item.get("event") or "") == "turn_completed" for item in events)
        checks.append(
            LaunchCheck(
                "dispatcher_alive",
                task_id,
                True if finished else (alive(pid) if pid is not None else None),
                "turn completed, the dispatcher is no longer needed"
                if finished
                else (f"dispatcher pid {pid}" if pid is not None else "dispatcher pid not recorded"),
            )
        )

        failure = _failure_after_launch(events)
        checks.append(
            LaunchCheck(
                "no_failure_after_launch",
                task_id,
                failure is None,
                "no failures after launch"
                if failure is None
                else f"a failure was recorded after launch: {failure}",
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
        if (item.id in DECISIVE_CHECKS and item.passed is not True)
        or (item.id in DECISIVE_ON_FAILURE and item.passed is False)
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
        return "Launch checklist: nothing to check — no task was named."
    lines: list[str] = []
    for task_id in dict.fromkeys(item.task_id for item in checks):
        lines.append(f"{task_id}:")
        for item in checks:
            if item.task_id == task_id:
                lines.append(f"  [{item.mark}] {item.id}: {item.detail}")
    verdict = {
        LaunchVerdict.CONFIRMED: "LAUNCH CONFIRMED",
        LaunchVerdict.IN_PROGRESS: (
            "LAUNCH IN PROGRESS — dispatcher alive, no failures, some steps still ahead"
        ),
        LaunchVerdict.FAILED: "LAUNCH FAILED — this is a failure, not a success",
    }[launch_verdict(checks)]
    return verdict + "\n" + "\n".join(lines)


# Шаги запуска человеческим языком. Порядок берётся из журнала, а не
# отсюда: журнал и есть настоящая последовательность.
TIMELINE_STEPS = {
    "reservation_created": "slot reserved",
    "create_requested": "thread creation requested",
    "app_server_create_claimed": "creation started",
    "app_server_thread_created": "thread created",
    "app_server_project_scoped_create": "created in the project's space",
    "prep_completed": "working directory prepared",
    "automatic_turn_claimed": "turn claimed",
    "start_acknowledged": "work started",
    "turn_completed": "turn completed",
    "implementation_completed": "implementation completed",
    "verification_started": "verification started",
    "verification_passed": "verification passed",
}

TIMELINE_FAILURES = {
    "create_failed": "thread creation failed",
    "start_failed": "start failed",
    "prep_failed": "directory preparation failed",
    "interrupt_observed": "work interrupted",
    "retry_scheduled": "retry scheduled",
    "scope_violation_recorded": "outside the declared scope",
    "rule_declaration_missing": "the report lists no applied rules",
    "scope_not_observed": "the scope could not be checked",
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
            lines.append("  [✗] slot not reserved — the task was never taken up")
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
        INSIDE: "in the project",
        OUTSIDE: "visible, but outside the project",
        ABSENT: "unknown to Desktop",
    }
    if before == after == INSIDE:
        return ["  [✓] placement in the project confirmed"]
    lines = [f"  [✗] placement: {names.get(before, before)}"]
    if after == INSIDE:
        lines.append("  [→] moved into the project")
        lines.append("  [✓] placement in the project confirmed")
    else:
        lines.append(f"  [✗] the move did not help: {names.get(after, after)}")
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
    task_id: str,
    thread_id: str,
    session: Mapping[str, Any],
    *,
    now: Callable[[], float] | None = None,
    required: str = "in_project",
) -> LaunchCheck:
    """Видна ли ветка, по измерению, сделанному при размещении.

    Само измерение делает путь создания: у него есть живое соединение с
    сервером, и спрашивать размещение заново на каждый опрос ленты значило
    бы поднимать app-server по разу в секунду. Здесь читается записанный
    результат.

    Пункт не решающий, пока измерения ещё может не быть: между созданием
    ветки и записью размещения есть окно, и отказ по нему плодил бы
    ложные тикеты.

    M11-R5. Но неизмеренность не вечна. Если ветка создана давно, а
    размещение так и не записано, мерить стало некому - диспетчер умер
    между созданием и гейтом. Прежде этот случай оставался
    неопределённым навсегда: вердикт держался в IN_PROGRESS, тикет не
    заводился, и задача просто не двигалась. Теперь истёкший срок - это
    отрицательный результат, а он уже уходит в один нормализованный
    тикет наравне с OUTSIDE и ABSENT.
    """

    if not thread_id:
        return LaunchCheck(
            "visible_in_desktop", task_id, None, "nothing to look for: no thread bound"
        )
    if required == "any":
        return LaunchCheck(
            "visible_in_desktop", task_id, None, "placement not required by the config"
        )
    placement = str(session.get("desktop_placement") or "")
    if placement == INSIDE:
        return LaunchCheck(
            "visible_in_desktop", task_id, True, "thread in the project and visible in the sidebar"
        )
    if placement == OUTSIDE:
        # При required="visible" вне проекта - всё ещё видимая ветка, и
        # гейт размещения её пропускает. Объявлять её отказом здесь
        # значило бы заводить тикет на то, что конфиг разрешил.
        if required == "visible":
            return LaunchCheck(
                "visible_in_desktop",
                task_id,
                True,
                "thread visible; outside the project, which the config allows",
            )
        return LaunchCheck(
            "visible_in_desktop", task_id, False, "the server knows the thread, but it is outside the project"
        )
    if placement == ABSENT:
        return LaunchCheck(
            "visible_in_desktop", task_id, False, "the server does not know the thread: it was not persisted"
        )
    waited = _seconds_since_create(session, now=now)
    if waited is not None and waited > PLACEMENT_MEASUREMENT_DEADLINE_SECONDS:
        return LaunchCheck(
            "visible_in_desktop",
            task_id,
            False,
            f"placement not measured {int(waited)} s after the thread was created: "
            "nobody is left to measure it",
        )
    return LaunchCheck(
        "visible_in_desktop", task_id, None, "placement not measured yet"
    )


def _seconds_since_create(
    session: Mapping[str, Any], *, now: Callable[[], float] | None = None
) -> float | None:
    """Сколько прошло с подтверждения создания ветки, или None.

    None означает "срок считать не от чего", а не "срок не истёк": без
    отметки времени нельзя объявить просрочку, и подменять одно другим
    здесь нельзя - это ровно та подмена, ради которой написан весь
    модуль.
    """

    raw = session.get("create_acknowledged_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        created = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    current = (
        datetime.fromtimestamp(now(), tz=timezone.utc)
        if now is not None
        else datetime.now(timezone.utc)
    )
    return (current - created).total_seconds()


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


def placement_observation(client: Any, thread_id: str) -> dict[str, Any]:
    """Что сервер сообщает о пригодности ветки к правке человеком.

    M11-R5 требовал проверять не только принадлежность проекту, но и
    редактируемость. Замерено на живом сервере: ``canAcceptDirectInput``
    приходит null и в ``thread/read`` незагруженной ветки, и во всех
    тридцати строках ``thread/list``. Поле живое, а не долговечное:
    строить на нём гейт нельзя, потому что "нельзя править" и "никто не
    держит" оно не различает.

    Поэтому здесь наблюдение, а не решение. Оно пишется рядом с
    размещением, чтобы вопрос о передаче владения решался по записям, а
    не по памяти. ``status.type == "notLoaded"`` - то состояние, в
    котором ветку никто не держит.
    """

    try:
        thread = client.read_thread(thread_id) or {}
    except Exception as exc:
        return {"observed": False, "reason": str(exc)}
    status = thread.get("status")
    return {
        "observed": True,
        "can_accept_direct_input": thread.get("canAcceptDirectInput"),
        "status_type": (
            str(status.get("type")) if isinstance(status, Mapping) else None
        ),
        "originator": thread.get("originator"),
        "thread_source": thread.get("threadSource"),
    }


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
