from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from _appserver_fakes import activate_via_app_server
from _gates import patch_hook_trust_gates
from _handoff import bump_task_checkpoint
from _plan_contract import canonicalize_plan, canonical_verification
from _relay import reserve_ready_frontier
from codex_autopilot.ai_studio import AIStudioRuntime, ContextBoundaryError
from _plan_contract import initialize_verified_project as initialize_project
from codex_autopilot.config import load_config
from codex_autopilot.department_acceptance import (
    DepartmentAcceptanceError,
    RubricReference,
    load_department_rubric,
    load_task_department_acceptance,
    require_rubric_attestation,
    store_department_rubric,
    task_department_binding,
)
from codex_autopilot.lifecycle import DesktopLifecycleError, complete_desktop_worker
from codex_autopilot.lifecycle_base import WorkerProtocolError
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.memory_mcp import MemoryMcpServer
from codex_autopilot.plan import validate_plan
from codex_autopilot.run_state import StateStore
from codex_autopilot.task_state import TaskState
from codex_autopilot.verification import VERIFICATION_PREFIX, verifier_route


STALE_PLAN_REFERENCE = RubricReference(
    record_id="FACT-004",
    version=1,
    sha256="0" * 64,
)


def _task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    department_bound: bool = True,
) -> dict[str, object]:
    resources: list[dict[str, object]] = []
    dependency_outputs: list[str] = []
    verifier_role = "builder"
    if department_bound:
        resources = [
            {
                "id": "department-binding",
                "kind": "logical",
                "target": "department-id:runtime-engineering",
                "access": "read",
            },
            {
                "id": "rubric-binding",
                "kind": "logical",
                "target": (
                    "project-memory:department-acceptance-rubric:runtime-engineering"
                ),
                "access": "read",
            },
        ]
        dependency_outputs = ["M0R"]
        verifier_role = "runtime-engineering-lead"
    return {
        "id": task_id,
        "title": f"Build {task_id}",
        "objective": f"Produce {task_id}.",
        "definition_of_done": [f"{task_id} is accepted against the department rubric."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files and shell checks are sufficient.",
        "reasoning": "medium",
        "role": "builder",
        "depends_on": list(depends_on),
        "priority": 0,
        "verification": canonical_verification(verifier_role=verifier_role),
        "resources": resources,
        "required_capabilities": [],
        "context": {"dependency_outputs": dependency_outputs},
        "outputs": (
            [
                {
                    "id": "runtime-engineering-rubric-reference",
                    "description": "Verified immutable Runtime Engineering rubric tuple.",
                    "required": True,
                }
            ]
            if task_id == "M0R"
            else []
        ),
        "tags": [],
    }


def _plan() -> dict[str, object]:
    return canonicalize_plan({
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Exercise department-owned acceptance.",
        "user_request": "A department lead must accept each task against pinned rubric data.",
        "model_strategy": "auto",
        "execution_strategy": "serial",
        "max_parallel_workers": 1,
        "computer_use_slots": 1,
        "roles": [
            {
                "id": "builder",
                "name": "Runtime Engineer",
                "responsibilities": ["Build runtime changes."],
            },
            {
                "id": "runtime-engineering-lead",
                "name": "Runtime Engineering Lead",
                "responsibilities": ["Accept runtime changes."],
            },
        ],
        "departments": [],
        "tasks": [
            _task("M0R", department_bound=False),
            _task("A", depends_on=("M0R",)),
            _task("B", depends_on=("M0R", "A")),
        ],
    })


def _with_stale_rubric_guidance(raw: dict[str, object]) -> dict[str, object]:
    lead = raw["roles"][1]
    lead.update(
        domain_focus=[
            'department={"id":"runtime-engineering","rubric":'
            '{"record_id":"FACT-004","version":1,"sha256":"'
            + "f" * 64
            + '"}}',
            "Runtime acceptance boundaries.",
        ],
        context_priorities=[
            "Pinned Project Memory record FACT-004.",
            "Original request and reproduced evidence.",
        ],
        verification_expectations=[
            "Reject self-assessment and unverifiable claims.",
            "Attest rubric record_id FACT-004, version 1, sha256 "
            + "f" * 64
            + ".",
        ],
    )
    raw["tasks"][1]["definition_of_done"].append(
        "The invalid rubric reference FACT-004 is not used."
    )
    return raw


class DepartmentAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")
        self.memory = ProjectMemory(self.root)
        evidence_path = self.root / "rubric-evidence.txt"
        evidence_path.write_text("department standard approved\n", encoding="utf-8")
        evidence = self.memory.record_evidence(
            kind="file",
            summary="Reviewed source for the initial department rubric.",
            path="rubric-evidence.txt",
            created_by="department-acceptance-test",
        )
        self.reference = store_department_rubric(
            self.memory,
            department_id="runtime-engineering",
            version=1,
            criteria=[
                {
                    "id": "correctness",
                    "requirement": "Every Definition of Done item has reproduced evidence.",
                }
            ],
            standards=["Reject claims that are not backed by Project Memory evidence."],
            evidence_ids=[str(evidence["id"])],
            created_by="Runtime Engineering Lead",
        )
        self.reference_evidence = self.memory.record_evidence(
            kind="tool",
            summary="M0R produced the immutable Runtime Engineering rubric tuple.",
            command="load verified rubric reference",
            tool_name="ProjectMemory.get_record",
            result=json.dumps(
                {
                    "department_id": "runtime-engineering",
                    "record_id": self.reference.record_id,
                    "version": self.reference.version,
                    "sha256": self.reference.sha256,
                    "scope": "department-acceptance-rubric:runtime-engineering",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            exit_code=0,
            milestone_id="M0R",
            created_by="Runtime Engineering Lead",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_contract_round_trip_and_lead_route_are_deterministic(self) -> None:
        plan = validate_plan(_plan(), "adaptive")
        task = plan.task_map["A"]
        runtime = AIStudioRuntime(
            plan,
            self.root,
            language="en",
            skill_path=self.skill,
            memory=self.memory,
        )
        context = runtime.select_context(
            "A",
            task_states={"M0R": "VERIFIED", "A": "VERIFYING", "B": "WAITING"},
        )
        acceptance = load_task_department_acceptance(
            self.memory,
            departments=plan.departments,
            task=task,
            role_names={item.id: item.name for item in plan.roles},
            dependency_outputs=context.dependency_outputs,
        )

        self.assertEqual(verifier_route(plan, task).role_id, "runtime-engineering-lead")
        self.assertEqual(plan.departments, ())
        self.assertEqual(acceptance.department.rubric, self.reference)
        self.assertEqual(acceptance.source.evidence_id, self.reference_evidence["id"])

    def test_missing_ambiguous_and_inconsistent_mappings_fail_closed(self) -> None:
        cases: list[tuple[str, callable]] = [
            (
                "missing task department",
                lambda raw: raw["tasks"][1]["resources"].pop(0),
            ),
            (
                "unknown task department",
                lambda raw: raw["tasks"][1]["resources"][0].update(
                    target="department-id:missing"
                ),
            ),
            (
                "ambiguous department",
                lambda raw: raw["departments"].extend(
                    [
                        {
                            "id": "runtime-engineering",
                            "name": "Runtime Engineering",
                            "lead_role_id": "runtime-engineering-lead",
                            "rubric": STALE_PLAN_REFERENCE.to_dict(),
                        },
                        {
                            "id": "runtime-engineering",
                            "name": "Runtime Engineering",
                            "lead_role_id": "runtime-engineering-lead",
                            "rubric": STALE_PLAN_REFERENCE.to_dict(),
                        },
                    ]
                ),
            ),
            (
                "unknown Lead Role",
                lambda raw: raw["departments"].append(
                    {
                        "id": "runtime-engineering",
                        "name": "Runtime Engineering",
                        "lead_role_id": "missing-lead",
                        "rubric": STALE_PLAN_REFERENCE.to_dict(),
                    }
                ),
            ),
            (
                "inconsistent verifier role",
                lambda raw: raw["tasks"][1]["verification"].update(
                    verifier_role="builder"
                ),
            ),
        ]
        for label, mutate in cases:
            with self.subTest(label=label):
                raw = _plan()
                mutate(raw)
                with self.assertRaisesRegex(
                    ValueError,
                    "department|Lead Role|conflicts|rubric-binding",
                ):
                    validate_plan(raw, "adaptive")

    def test_tags_are_not_department_bindings(self) -> None:
        raw = _plan()
        task = raw["tasks"][1]
        task["resources"] = []
        task["context"] = {}
        task["tags"] = ["department_id=runtime-engineering"]
        task["verification"].update(verifier_role="builder")

        plan = validate_plan(raw, "adaptive")

        self.assertIsNone(task_department_binding(plan.task_map["A"]))
        self.assertEqual(verifier_route(plan, plan.task_map["A"]).role_id, "builder")

    def test_dependency_rubric_tuple_is_required_and_unambiguous(self) -> None:
        plan = validate_plan(_plan(), "adaptive")
        task = plan.task_map["A"]
        non_tuple_evidence_id = str(
            self.memory.get_record(self.reference.record_id)["evidence"][0]["id"]
        )
        missing = [
            {
                "dependency_task_id": "M0R",
                "dependency_state": "VERIFIED",
                "output_id": "runtime-engineering-rubric-reference",
                "evidence_ids": [non_tuple_evidence_id],
            }
        ]
        with self.assertRaisesRegex(DepartmentAcceptanceError, "no rubric reference"):
            load_task_department_acceptance(
                self.memory,
                departments=plan.departments,
                task=task,
                role_names={item.id: item.name for item in plan.roles},
                dependency_outputs=missing,
            )

        conflicting = self.memory.record_evidence(
            kind="tool",
            summary="A conflicting tuple must not be selected.",
            command="emit stale tuple",
            tool_name="test",
            result=json.dumps(
                {
                    "department_id": "runtime-engineering",
                    "record_id": "FACT-004",
                    "version": 1,
                    "sha256": "0" * 64,
                    "scope": "department-acceptance-rubric:runtime-engineering",
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            exit_code=0,
            created_by="department-acceptance-test",
        )
        ambiguous = [
            {
                "dependency_task_id": "M0R",
                "dependency_state": "VERIFIED",
                "output_id": "runtime-engineering-rubric-reference",
                "evidence_ids": [
                    str(self.reference_evidence["id"]),
                    str(conflicting["id"]),
                ],
            }
        ]
        with self.assertRaisesRegex(DepartmentAcceptanceError, "ambiguous"):
            load_task_department_acceptance(
                self.memory,
                departments=plan.departments,
                task=task,
                role_names={item.id: item.name for item in plan.roles},
                dependency_outputs=ambiguous,
            )

    def test_static_department_rubric_does_not_override_dependency_output(self) -> None:
        raw = _plan()
        raw["departments"] = [
            {
                "id": "runtime-engineering",
                "name": "Runtime Engineering",
                "lead_role_id": "runtime-engineering-lead",
                "rubric": STALE_PLAN_REFERENCE.to_dict(),
            }
        ]
        plan = validate_plan(raw, "adaptive")
        task = plan.task_map["A"]
        context = AIStudioRuntime(
            plan,
            self.root,
            language="en",
            skill_path=self.skill,
            memory=self.memory,
        ).select_context(
            "A",
            task_states={"M0R": "VERIFIED", "A": "VERIFYING", "B": "WAITING"},
        )

        acceptance = load_task_department_acceptance(
            self.memory,
            departments=plan.departments,
            task=task,
            role_names={item.id: item.name for item in plan.roles},
            dependency_outputs=context.dependency_outputs,
        )

        self.assertEqual(plan.departments[0].rubric, STALE_PLAN_REFERENCE)
        self.assertEqual(acceptance.department.rubric, self.reference)

    def test_rubric_versions_are_immutable_and_changes_need_outcome_evidence(self) -> None:
        identical = store_department_rubric(
            self.memory,
            department_id="runtime-engineering",
            version=1,
            criteria=[
                {
                    "id": "correctness",
                    "requirement": "Every Definition of Done item has reproduced evidence.",
                }
            ],
            standards=["Reject claims that are not backed by Project Memory evidence."],
            evidence_ids=[str(self.memory.get_record(self.reference.record_id)["evidence"][0]["id"])],
            created_by="Runtime Engineering Lead",
        )
        self.assertEqual(identical, self.reference)

        weak = self.memory.record_evidence(
            kind="user_instruction",
            summary="A single proposal to relax the rubric.",
            user_instruction="Relax the rubric.",
            created_by="department-acceptance-test",
        )
        with self.assertRaisesRegex(DepartmentAcceptanceError, "outcome evidence"):
            store_department_rubric(
                self.memory,
                department_id="runtime-engineering",
                version=2,
                criteria=[{"id": "correctness", "requirement": "Relaxed."}],
                evidence_ids=[str(weak["id"])],
                created_by="Runtime Engineering Lead",
            )

        outcome = self.memory.record_evidence(
            kind="test",
            summary="Comparative acceptance replay improved the outcome.",
            command="replay department rubric",
            result="rejection precision improved",
            exit_code=0,
            created_by="department-acceptance-test",
        )
        second = store_department_rubric(
            self.memory,
            department_id="runtime-engineering",
            version=2,
            criteria=[{"id": "correctness", "requirement": "Reproduce all acceptance evidence."}],
            evidence_ids=[str(outcome["id"])],
            created_by="Runtime Engineering Lead",
        )
        self.assertEqual(second.version, 2)
        with self.assertRaisesRegex(DepartmentAcceptanceError, "immutable"):
            store_department_rubric(
                self.memory,
                department_id="runtime-engineering",
                version=2,
                criteria=[{"id": "correctness", "requirement": "Changed in place."}],
                evidence_ids=[str(outcome["id"])],
                created_by="Runtime Engineering Lead",
            )

    def test_each_fresh_prompt_loads_the_exact_pinned_memory_rubric(self) -> None:
        plan = validate_plan(_plan(), "adaptive")
        first = AIStudioRuntime(
            plan,
            self.root,
            language="en",
            skill_path=self.skill,
            memory=self.memory,
        ).build_prompt(
            "A",
            phase="verification",
            task_states={"M0R": "VERIFIED", "A": "VERIFYING", "B": "WAITING"},
            reservation_token="first",
            verification_round=1,
        )
        second = AIStudioRuntime(
            plan,
            self.root,
            language="en",
            skill_path=self.skill,
            memory=self.memory,
        ).build_prompt(
            "A",
            phase="verification",
            task_states={"M0R": "VERIFIED", "A": "VERIFYING", "B": "WAITING"},
            reservation_token="second",
            verification_round=2,
        )
        for prompt in (first, second):
            self.assertIn(self.reference.record_id, prompt)
            self.assertIn(self.reference.sha256, prompt)
            self.assertNotIn(STALE_PLAN_REFERENCE.record_id, prompt)
            self.assertIn("department_acceptance", prompt)
            self.assertIn('"rubric":{', prompt)
            self.assertIn('"dependency_task_id":"M0R"', prompt)

        with sqlite3.connect(self.memory.path) as db:
            db.execute(
                "UPDATE records SET statement=? WHERE id=?",
                ('{"changed":true}', self.reference.record_id),
            )
            db.commit()
        with self.assertRaisesRegex(ContextBoundaryError, "rubric"):
            AIStudioRuntime(
                plan,
                self.root,
                language="en",
                skill_path=self.skill,
                memory=self.memory,
            ).build_prompt(
                "A",
                phase="verification",
                    task_states={
                        "M0R": "VERIFIED",
                        "A": "VERIFYING",
                        "B": "WAITING",
                    },
                reservation_token="third",
                verification_round=3,
            )

    def test_m1_prompt_omits_superseded_rubric_identity_from_role_and_dod(self) -> None:
        plan = validate_plan(_with_stale_rubric_guidance(_plan()), "adaptive")

        prompt = AIStudioRuntime(
            plan,
            self.root,
            language="en",
            skill_path=self.skill,
            memory=self.memory,
        ).build_prompt(
            "A",
            phase="verification",
            task_states={"M0R": "VERIFIED", "A": "VERIFYING", "B": "WAITING"},
            reservation_token="m1-regression",
            verification_round=1,
        )

        self.assertNotIn("FACT-004", prompt)
        self.assertNotIn("f" * 64, prompt)
        self.assertIn(self.reference.record_id, prompt)
        self.assertIn(self.reference.sha256, prompt)
        self.assertIn("<superseded-department-rubric-record>", prompt)
        self.assertIn("Runtime acceptance boundaries.", prompt)
        self.assertIn("Original request and reproduced evidence.", prompt)
        self.assertIn("Reject self-assessment and unverifiable claims.", prompt)

    def test_attestation_rejects_missing_or_different_rubric(self) -> None:
        with self.assertRaisesRegex(DepartmentAcceptanceError, "must attest"):
            require_rubric_attestation(self.reference, None)
        with self.assertRaisesRegex(DepartmentAcceptanceError, "different rubric"):
            require_rubric_attestation(
                self.reference,
                RubricReference(
                    record_id=self.reference.record_id,
                    version=self.reference.version,
                    sha256="f" * 64,
                ),
            )

    def test_mcp_is_a_production_storage_path_for_versioned_rubrics(self) -> None:
        server = MemoryMcpServer(self.root)
        outcome = self.memory.record_evidence(
            kind="test",
            summary="Outcome evidence for rubric v2.",
            command="compare rubric outcomes",
            result="PASS",
            exit_code=0,
            created_by="department-acceptance-test",
        )
        stored = server.actions["store_department_rubric"](
            {
                "department_id": "runtime-engineering",
                "version": 2,
                "criteria": [{"id": "correctness", "requirement": "Reproduce evidence."}],
                "standards": [],
                "evidence_ids": [str(outcome["id"])],
                "created_by": "Runtime Engineering Lead",
            }
        )
        self.assertEqual(stored["version"], 2)
        self.assertEqual(len(stored["sha256"]), 64)

    def test_live_lifecycle_uses_lead_title_and_rejects_missing_attestation(self) -> None:
        plan_file = self.root / "plan-input.json"
        plan_file.write_text(
            json.dumps(_with_stale_rubric_guidance(_plan())),
            encoding="utf-8",
        )
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
        )
        cfg = load_config(self.root)
        state_store = StateStore(self.root / ".codex-autopilot")
        state = state_store.load()
        state.task_states["M0R"] = TaskState.VERIFIED.value
        state_store.save(state)
        with mock.patch(
            "codex_autopilot.lifecycle_reservations.require_trusted_stop_hook_for_config"
        ):
            patch_hook_trust_gates(self)
            implementation = reserve_ready_frontier(cfg)[0]
            activate_via_app_server(cfg, self.root, implementation, "implementation-thread")
            bump_task_checkpoint(self.root, "A", "implementation complete")
            self.memory.record_evidence(
                kind="test",
                summary="Implementation evidence.",
                command="run implementation check",
                result="PASS",
                exit_code=0,
                milestone_id="A",
                created_by="department-acceptance-test",
            )
            outcome = complete_desktop_worker(
                cfg,
                thread_id="implementation-thread",
                turn_id="implementation-turn",
                final_message="AUTOPILOT_RULES: R30\nAUTOPILOT_STATUS: ROTATE",
            )

        verifier = outcome.descriptors[0]
        self.assertEqual(
            verifier.title,
            "Runtime Engineering Lead | Verify A | Build A",
        )
        self.assertIn(self.reference.record_id, verifier.prompt)
        self.assertNotIn("FACT-004", verifier.prompt)
        activate_via_app_server(cfg, self.root, verifier, "verifier-thread")
        bump_task_checkpoint(self.root, "A", "verifier reviewed")
        self.memory.record_evidence(
            kind="test",
            summary="Independent department acceptance evidence.",
            command="reproduce acceptance",
            result="PASS",
            exit_code=0,
            milestone_id="A",
            role="independent_verification",
            created_by="Runtime Engineering Lead",
        )
        original_statement = str(
            self.memory.get_record(self.reference.record_id)["statement"]
        )
        changed_statement = json.loads(original_statement)
        changed_statement["criteria"][0]["requirement"] = "Changed after launch."
        with sqlite3.connect(self.memory.path) as db:
            db.execute(
                "UPDATE records SET statement=? WHERE id=?",
                (
                    json.dumps(
                        changed_statement,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    self.reference.record_id,
                ),
            )
            db.commit()
        # Ошибка поведения модели, а не поломка машины: иначе отказ
        # валит диспетчер и останавливает прогон целиком (A3).
        with self.assertRaisesRegex(WorkerProtocolError, "digest changed"):
            complete_desktop_worker(
                cfg,
                thread_id="verifier-thread",
                turn_id="verifier-turn-changed",
                final_message=(
                    "AUTOPILOT_RULES: R30\n"
                    + VERIFICATION_PREFIX
                    + json.dumps(
                        {
                            "verdict": "PASS",
                            "issues": [],
                            "rubric": self.reference.to_dict(),
                        },
                        separators=(",", ":"),
                    )
                ),
            )
        with sqlite3.connect(self.memory.path) as db:
            db.execute(
                "UPDATE records SET statement=? WHERE id=?",
                (original_statement, self.reference.record_id),
            )
            db.commit()
        # Ошибка поведения модели, а не поломка машины: иначе отказ
        # валит диспетчер и останавливает прогон целиком (A3).
        with self.assertRaisesRegex(WorkerProtocolError, "must attest"):
            complete_desktop_worker(
                cfg,
                thread_id="verifier-thread",
                turn_id="verifier-turn-missing",
                final_message=(
                    "AUTOPILOT_RULES: R30\n"
                    + VERIFICATION_PREFIX
                    + '{"verdict":"PASS","issues":[]}'
                ),
            )
        self.assertEqual(
            StateStore(self.root / ".codex-autopilot").load().task_states["A"],
            TaskState.VERIFYING.value,
        )

        accepted = complete_desktop_worker(
            cfg,
            thread_id="verifier-thread",
            turn_id="verifier-turn-pass",
            final_message=(
                "AUTOPILOT_RULES: R30\n"
                + VERIFICATION_PREFIX
                + json.dumps(
                    {
                        "verdict": "PASS",
                        "issues": [],
                        "rubric": self.reference.to_dict(),
                    },
                    separators=(",", ":"),
                )
            ),
        )
        self.assertEqual([item.task_id for item in accepted.descriptors], ["B"])
        verification = self.memory.list_verification_results(task_id="A", limit=8).records
        record = next(item for item in verification if item["check_id"] == "independent-acceptance")
        details = self.memory.get_verification_result(record["id"])["details"]
        self.assertEqual(
            details["department_rubric"]["reference"],
            self.reference.to_dict(),
        )

    def test_loaded_rubric_rejects_wrong_scope(self) -> None:
        department = replace(
            validate_plan(
                {
                    **_plan(),
                    "departments": [
                        {
                            "id": "runtime-engineering",
                            "name": "Runtime Engineering",
                            "lead_role_id": "runtime-engineering-lead",
                            "rubric": self.reference.to_dict(),
                        }
                    ],
                },
                "adaptive",
            ).departments[0],
            rubric=self.reference,
        )
        with sqlite3.connect(self.memory.path) as db:
            db.execute(
                "UPDATE records SET scope='project' WHERE id=?",
                (self.reference.record_id,),
            )
            db.commit()
        with self.assertRaisesRegex(DepartmentAcceptanceError, "not verified department memory"):
            load_department_rubric(self.memory, department)


if __name__ == "__main__":
    unittest.main()
