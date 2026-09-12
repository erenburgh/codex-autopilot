#!/usr/bin/env python3
"""Внести ветки прогона в проект так же, как это делает сам Desktop.

Зачем. Desktop умеет забирать ветки, созданные через App Server: обход
thread/list вызывает observe -> thread/read -> adopt, и adopt пишет запись
в thread-project-assignments. Но весь обход живёт внутри migrate(), а
там при падении любой ветки из пачки бросается исключение на весь цикл, и
флаг threadAssignmentsMigrated пишется только в самом конце. Одна сбойная
ветка блокирует перенос целиком: очередь не рассасывается никогда.

Скрипт делает ту же запись, что делает adopt, и ничего сверх неё.

БЕЗОПАСНОСТЬ. Приложение держит состояние в памяти и перезаписывает файл
своей копией, поэтому работать можно ТОЛЬКО при закрытом Codex. Скрипт
сам отказывается запускаться, если приложение живо. Перед записью
делается резервная копия рядом с файлом.

Запуск:
  python3 scripts/adopt_threads_into_project.py            # показать, что будет сделано
  python3 scripts/adopt_threads_into_project.py --apply    # записать
  python3 scripts/adopt_threads_into_project.py --thread <id> --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_autopilot.config import load_config  # noqa: E402
from codex_autopilot.preflight import default_codex_home  # noqa: E402
from codex_autopilot.run_state import StateStore  # noqa: E402

ASSIGNMENTS = "thread-project-assignments"
ORDERS = "sidebar-project-thread-orders"
PROJECTLESS = "projectless-thread-ids"
MAPPING = "app-server-project-id-by-legacy-project-id-by-host"


def codex_is_running() -> bool:
    try:
        out = subprocess.run(
            ("pgrep", "-f", "ChatGPT.app/Contents/MacOS"),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return bool(out.stdout.strip())


def legacy_project_id(state: dict, app_server_project_id: str) -> str | None:
    """Обратное отображение проекта App Server в проект Desktop."""

    for mapping in (state.get(MAPPING) or {}).values():
        for legacy, server in (mapping or {}).items():
            if server == app_server_project_id:
                return legacy
    return None


def run_threads(cfg) -> list[tuple[str, str]]:
    seen: dict[str, str] = {}
    for session in StateStore(cfg.state_dir).load().worker_sessions:
        thread_id = session.get("thread_id")
        if thread_id and thread_id not in seen:
            seen[str(thread_id)] = str(session.get("task_id") or "?")
    return list(seen.items())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", action="append", help="конкретная ветка; можно несколько")
    parser.add_argument("--apply", action="store_true", help="записать; без него только показ")
    args = parser.parse_args()

    cfg = load_config(ROOT)
    path = default_codex_home().expanduser().resolve() / ".codex-global-state.json"
    state = json.loads(path.read_text(encoding="utf-8"))

    project = legacy_project_id(state, cfg.desktop.project_id or "")
    if project is None:
        raise SystemExit(
            f"проект App Server {cfg.desktop.project_id!r} не отображается ни в один "
            "проект Desktop — вносить некуда"
        )
    name = ((state.get("local-projects") or {}).get(project) or {}).get("name")
    print(f"проект Desktop: {name!r} ({project})\n")

    targets = [(t, "?") for t in (args.thread or [])] or run_threads(cfg)
    assignments = state.get(ASSIGNMENTS) or {}
    projectless = list(state.get(PROJECTLESS) or [])
    orders = state.get(ORDERS) or {}
    order = list((orders.get(project) or {}).get("threadIds") or [])

    planned: list[tuple[str, str]] = []
    for thread_id, task in targets:
        if assignments.get(thread_id, {}).get("projectId") == project:
            print(f"  {task:4s} {thread_id}  уже в проекте")
            continue
        planned.append((thread_id, task))
        print(f"  {task:4s} {thread_id}  БУДЕТ ВНЕСЕНА")

    if not planned:
        print("\nвносить нечего")
        return 0
    if not args.apply:
        print(f"\nпоказ без записи: {len(planned)} веток. Добавь --apply")
        return 0
    if codex_is_running():
        raise SystemExit(
            "\nCodex запущен. Он держит состояние в памяти и перезапишет файл своей "
            "копией — закрой приложение и повтори."
        )

    backup = path.with_name(path.name + f".backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)

    for thread_id, _task in planned:
        # Ровно та запись, которую делает adopt в самом приложении.
        assignments[thread_id] = {"projectKind": "local", "projectId": project}
        if thread_id in projectless:
            projectless.remove(thread_id)
        if thread_id not in order:
            order.append(thread_id)

    state[ASSIGNMENTS] = assignments
    state[PROJECTLESS] = projectless
    orders[project] = {"threadIds": order}
    state[ORDERS] = orders
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nвнесено веток: {len(planned)}")
    print(f"резервная копия: {backup}")
    print("открой Codex и проверь проект")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
