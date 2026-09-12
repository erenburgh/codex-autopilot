#!/usr/bin/env python3
"""Живая лента событий прогона: видно, что происходит прямо сейчас.

Хук отдаёт один ответ и показать ход работы построчно не может. Эта
команда закрывает другую половину: пока задача идёт, она печатает события
журнала по мере появления, а не оставляет смотреть в тишину.

Запуск:
  python3 scripts/watch_run.py              # следить дальше
  python3 scripts/watch_run.py --tail 20    # показать последние и следить
  python3 scripts/watch_run.py --once       # напечатать и выйти
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_autopilot.config import load_config  # noqa: E402

MARKS = {
    "create_failed": "✗",
    "start_failed": "✗",
    "prep_failed": "✗",
    "interrupt_observed": "✗",
    "retry_scheduled": "!",
    "scope_violation_recorded": "!",
    "rule_declaration_missing": "!",
    "scope_not_observed": "?",
    "desktop_placement_verified": "→",
    "turn_completed": "✓",
    "verification_passed": "✓",
    "implementation_completed": "✓",
}


def mark(event: str) -> str:
    return MARKS.get(event, "·")


def render(item: dict) -> str:
    at = str(item.get("at") or "")
    try:
        stamp = datetime.datetime.fromisoformat(at).astimezone().strftime("%H:%M:%S")
    except ValueError:
        stamp = at[:8]
    name = str(item.get("event") or "")
    task = str(item.get("task_id") or "")
    detail = str(item.get("detail") or "")
    line = f"{stamp} [{mark(name)}] {name}"
    if task:
        line += f"  {task}"
    if detail:
        line += f"  — {detail[:90]}"
    return line


def journal(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("lifecycle_journal", [])
    except (OSError, json.JSONDecodeError):
        # Состояние пишется атомарно, но прочитать можно ровно в момент
        # подмены: это не ошибка, а повод перечитать на следующем круге.
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tail", type=int, default=10, help="сколько последних показать")
    parser.add_argument("--once", action="store_true", help="напечатать и выйти")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    cfg = load_config(ROOT)
    path = cfg.state_dir / "run-state.json"
    items = journal(path)
    seen = int(items[-1]["sequence"]) if items else 0
    for item in items[-max(0, args.tail):]:
        print(render(item))
    if args.once:
        return 0

    print("— слежу за журналом, Ctrl+C чтобы выйти —")
    try:
        while True:
            time.sleep(max(0.2, args.interval))
            fresh = [
                item for item in journal(path) if int(item.get("sequence") or 0) > seen
            ]
            for item in fresh:
                print(render(item))
                seen = int(item["sequence"])
    except KeyboardInterrupt:
        print("\n— слежение остановлено, прогон не тронут —")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
