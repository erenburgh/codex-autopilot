"""DevOps правит код рантайма, но не объявляет починку сам.

Право чинить даётся вместе со шлюзом, и проверяется здесь именно шлюз:
что он пропускает доказанную правку и что он отклоняет каждую из
недоказанных. Тест, который не падал до правки, ничего не доказывает;
патч, задевший охранника, не спасают зелёные тесты.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.runtime_repair import (
    GUARDED_DEFINITIONS,
    RuntimeRepairError,
    RuntimeTree,
    apply_runtime_patch,
    guard_hashes,
    resolve_runtime_tree,
    revert_runtime_patch,
)

# Модуль с поломкой: складывает на единицу больше, чем следует.
ARITH = '''"""Счёт, на котором показываем починку."""


def total(items):
    return sum(items) + 1
'''

GUARDS_BASE = '''
def _require_desktop_owned(cfg):
    if not cfg:
        raise RuntimeError("desktop-owned surface required")


def _require_relay_executor(session, owner):
    if session != owner:
        raise RuntimeError("relay executor mismatch")


def _dispatcher_owns_reservation(state, token):
    return token in state
'''

# Имя переменной здесь нарочно не настоящее: тесты не читают окружение
# живой Codex-сессии, и проверка чистого окружения следит за этим по
# тексту файла - включая строки-заготовки вроде этой.
GUARD_CLI = '''
import os


def _relay_executor_thread_id():
    thread_id = str(os.environ.get("FAKE_OWNING_THREAD") or "").strip()
    if not thread_id:
        raise RuntimeError("refusing an unowned mutation")
    return thread_id
'''

BASELINE = '''
import unittest

from codex_autopilot.arith import total


class BaselineTests(unittest.TestCase):
    def test_an_empty_bill_is_not_negative(self) -> None:
        self.assertGreaterEqual(total([]), 0)
'''

REPRO = '''
import unittest

from codex_autopilot.arith import total


class ReproTests(unittest.TestCase):
    def test_two_items_add_up_to_their_sum(self) -> None:
        self.assertEqual(total([1, 2]), 3)
'''


class RuntimeRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        package = self.tmp / "src" / "codex_autopilot"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "arith.py").write_text(ARITH, encoding="utf-8")
        (package / "lifecycle_base.py").write_text(GUARDS_BASE, encoding="utf-8")
        (package / "cli.py").write_text(GUARD_CLI, encoding="utf-8")
        (package / "pipeline_engineer.py").write_text("", encoding="utf-8")
        tests = self.tmp / "tests"
        tests.mkdir()
        (tests / "test_baseline.py").write_text(BASELINE, encoding="utf-8")
        self.tree = RuntimeTree(src=self.tmp / "src", tests=tests)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def repair(self, **overrides):
        kwargs = {
            "module": "arith.py",
            "old": "return sum(items) + 1",
            "new": "return sum(items)",
            "test_name": "test_total_adds_up",
            "test_source": REPRO,
            "at": "2026-09-17T00:00:00+00:00",
            "tree": self.tree,
        }
        kwargs.update(overrides)
        return apply_runtime_patch(**kwargs)

    # --- что шлюз пропускает ------------------------------------------

    def test_a_proven_repair_reaches_the_installation(self) -> None:
        record = self.repair()
        source = (self.tree.package / "arith.py").read_text(encoding="utf-8")
        self.assertIn("return sum(items)", source)
        self.assertNotIn("+ 1", source)
        self.assertTrue(
            (self.tree.tests / "test_total_adds_up.py").is_file(),
            "тест-воспроизведение остаётся в наборе: это и есть доказательство",
        )
        self.assertNotEqual(record.sha256_before, record.sha256_after)

    def test_the_repair_can_be_taken_back(self) -> None:
        record = self.repair()
        revert_runtime_patch(record.patch_id, tree=self.tree)
        source = (self.tree.package / "arith.py").read_text(encoding="utf-8")
        self.assertIn("+ 1", source)
        self.assertFalse(
            (self.tree.tests / "test_total_adds_up.py").exists(),
            "снятая правка уносит и свой тест, иначе набор остаётся красным",
        )

    def test_a_revert_refuses_to_discard_a_later_change(self) -> None:
        record = self.repair()
        target = self.tree.package / "arith.py"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# кто-то правил после\n",
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeRepairError) as refusal:
            revert_runtime_patch(record.patch_id, tree=self.tree)
        self.assertIn("changed after this patch", str(refusal.exception))

    # --- что шлюз отклоняет -------------------------------------------

    def test_a_test_that_passes_without_the_patch_proves_nothing(self) -> None:
        passing = REPRO.replace("total([1, 2]), 3", "total([1, 2]), 4")
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(test_source=passing)
        self.assertIn("proves nothing", str(refusal.exception))
        self.assertIn("+ 1", (self.tree.package / "arith.py").read_text(encoding="utf-8"))

    def test_a_patch_that_breaks_the_rest_is_refused(self) -> None:
        """Своё доказать мало: соседнее обязано остаться целым.

        Правка ниже чинит ровно то, на что написан тест-воспроизведение,
        и ломает то, о чём он не знает, - пустой счёт уходит в минус.
        """

        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(new="return sum(items) if items else -1")
        self.assertIn("breaks the rest of the runtime", str(refusal.exception))
        self.assertIn("+ 1", (self.tree.package / "arith.py").read_text(encoding="utf-8"))

    def test_a_patch_that_touches_a_guard_is_refused(self) -> None:
        """Зелёные тесты не оправдывают снятого охранника."""

        repro = '''
import unittest

from codex_autopilot.cli import _relay_executor_thread_id


class GuardTests(unittest.TestCase):
    def test_an_unowned_mutation_is_allowed(self) -> None:
        self.assertEqual(_relay_executor_thread_id(), "anyone")
'''
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(
                module="cli.py",
                old='raise RuntimeError("refusing an unowned mutation")',
                new='return "anyone"',
                test_name="test_guard_is_gone",
                test_source=repro,
            )
        self.assertIn("guarded definitions", str(refusal.exception))
        self.assertIn(
            "refusing an unowned mutation",
            (self.tree.package / "cli.py").read_text(encoding="utf-8"),
        )

    def test_authority_and_the_gateway_itself_are_out_of_reach(self) -> None:
        for module in ("pipeline_engineer.py", "runtime_repair.py", "hook_trust.py"):
            with self.assertRaises(RuntimeRepairError) as refusal:
                self.repair(module=module)
            self.assertIn("out of reach", str(refusal.exception))

    def test_a_fragment_that_occurs_twice_is_refused(self) -> None:
        target = self.tree.package / "arith.py"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n\ndef again(items):\n    return sum(items) + 1\n",
            encoding="utf-8",
        )
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair()
        self.assertIn("occurs 2 times", str(refusal.exception))

    def test_a_fragment_that_does_not_occur_is_refused(self) -> None:
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(old="return sum(items) + 2")
        self.assertIn("does not occur", str(refusal.exception))

    def test_a_reproduction_test_must_be_named_like_a_test(self) -> None:
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(test_name="fixup")
        self.assertIn("test_<something>", str(refusal.exception))

    def test_an_existing_test_is_never_overwritten(self) -> None:
        (self.tree.tests / "test_total_adds_up.py").write_text(BASELINE, encoding="utf-8")
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair()
        self.assertIn("already exists", str(refusal.exception))


class GuardedDefinitionsTests(unittest.TestCase):
    def test_the_guards_named_here_exist_in_the_runtime(self) -> None:
        """Список охранников не должен тихо устареть.

        Если функцию переименуют, хэш считать будет нечего - и шлюз
        начнёт пропускать правки в том самом месте, которое стережёт.
        """

        tree = resolve_runtime_tree()
        hashes = guard_hashes(tree.src)
        self.assertEqual(len(hashes), len(GUARDED_DEFINITIONS))
        for module, name in GUARDED_DEFINITIONS:
            self.assertIn(f"{module}:{name}", hashes)


if __name__ == "__main__":
    unittest.main()


class InstalledLayoutTests(unittest.TestCase):
    """Установка обязана привезти то, чем починка доказывается.

    Шлюз ищет тесты рядом с исходниками и без них отказывается чинить.
    Если установщик перестанет их класть, самопочинка тихо исчезнет на
    машине пользователя, а здесь всё останется зелёным - поэтому форма
    установки проверяется отдельно.
    """

    def test_the_installer_ships_the_suite_next_to_the_sources(self) -> None:
        import re

        root = Path(__file__).resolve().parents[1]
        script = (root / "install.sh").read_text(encoding="utf-8")
        copies = dict(
            re.findall(r'cp -R "\$source_dir/([^"]+)" "\$target/([^"]+)"', script)
        )
        self.assertEqual(copies.get("src"), "runtime/src")
        self.assertEqual(
            copies.get("tests"),
            "runtime/tests",
            "без набора тестов в установке devops-repair-runtime откажет на "
            "первом же обращении: доказывать починку будет нечем",
        )

    def test_the_gateway_looks_for_the_suite_where_the_installer_puts_it(self) -> None:
        tree = resolve_runtime_tree()
        self.assertEqual(tree.tests.parent, tree.src.parent)
        self.assertEqual(tree.tests.name, "tests")
        self.assertEqual(tree.src.name, "src")
