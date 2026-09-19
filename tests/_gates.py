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

import ast
from pathlib import Path
from unittest import mock

SYMBOL = "require_trusted_stop_hook_for_config"
SRC = Path(__file__).resolve().parents[1] / "src" / "codex_autopilot"


def hook_gate_modules() -> tuple[str, ...]:
    """The production modules that import the trust gate.

    Found by parsing, not by substring. The substring version looked for
    "import <symbol>" and therefore saw only modules that imported the gate
    alone: a module importing it beside another name - the ordinary way -
    was silently left unsubstituted, and its tests went to the developer
    machine's real App Server. That is exactly the failure this helper was
    written to prevent, and it was hiding inside the helper itself.
    """

    modules = []
    for path in sorted(SRC.glob("*.py")):
        if path.stem in {"hook_trust", "__init__"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = any(
            isinstance(node, ast.ImportFrom)
            and any(alias.name == SYMBOL for alias in node.names)
            for node in ast.walk(tree)
        )
        if imported:
            modules.append(path.stem)
    if not modules:
        raise AssertionError(
            f"no module imports {SYMBOL}: the substitution would be empty "
            "and the tests would go to the real App Server again"
        )
    return tuple(modules)


def patch_hook_trust_gates(case) -> dict[str, mock.MagicMock]:
    """Substitute the gate everywhere production calls it.

    Returns the mocks by module name: a test that checks the gate itself
    takes the one it needs from here instead of starting a second patch on
    top - otherwise the last one wins and the check quietly measures
    nothing.
    """

    gates: dict[str, mock.MagicMock] = {}
    for module in hook_gate_modules():
        patcher = mock.patch(f"codex_autopilot.{module}.{SYMBOL}")
        gates[module] = patcher.start()
        case.addCleanup(patcher.stop)
    return gates
