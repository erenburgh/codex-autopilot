"""A local import does not repeat a name the module already imported.

`from .run_state import StateStore` inside a function makes `StateStore`
a local name for the WHOLE function, not only after that line. Any use
of the same name higher in the function - in another `if` branch, in
another CLI command - fails with `UnboundLocalError` at runtime, and
fails for the user: that is how the `timeline` command broke, though the
import was added to a completely different command.

The ordinary undefined-name check is not enough here: the name is
defined at module level, the defect is in the shadowing. So the rule is
simple - a local import of a name the module already imported is
forbidden.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "codex_autopilot"


def _bound_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    return [alias.asname or alias.name.split(".")[0] for alias in node.names]


def _violations(tree: ast.Module) -> list[tuple[str, str, int]]:
    module_level: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module_level.update(_bound_names(node))

    found: list[tuple[str, str, int]] = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(function):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for name in _bound_names(node):
                if name in module_level:
                    found.append((name, function.name, node.lineno))
    return found


class NoShadowedImportsTests(unittest.TestCase):
    def test_no_local_import_shadows_a_module_level_name(self) -> None:
        offences: list[str] = []
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for name, function, lineno in _violations(tree):
                offences.append(
                    f"{path.name}:{lineno}: локальный импорт {name!r} в {function}() "
                    f"затеняет модульный импорт и ломает использование выше по функции"
                )
        self.assertEqual(offences, [], "\n".join(offences))

    def test_the_check_sees_a_shadowing_import(self) -> None:
        """Проверка обязана ловить дефект, а не молчать на любом коде."""

        tree = ast.parse(
            "from .run_state import StateStore\n"
            "def main():\n"
            "    if first:\n"
            "        return StateStore(path).load()\n"
            "    from .run_state import StateStore\n"
            "    return StateStore(other).load()\n"
        )
        self.assertEqual(_violations(tree), [("StateStore", "main", 5)])


if __name__ == "__main__":
    unittest.main()
