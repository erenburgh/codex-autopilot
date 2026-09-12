#!/usr/bin/env python3
"""Провести ветку по циклу видимости и показать, где он обрывается.

Цикл: создана невидимой -> стала видимой -> легла в проект.

Скрипт не рассуждает, а делает шаги и после каждого сверяется с
СОБСТВЕННЫМИ записями Desktop (~/.codex/.codex-global-state.json).
Успех вызова на стороне App Server видимостью не считается: project/update
и thread/metadata/update проходят в пространстве имён App Server, не меняя
метаданных сайдбара Electron - это записано в самом продукте
(project_association.py) и подтверждено на живом прогоне.

Состояния, которые различаются по записям Desktop:

  НЕТ НИГДЕ          Desktop о ветке не знает - её не видно вообще
  ВИДНА ВНЕ ПРОЕКТА  ветка в projectless-thread-ids: видна в Recents
  В ПРОЕКТЕ          ветка в thread-project-assignments и порядке сайдбара

Запуск:
  python3 scripts/promote_thread_visibility.py                # активная ветка прогона
  python3 scripts/promote_thread_visibility.py --thread <id>
  python3 scripts/promote_thread_visibility.py --dry-run      # только замер, без вызовов
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_autopilot.appserver import AppServerClient  # noqa: E402
from codex_autopilot.config import load_config  # noqa: E402
from codex_autopilot.preflight import default_codex_home  # noqa: E402
from codex_autopilot.run_state import StateStore  # noqa: E402

PROJECT_KEYS = ("thread-project-assignments", "sidebar-project-thread-orders")
KNOWN_KEYS = PROJECT_KEYS + ("projectless-thread-ids", "electron-persisted-atom-state")

ABSENT = "НЕТ НИГДЕ"
OUTSIDE = "ВИДНА ВНЕ ПРОЕКТА"
INSIDE = "В ПРОЕКТЕ"


def desktop_state() -> dict:
    path = default_codex_home().expanduser().resolve() / ".codex-global-state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def placement(thread_id: str) -> tuple[str, list[str]]:
    """Где ветка по мнению самого Desktop, и в каких его записях."""

    state = desktop_state()
    hits = [
        key
        for key in KNOWN_KEYS
        if thread_id in json.dumps(state.get(key), ensure_ascii=False)
    ]
    if any(key in PROJECT_KEYS for key in hits):
        return INSIDE, hits
    if hits:
        return OUTSIDE, hits
    return ABSENT, hits


def active_thread(cfg) -> str:
    state = StateStore(cfg.state_dir).load()
    live = [
        item
        for item in state.worker_sessions
        if item.get("thread_id") and item.get("status") == "ACTIVE"
    ]
    if not live:
        raise SystemExit(
            "активной ветки в прогоне нет — укажи её явно через --thread"
        )
    return str(live[-1]["thread_id"])


def report(stage: str, thread_id: str) -> str:
    where, hits = placement(thread_id)
    detail = ", ".join(hits) if hits else "ни в одной записи"
    print(f"  {stage:<22} {where:<18} ({detail})")
    return where


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", help="идентификатор ветки; по умолчанию активная")
    parser.add_argument("--project", help="проект App Server; по умолчанию из конфига")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только замерить текущее размещение, ничего не вызывая",
    )
    parser.add_argument("--settle", type=float, default=3.0,
                        help="сколько секунд ждать перезаписи состояния Desktop")
    args = parser.parse_args()

    cfg = load_config(ROOT)
    thread_id = args.thread or active_thread(cfg)
    project_id = args.project or cfg.desktop.project_id
    if not project_id:
        raise SystemExit("в конфиге прогона не задан проект App Server")

    print(f"ветка:  {thread_id}")
    print(f"проект: {project_id}\n")
    before = report("до вмешательства", thread_id)
    if args.dry_run:
        return 0 if before == INSIDE else 1
    if before == INSIDE:
        print("\nветка уже в проекте — делать нечего")
        return 0

    print("\nшаг 1: привязка к проекту через App Server")
    client = AppServerClient(
        cfg.desktop.binary, cfg.state_dir / "logs" / "promote-visibility.jsonl"
    )
    with client:
        thread = client.assign_thread_to_project(thread_id, project_id)
        print(f"  App Server вернул projectId={thread.get('projectId')!r}")
        # Чтение обратно: успех вызова и фактическое состояние - разные вещи.
        fresh = client.read_thread(thread_id)
        print(f"  thread/read показывает projectId={fresh.get('projectId')!r}")

    # Desktop переписывает своё состояние не мгновенно.
    time.sleep(max(0.0, args.settle))
    after = report("после привязки", thread_id)

    print()
    if after == INSIDE:
        print("ЦИКЛ ПРОЙДЕН: ветка лежит в проекте.")
        return 0
    if after == OUTSIDE:
        print(
            "ЦИКЛ ОБОРВАН НА ПОСЛЕДНЕМ ШАГЕ: ветка стала видимой, но осталась\n"
            "вне проекта. Привязка на стороне App Server прошла, а записи\n"
            "сайдбара Desktop её не получили — значит этим вызовом ветку в\n"
            "проект не положить, и нужен путь, где ветку заводит сам Desktop."
        )
        return 2
    print(
        "ЦИКЛ НЕ НАЧАЛСЯ: Desktop о ветке не знает даже после привязки.\n"
        "Проверь, что приложение запущено и проект открыт, и повтори."
    )
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
