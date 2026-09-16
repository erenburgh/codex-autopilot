from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _plan_contract import initialize_verified_project as initialize_project
from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
from _handoff import bump_task_checkpoint
from _plan_contract import TEST_OUTCOME_ID, canonicalize_plan, canonical_verification
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
from codex_autopilot.plan_verification import (
    FULL_PLAN_REVALIDATION,
    PLAN_VERIFICATION_PREFIX,
)
from codex_autopilot.resilience import (
    active_plan_change,
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
        "produces_outcomes": [TEST_OUTCOME_ID],
        "acceptance_class": "mixed",
    }


def graph(tasks: list[dict[str, object]], *, max_workers: int = 2) -> dict[str, object]:
    return canonicalize_plan({
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
    })


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
        migrating = bool(raw.get("milestones"))
        if migrating:
            # План v0.8 впускается только как миграция существующего
            # прогона: доказательство - его состояние и его план на
            # диске. Свежий проект этот формат не принимает вовсе.
            state_dir = self.root / ".codex-autopilot"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "plan.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "goal": "Previous run.",
                        "model_strategy": "auto",
                        "milestones": [
                            {"id": item["id"]} for item in raw["milestones"]
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "run-state.json").write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "run_id": "existing-v08-run",
                        "status": "DONE",
                        "phase": "DONE",
                        "milestone_index": max(len(raw["milestones"]) - 1, 0),
                        "milestone_id": (
                            raw["milestones"][-1]["id"] if raw["milestones"] else None
                        ),
                        "worker_history": [],
                    }
                ),
                encoding="utf-8",
            )
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
            replace=migrating,
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
        proposed = complete_desktop_worker(
            cfg,
            thread_id="replanner-PC1",
            turn_id="turn-PC1",
            final_message=result_line,
            hook_gate=lambda _cfg: None,
        )
        self.assertEqual(proposed.worker_status, "PLAN_CHANGE_PROPOSED")
        self.assertEqual(len(proposed.descriptors), 1)
        plan_verifier = proposed.descriptors[0]
        self.assertEqual(plan_verifier.kind, "plan_verifier")
        self.assertIn("PLAN_VERIFICATION_CONTEXT", plan_verifier.prompt)
        self.mark_active(store, plan_verifier.reservation_token, "plan-verifier-PC1")
        applied = complete_desktop_worker(
            cfg,
            thread_id="plan-verifier-PC1",
            turn_id="turn-plan-verifier-PC1",
            final_message=(
                PLAN_VERIFICATION_PREFIX
                + ' {"verdict":"PASS","issues":[]}'
            ),
            hook_gate=lambda _cfg: None,
        )
        self.assertEqual(applied.worker_status, "PLAN_VERIFIED")
        self.assertEqual([item.task_id for item in applied.descriptors], ["P"])
        plan = load_plan(cfg.state_dir, cfg.profile)
        state = store.load()
        self.assertEqual(plan.graph_version, 2)
        self.assertEqual(state.graph_version, 2)
        self.assertEqual(state.task_states["P"], TaskState.RUNNING.value)
        self.assertEqual(state.task_states["A"], TaskState.WAITING.value)
        self.assertIsNone(state.active_plan_change_id)
        self.assertEqual(state.plan_changes[0]["status"], "APPLIED")

    def test_t3_plan_verifier_rejects_unnecessary_work_before_graph_commit(self) -> None:
        cfg, store = self.initialize(graph([task("A")], max_workers=1))
        worker = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )[0]
        self.mark_active(store, worker.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, worker.task_id, "Plan change requested.")
        replanner = complete_desktop_worker(
            cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=request_line("A"),
            hook_gate=lambda _cfg: None,
        ).descriptors[0]
        self.mark_active(store, replanner.reservation_token, "replanner-PC1")
        current = load_plan(cfg.state_dir, cfg.profile)
        candidate = self.candidate_with_prerequisite(current)
        proposed = complete_desktop_worker(
            cfg,
            thread_id="replanner-PC1",
            turn_id="turn-PC1",
            final_message=PLAN_CHANGE_RESULT_PREFIX
            + " "
            + json.dumps(
                {"request_id": "PC1", "base_graph_version": 1, "plan": candidate},
                separators=(",", ":"),
            ),
            hook_gate=lambda _cfg: None,
        )
        plan_verifier = proposed.descriptors[0]
        self.assertEqual(plan_verifier.kind, "plan_verifier")
        before_plan = (cfg.state_dir / "plan.json").read_bytes()
        self.mark_active(
            store, plan_verifier.reservation_token, "plan-verifier-PC1"
        )

        rejected = complete_desktop_worker(
            cfg,
            thread_id="plan-verifier-PC1",
            turn_id="turn-plan-verifier-PC1",
            final_message=PLAN_VERIFICATION_PREFIX
            + " "
            + json.dumps(
                {
                    "verdict": "REVISE",
                    "issues": [
                        {
                            "category": "necessity",
                            "summary": "Task P is not necessary for the Goal Contract.",
                            "task_ids": ["P"],
                            "outcome_ids": [],
                        }
                    ],
                },
                separators=(",", ":"),
            ),
            hook_gate=lambda _cfg: None,
        )

        self.assertEqual(rejected.worker_status, "PLAN_REVISION_REQUIRED")
        self.assertEqual((cfg.state_dir / "plan.json").read_bytes(), before_plan)
        self.assertEqual(load_plan(cfg.state_dir, cfg.profile).graph_version, 1)
        self.assertEqual(len(rejected.descriptors), 1)
        self.assertEqual(rejected.descriptors[0].kind, "replanner")
        state = store.load()
        change = active_plan_change(state, request_id="PC1")
        self.assertEqual(change["status"], "REPLANNER_RESERVED")
        self.assertEqual(
            change["plan_verification_history"][-1]["issues"][0]["category"],
            "necessity",
        )
        self.assertIn("semantic plan verification rejected", change["rejections"][-1]["reason"])

    def test_t4_accumulated_patches_require_full_revalidation_and_can_be_rejected(self) -> None:
        cfg, store = self.initialize(graph([task("A")], max_workers=1))
        state = store.load()
        state.accepted_plan_patches_since_full_revalidation = 2
        store.save(state)
        worker = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )[0]
        self.mark_active(store, worker.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, worker.task_id, "Resource patch requested.")
        replanner = complete_desktop_worker(
            cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=request_line("A", kind="resource"),
            hook_gate=lambda _cfg: None,
        ).descriptors[0]
        self.mark_active(store, replanner.reservation_token, "replanner-PC1")
        current = load_plan(cfg.state_dir, cfg.profile)
        candidate = plan_to_dict(current)
        candidate["graph_version"] = 2
        candidate["tasks"][0]["resources"].append(
            {
                "id": "docs",
                "kind": "directory",
                "target": "docs",
                "access": "write",
            }
        )
        proposed = complete_desktop_worker(
            cfg,
            thread_id="replanner-PC1",
            turn_id="turn-PC1",
            final_message=PLAN_CHANGE_RESULT_PREFIX
            + " "
            + json.dumps(
                {"request_id": "PC1", "base_graph_version": 1, "plan": candidate},
                separators=(",", ":"),
            ),
            hook_gate=lambda _cfg: None,
        )
        plan_verifier = proposed.descriptors[0]
        self.assertEqual(plan_verifier.kind, "plan_verifier")
        self.assertIn(
            f"Verification mode: {FULL_PLAN_REVALIDATION}",
            plan_verifier.prompt,
        )
        before_plan = (cfg.state_dir / "plan.json").read_bytes()
        self.mark_active(
            store, plan_verifier.reservation_token, "plan-verifier-PC1"
        )
        rejected = complete_desktop_worker(
            cfg,
            thread_id="plan-verifier-PC1",
            turn_id="turn-plan-verifier-PC1",
            final_message=PLAN_VERIFICATION_PREFIX
            + " "
            + json.dumps(
                {
                    "verdict": "REVISE",
                    "issues": [
                        {
                            "category": "integration_completeness",
                            "summary": "The accumulated graph no longer proves integrated acceptance.",
                            "task_ids": ["A"],
                            "outcome_ids": [],
                        }
                    ],
                },
                separators=(",", ":"),
            ),
            hook_gate=lambda _cfg: None,
        )

        self.assertEqual(rejected.worker_status, "PLAN_REVISION_REQUIRED")
        self.assertEqual((cfg.state_dir / "plan.json").read_bytes(), before_plan)
        state = store.load()
        self.assertEqual(
            state.accepted_plan_patches_since_full_revalidation, 2
        )
        change = active_plan_change(state, request_id="PC1")
        self.assertEqual(
            change["plan_verification_history"][-1]["mode"],
            FULL_PLAN_REVALIDATION,
        )
        self.assertEqual(rejected.descriptors[0].kind, "replanner")

    def test_migrated_serial_run_accepts_resource_replan_and_preserves_provenance(self) -> None:
        # Мигрированный прогон заводится настоящим payload v0.8, а не
        # schema-3 планом, объявившим себя мигрированным: заявить
        # происхождение нельзя, его производит только миграция.
        raw = {
            "schema_version": 2,
            "goal": "Exercise durable plan evolution and recovery.",
            "user_request": "Exercise durable plan evolution and recovery exactly as specified.",
            "model_strategy": "auto",
            "roles": [
                {
                    "id": "builder",
                    "name": "Builder",
                    "responsibilities": ["Implement and replan bounded tasks."],
                }
            ],
            "milestones": [
                {
                    "id": "A",
                    "title": "Task A",
                    "objective": "Complete A safely.",
                    "definition_of_done": ["A is verified."],
                    "execution_mode": "code",
                    "execution_mode_reason": "Repository files and tests are sufficient.",
                    "reasoning": "high",
                    "role": "builder",
                }
            ],
        }
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
        self.assertEqual(
            plan_to_dict(updated)["compatibility"],
            {"migrated_from_schema": 2, "legacy_serial": True},
        )
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
        cfg, _store = self.initialize(legacy)
        current = load_plan(cfg.state_dir, cfg.profile)
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
        outcome = complete_desktop_worker(
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
        # Негодный граф не применяется никогда - это и было содержанием
        # прежней проверки. Изменилось одно: отказ больше не валит
        # диспетчер, а возвращается реплэннеру с причиной.
        self.assertEqual((cfg.state_dir / "plan.json").read_bytes(), before_plan)
        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_REJECTED")
        state = store.load()
        self.assertEqual(state.graph_version, 1)
        change = active_plan_change(state, request_id="PC1")
        self.assertEqual(change["status"], "REPLANNER_RESERVED")
        self.assertIn("cycle", change["rejections"][-1]["reason"])
        self.assertEqual(len(outcome.descriptors), 1)
        self.assertEqual(
            self.session_kind(state, outcome.descriptors[0].reservation_token),
            "replanner",
        )

    def session_kind(self, state, token: str) -> str:
        return next(
            str(item["kind"])
            for item in state.worker_sessions
            if item.get("reservation_token") == token
        )

    def test_the_replanner_hands_its_successor_to_the_same_dispatcher(self) -> None:
        """Владение переходом обязано дойти и до реплэннера.

        Инженеру и воркеру это чинили по отдельности, реплэннера
        пропустили: сторона вызываемого была готова, а вызывающий флаг не
        передавал. Весь учёт преемника у реплэннера был недостижим из
        продакшена, и следующий шаг отвечал "current dispatcher does not
        own the completed-to-successor transition" - на первой же смене
        плана, то есть почти сразу.
        """

        import os

        cfg, store = self.initialize(graph([task("A")], max_workers=1))
        descriptor = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )[0]
        self.mark_active(store, descriptor.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, descriptor.task_id, "Need a replan.")
        replanner = complete_desktop_worker(
            cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=request_line("A"),
            hook_gate=lambda _cfg: None,
        ).descriptors[0]
        self.mark_active(store, replanner.reservation_token, "replanner-PC1")

        # Живая картина: ход реплэннера ведёт этот же процесс-диспетчер.
        state = store.load()
        for item in state.worker_sessions:
            if item.get("reservation_token") == replanner.reservation_token:
                item["automatic_dispatch_pid"] = os.getpid()
                item["automatic_dispatch_state"] = "RUNNING"
        store.save(state)

        current = load_plan(cfg.state_dir, cfg.profile)
        candidate = self.candidate_with_prerequisite(current)
        outcome = complete_desktop_worker(
            cfg,
            thread_id="replanner-PC1",
            turn_id="turn-PC1",
            final_message=PLAN_CHANGE_RESULT_PREFIX
            + " "
            + json.dumps(
                {"request_id": "PC1", "base_graph_version": 1, "plan": candidate},
                separators=(",", ":"),
            ),
            hook_gate=lambda _cfg: None,
            dispatcher_reservation_token=replanner.reservation_token,
            dispatcher_pid=os.getpid(),
        )
        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_PROPOSED")
        self.assertTrue(outcome.descriptors)
        session = next(
            item
            for item in store.load().worker_sessions
            if item.get("reservation_token") == replanner.reservation_token
        )
        self.assertEqual(session.get("automatic_dispatch_state"), "ADVANCING")
        self.assertEqual(
            session.get("automatic_successor_tokens"),
            [item.reservation_token for item in outcome.descriptors],
        )

    def test_rejected_graph_returns_its_reason_to_the_next_replanner(self) -> None:
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
        unknown_field = self.candidate_with_prerequisite(current)
        unknown_field["nonsense_field"] = {"whatever": 1}
        outcome = complete_desktop_worker(
            cfg,
            thread_id="replanner-PC1",
            turn_id="turn-PC1",
            final_message=PLAN_CHANGE_RESULT_PREFIX
            + " "
            + json.dumps(
                {"request_id": "PC1", "base_graph_version": 1, "plan": unknown_field},
                separators=(",", ":"),
            ),
            hook_gate=lambda _cfg: None,
        )
        self.assertEqual(outcome.worker_status, "PLAN_CHANGE_REJECTED")
        prompt = outcome.descriptors[0].prompt
        # Причина отказа должна дойти до модели двумя путями: машинным -
        # в конверте, и словами - в самой инструкции. Иначе переделка
        # идёт вслепую и возвращает ту же ошибку.
        self.assertIn("rejected_attempts", prompt)
        self.assertIn("allowed_plan_fields", prompt)
        self.assertIn("The previous attempt was rejected by the runtime", prompt)
        self.assertIn("plan has unknown fields: ['nonsense_field']", prompt)

    def test_user_declared_worker_count_reaches_the_replanner(self) -> None:
        """Потолок воркеров живёт в плане, а план переписывает реплэннер.

        Пользователь меняет число в своём config.toml. Без передачи в
        задание реплэннер копирует старое число из текущего графа, и
        правка не доезжает никуда - прогон навсегда остаётся с тем
        потолком, с каким был создан.
        """

        cfg, store = self.initialize(graph([task("A")], max_workers=2))
        config_file = self.root / ".codex-autopilot" / "config.toml"
        config_file.write_text(
            config_file.read_text(encoding="utf-8").replace(
                "max_parallel_workers = 2", "max_parallel_workers = 7"
            ),
            encoding="utf-8",
        )
        cfg = load_config(self.root)
        descriptor = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )[0]
        self.mark_active(store, descriptor.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, descriptor.task_id, "Need a replan.")
        replanner = complete_desktop_worker(
            cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=request_line("A"),
            hook_gate=lambda _cfg: None,
        ).descriptors[0]
        prompt = replanner.prompt
        self.assertIn("required_max_parallel_workers", prompt)
        self.assertIn("Set max_parallel_workers=7", prompt)

    def test_config_without_the_key_never_forces_one_worker(self) -> None:
        """Умолчание - не выбор человека.

        Совместимость с v0.8 держит здесь единицу. Принять её за
        пожелание значило бы загнать любой прогон со старым конфигом в
        один поток при первой же смене плана.
        """

        cfg, store = self.initialize(graph([task("A")], max_workers=2))
        config_file = self.root / ".codex-autopilot" / "config.toml"
        config_file.write_text(
            "\n".join(
                line
                for line in config_file.read_text(encoding="utf-8").splitlines()
                if not line.startswith("max_parallel_workers")
            )
            + "\n",
            encoding="utf-8",
        )
        cfg = load_config(self.root)
        self.assertFalse(cfg.runtime.max_parallel_workers_declared)
        descriptor = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )[0]
        self.mark_active(store, descriptor.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, descriptor.task_id, "Need a replan.")
        replanner = complete_desktop_worker(
            cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=request_line("A"),
            hook_gate=lambda _cfg: None,
        ).descriptors[0]
        self.assertNotIn("required_max_parallel_workers", replanner.prompt)
        self.assertNotIn("max_parallel_workers=1", replanner.prompt)

    def test_matching_worker_count_adds_no_instruction(self) -> None:
        """Совпадающее число - не правка, и говорить о ней нечего."""

        cfg, store = self.initialize(graph([task("A")], max_workers=2))
        descriptor = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )[0]
        self.mark_active(store, descriptor.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, descriptor.task_id, "Need a replan.")
        replanner = complete_desktop_worker(
            cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=request_line("A"),
            hook_gate=lambda _cfg: None,
        ).descriptors[0]
        self.assertNotIn("required_max_parallel_workers", replanner.prompt)

    def test_replanner_that_never_matches_the_schema_stops_the_run_loudly(self) -> None:
        cfg, store = self.initialize(graph([task("A")], max_workers=1))
        descriptor = reserve_ready_frontier(
            cfg,
            relay_owner_thread_id="owner",
            hook_gate=lambda _cfg: None,
        )[0]
        self.mark_active(store, descriptor.reservation_token, "worker-A")
        bump_task_checkpoint(self.root, descriptor.task_id, "Need cycle-safe replan.")
        pending = complete_desktop_worker(
            cfg,
            thread_id="worker-A",
            turn_id="turn-A",
            final_message=request_line("A"),
            hook_gate=lambda _cfg: None,
        ).descriptors[0]
        current = load_plan(cfg.state_dir, cfg.profile)
        bad = self.candidate_with_prerequisite(current)
        bad["nonsense_field"] = {"whatever": 1}
        outcome = None
        for attempt in range(3):
            self.mark_active(store, pending.reservation_token, f"replanner-{attempt}")
            outcome = complete_desktop_worker(
                cfg,
                thread_id=f"replanner-{attempt}",
                turn_id=f"turn-{attempt}",
                final_message=PLAN_CHANGE_RESULT_PREFIX
                + " "
                + json.dumps(
                    {"request_id": "PC1", "base_graph_version": 1, "plan": bad},
                    separators=(",", ":"),
                ),
                hook_gate=lambda _cfg: None,
            )
            if not outcome.descriptors:
                break
            pending = outcome.descriptors[0]
        # Бесконечно возвращать одну и ту же ошибку значит жечь лимиты.
        # Прогон обязан встать и назвать причину человеку.
        self.assertEqual(outcome.descriptors, ())
        state = store.load()
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.phase, "PLAN_CHANGE_REJECTED")
        self.assertIsNone(state.active_plan_change_id)
        record = next(item for item in state.plan_changes if item["id"] == "PC1")
        self.assertEqual(record["status"], "REJECTED")
        self.assertEqual(len(record["rejections"]), 3)
        # Остановка обязана называть причину там, куда человек смотрит.
        from codex_autopilot.control import status_text

        text = status_text(self.root)
        self.assertIn("PC1 / REJECTED", text)
        self.assertIn("nonsense_field", text)

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
