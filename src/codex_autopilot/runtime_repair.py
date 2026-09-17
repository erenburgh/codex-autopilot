"""Ограниченная починка рантайма самим рантаймом.

Задача перестаёт двигаться не только из-за проекта. Она встаёт из-за
самого рантайма: ветка ушла не в ту фазу, ответ воркера не разобрался,
диспетчер упал на собственной ошибке. До сих пор это чинил человек - и
значит, у пользователя без такого человека прогон просто стоял.

Здесь дежурный инженер получает право править код рантайма, но не право
объявлять починку. Правка проходит шлюз: тест-воспроизведение обязан
УПАСТЬ на нынешнем коде и ПРОЙТИ на исправленном, весь набор тестов
обязан остаться зелёным, а охранные функции - побайтно теми же. Если
что-то из этого не так, живой установки правка не касается вовсе.

Почему это работает без перезапуска: каждый ход диспетчера - отдельный
процесс `python -m codex_autopilot.cli`, он читает исходники заново.
Правка действует со следующего хода, и ничего не переустанавливается
поверх работающего.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


class RuntimeRepairError(Exception):
    """Правка не прошла шлюз и до живой установки не дошла."""


# Модули, которые инженер не правит никогда. Полномочия и доверие не
# меняет тот, кто ими пользуется, а шлюз не переписывает сам себя -
# иначе первая же правка снимает все остальные проверки.
UNPATCHABLE_MODULES = frozenset(
    {
        "pipeline_engineer.py",
        "runtime_repair.py",
        "hook_trust.py",
    }
)

# Функции, текст которых обязан пережить любую правку без изменений.
# Это те самые охранники, что отказывали по делу: владение веткой,
# владение резервацией, принадлежность поверхности. Патч, задевший
# любую из них, отклоняется целиком - даже когда тесты зелёные.
GUARDED_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("lifecycle_base.py", "_require_desktop_owned"),
    ("lifecycle_base.py", "_require_relay_executor"),
    ("lifecycle_base.py", "_dispatcher_owns_reservation"),
    ("cli.py", "_relay_executor_thread_id"),
)

TEST_TIMEOUT_SECONDS = 900


@dataclass(frozen=True, slots=True)
class RuntimeTree:
    """Где лежит правимый рантайм и его доказательства."""

    src: Path
    tests: Path

    @property
    def package(self) -> Path:
        return self.src / "codex_autopilot"

    @property
    def root(self) -> Path:
        return self.src.parent


@dataclass(frozen=True, slots=True)
class PatchRecord:
    patch_id: str
    module: str
    sha256_before: str
    sha256_after: str
    test_name: str
    at: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def resolve_runtime_tree(*, module_file: str | None = None) -> RuntimeTree:
    """Дерево рантайма по собственному расположению этого модуля.

    Одинаково работает в установке (<...>/runtime/src, <...>/runtime/tests)
    и в рабочем дереве (repo/src, repo/tests): и там, и там тесты лежат
    рядом с исходниками. Без тестов чинить нельзя - доказать, что правка
    не сломала соседнее, будет нечем.
    """

    here = Path(module_file or __file__).absolute()
    src = here.parents[1]
    tests = src.parent / "tests"
    if not (src / "codex_autopilot").is_dir():
        raise RuntimeRepairError(f"runtime sources are not where expected: {src}")
    if not tests.is_dir():
        raise RuntimeRepairError(
            "this installation ships no test suite, so a repair cannot be proven; "
            f"expected {tests}"
        )
    return RuntimeTree(src=src, tests=tests)


def guard_hashes(src: Path) -> dict[str, str]:
    """Хэши текста охранных функций - тождество, которое нельзя тронуть."""

    hashes: dict[str, str] = {}
    for module, name in GUARDED_DEFINITIONS:
        text = (src / "codex_autopilot" / module).read_text(encoding="utf-8")
        node = _definition(text, name)
        if node is None:
            raise RuntimeRepairError(f"guarded definition disappeared: {module}:{name}")
        body = ast.get_source_segment(text, node) or ""
        hashes[f"{module}:{name}"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return hashes


def _definition(text: str, name: str) -> ast.AST | None:
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def apply_runtime_patch(
    *,
    module: str,
    old: str,
    new: str,
    test_name: str,
    test_source: str,
    at: str,
    tree: RuntimeTree | None = None,
) -> PatchRecord:
    """Провести правку через шлюз и применить её к живой установке."""

    tree = tree or resolve_runtime_tree()
    target = _target_path(tree, module)
    before = target.read_text(encoding="utf-8")
    patched = _replace_once(before, old, new, module=module)
    _validate_test_name(test_name)
    if (tree.tests / f"{test_name}.py").exists():
        raise RuntimeRepairError(
            f"{test_name}.py already exists: a repair proves itself with its own test, "
            "it does not overwrite someone else's"
        )

    staging = Path(tempfile.mkdtemp(prefix="codex-autopilot-repair-"))
    try:
        shutil.copytree(tree.src, staging / "src")
        shutil.copytree(tree.tests, staging / "tests")
        (staging / "tests" / f"{test_name}.py").write_text(test_source, encoding="utf-8")

        if _run_one_test(staging, test_name).returncode == 0:
            raise RuntimeRepairError(
                "the reproduction test passes on the current code, so it proves nothing: "
                "a repair is accepted only for a failure that can be shown first"
            )

        staged_module = staging / "src" / "codex_autopilot" / module
        try:
            ast.parse(patched)
        except SyntaxError as exc:
            raise RuntimeRepairError(f"the patched module does not parse: {exc}") from exc
        staged_module.write_text(patched, encoding="utf-8")

        after = _run_one_test(staging, test_name)
        if after.returncode != 0:
            raise RuntimeRepairError(
                "the reproduction test still fails with the patch applied:\n" + _tail(after)
            )

        suite = _run_suite(staging)
        if suite.returncode != 0:
            raise RuntimeRepairError(
                "the patch breaks the rest of the runtime:\n" + _tail(suite)
            )

        expected = guard_hashes(tree.src)
        observed = guard_hashes(staging / "src")
        touched = sorted(key for key, value in expected.items() if observed.get(key) != value)
        if touched:
            raise RuntimeRepairError(
                "the patch changes guarded definitions and is refused: " + ", ".join(touched)
            )

        record = PatchRecord(
            patch_id=_patch_id(module, old, new, at),
            module=module,
            sha256_before=_sha256(before),
            sha256_after=_sha256(patched),
            test_name=test_name,
            at=at,
        )
        _store_backup(tree, record, original=before)
        target.write_text(patched, encoding="utf-8")
        (tree.tests / f"{test_name}.py").write_text(test_source, encoding="utf-8")
        return record
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def revert_runtime_patch(patch_id: str, *, tree: RuntimeTree | None = None) -> PatchRecord:
    """Вернуть модуль к тексту до правки вместе с её тестом."""

    tree = tree or resolve_runtime_tree()
    folder = tree.root / "patches" / patch_id
    manifest = folder / "patch.json"
    if not manifest.is_file():
        raise RuntimeRepairError(f"unknown patch {patch_id}")
    record = PatchRecord(**json.loads(manifest.read_text(encoding="utf-8")))
    target = _target_path(tree, record.module)
    if _sha256(target.read_text(encoding="utf-8")) != record.sha256_after:
        raise RuntimeRepairError(
            "the module changed after this patch was applied; reverting would silently "
            "discard that later change"
        )
    target.write_text(
        (folder / f"{record.module}.orig").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tree.tests / f"{record.test_name}.py").unlink(missing_ok=True)
    return record


def _target_path(tree: RuntimeTree, module: str) -> Path:
    if module != Path(module).name or not module.endswith(".py"):
        raise RuntimeRepairError(f"a repair names one module of the runtime, not {module!r}")
    if module in UNPATCHABLE_MODULES:
        raise RuntimeRepairError(
            f"{module} is out of reach for a repair: authority, trust and this gateway "
            "are not rewritten by the one who uses them"
        )
    target = tree.package / module
    if not target.is_file():
        raise RuntimeRepairError(f"no such runtime module: {module}")
    return target


def _replace_once(text: str, old: str, new: str, *, module: str) -> str:
    if not old.strip():
        raise RuntimeRepairError("the replaced fragment must not be empty")
    if old == new:
        raise RuntimeRepairError("the patch changes nothing")
    found = text.count(old)
    if found == 0:
        raise RuntimeRepairError(f"the fragment does not occur in {module}")
    if found > 1:
        raise RuntimeRepairError(
            f"the fragment occurs {found} times in {module}; a repair must name one place"
        )
    return text.replace(old, new)


def _validate_test_name(test_name: str) -> None:
    if not test_name.startswith("test_") or not test_name.replace("_", "").isalnum():
        raise RuntimeRepairError(
            f"the reproduction test must be named test_<something>, not {test_name!r}"
        )


def _run_one_test(staging: Path, test_name: str) -> subprocess.CompletedProcess[str]:
    return _run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", f"{test_name}.py"],
        staging,
    )


def _run_suite(staging: Path) -> subprocess.CompletedProcess[str]:
    return _run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], staging)


def _run(command: list[str], staging: Path) -> subprocess.CompletedProcess[str]:
    """Прогон в копии и без прав на живой прогон.

    Тест пишет инженер, то есть это его код. Он выполняется в копии, а
    окружение чистится от CODEX_*: без CODEX_THREAD_ID любая попытка
    тронуть живой прогон упрётся в того же охранника, что и всегда.
    """

    home = staging / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("CODEX_")}
    env.update(
        {
            "PYTHONPATH": str(staging / "src"),
            "HOME": str(home),
            "TMPDIR": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return subprocess.run(
        command,
        cwd=staging,
        env=env,
        capture_output=True,
        text=True,
        timeout=TEST_TIMEOUT_SECONDS,
    )


def _store_backup(tree: RuntimeTree, record: PatchRecord, *, original: str) -> None:
    folder = tree.root / "patches" / record.patch_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{record.module}.orig").write_text(original, encoding="utf-8")
    (folder / "patch.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _patch_id(module: str, old: str, new: str, at: str) -> str:
    digest = hashlib.sha256("\n".join((module, old, new, at)).encode("utf-8")).hexdigest()
    return f"patch-{digest[:16]}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tail(result: subprocess.CompletedProcess[str], *, limit: int = 2_000) -> str:
    return ((result.stdout or "") + (result.stderr or ""))[-limit:]
