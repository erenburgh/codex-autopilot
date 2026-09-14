#!/usr/bin/env python3
"""Прописать собственный скрипт Autopilot в execpolicy Codex.

Зачем это существует. Codex спрашивает разрешение на команду, которой нет в
execpolicy. Для Autopilot это означает, что запуск его же установленного
скрипта может упереться в нативный диалог - а диспетчер по своему правилу не
отвечает на approvals ни при каких условиях. Запрос повисает в задаче, на
которую пользователь не смотрит, инициирующий ход молчит, и прогон не
начинается.

Что именно замерено на живой машине:

- ``codex execpolicy check`` разрешает команду, когда она идёт прямым argv;
- та же команда внутри ``/bin/zsh -lc "..."`` не совпадает ни с одним правилом,
  потому что правила сопоставляются по токенам argv, а вся строка шелла - один
  токен.

Отсюда два следствия. SKILL требует запускать скрипт без обёртки шеллом, а
здесь заводится ровно то правило, которое такой запуск закрывает.

Разрешены только команды инициирующего хода. Всё остальное - ``devops-*``,
``uninstall``, ``hook``, ``relay-*`` - по-прежнему спрашивает.

Путь скрипта версионирован, поэтому блок переписывается на каждой установке:
старые строки Autopilot удаляются, новые добавляются. Правила, написанные
пользователем или самим Codex, не трогаются.
"""

from __future__ import annotations

import argparse
from pathlib import Path

MARKER = "# codex-autopilot (managed): собственный скрипт плагина, прямой argv"
ALLOWED_COMMANDS = ("start-skill", "timeline")


def build_block(script: str, commands: tuple[str, ...] = ALLOWED_COMMANDS) -> str:
    lines = [MARKER]
    for command in commands:
        lines.append(f'prefix_rule(pattern=[{script!r}, {command!r}], decision="allow")')
    return "\n".join(lines)


def strip_managed(existing: str) -> str:
    """Убрать прошлый управляемый блок, не трогая чужие правила."""
    kept: list[str] = []
    inside = False
    for line in existing.splitlines():
        if line.strip() == MARKER:
            inside = True
            continue
        if inside:
            if line.startswith("prefix_rule(") and "codex-autopilot" in line:
                continue
            inside = False
        kept.append(line)
    return "\n".join(kept).rstrip("\n")


def register(script: str, rules_path: Path, commands: tuple[str, ...] = ALLOWED_COMMANDS) -> str:
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    existing = rules_path.read_text(encoding="utf-8") if rules_path.exists() else ""
    body = strip_managed(existing)
    block = build_block(script, commands)
    text = (body + "\n\n" if body else "") + block + "\n"
    rules_path.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True, help="installed skill-side launcher path")
    parser.add_argument("--rules", required=True, type=Path, help="Codex execpolicy rules file")
    args = parser.parse_args()
    register(args.script, args.rules)
    print(
        f"Execpolicy: allowed {args.script} "
        f"{{{', '.join(ALLOWED_COMMANDS)}}} in {args.rules}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
