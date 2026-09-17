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

import ast
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


def _bound_at_module_level(tree: ast.Module) -> set[str]:
    """Всё, что модуль связывает сам: импорты, def, class, присваивания."""

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for item in ast.walk(target):
                    if isinstance(item, ast.Name):
                        names.add(item.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _names_in(annotation: ast.AST | None):
    if annotation is None:
        return
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        # Аннотация строкой - тоже аннотация; разбирается как выражение.
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                yield root.id


def _undefined_in_annotations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = _bound_at_module_level(tree) | set(dir(builtins))
    found: list[str] = []
    for node in ast.walk(tree):
        annotations: list[ast.AST | None] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in args.posonlyargs + args.args + args.kwonlyargs:
                annotations.append(arg.annotation)
            for extra in (args.vararg, args.kwarg):
                if extra is not None:
                    annotations.append(extra.annotation)
            annotations.append(node.returns)
            where = node.name
        elif isinstance(node, ast.AnnAssign):
            annotations.append(node.annotation)
            where = "<module>"
        else:
            continue
        for annotation in annotations:
            for name in _names_in(annotation):
                if name not in bound:
                    found.append(f"{path.name}:{where}: {name}")
    return sorted(set(found))


class UndefinedNameTests(unittest.TestCase):
    def test_every_module_resolves_its_own_names(self) -> None:
        problems: list[str] = []
        for path in sorted(SOURCE.glob("*.py")):
            problems.extend(_undefined(path))
        self.assertEqual(problems, [], "имена без определения в модуле: " + "; ".join(problems))

    def test_every_annotation_names_only_what_the_module_knows(self) -> None:
        """Аннотации - слепое пятно symtable, и оно уже стоило правки.

        При ``from __future__ import annotations`` аннотация становится
        строкой и не исполняется: symtable не видит в ней обращения, и
        имя, которое забыли импортировать, живёт в модуле незамеченным.
        Замерено 17.09: ``LaunchCheck`` в аннотации новой функции control.py
        не был импортирован - набор зелёный; тем же способом в модулях
        стояли ``Config`` и ``RunState`` без импорта, пять мест.

        NameError здесь не случится, пока аннотации не исполняют. Но
        первый же ``typing.get_type_hints`` - или снятие future-импорта -
        уронит модуль там, где тест обещал, что имён без определения нет.
        """

        problems: list[str] = []
        for path in sorted(SOURCE.glob("*.py")):
            problems.extend(_undefined_in_annotations(path))
        self.assertEqual(
            problems, [], "имена в аннотациях без определения в модуле: " + "; ".join(problems)
        )


if __name__ == "__main__":
    unittest.main()
