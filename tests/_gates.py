"""Substituting the hook-trust gates for the lifecycle tests.

The trust gate talks to the developer machine's REAL App Server. While
tests substituted it in only some modules, the suite passed only because
the hooks on the laptop happened to be trusted, and failed with two
dozen errors right after reinstalling the plugin.

The module list is not hard-coded: it is derived from the sources. A
sixth call site will be substituted by itself instead of showing up as a
suddenly red suite.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

SYMBOL = "require_trusted_stop_hook_for_config"
SRC = Path(__file__).resolve().parents[1] / "src" / "codex_autopilot"


def hook_gate_modules() -> tuple[str, ...]:
    """Модули продакшена, импортирующие гейт доверия."""

    modules = [
        path.stem
        for path in sorted(SRC.glob("*.py"))
        if path.stem not in {"hook_trust", "__init__"}
        and f"import {SYMBOL}" in path.read_text(encoding="utf-8")
    ]
    if not modules:
        raise AssertionError(
            f"ни один модуль не импортирует {SYMBOL}: подстановка стала бы пустой "
            "и тесты снова пошли бы в настоящий App Server"
        )
    return tuple(modules)


def patch_hook_trust_gates(case) -> dict[str, mock.MagicMock]:
    """Подставить гейт во всех местах, где его зовёт продакшен.

    Возвращает моки по имени модуля: тесты, которые проверяют сам гейт,
    берут нужный отсюда, а не заводят второй патч поверх - иначе активным
    остаётся последний, и проверка молча уходит в пустоту.
    """

    gates: dict[str, mock.MagicMock] = {}
    for module in hook_gate_modules():
        patcher = mock.patch(f"codex_autopilot.{module}.{SYMBOL}")
        gates[module] = patcher.start()
        case.addCleanup(patcher.stop)
    return gates
