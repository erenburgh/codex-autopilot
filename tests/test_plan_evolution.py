from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
from _handoff import bump_task_checkpoint
from _plan_contract import canonical_verification
from _relay import reserve_ready_frontier  # R21: без зависимости от окружения
from codex_autopilot.lifecycle import (
    reconcile_desktop_runtime,
    DesktopLifecycleError,
    complete_desktop_worker,
    pause_desktop_run,
    record_desktop_failure,
)
from codex_autopilot.plan import (
    load_plan,
    plan_to_dict,
    validate_plan,
    validate_plan_change,
)
from codex_autopilot.resilience import (
    PLAN_CHANGE_REQUEST_PREFIX,
    PLAN_CHANGE_RESULT_PREFIX,
    PlanChangeProtocolError,
    commit_plan_change,
    parse_plan_change_request,
    reconcile_plan_change_state,
    recover_plan_change_transaction,
    register_plan_change_request,
)
from codex_autopilot.run_state import StateStore
from codex_autopilot.task_state import TaskState


def task(task_id: str, *, depends_on: tuple[str, ...] = ()) -> dict[str, object]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Complete {task_id} safely.",
        "definition_of_done": [f"{task_id} is verified."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files and tests are sufficient.",
        "reasoning": "high",
        "role": "builder",
        "depends_on": list(depends_on),
        "priority": 0,
        "verification": canonical_verification(),
        "resources": [
            {
                "id": "tree",
                "kind": "directory",
                "target": f"src/{task_id.lower()}",
                "access": "write",
            }
        ],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
    }


def graph(tasks: list[dict[str, object]], *, max_workers: int = 2) -> dict[str, object]:
    return {
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Exercise durable plan evolution and recovery.",
        "user_request": "Exercise durable plan evolution and recovery exactly as specified.",
        "model_strategy": "auto",
        "execution_strategy": "parallel",
        "max_parallel_workers": max_workers,
        "computer_use_slots": 1,
        "roles": [
            {
                "id": "builder",
                "name": "Builder",
                "responsibilities": ["Implement and replan bounded tasks."],
            }
        ],
        "tasks": tasks,
    }


def request_line(task_id: str, *, kind: str = "prerequisite") -> str:
    change = (
        {"description": "Add a prerequisite task.", "suggested_task_id": "P"}
        if kind == "prerequisite"
        else {"dependency_task_id": "P"}
        if kind == "dependency"
        else {
            "resources": [
                {
                    "id": "tree",
                    "kind": "directory",
                    "target": "src/a",
                    "access": "write",
                }
            ]
        }
        if kind == "resource"
        else {"verification": {"policy": "independent", "required": True}}
    )
    return PLAN_CHANGE_REQUEST_PREFIX + " " + json.dumps(
        {
            "request_version": 1,
            "kind": kind,
            "target_task_id": task_id,
            "summary": f"Change {kind} contract",
            "rationale": "The current task cannot be verified safely without it.",
            "change": change,
            "evidence_ids": [],
        },
        separators=(",", ":"),
    )


class PlanEvolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-autopilot-plan-evolution-")
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def initialize(self, raw: dict[str, object]):
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(raw), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
        )
        return load_config(self.root), StateStore(self.root / ".codex-autopilot")

    @staticmethod
    def mark_active(store: StateStore, token: str, thread_id: str) -> None:
        state = store.load()
        session = next(
            item for item in state.worker_sessions if item["reservation_token"] == token
        )
        session["status"] = "ACTIVE"
        session["thread_id"] = thread_id
        store.save(state)

    def candidate_with_prerequisite(self, current) -> dict[str, object]:
        raw = plan_to_dict(current)
        raw["graph_version"] = current.graph_version + 1
        prerequisite = task("P")
        requester = dict(raw["tasks"][0])
        requester["depends_on"] = ["P"]
        raw["tasks"] = [prerequisite, requester, *raw["tasks"][1:]]
        return raw

    def test_typed_request_protocol_covers_all_four_change_kinds(self) -> None:
        for kind in ("prerequisite", "dependency", "resource", "verification"):
            with self.subTest(kind=kind):
                parsed = parse_plan_change_request("context\n" + request_line("A", kind=kind))
                self.assertIsNotNone(parsed)
                self.assertEqual(parsed.kind, kind)
                self.assertEqual(parsed.target_task_id, "A")
        with self.assertRaisesRegex(PlanChangeProtocolError, "final non-empty line"):
            parse_plan_change_request(request_line("A") + "\nextra")

    def test_worker_request_creates_one_fresh_replanner_and_applies_dynamic_prerequisite(self) -> None:
        cfg, store = self.initialize(graph([task("A")], max_workers=1))
        descriptor = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )[0]
        self.mark_active(store, descriptor.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, descriptor.task_id, "Plan change requested.")
        outcome = complete_desktop_worker(
            cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=request_line("A"),
            hook_gate=lambda _cfg: None,
        )
        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_REQUEST")
        self.assertEqual(len(outcome.descriptors), 1)
        replanner = outcome.descriptors[0]
        self.assertEqual(replanner.kind, "replanner")
        self.assertEqual(replanner.title, "Planner | PC-1 | Change prerequisite contract")
        self.assertIn('"request_id":"PC1"', replanner.prompt)
        self.assertIn('"verified_state":[]', replanner.prompt)

        self.mark_active(store, replanner.reservation_token, "replanner-PC1")
        current = load_plan(cfg.state_dir, cfg.profile)
        candidate = self.candidate_with_prerequisite(current)
        result_line = PLAN_CHANGE_RESULT_PREFIX + " " + json.dumps(
            {
                "request_id": "PC1",
                "base_graph_version": 1,
                "plan": candidate,
            },
            separators=(",", ":"),
        )
        applied = complete_desktop_worker(
            cfg,
            thread_id="replanner-PC1",
            turn_id="turn-PC1",
            final_message=result_line,
            hook_gate=lambda _cfg: None,
        )
        self.assertEqual(applied.worker_status, "PLAN_CHANGE_APPLIED")
        self.assertEqual([item.task_id for item in applied.descriptors], ["P"])
        plan = load_plan(cfg.state_dir, cfg.profile)
        state = store.load()
        self.assertEqual(plan.graph_version, 2)
        self.assertEqual(state.graph_version, 2)
        self.assertEqual(state.task_states["P"], TaskState.RUNNING.value)
        self.assertEqual(state.task_states["A"], TaskState.WAITING.value)
        self.assertIsNone(state.active_plan_change_id)
        self.assertEqual(state.plan_changes[0]["status"], "APPLIED")

    def test_migrated_serial_run_accepts_resource_replan_and_preserves_provenance(self) -> None:
        raw = graph([task("A")], max_workers=1)
        raw["execution_strategy"] = "serial"
        raw["compatibility"] = {"migrated_from_schema": 2, "legacy_serial": True}
        cfg, store = self.initialize(raw)
        descriptor = reserve_ready_frontier(
            cfg, relay_owner_thread_id="owner", hook_gate=lambda _cfg: None,
        )[0]
        self.mark_active(store, descriptor.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, "A", "Resource change requested.\n")
        outcome = complete_desktop_worker(
            cfg, thread_id="worker-A", turn_id="turn-A",
            final_message=request_line("A", kind="resource"),
            hook_gate=lambda _cfg: None,
        )
        replanner = outcome.descriptors[0]
        self.assertEqual(replanner.kind, "replanner")
        self.mark_active(store, replanner.reservation_token, "replanner-PC1")
        current = load_plan(cfg.state_dir, cfg.profile)
        candidate = plan_to_dict(current)
        candidate["graph_version"] += 1
        candidate["tasks"][0]["resources"].append(
            {"id": "docs", "kind": "directory", "target": "docs", "access": "write"}
        )
        result = complete_desktop_worker(
            cfg, thread_id="replanner-PC1", turn_id="turn-PC1",
            final_message=PLAN_CHANGE_RESULT_PREFIX + " " + json.dumps({
                "request_id": "PC1", "base_graph_version": 1, "plan": candidate,
            }),
            hook_gate=lambda _cfg: None,
        )
        self.assertEqual(result.worker_status, "PLAN_CHANGE_APPLIED")
        updated = load_plan(cfg.state_dir, cfg.profile)
        self.assertEqual(updated.graph_version, 2)
        self.assertEqual(updated.source_schema_version, 2)
        self.assertTrue(updated.legacy_serial)
        self.assertEqual(updated.execution_strategy, "serial")
        self.assertEqual(updated.max_parallel_workers, 1)
        self.assertEqual(updated.computer_use_slots, 1)
        self.assertEqual(plan_to_dict(updated)["compatibility"], raw["compatibility"])
        self.assertEqual(updated.task_map["A"].resources[-1].target, "docs")
        self.assertEqual(store.load().plan_changes[0]["status"], "APPLIED")

    def test_replanner_still_rejects_an_actual_schema_two_payload(self) -> None:
        raw = graph([task("A")], max_workers=1)
        legacy = {
            key: raw[key] for key in ("goal", "user_request", "model_strategy", "roles")
        }
        legacy["schema_version"] = 2
        legacy["milestones"] = [{
            key: raw["tasks"][0][key]
            for key in (
                "id", "title", "objective", "definition_of_done", "execution_mode",
                "execution_mode_reason", "reasoning", "role",
            )
        }]
        current = validate_plan(legacy, "adaptive")
        with self.assertRaisesRegex(ValueError, "canonical v0.9 schema"):
            validate_plan_change(current, legacy, "adaptive")

    def test_cycle_from_replanner_is_rejected_without_plan_or_state_write(self) -> None:
        cfg, store = self.initialize(graph([task("A")], max_workers=1))
        descriptor = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )[0]
        self.mark_active(store, descriptor.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, descriptor.task_id, "Need cycle-safe replan.")
        replanner = complete_desktop_worker(
            cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=request_line("A"),
            hook_gate=lambda _cfg: None,
        ).descriptors[0]
        self.mark_active(store, replanner.reservation_token, "replanner-PC1")
        current = load_plan(cfg.state_dir, cfg.profile)
        cyclic = self.candidate_with_prerequisite(current)
        cyclic["tasks"][0]["depends_on"] = ["A"]
        before_plan = (cfg.state_dir / "plan.json").read_bytes()
        before_state = (cfg.state_dir / "run-state.json").read_bytes()
        with self.assertRaisesRegex(DesktopLifecycleError, "cycle"):
            complete_desktop_worker(
                cfg,
                thread_id="replanner-PC1",
                turn_id="turn-PC1",
                final_message=PLAN_CHANGE_RESULT_PREFIX
                + " "
                + json.dumps(
                    {
                        "request_id": "PC1",
                        "base_graph_version": 1,
                        "plan": cyclic,
                    },
                    separators=(",", ":"),
                ),
                hook_gate=lambda _cfg: None,
            )
        self.assertEqual((cfg.state_dir / "plan.json").read_bytes(), before_plan)
        self.assertEqual((cfg.state_dir / "run-state.json").read_bytes(), before_state)

    def test_interrupted_two_file_plan_commit_recovers_by_redo(self) -> None:
        cfg, store = self.initialize(graph([task("A")], max_workers=1))
        current = load_plan(cfg.state_dir, cfg.profile)
        state = store.load()
        state.task_states["A"] = TaskState.RUNNING.value
        state.active_task_ids = ["A"]
        request = parse_plan_change_request(request_line("A"))
        self.assertIsNotNone(request)
        register_plan_change_request(
            state,
            request,
            requester_task_id="A",
            requester_session_token="original",
        )
        candidate = validate_plan_change(
            current,
            self.candidate_with_prerequisite(current),
            cfg.profile,
        )
        state.active_task_ids = []
        state.task_states["A"] = TaskState.BLOCKED.value
        reconcile_plan_change_state(
            current,
            candidate,
            state,
            request_id="PC1",
            requester_task_id="A",
        )

        def crash(stage: str) -> None:
            if stage == "plan_written":
                raise RuntimeError("simulated crash")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            commit_plan_change(
                cfg.state_dir,
                profile=cfg.profile,
                current=current,
                candidate=candidate,
                state=state,
                request_id="PC1",
                fault_hook=crash,
            )
        self.assertEqual(load_plan(cfg.state_dir, cfg.profile).graph_version, 2)
        self.assertEqual(store.load().graph_version, 1)
        self.assertTrue(recover_plan_change_transaction(cfg.state_dir, cfg.profile))
        self.assertEqual(load_plan(cfg.state_dir, cfg.profile).graph_version, 2)
        self.assertEqual(store.load().graph_version, 2)
        self.assertFalse(recover_plan_change_transaction(cfg.state_dir, cfg.profile))


    def test_rate_limit_is_account_barrier_and_preserves_other_worker(self) -> None:
        cfg, store = self.initialize(graph([task("A"), task("B")], max_workers=2))
        descriptors = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )
        a = next(item for item in descriptors if item.task_id == "A")
        record_desktop_failure(
            cfg,
            a.reservation_token,
            reason="account bucket exhausted",
            definitive=True,
            rate_limited=True,
            reset_at=200,
            now_epoch=100,
            hook_gate=lambda _cfg: None,
        )
        limited = store.load()
        self.assertEqual(limited.rate_limit_until, 205)
        self.assertEqual(limited.task_retry_at["A"], 205)
        self.assertEqual(limited.task_states["A"], TaskState.RETRY_WAIT.value)
        self.assertEqual(limited.task_states["B"], TaskState.RUNNING.value)
        self.assertIn("B", limited.active_task_ids)
        self.assertEqual(
            reserve_ready_frontier(
                cfg,
                relay_owner_thread_id="owner",
                now_epoch=150,
                hook_gate=lambda _cfg: None,
            ),
            (),
        )
        fresh = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            now_epoch=206,
            hook_gate=lambda _cfg: None,
        )
        self.assertEqual([item.task_id for item in fresh], ["A"])
        recovered = store.load()
        self.assertIsNone(recovered.rate_limit_until)
        self.assertEqual(recovered.task_states["B"], TaskState.RUNNING.value)

    def test_crash_reconciliation_keeps_unknown_owner_locked_and_is_idempotent(self) -> None:
        cfg, store = self.initialize(graph([task("A"), task("B")], max_workers=2))
        descriptors = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )
        tokens = {item.task_id: item.reservation_token for item in descriptors}
        result = reconcile_desktop_runtime(
            cfg,
            authoritative_states={tokens["A"]: "absent", tokens["B"]: "unknown"},
            now_epoch=50,
        )
        self.assertEqual(result.retried_task_ids, ("A",))
        self.assertEqual(result.unresolved_task_ids, ("B",))
        state = store.load()
        self.assertEqual(state.task_states["A"], TaskState.RETRY_WAIT.value)
        self.assertEqual(state.task_states["B"], TaskState.RUNNING.value)
        owners = {
            item["owner"]["task_id"] for item in state.resource_locks
        }
        self.assertEqual(owners, {"B"})
        retry_at = state.task_retry_at["A"]

        repeated = reconcile_desktop_runtime(
            cfg,
            authoritative_states={tokens["A"]: "absent", tokens["B"]: "unknown"},
            now_epoch=60,
        )
        self.assertEqual(repeated.retried_task_ids, ())
        self.assertEqual(store.load().task_retry_at["A"], retry_at)


if __name__ == "__main__":
    unittest.main()
