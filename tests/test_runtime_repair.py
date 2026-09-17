"""DevOps edits the runtime's code but does not declare the repair itself.

The right to repair comes together with the gateway, and it is the
gateway that is checked here: that it admits a proven edit and refuses
every unproven one. A test that did not fail before the edit proves
nothing; green tests do not save a patch that touched a guard.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.runtime_repair import (
    GUARDED_DEFINITIONS,
    Edit,
    RuntimeRepairError,
    RuntimeTree,
    apply_runtime_patch,
    guard_hashes,
    resolve_runtime_tree,
    revert_runtime_patch,
)

# The broken module: it adds one more than it should.
ARITH = '''"""Счёт, на котором показываем починку."""


def total(items):
    return sum(items) + 1
'''

REPORT = '''from codex_autopilot.arith import total


def summary(items):
    return "total: " + str(total(items)) + " (approx)"
'''

# Stubs of the guarded definitions: the gateway compares their text, and
# without them the fake tree fails the integrity check.
ENGINEER = '''
def classify_incident(signal):
    return signal


def incident_signature(signal):
    return "signature"


def escalate_to_user(incident, reason):
    return reason


def _require_named_actions(actions):
    if not actions:
        raise RuntimeError("named action required")


def _require_passing_healthcheck(result):
    if result is None:
        raise RuntimeError("healthcheck required")
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

# The variable name here is deliberately not the real one: tests do not
# read a live Codex session's environment, and the clean-environment
# check watches for that by file text - template strings like this included.
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
        (package / "report.py").write_text(REPORT, encoding="utf-8")
        (package / "pipeline_engineer.py").write_text(ENGINEER, encoding="utf-8")
        (package / "engineer_authority.py").write_text("", encoding="utf-8")
        (package / "hook_trust.py").write_text("", encoding="utf-8")
        tests = self.tmp / "tests"
        tests.mkdir()
        (tests / "test_baseline.py").write_text(BASELINE, encoding="utf-8")
        self.tree = RuntimeTree(src=self.tmp / "src", tests=tests)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def repair(
        self,
        *,
        module: str = "arith.py",
        old: str | None = "return sum(items) + 1",
        new: str = "return sum(items)",
        edits=None,
        **overrides,
    ):
        kwargs = {
            "edits": edits
            if edits is not None
            else (Edit(module=module, old=old, new=new),),
            "test_name": "test_total_adds_up",
            "test_source": REPRO,
            "at": "2026-09-17T00:00:00+00:00",
            "tree": self.tree,
        }
        kwargs.update(overrides)
        return apply_runtime_patch(**kwargs)

    # --- what the gateway admits --------------------------------------

    def test_a_proven_repair_reaches_the_installation(self) -> None:
        record = self.repair()
        source = (self.tree.package / "arith.py").read_text(encoding="utf-8")
        self.assertIn("return sum(items)", source)
        self.assertNotIn("+ 1", source)
        self.assertTrue(
            (self.tree.tests / "test_total_adds_up.py").is_file(),
            "тест-воспроизведение остаётся в наборе: это и есть доказательство",
        )
        change = record.changes[0]
        self.assertEqual(change.module, "arith.py")
        self.assertNotEqual(change.sha256_before, change.sha256_after)

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

    # --- what the gateway refuses -------------------------------------

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
        for module in ("engineer_authority.py", "runtime_repair.py", "hook_trust.py"):
            with self.assertRaises(RuntimeRepairError) as refusal:
                self.repair(module=module)
            self.assertIn("out of reach", str(refusal.exception))

    def test_a_repair_may_span_several_modules_at_once(self) -> None:
        """Настоящая починка бывает набором, и по частям её не применить.

        Так выглядело разделение ошибки модели и поломки машины на
        прогоне v1.0: три модуля, и после любого одного из них набор
        тестов красный. Шлюз принимает набор целиком или не принимает
        вовсе.
        """

        repro = REPRO.replace(
            "from codex_autopilot.arith import total",
            "from codex_autopilot.report import summary",
        ).replace("total([1, 2]), 3", 'summary([1, 2]), "total: 3"')
        edits = (
            Edit(module="arith.py", old="return sum(items) + 1", new="return sum(items)"),
            Edit(module="report.py", old=' + " (approx)"', new=""),
        )
        record = self.repair(edits=edits, test_source=repro)
        self.assertEqual(
            sorted(change.module for change in record.changes),
            ["arith.py", "report.py"],
        )
        self.assertNotIn(
            "approx", (self.tree.package / "report.py").read_text(encoding="utf-8")
        )

    def test_half_of_a_set_is_not_applied(self) -> None:
        """Если набор не доказан, живая установка не меняется ни в чём."""

        repro = REPRO.replace(
            "from codex_autopilot.arith import total",
            "from codex_autopilot.report import summary",
        ).replace("total([1, 2]), 3", 'summary([1, 2]), "total: 3"')
        edits = (
            Edit(module="arith.py", old="return sum(items) + 1", new="return sum(items)"),
        )
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(edits=edits, test_source=repro)
        self.assertIn("still fails with the patch applied", str(refusal.exception))
        self.assertIn("+ 1", (self.tree.package / "arith.py").read_text(encoding="utf-8"))

    def test_a_repair_may_add_a_new_module(self) -> None:
        """Иногда починка - это вынести код в новый файл.

        Так снимали формат v0.8: прежний модуль упёрся в потолок
        размера, и правка потребовала отдельного файла.
        """

        edits = (
            Edit(module="helpers.py", old=None, new="def exact(items):\n    return sum(items)\n"),
            Edit(
                module="arith.py",
                old="return sum(items) + 1",
                new="from codex_autopilot.helpers import exact\n\n    return exact(items)",
            ),
        )
        record = self.repair(edits=edits)
        created = next(item for item in record.changes if item.module == "helpers.py")
        self.assertIsNone(created.sha256_before, "новый модуль не имеет прошлого текста")
        self.assertTrue((self.tree.package / "helpers.py").is_file())

    def test_a_reverted_set_takes_the_new_module_with_it(self) -> None:
        edits = (
            Edit(module="helpers.py", old=None, new="def exact(items):\n    return sum(items)\n"),
            Edit(
                module="arith.py",
                old="return sum(items) + 1",
                new="from codex_autopilot.helpers import exact\n\n    return exact(items)",
            ),
        )
        record = self.repair(edits=edits)
        revert_runtime_patch(record.patch_id, tree=self.tree)
        self.assertFalse((self.tree.package / "helpers.py").exists())
        self.assertIn("+ 1", (self.tree.package / "arith.py").read_text(encoding="utf-8"))

    def test_a_new_module_never_overwrites_an_existing_one(self) -> None:
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(module="report.py", old=None, new="# подменяю целиком\n")
        self.assertIn("already exists", str(refusal.exception))

    def test_the_bookkeeping_of_incidents_is_repairable(self) -> None:
        """Инженер вправе чинить свой же модуль - кроме своих полномочий."""

        repro = '''
import unittest

from codex_autopilot.pipeline_engineer import incident_note


class NoteTests(unittest.TestCase):
    def test_a_note_is_returned(self) -> None:
        self.assertEqual(incident_note(), "noted")
'''
        record = self.repair(
            module="pipeline_engineer.py",
            old="def classify_incident(signal):",
            new='def incident_note():\n    return "noted"\n\n\ndef classify_incident(signal):',
            test_name="test_incident_note",
            test_source=repro,
        )
        self.assertEqual(record.changes[0].module, "pipeline_engineer.py")

    def test_a_guarded_definition_inside_that_module_is_still_untouchable(self) -> None:
        repro = '''
import unittest

from codex_autopilot.pipeline_engineer import _require_named_actions


class ActionTests(unittest.TestCase):
    def test_no_action_is_fine_now(self) -> None:
        self.assertIsNone(_require_named_actions([]))
'''
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(
                module="pipeline_engineer.py",
                old='        raise RuntimeError("named action required")',
                new="        return None",
                test_name="test_actions_are_optional",
                test_source=repro,
            )
        self.assertIn("guarded definitions", str(refusal.exception))

    # --- the reviewer's findings: duplicate, decorator, case, set ------

    def test_a_duplicate_of_a_guard_appended_after_it_is_refused(self) -> None:
        """Python исполняет последнее определение; первое - лишь текст."""

        repro = '''
import unittest

from codex_autopilot.cli import _relay_executor_thread_id


class GuardTests(unittest.TestCase):
    def test_the_second_definition_wins(self) -> None:
        self.assertEqual(_relay_executor_thread_id(), "anyone")
'''
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(
                module="cli.py",
                old="    return thread_id\n",
                new='    return thread_id\n\n\ndef _relay_executor_thread_id():\n    return "anyone"\n',
                test_name="test_duplicate_guard",
                test_source=repro,
            )
        self.assertIn("occurs 2 times", str(refusal.exception))
        self.assertEqual(
            (self.tree.package / "cli.py").read_text(encoding="utf-8").count("def _relay_executor_thread_id"),
            1,
        )

    def test_a_decorator_wrapped_around_a_guard_is_refused(self) -> None:
        """Обёртка над охранником - та же правка охранника."""

        repro = '''
import unittest

from codex_autopilot.cli import _relay_executor_thread_id


class GuardTests(unittest.TestCase):
    def test_wrapped(self) -> None:
        self.assertEqual(_relay_executor_thread_id(), "anyone")
'''
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(
                module="cli.py",
                old="def _relay_executor_thread_id():",
                new='def _anyone(fn):\n    return lambda: "anyone"\n\n\n@_anyone\ndef _relay_executor_thread_id():',
                test_name="test_decorated_guard",
                test_source=repro,
            )
        self.assertIn("guarded definitions", str(refusal.exception))

    def test_an_unpatchable_module_cannot_be_reached_by_a_case_change(self) -> None:
        """Файловая система установки не различает регистр."""

        for spelling in ("Hook_Trust.py", "HOOK_TRUST.py", "Runtime_Repair.py"):
            with self.assertRaises(RuntimeRepairError) as refusal:
                self.repair(module=spelling)
            self.assertIn("lower case", str(refusal.exception), spelling)

    def test_a_set_that_passes_its_own_test_but_breaks_a_neighbour_is_refused(self) -> None:
        """Ровно главный случай: набор, а не одиночная правка."""

        repro = REPRO.replace(
            "from codex_autopilot.arith import total",
            "from codex_autopilot.report import summary",
        ).replace("total([1, 2]), 3", 'summary([1, 2]), "total: 3"')
        edits = (
            Edit(module="arith.py", old="return sum(items) + 1", new="return sum(items) if items else -1"),
            Edit(module="report.py", old=' + " (approx)"', new=""),
        )
        with self.assertRaises(RuntimeRepairError) as refusal:
            self.repair(edits=edits, test_source=repro)
        self.assertIn("breaks the rest of the runtime", str(refusal.exception))
        self.assertIn("test_an_empty_bill_is_not_negative", str(refusal.exception))
        self.assertIn("+ 1", (self.tree.package / "arith.py").read_text(encoding="utf-8"))
        self.assertIn("approx", (self.tree.package / "report.py").read_text(encoding="utf-8"))

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
        # The runtime is copied as a repository-shaped tree in one loop; it
        # must hold the sources, the tests and everything the tests prove with.
        loop = re.search(r"for item in ([^;\n]+); do\n\s*\[ -e \"\$source_dir/\$item\" \] && cp -R \"\$source_dir/\$item\" \"\$target/runtime/\$item\"", script)
        self.assertIsNotNone(loop, "установщик не копирует дерево рантайма циклом по элементам")
        items = loop.group(1).split()
        for required in ("src", "tests", "plugins", "scripts", "pyproject.toml"):
            self.assertIn(
                required,
                items,
                f"без {required} в установке набор тестов красный, и devops-repair-runtime "
                "откажет на первом же обращении: доказывать починку будет нечем",
            )

    def test_the_gateway_looks_for_the_suite_where_the_installer_puts_it(self) -> None:
        tree = resolve_runtime_tree()
        self.assertEqual(tree.tests.parent, tree.src.parent)
        self.assertEqual(tree.tests.name, "tests")
        self.assertEqual(tree.src.name, "src")


class RepairCommandTests(unittest.TestCase):
    """Команда devops-repair-runtime: порядок и разбор набора.

    Проверяющая сломала чтение old_file (все правки считались новым
    модулем) - всё осталось зелёным: путь команды не исполнялся нигде.
    И порядок был неверным: правка применялась к установке ДО проверки
    тикета, так что чужой или закрытый тикет оставлял её применённой и
    нигде не записанной.
    """

    def setUp(self) -> None:
        import json
        from unittest import mock

        from _gates import patch_hook_trust_gates
        from _plan_contract import initialize_verified_project as initialize_project
        from codex_autopilot.config import load_config
        from codex_autopilot.pipeline_engineer import (
            IncidentClass,
            IncidentSignal,
            PipelineIncidentStore,
            SideEffectOutcome,
        )
        from test_desktop_lifecycle import graph, task

        patch_hook_trust_gates(self)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        (self.tmp / ".git").mkdir()
        skill = self.tmp / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        raw = graph(max_workers=1)
        raw["tasks"] = [task("A", path="src/a")]
        plan_file = self.tmp / "input-plan.json"
        plan_file.write_text(json.dumps(raw), encoding="utf-8")
        initialize_project(self.tmp, plan_file, profile="adaptive", skill_path=skill, desktop_project_id="desktop-project")
        self.cfg = load_config(self.tmp)
        self.store = PipelineIncidentStore(self.cfg.state_dir)
        incident = self.store.open_incident(
            IncidentSignal(
                signal_id="s-1",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                summary="dispatcher died",
                affected_task_ids=("A",),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
            ),
            at="t1",
        )
        self.incident_id = incident["incident_id"]
        # A patch set with one edit of an existing module and one new module.
        (self.tmp / "old.txt").write_text("OLD FRAGMENT", encoding="utf-8")
        (self.tmp / "new.txt").write_text("NEW FRAGMENT", encoding="utf-8")
        (self.tmp / "fresh.py").write_text("X = 1\n", encoding="utf-8")
        (self.tmp / "patch.json").write_text(json.dumps({"edits": [
            {"module": "status.py", "old_file": str(self.tmp / "old.txt"), "new_file": str(self.tmp / "new.txt")},
            {"module": "fresh_module.py", "new_file": str(self.tmp / "fresh.py")},
        ]}), encoding="utf-8")
        (self.tmp / "test_repro.py").write_text("import unittest\n", encoding="utf-8")
        self.applied: list[dict] = []
        self.mock = mock

    def run_command(self):
        from codex_autopilot import cli
        from codex_autopilot.runtime_repair import ModuleChange, PatchRecord

        def fake_apply(**kwargs):
            self.applied.append(kwargs)
            return PatchRecord(
                patch_id="patch-test",
                changes=(ModuleChange(module="status.py", sha256_before="a", sha256_after="b"),),
                test_name=kwargs["test_name"],
                at=kwargs["at"],
            )

        with self.mock.patch.object(cli, "_relay_executor_thread_id", return_value="owner"), \
             self.mock.patch("codex_autopilot.runtime_repair.apply_runtime_patch", side_effect=fake_apply):
            return cli.main([
                "devops-repair-runtime", "--project", str(self.tmp),
                "--incident-id", self.incident_id,
                "--patch-file", str(self.tmp / "patch.json"),
                "--test-file", str(self.tmp / "test_repro.py"),
                "--test-name", "test_repro",
            ])

    def test_the_patch_file_becomes_edits_with_their_fragments(self) -> None:
        self.store.ensure_pipeline_engineer(self.incident_id, at="t2")
        self.assertEqual(self.run_command(), 0)
        self.assertEqual(len(self.applied), 1)
        edits = {edit.module: edit for edit in self.applied[0]["edits"]}
        self.assertEqual(edits["status.py"].old, "OLD FRAGMENT")
        self.assertEqual(edits["status.py"].new, "NEW FRAGMENT")
        self.assertIsNone(edits["fresh_module.py"].old, "без old_file - новый модуль")
        self.assertEqual(edits["fresh_module.py"].new, "X = 1\n")
        incident = next(item for item in self.store.load()["incidents"] if item["incident_id"] == self.incident_id)
        self.assertEqual(incident["runtime_patches"][0]["patch_id"], "patch-test")

    def test_a_ticket_not_held_by_the_engineer_stops_the_repair_before_it_is_applied(self) -> None:
        """Тикет проверяется до правки: чужой тикет ничего не меняет."""

        # main turns a refusal into exit code 2 and a line on stderr, not an exception.
        self.assertEqual(self.run_command(), 2)
        self.assertEqual(self.applied, [], "правка применилась до проверки тикета")


if __name__ == "__main__":
    unittest.main()
