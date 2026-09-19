from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from codex_autopilot.plan import Plan, ResourceClaim, validate_plan
from codex_autopilot.resources import (
    LockOwner,
    NormalizedResourceClaim,
    ResourceLockCoordinator,
    acquire_resources_in_state,
    build_scheduler_availability,
    claims_conflict,
    claims_match,
    normalize_claim,
    release_resources_in_state,
    validate_persisted_resource_state,
)
from codex_autopilot.run_state import RunState, StateStore
from codex_autopilot.task_state import TaskState
from _plan_contract import canonicalize_plan, canonical_verification


def raw_task(
    task_id: str,
    *,
    resources: list[dict[str, str]] | None = None,
    execution_mode: str = "code",
) -> dict[str, object]:
    return {
        "id": task_id,
        "title": task_id,
        "objective": f"Complete {task_id}.",
        "definition_of_done": [f"{task_id} is verified."],
        "execution_mode": execution_mode,
        "execution_mode_reason": "The declared capability is required.",
        "reasoning": "medium",
        "role": "worker",
        "depends_on": [],
        "priority": 0,
        "verification": canonical_verification(),
        "resources": resources or [],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
    }


def resource(
    claim_id: str,
    kind: str,
    target: str,
    access: str,
) -> dict[str, str]:
    return {
        "id": claim_id,
        "kind": kind,
        "target": target,
        "access": access,
    }


def make_plan(
    tasks: list[dict[str, object]],
    *,
    computer_use_slots: int = 1,
) -> Plan:
    return validate_plan(
        canonicalize_plan({
            "schema_version": 3,
            "graph_version": 1,
            "goal": "Exercise durable resource coordination.",
            "user_request": "Exercise durable resource coordination exactly as specified.",
            "model_strategy": "auto",
            "execution_strategy": "parallel",
            "max_parallel_workers": max(2, len(tasks)),
            "computer_use_slots": computer_use_slots,
            "roles": [
                {
                    "id": "worker",
                    "name": "Worker",
                    "responsibilities": ["Complete one task."],
                }
            ],
            "tasks": tasks,
        }),
        "adaptive",
    )


class ResourceMatchingTests(unittest.TestCase):
    def test_access_matrix_allows_only_read_read_for_matching_resources(self) -> None:
        for left_access in ("read", "write", "exclusive"):
            for right_access in ("read", "write", "exclusive"):
                with self.subTest(left=left_access, right=right_access):
                    left = NormalizedResourceClaim("left", "logical", "database", left_access)
                    right = NormalizedResourceClaim("right", "logical", "database", right_access)
                    self.assertEqual(
                        claims_conflict(left, right),
                        not (left_access == right_access == "read"),
                    )

    def test_named_kinds_use_nfkc_whitespace_casefold_and_never_cross_alias(self) -> None:
        root = Path("/project")
        named_kinds = (
            "application",
            "environment",
            "browser",
            "device",
            "external_sandbox",
            "logical",
        )
        for kind in named_kinds:
            with self.subTest(kind=kind):
                left = normalize_claim(ResourceClaim("left", kind, "  Ａlpha   ONE ", "write"), root)
                right = normalize_claim(ResourceClaim("right", kind, "alpha one", "read"), root)
                self.assertEqual(left.target, "alpha one")
                self.assertTrue(claims_match(left, right))
                self.assertTrue(claims_conflict(left, right))

        application = normalize_claim(
            ResourceClaim("app", "application", "chrome", "exclusive"), root
        )
        browser = normalize_claim(
            ResourceClaim("browser", "browser", "chrome", "exclusive"), root
        )
        self.assertFalse(claims_match(application, browser))

    def test_one_file_under_two_spellings_is_one_resource(self) -> None:
        """Two tasks must not both hold an exclusive lock on one file.

        Named claims are casefolded on purpose; filesystem claims were
        compared by exact string. On the filesystem Codex Desktop runs on -
        macOS, case-insensitive - ``Shared.json`` and ``shared.json`` ARE
        one file, so the coordinator granted two exclusive writers to it and
        the whole promise of the lock was gone.

        Folding both sides is the fail-closed direction: on a
        case-sensitive filesystem it can only serialize two tasks that did
        not have to be serialized, while the exact comparison silently let
        two writers into the same bytes.
        """

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            upper = normalize_claim(
                ResourceClaim("upper", "path", "src/Shared.json", "exclusive"), root
            )
            lower = normalize_claim(
                ResourceClaim("lower", "path", "src/shared.json", "exclusive"), root
            )
            self.assertTrue(claims_match(upper, lower))
            self.assertTrue(claims_conflict(upper, lower))

            directory = normalize_claim(
                ResourceClaim("dir", "directory", "SRC", "write"), root
            )
            self.assertTrue(claims_match(directory, lower))

            pattern = normalize_claim(
                ResourceClaim("glob", "glob", "src/**/*.JSON", "write"), root
            )
            self.assertTrue(claims_match(pattern, lower))

            unrelated = normalize_claim(
                ResourceClaim("other", "path", "src/other.json", "exclusive"), root
            )
            self.assertFalse(claims_match(upper, unrelated))

    def test_path_directory_and_glob_matching_is_deterministic_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = normalize_claim(
                ResourceClaim("file", "path", "src/pkg/main.py", "write"), root
            )
            same_tree = normalize_claim(
                ResourceClaim("tree", "directory", "src", "read"), root
            )
            other_tree = normalize_claim(
                ResourceClaim("docs", "directory", "docs", "write"), root
            )
            python_glob = normalize_claim(
                ResourceClaim("python", "glob", "src/**/*.py", "read"), root
            )
            markdown_glob = normalize_claim(
                ResourceClaim("markdown", "glob", "src/**/*.md", "read"), root
            )

            self.assertTrue(claims_match(path, same_tree))
            self.assertTrue(claims_match(path, python_glob))
            self.assertFalse(claims_match(path, other_tree))
            # Arbitrary glob-language intersection is conservative: a shared
            # literal root is treated as overlap even when suffixes differ.
            self.assertTrue(claims_match(python_glob, markdown_glob))
            self.assertTrue(path.target.startswith(str(root.resolve())))


class _ResourceHarness:
    """The same path production reserves through.

    lifecycle_reservations holds ``coordinator.transaction()`` and calls
    the module functions inside it: the transaction takes the lock, and
    acquire_resources_in_state and release_resources_in_state make the
    decision. There is no logic of its own here - conflicts, the journal
    and the slots are counted by those functions.

    Coordinator methods with the same names used to stand in their
    place. They repeated this path and were never called from
    production, so they are gone; the tests are moved onto the functions
    that actually run.
    """

    def __init__(self, state_store: StateStore, project_root: Path) -> None:
        self.state_store = state_store
        self.project_root = project_root
        self._coordinator = ResourceLockCoordinator(state_store, project_root)

    def acquire(self, plan, task_id, owner, *, now=None):
        with self._coordinator.transaction():
            state = self.state_store.load()
            result = acquire_resources_in_state(
                plan, state, self.project_root, task_id, owner, now=now
            )
            if result.acquired and not result.reused:
                self.state_store.save(state)
            return result

    def release(self, ownership_token, *, reason, now=None):
        with self._coordinator.transaction():
            state = self.state_store.load()
            released = release_resources_in_state(
                state, ownership_token, reason=reason, now=now
            )
            if released:
                self.state_store.save(state)
            return released

    def snapshot(self):
        with self._coordinator.transaction():
            state = self.state_store.load()
            return validate_persisted_resource_state(
                state.resource_locks,
                state.resource_lock_journal,
                state.resource_journal_sequence,
            )

    def availability(self, plan):
        with self._coordinator.transaction():
            return build_scheduler_availability(
                plan, self.state_store.load(), self.project_root
            )


class ResourceCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state_store = StateStore(self.root / ".codex-autopilot")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def initialize(self, plan: Plan, *, attempt: int = 1) -> "_ResourceHarness":
        state = RunState(
            graph_version=plan.graph_version,
            execution_strategy=plan.execution_strategy,
            max_parallel_workers=plan.max_parallel_workers,
            computer_use_slots=plan.computer_use_slots,
            task_states={task.id: TaskState.READY.value for task in plan.tasks},
            task_attempts={task.id: attempt for task in plan.tasks},
            task_revisions={task.id: 0 for task in plan.tasks},
        )
        self.state_store.save(state)
        return _ResourceHarness(self.state_store, self.root)

    def owner(self, plan: Plan, task_id: str, *, attempt: int = 1) -> LockOwner:
        state = self.state_store.load()
        return LockOwner.create(
            run_id=state.run_id,
            task_id=task_id,
            attempt=attempt,
            worker_id=f"worker-{task_id}",
            thread_id=f"thread-{task_id}",
            turn_id=f"turn-{task_id}",
        )

    def test_readers_share_but_writer_waits_then_acquires_after_release(self) -> None:
        plan = make_plan(
            [
                raw_task("reader-a", resources=[resource("file", "path", "data.json", "read")]),
                raw_task("reader-b", resources=[resource("file", "path", "data.json", "read")]),
                raw_task("writer", resources=[resource("file", "path", "data.json", "write")]),
            ]
        )
        coordinator = self.initialize(plan)
        reader_a = self.owner(plan, "reader-a")
        reader_b = self.owner(plan, "reader-b")
        writer = self.owner(plan, "writer")

        self.assertTrue(coordinator.acquire(plan, "reader-a", reader_a).acquired)
        self.assertTrue(coordinator.acquire(plan, "reader-b", reader_b).acquired)
        denied = coordinator.acquire(plan, "writer", writer)
        self.assertFalse(denied.acquired)
        self.assertEqual(denied.reason, "resource_conflict")
        self.assertEqual({item.held_task_id for item in denied.conflicts}, {"reader-a", "reader-b"})

        self.assertTrue(coordinator.release(reader_a.ownership_token, reason="reader completed"))
        self.assertTrue(coordinator.release(reader_b.ownership_token, reason="reader completed"))
        self.assertTrue(coordinator.acquire(plan, "writer", writer).acquired)

    def test_exclusive_named_resource_is_non_mergeable_but_unrelated_name_is_free(self) -> None:
        plan = make_plan(
            [
                raw_task(
                    "commit-a",
                    resources=[resource("index", "logical", "git:index", "exclusive")],
                ),
                raw_task(
                    "commit-b",
                    resources=[resource("index", "logical", "GIT:INDEX", "read")],
                ),
                raw_task(
                    "cache",
                    resources=[resource("cache", "logical", "build-cache", "exclusive")],
                ),
            ]
        )
        coordinator = self.initialize(plan)
        self.assertTrue(coordinator.acquire(plan, "commit-a", self.owner(plan, "commit-a")).acquired)
        denied = coordinator.acquire(plan, "commit-b", self.owner(plan, "commit-b"))
        self.assertFalse(denied.acquired)
        self.assertEqual(denied.reason, "resource_conflict")
        self.assertTrue(coordinator.acquire(plan, "cache", self.owner(plan, "cache")).acquired)

    def test_disjoint_shared_working_tree_writes_can_coexist(self) -> None:
        plan = make_plan(
            [
                raw_task(
                    "source",
                    resources=[resource("source", "directory", "src", "write")],
                ),
                raw_task(
                    "documentation",
                    resources=[resource("docs", "directory", "docs", "write")],
                ),
            ]
        )
        coordinator = self.initialize(plan)
        self.assertTrue(
            coordinator.acquire(plan, "source", self.owner(plan, "source")).acquired
        )
        self.assertTrue(
            coordinator.acquire(
                plan,
                "documentation",
                self.owner(plan, "documentation"),
            ).acquired
        )
        self.assertEqual(len(coordinator.snapshot()), 2)

    def test_lock_and_journal_survive_reload_and_release_is_replayed(self) -> None:
        plan = make_plan(
            [raw_task("writer", resources=[resource("tree", "directory", "src", "write")])]
        )
        coordinator = self.initialize(plan)
        owner = self.owner(plan, "writer")
        acquired = coordinator.acquire(
            plan,
            "writer",
            owner,
            now="2026-09-10T10:00:00+00:00",
        )
        self.assertTrue(acquired.acquired)

        reloaded = _ResourceHarness(
            StateStore(self.root / ".codex-autopilot"), self.root
        )
        locks = reloaded.snapshot()
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0].owner, owner)
        self.assertEqual(locks[0].lock_id, acquired.lock_id)
        self.assertTrue(
            reloaded.release(
                owner.ownership_token,
                reason="authoritative worker completion",
                now="2026-09-10T10:02:00+00:00",
            )
        )
        state = self.state_store.load()
        self.assertEqual(state.resource_locks, [])
        self.assertEqual(
            [item["event"] for item in state.resource_lock_journal],
            ["acquired", "released"],
        )
        self.assertEqual(state.resource_journal_sequence, 2)

    def test_computer_use_slot_serializes_gui_without_blocking_code(self) -> None:
        plan = make_plan(
            [
                raw_task("gui-a", execution_mode="computer_use"),
                raw_task("gui-b", execution_mode="computer_use"),
                raw_task("code"),
            ],
            computer_use_slots=1,
        )
        coordinator = self.initialize(plan)
        gui_a = self.owner(plan, "gui-a")
        gui_b = self.owner(plan, "gui-b")
        code = self.owner(plan, "code")

        first = coordinator.acquire(plan, "gui-a", gui_a)
        self.assertTrue(first.acquired)
        self.assertEqual(first.computer_use_slot, 0)
        availability = coordinator.availability(plan)
        self.assertFalse(availability.resource_available["gui-b"])
        self.assertTrue(availability.resource_available["code"])
        self.assertTrue(coordinator.acquire(plan, "code", code).acquired)
        denied = coordinator.acquire(plan, "gui-b", gui_b)
        self.assertFalse(denied.acquired)
        self.assertEqual(denied.reason, "computer_use_capacity")

        self.assertTrue(coordinator.release(gui_a.ownership_token, reason="GUI completed"))
        second = coordinator.acquire(plan, "gui-b", gui_b)
        self.assertTrue(second.acquired)
        self.assertEqual(second.computer_use_slot, 0)

    def test_atomic_acquisition_allows_only_one_overlapping_writer(self) -> None:
        shared = [resource("file", "path", "shared.json", "write")]
        plan = make_plan([raw_task("writer-a", resources=shared), raw_task("writer-b", resources=shared)])
        coordinator = self.initialize(plan)
        owners = {
            task_id: self.owner(plan, task_id) for task_id in ("writer-a", "writer-b")
        }
        barrier = threading.Barrier(2)

        def acquire(task_id: str) -> bool:
            barrier.wait()
            separate = _ResourceHarness(
                StateStore(self.root / ".codex-autopilot"), self.root
            )
            return separate.acquire(plan, task_id, owners[task_id]).acquired

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(acquire, ("writer-a", "writer-b")))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(coordinator.snapshot()), 1)

    def test_same_task_cannot_gain_a_second_owner_and_attempt_must_match(self) -> None:
        plan = make_plan([raw_task("task")])
        coordinator = self.initialize(plan, attempt=2)
        stale = self.owner(plan, "task", attempt=1)
        with self.assertRaisesRegex(ValueError, "attempt"):
            coordinator.acquire(plan, "task", stale)

        first = self.owner(plan, "task", attempt=2)
        second = self.owner(plan, "task", attempt=2)
        acquired = coordinator.acquire(plan, "task", first)
        self.assertTrue(acquired.acquired)
        reused = coordinator.acquire(plan, "task", first)
        self.assertTrue(reused.acquired)
        self.assertTrue(reused.reused)
        denied = coordinator.acquire(plan, "task", second)
        self.assertFalse(denied.acquired)
        self.assertEqual(denied.reason, "owner_conflict")

    def test_journal_replay_rejects_impossible_or_conflicting_persistence(self) -> None:
        plan = make_plan(
            [raw_task("writer", resources=[resource("file", "path", "shared.json", "write")])]
        )
        coordinator = self.initialize(plan)
        coordinator.acquire(plan, "writer", self.owner(plan, "writer"))
        state = self.state_store.load()

        impossible = copy.deepcopy(state.resource_lock_journal)
        impossible[0]["event"] = "released"
        impossible[0]["reason"] = "fabricated release"
        with self.assertRaisesRegex(ValueError, "without an active acquisition"):
            validate_persisted_resource_state(
                state.resource_locks,
                impossible,
                state.resource_journal_sequence,
            )

        second_lock = copy.deepcopy(state.resource_locks[0])
        second_lock["lock_id"] = "resource-lock-000002"
        second_lock["owner"]["ownership_token"] = "other-token"
        second_lock["owner"]["task_id"] = "other-task"
        second_event = copy.deepcopy(state.resource_lock_journal[0])
        second_event.update(
            {
                "sequence": 2,
                "lock_id": "resource-lock-000002",
                "ownership_token": "other-token",
                "task_id": "other-task",
            }
        )
        with self.assertRaisesRegex(ValueError, "conflict"):
            validate_persisted_resource_state(
                [*state.resource_locks, second_lock],
                [*state.resource_lock_journal, second_event],
                2,
            )


if __name__ == "__main__":
    unittest.main()
