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

import ast
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


def _bound_at_module_level(tree: ast.Module) -> set[str]:
    """Everything the module binds itself: imports, def, class, assignments."""

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
        # A string annotation is an annotation too; it parses as an expression.
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
        """Annotations are a blind spot of symtable; it already cost a fix.

        With ``from __future__ import annotations`` an annotation becomes
        a string and is not executed: symtable sees no reference in it,
        and a name someone forgot to import lives on in the module
        unnoticed. Measured on 17 Sep: ``LaunchCheck`` in the annotation
        of a new function in control.py was not imported - the suite was
        green; the same way ``Config`` and ``RunState`` stood in modules
        without an import, five places.

        No NameError happens here while annotations are not executed. But
        the first ``typing.get_type_hints`` - or dropping the future
        import - breaks the module exactly where the test promised there
        are no undefined names.
        """

        problems: list[str] = []
        for path in sorted(SOURCE.glob("*.py")):
            problems.extend(_undefined_in_annotations(path))
        self.assertEqual(
            problems, [], "имена в аннотациях без определения в модуле: " + "; ".join(problems)
        )


if __name__ == "__main__":
    unittest.main()
