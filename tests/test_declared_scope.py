"""Правило R7: работа не выходит за объявленную область."""

from __future__ import annotations

from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest import mock

from _gates import patch_hook_trust_gates

from codex_autopilot.plan import ResourceClaim, Task, TaskContext, VerificationPolicy
from codex_autopilot.scope import (
    ScopeNotObservable,
    audit_declared_scope,
    observe_changed_paths,
    scope_baseline,
)


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(root), *args), check=True, capture_output=True)


def task_with(claims: tuple[ResourceClaim, ...]) -> Task:
    return Task(
        id="T1",
        title="t",
        objective="o",
        definition_of_done=("d",),
        execution_mode="implement",
        execution_mode_reason="r",
        reasoning=None,
        role="engineer",
        depends_on=(),
        priority=1,
        verification=VerificationPolicy(policy="deterministic", required=True),
        resources=claims,
        required_capabilities=(),
        context=TaskContext(),
        outputs=(),
        tags=(),
    )


class ObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def init_repo(self) -> str:
        git(self.root, "init", "--initial-branch=main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "seed")
        baseline = scope_baseline(self.root)
        assert baseline
        return baseline

    def test_unobservable_project_is_reported_not_passed(self) -> None:
        """Отсутствие наблюдения - не чистый результат, а отказ проверки."""

        with self.assertRaises(ScopeNotObservable):
            observe_changed_paths(self.root, None)

    def test_observation_sees_modified_committed_and_untracked(self) -> None:
        baseline = self.init_repo()
        (self.root / "seed.txt").write_text("changed\n", encoding="utf-8")
        (self.root / "fresh.txt").write_text("new\n", encoding="utf-8")
        observed = observe_changed_paths(self.root, baseline)
        self.assertEqual(
            observed,
            (str(self.root / "fresh.txt"), str(self.root / "seed.txt")),
        )

    def test_runtime_state_is_not_counted_as_worker_work(self) -> None:
        """.codex-autopilot пишет сам автопилот, а не воркер."""

        baseline = self.init_repo()
        state = self.root / ".codex-autopilot" / "handoff"
        state.mkdir(parents=True)
        (state / "T1.md").write_text("handoff\n", encoding="utf-8")
        self.assertEqual(observe_changed_paths(self.root, baseline), ())


class DeclaredScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path("/project").resolve()

    def audit(self, claims, changed):
        return audit_declared_scope(
            task_with(claims), tuple(changed), project_root=self.root
        )

    def test_empty_declaration_forbids_every_change(self) -> None:
        """В живом прогоне v0.9 resources был пуст у ВСЕХ задач.

        Поэтому выйти за область было невозможно по построению, и воркеры
        правили что угодно. Пустая заявка обязана давать дефект.
        """

        violations = self.audit((), [str(self.root / "src/a.py")])
        self.assertEqual(len(violations), 1)
        self.assertIn("R7", violations[0])
        self.assertIn("не объявила ни одной файловой заявки", violations[0])

    def test_change_inside_declared_directory_is_clean(self) -> None:
        claim = ResourceClaim(id="c1", kind="directory", target="src", access="write")
        self.assertEqual(self.audit((claim,), [str(self.root / "src/a.py")]), [])

    def test_change_outside_declared_directory_is_reported(self) -> None:
        claim = ResourceClaim(id="c1", kind="directory", target="src", access="write")
        violations = self.audit((claim,), [str(self.root / "docs/readme.md")])
        self.assertEqual(len(violations), 1)
        self.assertIn("вне объявленной", violations[0])
        self.assertIn("PLAN_CHANGE_REQUEST", violations[0])

    def test_read_access_does_not_authorize_a_change(self) -> None:
        """Прочитать файл можно, менять его - нет."""

        claim = ResourceClaim(id="c1", kind="directory", target="src", access="read")
        violations = self.audit((claim,), [str(self.root / "src/a.py")])
        self.assertEqual(len(violations), 1)

    def test_glob_claim_matches(self) -> None:
        claim = ResourceClaim(id="c1", kind="glob", target="src/*.py", access="write")
        self.assertEqual(self.audit((claim,), [str(self.root / "src/a.py")]), [])
        self.assertEqual(len(self.audit((claim,), [str(self.root / "src/deep/a.py")])), 1)


class ScopeIsCheckedOnCompletionTests(unittest.TestCase):
    """R19: правило считается реализованным, только если живой путь его зовёт.

    Поэтому проверка ведётся не на вызове аудита напрямую, а через
    настоящее завершение задачи в настоящем git-репозитории.
    """

    def setUp(self) -> None:
        from codex_autopilot.bootstrap import initialize_project
        from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
        from codex_autopilot.memory import ProjectMemory
        from test_desktop_lifecycle import graph

        gate = mock.patch(
            "codex_autopilot.lifecycle_reservations.require_trusted_stop_hook_for_config"
        )
        gate.start()
        self.addCleanup(gate.stop)
        # Гейт доверия хукам читает НАСТОЯЩИЙ App Server машины. Без этой
        # подстановки набор проходил только потому, что у разработчика хуки
        # оказались доверены, и рушился сразу после переустановки плагина.
        patch_hook_trust_gates(self)
        dispatch = mock.patch(
            "codex_autopilot.control.spawn_automatic_app_server_relay", return_value=4242
        )
        dispatch.start()
        self.addCleanup(dispatch.stop)

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        git(self.root, "init", "--initial-branch=main")
        git(self.root, "config", "user.email", "t@example.com")
        git(self.root, "config", "user.name", "t")
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(graph()), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        # Всё, что существует до старта задачи, обязано быть в истории:
        # иначе чужие файлы попадут в диф и подтвердят правило ложно.
        git(self.root, "add", "-A")
        git(self.root, "commit", "-m", "project")
        self.cfg = load_config(self.root)
        self.memory = ProjectMemory(self.root)

    def run_task_a(self, write: Path, *, rules_line: str | None = None) -> None:
        from _appserver_fakes import activate_via_app_server
        from _handoff import bump_task_checkpoint
        from _relay import reserve_ready_frontier
        from codex_autopilot.lifecycle import complete_desktop_worker

        descriptors = reserve_ready_frontier(self.cfg)
        descriptor = next(item for item in descriptors if item.task_id == "A")
        activate_via_app_server(self.cfg, self.root, descriptor, "thread-a")
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text("work\n", encoding="utf-8")
        bump_task_checkpoint(self.root, "A", "Completed: A")
        self.memory.record_evidence(
            kind="test",
            summary="A passed.",
            created_by="scope-test",
            milestone_id="A",
            command="verify A",
            result="PASS",
            exit_code=0,
        )
        complete_desktop_worker(
            self.cfg,
            thread_id="thread-a",
            turn_id="turn-a",
            final_message=(
                f"{rules_line}\nAUTOPILOT_STATUS: ROTATE"
                if rules_line
                else "AUTOPILOT_STATUS: ROTATE"
            ),
        )

    def journal_events(self, name: str) -> list[dict]:
        from codex_autopilot.run_state import StateStore

        state = StateStore(self.cfg.state_dir).load()
        return [item for item in state.lifecycle_journal if item.get("event") == name]

    def test_change_outside_the_declared_area_is_recorded_on_completion(self) -> None:
        from codex_autopilot.rules import violation_counts

        self.run_task_a(self.root / "stray.py")
        recorded = self.journal_events("scope_violation_recorded")
        self.assertEqual(len(recorded), 1)
        self.assertIn("stray.py", recorded[0]["detail"])
        self.assertEqual(violation_counts(self.cfg.state_dir).get("R7"), 1)
        self.assertEqual(self.journal_events("scope_not_observed"), [])

    def test_change_inside_the_declared_area_is_clean(self) -> None:
        from codex_autopilot.rules import violation_counts

        self.run_task_a(self.root / "src" / "a" / "impl.py")
        self.assertEqual(self.journal_events("scope_violation_recorded"), [])
        self.assertEqual(violation_counts(self.cfg.state_dir).get("R7"), None)

    def test_r16_report_without_applied_rules_is_recorded(self) -> None:
        from codex_autopilot.rules import violation_counts

        self.run_task_a(self.root / "src" / "a" / "impl.py")
        recorded = self.journal_events("rule_declaration_missing")
        self.assertEqual(len(recorded), 1)
        self.assertIn("AUTOPILOT_RULES", recorded[0]["detail"])
        self.assertEqual(violation_counts(self.cfg.state_dir).get("R16"), 1)

    def test_r16_report_listing_applied_rules_is_clean(self) -> None:
        from codex_autopilot.rules import violation_counts

        self.run_task_a(
            self.root / "src" / "a" / "impl.py", rules_line="AUTOPILOT_RULES: R7, R16"
        )
        self.assertEqual(self.journal_events("rule_declaration_missing"), [])
        self.assertEqual(violation_counts(self.cfg.state_dir).get("R16"), None)

    def test_r16_report_citing_a_rule_that_does_not_exist_is_recorded(self) -> None:
        """Иначе правило "соблюдается" ссылкой на то, чего нет."""

        self.run_task_a(
            self.root / "src" / "a" / "impl.py", rules_line="AUTOPILOT_RULES: R999"
        )
        recorded = self.journal_events("rule_declaration_missing")
        self.assertEqual(len(recorded), 1)
        self.assertIn("R999", recorded[0]["detail"])


if __name__ == "__main__":
    unittest.main()
