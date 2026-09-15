"""Имя, которого нет в модуле, не должно доезжать до пользователя.

Дважды подряд одно и то же: строка обращалась к имени, которое забыли
импортировать, тесты были зелёными, а NameError случался у человека -
один раз в самом конце успешного preflight, сразу после выданного
разрешения, второй раз в пути восстановления девопса.

Тесты покрывают не каждую ветку и покрывать не обязаны. Но имя,
которого в модуле нет, находится без запуска - разбором, - и такой
проверке место в наборе.
"""

from __future__ import annotations

import builtins
from pathlib import Path
import symtable
import unittest


SOURCE = Path(__file__).resolve().parent.parent / "src" / "codex_autopilot"
# Имена, которые интерпретатор кладёт в модуль сам.
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
