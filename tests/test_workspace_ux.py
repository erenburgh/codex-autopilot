from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_autopilot.config import Config, DesktopConfig, RetryConfig, RuntimeConfig
from codex_autopilot.plan import validate_plan
from codex_autopilot.project_association import (
    ProjectAssociationError,
    match_saved_project,
    resolve_preflight_project,
)
from codex_autopilot.run_state import RunState
from codex_autopilot.status import project_status_snapshot, render_project_status
from codex_autopilot.thread_titles import (
    MAX_THREAD_TITLE_CHARS,
    ThreadTitleError,
    implementation_thread_title,
    planner_thread_title,
    replanner_thread_title,
    revision_thread_title,
    task_phase_thread_title,
    verifier_thread_title,
)


def task(task_id: str, *, depends_on: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Complete {task_id}.",
        "definition_of_done": [f"{task_id} is complete."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository checks are sufficient.",
        "reasoning": "medium",
        "role": "builder",
        "depends_on": list(depends_on),
        "priority": 0,
        "verification": {
            "policy": "self",
            "required": True,
            "max_revision_attempts": 1,
        },
        "resources": [],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
    }


def plan():
    return validate_plan(
        {
            "schema_version": 3,
            "graph_version": 1,
            "goal": "Exercise workspace UX.",
            "user_request": "Show precise human roles throughout the workspace UX.",
            "model_strategy": "auto",
            "execution_strategy": "parallel",
            "max_parallel_workers": 3,
            "computer_use_slots": 1,
            "roles": [
                {
                    "id": "builder",
                    "name": "Builder",
                    "responsibilities": ["Complete one task."],
                }
            ],
            "tasks": [
                task("T44"),
                task("T45"),
                task("T46"),
                task("T47", depends_on=("T44",)),
            ],
        },
        "adaptive",
    )


class ThreadTitleTests(unittest.TestCase):
    def test_exact_phase_titles_are_human_readable_and_stable(self) -> None:
        title = "Normalize workspace metadata"
        self.assertEqual(
            implementation_thread_title("T44", title, role_name="Resilience Engineer"),
            "Resilience Engineer | T44 | Normalize workspace metadata",
        )
        self.assertEqual(
            verifier_thread_title("T44", title, role_name="Independent Reviewer"),
            "Independent Reviewer Verifier | T44 | Verify workspace metadata",
        )
        self.assertEqual(
            revision_thread_title("T44", 1, title, role_name="Resilience Engineer"),
            "Resilience Engineer | T44-R1 | Revise workspace metadata",
        )
        self.assertEqual(
            planner_thread_title("Build dependency-aware runtime"),
            "Planner | PLAN | Build dependency-aware runtime",
        )
        self.assertEqual(
            replanner_thread_title("PC7", "Add a prerequisite audit"),
            "Planner | PC-7 | Add a prerequisite audit",
        )

    def test_title_normalization_is_bounded_without_uuid_or_project_prefix(self) -> None:
        result = implementation_thread_title(
            "T44",
            "  A   very long " + "task " * 40,
            role_name="Resilience Engineer",
        )
        self.assertLessEqual(len(result), MAX_THREAD_TITLE_CHARS)
        self.assertTrue(
            result.startswith("Resilience Engineer | T44 | A very long task")
        )
        self.assertTrue(result.endswith("…"))
        self.assertNotIn("Codex Autopilot", result)

    def test_production_title_dispatch_rejects_a_missing_role(self) -> None:
        with self.assertRaises(TypeError):
            task_phase_thread_title(  # type: ignore[call-arg]
                task_id="T44",
                task_title="Normalize workspace metadata",
                kind="implementation",
            )
        with self.assertRaisesRegex(ThreadTitleError, "role_name must be non-empty"):
            task_phase_thread_title(
                task_id="T44",
                task_title="Normalize workspace metadata",
                kind="implementation",
                role_name=" ",
            )


class ProjectAssociationTests(unittest.TestCase):
    def test_explicit_target_project_wins_over_longest_root_match(self) -> None:
        target = Path("/workspace/product/service")
        projects = [
            {"id": "outer", "roots": [{"path": "/workspace"}]},
            {"id": "inner", "roots": [{"path": "/workspace/product"}]},
        ]
        selected, source = resolve_preflight_project(
            target,
            projects,
            explicit_project_id="outer",
        )
        self.assertEqual(selected["id"], "outer")
        self.assertEqual(source, "explicit target")

    def test_automatic_match_uses_unique_longest_saved_root(self) -> None:
        projects = [
            {"id": "outer", "roots": [{"path": "/workspace"}]},
            {"id": "inner", "roots": [{"path": "/workspace/product"}]},
        ]
        self.assertEqual(
            match_saved_project(Path("/workspace/product/service"), projects)["id"],
            "inner",
        )

    def test_explicit_project_must_contain_target(self) -> None:
        with self.assertRaisesRegex(ProjectAssociationError, "does not contain"):
            match_saved_project(
                Path("/workspace/product"),
                [{"id": "elsewhere", "roots": [{"path": "/other"}]}],
                explicit_project_id="elsewhere",
            )


class SemanticStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.plan = plan()
        self.cfg = Config(
            root=self.root,
            state_dir=self.root / ".codex-autopilot",
            roadmap=self.root / "ROADMAP.md",
            profile="adaptive",
            language="en",
            skill_name="codex-autopilot-adaptive",
            skill_path=self.root / "SKILL.md",
            desktop=DesktopConfig(desktop_project_id="desktop-project"),
            retry=RetryConfig(),
            runtime=RuntimeConfig(
                execution_strategy="parallel",
                max_parallel_workers=3,
                computer_use_slots=1,
                worker_surface="desktop_owned",
            ),
        )
        self.state = RunState(
            status="RUNNING",
            phase="DESKTOP_WORKERS_ACTIVE",
            graph_version=1,
            execution_strategy="parallel",
            max_parallel_workers=3,
            computer_use_slots=1,
            task_states={
                "T44": "RUNNING",
                "T45": "VERIFYING",
                "T46": "READY",
                "T47": "WAITING",
            },
            task_attempts={task.id: 1 for task in self.plan.tasks},
            task_revisions={task.id: 0 for task in self.plan.tasks},
            active_task_ids=["T44", "T45"],
            worker_sessions=[
                self._session(
                    "T44",
                    "implementation",
                    "Builder · Implement T44 · Task T44",
                    "code",
                ),
                self._session(
                    "T45",
                    "verifier",
                    "Builder · Verify T45 · Task T45",
                    "computer_use",
                ),
            ],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _session(
        task_id: str,
        kind: str,
        title: str,
        execution_mode: str,
    ) -> dict[str, object]:
        return {
            "task_id": task_id,
            "kind": kind,
            "status": "ACTIVE",
            "descriptor": {
                "title": title,
                "execution_mode": execution_mode,
            },
            "project_association_verification": (
                "unavailable: App Server thread/read did not expose projectId; "
                "Desktop project placement remains Codex App-authoritative"
            ),
        }

    def test_snapshot_exposes_semantic_groups_capacity_and_exact_active_titles(self) -> None:
        snapshot = project_status_snapshot(self.cfg, self.state, self.plan)
        self.assertEqual(snapshot["status"], "Running")
        self.assertEqual(snapshot["progress"], {"verified": 0, "total": 4})
        self.assertEqual(snapshot["worker_slots"], {"used": 2, "total": 3, "available": 1})
        self.assertEqual(
            snapshot["computer_use_slots"],
            {"used": 1, "total": 1, "available": 0},
        )
        self.assertEqual(
            snapshot["running"][0]["active_title"],
            "Builder · Implement T44 · Task T44",
        )
        self.assertEqual(
            snapshot["verifying"][0]["active_title"],
            "Builder · Verify T45 · Task T45",
        )
        self.assertEqual(snapshot["ready"][0]["id"], "T46")
        self.assertEqual(
            snapshot["pipeline_engineer"],
            {
                "role": "Pipeline Engineer · On call",
                "phase": "HEALTHY",
                "incident_count": 0,
                "paused_task_ids": [],
                "recovery_slot": None,
                "incidents": [],
                "pending_transport": [],
            },
        )
        self.assertEqual(
            snapshot["waiting"][0]["reason"],
            "waiting for verified dependencies: T44",
        )

    def test_rendered_status_contains_all_required_sections_and_limitations(self) -> None:
        rendered = render_project_status(
            self.cfg,
            self.state,
            self.plan,
            dispatcher_running=False,
        )
        for expected in (
            "Codex Autopilot — Running",
            "Verified progress: 0/4",
            "Worker slots: 2/3 used, 1 available",
            "Computer Use slots: 1/1 used, 0 available",
            "Pipeline Engineer · On call — HEALTHY",
            "Running:",
            "Verifying:",
            "Waiting:",
            "Ready:",
            "active title: Builder · Implement T44 · Task T44",
            "active title: Builder · Verify T45 · Task T45",
            "waiting for verified dependencies: T44",
            "App Server thread/read did not expose projectId",
        ):
            self.assertIn(expected, rendered)


if __name__ == "__main__":
    unittest.main()
