"""A name absent from the module must not reach the user.

The same thing twice in a row: a line referred to a name someone forgot
to import, the tests were green, and the NameError happened to a human -
once at the very end of a successful preflight, right after permission
was granted, and once on the DevOps recovery path.

Tests do not cover every branch and need not. But a name absent from the
module is found without running - by parsing - and such a check belongs
in the suite.
"""

from __future__ import annotations

import builtins
from pathlib import Path
import symtable
import unittest


SOURCE = Path(__file__).resolve().parent.parent / "src" / "codex_autopilot"
# Names the interpreter puts into the module by itself.
MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__", "__spec__"}


def _undefined(path: Path) -> list[str]:
    table = symtable.symtable(path.read_text(encoding="utf-8"), str(path), "exec")
    module_names = {item.get_name() for item in table.get_symbols()} | MODULE_DUNDERS
    found: list[str] = []

    def walk(scope: symtable.SymbolTable) -> None:
        for child in scope.get_children():
            for symbol in child.get_symbols():
                name = symbol.get_name()
                if (
                    symbol.is_global()
                    and not symbol.is_assigned()
                    and name not in module_names
                    and not hasattr(builtins, name)
                ):
                    found.append(f"{path.name}:{child.get_name()}: {name}")
            walk(child)

    walk(table)
    return found


class UndefinedNameTests(unittest.TestCase):
    def test_every_module_resolves_its_own_names(self) -> None:
        problems: list[str] = []
        for path in sorted(SOURCE.glob("*.py")):
            problems.extend(_undefined(path))
        self.assertEqual(problems, [], "имена без определения в модуле: " + "; ".join(problems))


if __name__ == "__main__":
    unittest.main()
