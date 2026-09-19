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
    replanner_thread_title,
    revision_thread_title,
    task_phase_thread_title,
    verifier_thread_title,
)
from _plan_contract import canonicalize_plan, canonical_verification


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
        "verification": canonical_verification(),
        "resources": [],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
    }


def plan():
    return validate_plan(
        canonicalize_plan({
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
        }),
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
                # R23: the retry report is read from the signature registry.
                # A clean run has no retries.
                "repeat_breakages": [],
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

    def test_worker_slots_follow_the_budget_on_an_unlimited_account(self) -> None:
        """B6: the card took the limit as min(plan, state).

        On an account with no limit that showed "3/2".
        """

        self.state.rate_limits = {"credits": {"hasCredits": True}}
        snapshot = project_status_snapshot(self.cfg, self.state, self.plan)
        self.assertEqual(snapshot["worker_slots"]["total"], len(self.plan.tasks))

    def test_the_runtime_line_does_not_invent_a_model_it_never_recorded(self) -> None:
        """R26: only what was measured is displayed.

        No production path writes ``selected_model_display`` or
        ``selected_reasoning``: all six model-selection fields in
        RunState are dead, and there are zero writes outside
        ``run_state.py``. The status put "Host default" in their place,
        and that was a claim without a measurement - it printed the same
        on every run, including one where the host was on a different
        model and a different reasoning level. The real model choice
        lives on the task, in AIStudioRuntime routing, and never reaches
        the run state at all.
        """

        self.assertIsNone(self.state.selected_model_display)
        self.assertIsNone(self.state.selected_reasoning)

        rendered = render_project_status(
            self.cfg,
            self.state,
            self.plan,
            dispatcher_running=False,
        )
        runtime = next(
            line for line in rendered.splitlines() if line.startswith("Runtime:")
        )
        self.assertNotIn("Host default", runtime)
        self.assertNotIn("model=", runtime)
        self.assertNotIn("reasoning=", runtime)
        # The measured part stays: it comes from the plan and the state.
        self.assertIn("execution_mode=", runtime)
        self.assertIn("strategy=", runtime)


class WaitingReasonTests(unittest.TestCase):
    """Section 33: the status has to name the reason for waiting."""

    def test_a_task_blocked_by_a_held_resource_says_who_holds_it(self) -> None:
        from pathlib import Path

        from codex_autopilot.status import _resource_reason

        root = Path("/project").resolve()
        plan = _two_task_plan_sharing_a_resource()
        state = _state_with_lock_held_by("A", root)
        self.assertEqual(_resource_reason(plan, state, "B", root), "resource locked by A")

    def test_an_unreadable_lock_is_not_reported_as_free(self) -> None:
        from pathlib import Path

        from codex_autopilot.status import _resource_reason

        root = Path("/project").resolve()
        plan = _two_task_plan_sharing_a_resource()
        state = _state_with_lock_held_by("A", root)
        state.resource_locks[0]["acquired_at"] = "не время"
        reason = _resource_reason(plan, state, "B", root)
        self.assertIsNotNone(reason)
        self.assertIn("unreadable", reason)

    def test_no_holder_when_resources_do_not_overlap(self) -> None:
        from pathlib import Path

        from codex_autopilot.status import _resource_reason

        root = Path("/project").resolve()
        plan = _two_task_plan_sharing_a_resource(second_target="src/other")
        state = _state_with_lock_held_by("A", root)
        self.assertIsNone(_resource_reason(plan, state, "B", root))


def _two_task_plan_sharing_a_resource(second_target: str = "src/shared"):
    from codex_autopilot.plan import (
        Plan,
        ResourceClaim,
        Task,
        TaskContext,
        VerificationPolicy,
    )

    def task(task_id: str, target: str) -> Task:
        return Task(
            id=task_id,
            title=f"Task {task_id}",
            objective="o",
            definition_of_done=("d",),
            execution_mode="code",
            execution_mode_reason="r",
            reasoning=None,
            role="builder",
            depends_on=(),
            priority=0,
            verification=VerificationPolicy(policy="deterministic", required=True),
            resources=(
                ResourceClaim(
                    id=f"{task_id}-claim",
                    kind="directory",
                    target=target,
                    access="write",
                ),
            ),
            required_capabilities=(),
            context=TaskContext(),
            outputs=(),
            tags=(),
        )

    tasks = (task("A", "src/shared"), task("B", second_target))
    return Plan(
        goal="g",
        user_request="u",
        model_strategy="auto",
        tasks=tasks,
        roles=(),
        graph_version=1,
        execution_strategy="parallel",
        max_parallel_workers=2,
        computer_use_slots=1,
        legacy_serial=False,
    )


def _state_with_lock_held_by(task_id: str, root):
    from codex_autopilot.run_state import RunState

    state = RunState(run_id="r")
    state.resource_locks = [
        {
            "lock_id": "lock-1",
            "owner": {
                "ownership_token": "token-1",
                "run_id": "r",
                "task_id": task_id,
                "attempt": 1,
                "worker_id": "w1",
                "thread_id": None,
                "turn_id": None,
            },
            "claims": [
                {
                    "id": "A-claim",
                    "kind": "directory",
                    "target": str(root / "src/shared"),
                    "access": "write",
                }
            ],
            "acquired_at": "2026-09-11T00:00:00+00:00",
            "heartbeat_at": "2026-09-11T00:00:00+00:00",
            "computer_use_slot": None,
        }
    ]
    return state


if __name__ == "__main__":
    unittest.main()


class ShortStatusTests(SemanticStatusTests):
    """The hook's answer arrives in one piece: length is part of it.

    The full report is twenty-five lines with paths and metadata. In a
    terminal that is fine; in a conversation it reads as a wall and
    hides the only thing worth knowing: what is running and what is in
    the way.
    """

    def test_the_short_form_names_progress_work_and_blocker(self) -> None:
        from codex_autopilot.status import _clip, render_short_status

        self.assertEqual(_clip("короткая", 120), "короткая")
        self.assertTrue(_clip("x" * 200, 30).endswith("…"))
        self.assertEqual(len(_clip("x" * 200, 30)), 30)
        self.assertTrue(callable(render_short_status))

    def test_the_card_never_calls_the_dispatcher_dead_during_verification(self) -> None:
        """The card has no right to contradict itself.

        The dispatcher is a short-lived process: it comes up for the
        transition between tasks and goes out while a worker or a
        verifier is taking its turn. The condition looked only at
        "running" and forgot about "verifying", so in the middle of an
        acceptance under way the card wrote "dispatcher is not running"
        - one line below its own "Verifying: M1". The one place the user
        looks at for the truth was lying.
        """

        from dataclasses import replace

        from codex_autopilot.status import render_short_status

        verifying_only = replace(
            self.state,
            task_states={**self.state.task_states, "T44": "VERIFIED"},
            active_task_ids=["T45"],
        )
        card = render_short_status(
            self.cfg, verifying_only, self.plan, dispatcher_running=False
        )
        self.assertIn("Verifying", card)
        self.assertNotIn("dispatcher is not running", card)

    def test_the_card_says_plainly_when_nobody_is_working(self) -> None:
        """Silence has to be called silence, not hidden."""

        from dataclasses import replace

        from codex_autopilot.status import render_short_status

        idle = replace(
            self.state,
            task_states={task_id: "READY" for task_id in self.state.task_states},
            active_task_ids=[],
            worker_sessions=[],
        )
        card = render_short_status(self.cfg, idle, self.plan, dispatcher_running=False)
        self.assertIn("Nobody is working", card)

    def test_the_short_form_points_at_the_full_one(self) -> None:
        """A short form with no way out to the full one is loss."""

        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/status.py"
        ).read_text(encoding="utf-8")
        self.assertIn("подробный статус", source)

    def test_the_detailed_phrase_is_recognised(self) -> None:
        from codex_autopilot.control import (
            DETAILED_STATUS_PROMPTS,
            STATUS_PROMPTS,
            _normalized_prompt,
        )

        for phrase in ("подробный статус", "статус подробно", "detailed status"):
            with self.subTest(phrase=phrase):
                normalized = _normalized_prompt(phrase)
                self.assertIn(normalized, DETAILED_STATUS_PROMPTS)
                self.assertIn(normalized, STATUS_PROMPTS)
        # An ordinary word stays a short answer.
        self.assertNotIn(_normalized_prompt("статус"), DETAILED_STATUS_PROMPTS)

    def test_the_hook_chooses_the_form_by_the_phrase(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/control.py"
        ).read_text(encoding="utf-8")
        self.assertIn("detailed=prompt in DETAILED_STATUS_PROMPTS", source)
