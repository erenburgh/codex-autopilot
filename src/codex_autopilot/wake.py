"""Будильник прогона: повтор по сроку поднимается сам, а не по слову человека.

Замерено на прогоне v1.0. Задача упиралась в лимит, рантайм честно
записывал срок повтора - и на этом всё: когда последний диспетчер
выходил, живого процесса не оставалось, и повтор по сроку некому было
поднять. Прогон стоял, пока хозяйка не писала "Resume" - каждые
несколько часов, руками, ради действия, которое рантайм умел сам.

Здесь ничего не обходится. Диспетчер, законно поднятый доверенным
Stop-хуком, и так продолжает прогон без повторной проверки хука - так
устроены все его преемники. Будильник - тот же преемник, только
отложенный: он спит до срока и делает ровно то, что диспетчер сделал бы
сразу, будь задача готова. Владелец тот же, проверка владения в
spawn_automatic_app_server_relay та же.

Чего будильник не делает: не будит остановленный человеком прогон
(пауза, BLOCKED), не будит законченный, не толкается с живым
диспетчером и не стреляет раньше, если лимит продлили.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .resources import ResourceLockCoordinator
from .run_state import StateStore, utc_now

# Спать дольше одного отрезка нельзя: за это время срок могли продлить.
MAX_NAP_SECONDS = 300


def due_wake_epoch(state: Any) -> int | None:
    """Ближайший срок повтора среди задач, которые ждут повтора."""

    due = [
        int(retry_at)
        for task_id, retry_at in (state.task_retry_at or {}).items()
        if state.task_states.get(task_id) == "RETRY_WAIT"
    ]
    return min(due) if due else None


def ensure_wake(
    cfg: Config,
    *,
    owner: str,
    owner_turn: str,
    spawn: Callable[..., int] | None = None,
) -> int | None:
    """Завести будильник, если есть кого будить и никто уже не ждёт.

    Возвращает pid спящего процесса или None, если будить некого.
    Живой будильник с не более поздним сроком считается достаточным:
    второй рядом с ним только толкался бы за ту же резервацию.
    """

    store = StateStore(cfg.state_dir)
    with ResourceLockCoordinator(store, cfg.root).transaction():
        state = store.load()
        due = due_wake_epoch(state)
        if due is None:
            return None
        if (
            isinstance(state.wake_pid, int)
            and _pid_alive(state.wake_pid)
            and isinstance(state.wake_at, int)
            and state.wake_at <= due
        ):
            return state.wake_pid
        pid = (spawn or _spawn_wake)(cfg, owner=owner, owner_turn=owner_turn, at_epoch=due)
        state.wake_pid = pid
        state.wake_at = due
        store.save(state)
        return pid


def run_wake(
    cfg: Config,
    *,
    at_epoch: int,
    owner: str,
    owner_turn: str,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    reserve: Callable[..., tuple[Any, ...]] | None = None,
    spawn_relay: Callable[..., int] | None = None,
) -> int:
    """Спать до срока и поднять диспетчер - или молча уйти, если нельзя."""

    from .resilience import append_resilience_event

    store = StateStore(cfg.state_dir)
    while True:
        state = store.load()
        if state.status in {"BLOCKED", "DONE"} or store.pause_requested():
            _finish(store, "wake_skipped", detail={"why": "run is stopped or paused"})
            return 0
        due = due_wake_epoch(state)
        if due is None:
            _finish(store, "wake_skipped", detail={"why": "nothing waits for a retry"})
            return 0
        target = max(due, int(at_epoch))
        if isinstance(state.rate_limit_until, int):
            target = max(target, state.rate_limit_until)
        remaining = target - now()
        if remaining > 0:
            sleep(min(remaining, MAX_NAP_SECONDS))
            continue
        if _dispatcher_alive(state):
            _finish(store, "wake_skipped", detail={"why": "a dispatcher is already running"})
            return 0
        break

    if reserve is None:
        from .lifecycle import reserve_ready_frontier as reserve
    if spawn_relay is None:
        from .control import spawn_automatic_app_server_relay as spawn_relay

    descriptors = reserve(
        cfg,
        now_epoch=int(now()),
        # Проверка хука уже была - при законном запуске той цепочки, из
        # которой этот будильник вырос. Так же продолжают прогон все
        # автоматические преемники.
        hook_gate=lambda _cfg: None,
        relay_owner_thread_id=owner,
    )
    if not descriptors:
        _finish(store, "wake_skipped", detail={"why": "the frontier reserved nothing"})
        return 0
    pids = [
        spawn_relay(
            cfg.root,
            reservation_token=item.reservation_token,
            initiator_thread_id=owner,
            initiator_turn_id=owner_turn,
        )
        for item in descriptors
    ]
    _finish(
        store,
        "wake_dispatched",
        detail={
            "tasks": [item.task_id for item in descriptors],
            "pids": pids,
        },
    )
    return 0


def _finish(store: StateStore, event: str, *, detail: dict[str, Any]) -> None:
    from .resilience import append_resilience_event

    state = store.load()
    append_resilience_event(state, event, at=utc_now(), detail=detail)
    state.wake_pid = None
    state.wake_at = None
    store.save(state)


def _dispatcher_alive(state: Any) -> bool:
    if _pid_alive(state.dispatcher_pid):
        return True
    return any(
        item.get("automatic_dispatch_state") == "RUNNING"
        and _pid_alive(item.get("automatic_dispatch_pid"))
        for item in state.worker_sessions
    )


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _spawn_wake(cfg: Config, *, owner: str, owner_turn: str, at_epoch: int) -> int:
    log_dir = cfg.state_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / f"wake-{at_epoch}.log").open("a", encoding="utf-8")
    env = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1])
    entries = [item for item in str(env.get("PYTHONPATH") or "").split(os.pathsep) if item]
    if source_root not in entries:
        entries.insert(0, source_root)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    command = [
        sys.executable,
        "-m",
        "codex_autopilot.cli",
        "_wake",
        "--project",
        str(cfg.root),
        "--at",
        str(int(at_epoch)),
        "--owner",
        owner,
        "--owner-turn",
        owner_turn,
    ]
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    finally:
        log.close()
    return proc.pid


# ---------------------------------------------------------------------------
# Переживает перезагрузку: обход по расписанию вместо одного спящего процесса
# ---------------------------------------------------------------------------
#
# Спящий процесс умирает вместе с машиной. Поэтому рядом с ним есть второй
# путь, которого перезагрузка не касается: агент launchd раз в несколько
# минут обходит известные проекты и заводит будильник там, где повтор по
# сроку ждёт, а живого будильника нет. Владелец и ход берутся из самого
# состояния прогона - из последнего завершённого хода причинного владельца,
# ровно так же, как их находит диспетчер для своих преемников.


def projects_registry_path() -> Path:
    """Список проектов, которые обходит агент.

    Лежит в корне установки, а не рядом с реестром запуска: тот живёт во
    временном каталоге, который macOS чистит при перезагрузке - а агент
    нужен ровно после неё. Удаление рантайма уносит список вместе с ним.
    """

    configured = os.environ.get("CODEX_AUTOPILOT_INSTALL_ROOT")
    root = (
        Path(configured).expanduser()
        if configured
        else Path.home() / "Library" / "Application Support" / "CodexAutopilot"
    )
    return root / "projects.json"


def register_project(root: Path, *, path: Path | None = None) -> None:
    """Запомнить проект для обхода. Повторная запись - не ошибка."""

    import json

    target = path or projects_registry_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    roots = set(registered_projects(path=target))
    roots.add(str(Path(root).resolve()))
    target.write_text(json.dumps(sorted(roots), ensure_ascii=False, indent=2), encoding="utf-8")


def registered_projects(*, path: Path | None = None) -> list[str]:
    import json

    target = path or projects_registry_path()
    if not target.is_file():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(item) for item in raw if isinstance(item, str)] if isinstance(raw, list) else []


def derive_owner(state: Any) -> tuple[str, str] | None:
    """Причинный владелец для будильника - из журнала, не из аргументов.

    Тот же критерий, что у диспетчера для преемников: последняя сессия,
    чей владелец relay записал завершённый ход. Если такого нет, будить
    некого от чьего-либо имени - и агент молча пропускает проект.
    """

    completed = {
        (str(item.get("thread_id") or ""), str(item.get("turn_id") or ""))
        for item in state.lifecycle_journal
        if str(item.get("event") or "") == "turn_completed"
    }
    for session in reversed(state.worker_sessions):
        owner = str(session.get("relay_owner_thread_id") or "")
        if not owner:
            continue
        for candidate in reversed(state.worker_sessions):
            if str(candidate.get("thread_id") or "") != owner:
                continue
            turn = str(candidate.get("turn_id") or "")
            if turn and (owner, turn) in completed:
                return owner, turn
    return None


def sweep(
    *,
    roots: list[str] | None = None,
    spawn: Callable[..., int] | None = None,
    load: Callable[[Path], Config] | None = None,
) -> dict[str, str]:
    """Один обход: для каждого проекта - завести будильник, если он нужен.

    Возвращает, что решено по каждому корню; агент печатает это в свой лог.
    """

    from .config import load_config as _load_config

    outcome: dict[str, str] = {}
    for raw in roots if roots is not None else registered_projects():
        root = Path(raw)
        if not (root / ".codex-autopilot" / "config.toml").is_file():
            outcome[raw] = "gone"
            continue
        try:
            cfg = (load or _load_config)(root)
            state = StateStore(cfg.state_dir).load()
        except Exception as exc:  # noqa: BLE001 - один больной проект не рушит обход
            outcome[raw] = f"unreadable: {exc}"
            continue
        if state.status in {"BLOCKED", "DONE"} or StateStore(cfg.state_dir).pause_requested():
            outcome[raw] = "stopped"
            continue
        if due_wake_epoch(state) is None:
            outcome[raw] = "nothing due"
            continue
        owner = derive_owner(state)
        if owner is None:
            outcome[raw] = "no completed owner"
            continue
        pid = ensure_wake(cfg, owner=owner[0], owner_turn=owner[1], spawn=spawn)
        outcome[raw] = f"wake {pid}"
    return outcome
