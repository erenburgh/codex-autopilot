from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from codex_autopilot.appserver import TurnResult
from codex_autopilot.bootstrap import initialize_project
from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
from _relay import reserve_ready_frontier  # R21: без зависимости от окружения
from codex_autopilot.lifecycle import (
    DESKTOP_SLOT_READY,
    WORKSPACE_HANDOFF_OK,
    acknowledge_desktop_create,
    acknowledge_desktop_send,
    complete_desktop_worker,
    create_descriptor_payload,
    prepare_desktop_thread,
    production_send_payload,
)
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.run_state import StateStore
from codex_autopilot.task_state import TaskState
from codex_autopilot.verification import (
    VERIFICATION_PREFIX,
    VerificationProtocolError,
    parse_verifier_result,
)


class FakePrepClient:
    def __init__(
        self,
        canonical_cwd: Path,
        *,
        slot_turn_id: str,
        slot_prompt: str,
    ) -> None:
        self.canonical_cwd = canonical_cwd
        self.cwd = canonical_cwd.parent / "saved-project"
        self.process_exited = False
        self.name: str | None = None
        self.turns = [
            {
                "id": slot_turn_id,
                "status": "completed",
                "items": [
                    {"type": "userMessage", "text": slot_prompt},
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": DESKTOP_SLOT_READY,
                    },
                ],
            }
        ]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.process_exited = True

    def resume_thread(self, thread_id):
        return {"thread": {"id": thread_id, "cwd": str(self.cwd)}}

    def start_plain_turn(self, *, thread_id, cwd, **_kwargs):
        self.cwd = cwd
        return {"turn": {"id": f"prep-{thread_id}"}}

    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        return TurnResult(
            thread_id,
            {
                "id": turn_id,
                "status": "completed",
                "items": [
                    {
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": WORKSPACE_HANDOFF_OK,
                    }
                ],
            },
            [],
        )

    def name_thread(self, _thread_id, name):
        self.name = name

    def read_thread(self, thread_id):
        return {
            "id": thread_id,
            "cwd": str(self.cwd),
            "name": self.name,
            "projectId": None,
            "turns": self.turns,
        }


def task(
    task_id: str,
    *,
    depends_on: tuple[str, ...] = (),
    policy: str = "self",
    required: bool = True,
    checks: list[dict[str, object]] | None = None,
    verifier_role: str | None = None,
    verifier_mode: str | None = None,
    max_revisions: int = 2,
) -> dict[str, object]:
    verification: dict[str, object] = {
        "policy": policy,
        "required": required,
        "deterministic_checks": checks or [],
        "max_revision_attempts": max_revisions,
    }
    if verifier_role:
        verification["verifier_role"] = verifier_role
    if verifier_mode:
        verification["execution_mode"] = verifier_mode
        verification["execution_mode_reason"] = (
            "The verifier must exercise the declared Computer Use capability."
        )
        verification["reasoning"] = "high"
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Produce the verified result for {task_id}.",
        "definition_of_done": [
            f"{task_id} artifact is correct.",
            f"{task_id} evidence is independently reproducible.",
        ],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files and shell-free checks are sufficient.",
        "reasoning": "medium",
        "role": "builder",
        "depends_on": list(depends_on),
        "priority": 0,
        "verification": verification,
        "resources": [],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
    }


def graph(first: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 3,
        "graph_version": 1,
        "goal": "Exercise verification and revision lifecycle.",
        "user_request": "Exercise verification and revision lifecycle exactly as specified.",
        "model_strategy": "auto",
        "execution_strategy": "serial",
        "max_parallel_workers": 1,
        "computer_use_slots": 1,
        "roles": [
            {
                "id": "builder",
                "name": "Builder",
                "responsibilities": ["Implement scoped corrections."],
            },
            {
                "id": "reviewer",
                "name": "Independent Reviewer",
                "responsibilities": ["Verify evidence without trusting worker claims."],
                "verification_expectations": ["Reproduce every required result."],
            },
        ],
        "tasks": [
            first,
            task("B", depends_on=("A",), required=False),
        ],
    }


class VerificationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hook_gate = mock.patch(
            "codex_autopilot.lifecycle.require_trusted_stop_hook_for_config"
        )
        self.hook_gate.start()
        self.addCleanup(self.hook_gate.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def initialize(
        self,
        first: dict[str, object],
        *,
        model_strategy: str = "auto",
    ) -> None:
        plan_file = self.root / "plan-input.json"
        payload = graph(first)
        payload["model_strategy"] = model_strategy
        plan_file.write_text(json.dumps(payload), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
            worker_surface=DESKTOP_OWNED_SURFACE,
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.root / ".codex-autopilot")
        self.memory = ProjectMemory(self.root)

    def activate(self, descriptor, thread_id: str) -> None:
        create_descriptor_payload(self.cfg, descriptor.reservation_token)
        acknowledge_desktop_create(
            self.cfg,
            descriptor.reservation_token,
            thread_id=thread_id,
            host_id="local",
            slot_turn_id=f"slot-{thread_id}",
            slot_final_message=DESKTOP_SLOT_READY,
        )
        client = FakePrepClient(
            self.root,
            slot_turn_id=f"slot-{thread_id}",
            slot_prompt=descriptor.slot_prompt(),
        )
        prepare_desktop_thread(
            self.cfg,
            descriptor.reservation_token,
            client_factory=lambda *_args: client,
        )
        self.assertTrue(client.process_exited)
        production_send_payload(self.cfg, descriptor.reservation_token)
        acknowledge_desktop_send(
            self.cfg,
            descriptor.reservation_token,
            thread_id=thread_id,
        )

    def evidence(self, task_id: str, label: str, *, role: str = "verification") -> str:
        handoff = self.root / ".codex-autopilot" / "HANDOFF.md"
        handoff.write_text(
            handoff.read_text(encoding="utf-8") + f"\nCompleted: {label}\n",
            encoding="utf-8",
        )
        item = self.memory.record_evidence(
            kind="test",
            summary=f"Evidence for {label}.",
            created_by="verification-lifecycle-test",
            milestone_id=task_id,
            role=role,
            command=f"verify {label}",
            result="PASS",
            exit_code=0,
        )
        return str(item["id"])

    def test_self_policy_evidence_never_replaces_independent_acceptance(self) -> None:
        self.initialize(task("A", policy="self"))
        implementation = reserve_ready_frontier(self.cfg)[0]
        self.activate(implementation, "implementation-thread")
        self.evidence("A", "self verification")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-thread",
            turn_id="implementation-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        self.assertEqual([item.task_id for item in outcome.descriptors], ["A"])
        self.assertEqual(outcome.descriptors[0].kind, "verifier")
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.VERIFYING.value)
        self.assertEqual(
            [item["kind"] for item in state.worker_sessions if item["task_id"] == "A"],
            ["implementation", "verifier"],
        )

    def test_false_success_revise_revision_then_fresh_verifier_unlocks_dependency(self) -> None:
        self.initialize(
            task(
                "A",
                policy="independent",
                verifier_role="reviewer",
                max_revisions=2,
            )
        )
        implementation = reserve_ready_frontier(self.cfg)[0]
        self.activate(implementation, "implementation-thread")
        implementation_evidence = self.evidence("A", "false implementation")
        first = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-thread",
            turn_id="implementation-turn",
            final_message=(
                "FALSE-SUCCESS-SELF-ASSESSMENT: everything is perfect.\n"
                "AUTOPILOT_STATUS: ROTATE"
            ),
        )

        self.assertEqual(len(first.descriptors), 1)
        verifier_one = first.descriptors[0]
        self.assertEqual(verifier_one.kind, "verifier")
        self.assertEqual(
            verifier_one.title,
            "Independent Reviewer Verifier | A | Verify Task A",
        )
        self.assertIn(implementation_evidence, verifier_one.prompt)
        self.assertNotIn("FALSE-SUCCESS-SELF-ASSESSMENT", verifier_one.prompt)
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.VERIFYING.value)
        self.assertEqual(state.task_states["B"], TaskState.WAITING.value)
        self.assertEqual(
            next(
                item["detail"]
                for item in state.lifecycle_journal
                if item["event"] == "implementation_completed"
            ),
            TaskState.IMPLEMENTED.value,
        )

        self.activate(verifier_one, "verifier-thread-1")
        self.evidence("A", "independent rejection", role="independent_verification")
        revise_payload = {
            "verdict": "REVISE",
            "issues": [
                {
                    "code": "ISSUE-OUTPUT",
                    "summary": "Output is incorrect",
                    "details": "Observed stale output; regenerate and re-run the assertion.",
                    "dod_refs": [1, 2],
                }
            ],
        }
        second = complete_desktop_worker(
            self.cfg,
            thread_id="verifier-thread-1",
            turn_id="verifier-turn-1",
            final_message=(
                "PRIVATE VERIFIER TRANSCRIPT MUST NOT PROPAGATE\n"
                + VERIFICATION_PREFIX
                + json.dumps(revise_payload, separators=(",", ":"))
            ),
        )
        revision = second.descriptors[0]
        recorded_rejection = self.memory.list_verification_results(
            task_id="A", limit=8
        ).records
        self.assertEqual(len(recorded_rejection), 1)
        self.assertEqual(recorded_rejection[0]["verdict"], "REVISE")
        self.assertEqual(
            recorded_rejection[0]["provider_thread_id"], "verifier-thread-1"
        )
        self.assertEqual(recorded_rejection[0]["provider_turn_id"], "verifier-turn-1")
        self.assertEqual(revision.kind, "revision")
        self.assertEqual(revision.title, "Builder | A-R1 | Revise Task A")
        self.assertIn("ISSUE-OUTPUT", revision.prompt)
        self.assertNotIn("PRIVATE VERIFIER TRANSCRIPT", revision.prompt)
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.REVISING.value)
        self.assertEqual(state.task_states["B"], TaskState.WAITING.value)
        self.assertEqual(state.task_revisions["A"], 1)

        self.activate(revision, "revision-thread-1")
        revision_evidence = self.evidence("A", "revision R1")
        third = complete_desktop_worker(
            self.cfg,
            thread_id="revision-thread-1",
            turn_id="revision-turn-1",
            final_message="Revision applied.\nAUTOPILOT_STATUS: ROTATE",
        )
        verifier_two = third.descriptors[0]
        self.assertEqual(verifier_two.kind, "verifier")
        self.assertEqual(
            verifier_two.title,
            "Independent Reviewer Verifier | A | Verify Task A",
        )
        self.assertIn(revision_evidence, verifier_two.prompt)
        self.assertNotEqual(verifier_one.reservation_token, verifier_two.reservation_token)

        self.activate(verifier_two, "verifier-thread-2")
        self.evidence("A", "independent pass", role="independent_verification")
        fourth = complete_desktop_worker(
            self.cfg,
            thread_id="verifier-thread-2",
            turn_id="verifier-turn-2",
            final_message=(
                "All criteria reproduced.\n"
                + VERIFICATION_PREFIX
                + '{"verdict":"PASS","issues":[]}'
            ),
        )
        self.assertEqual([item.task_id for item in fourth.descriptors], ["B"])
        recorded_verdicts = self.memory.list_verification_results(
            task_id="A", limit=8
        ).records
        self.assertEqual(
            [item["verdict"] for item in recorded_verdicts], ["REVISE", "PASS"]
        )
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.VERIFIED.value)
        self.assertEqual(state.task_states["B"], TaskState.RUNNING.value)
        verifier_sessions = [
            item for item in state.worker_sessions if item["kind"] == "verifier"
        ]
        self.assertEqual([item["verification_round"] for item in verifier_sessions], [1, 2])
        self.assertEqual(
            {item["thread_id"] for item in verifier_sessions},
            {"verifier-thread-1", "verifier-thread-2"},
        )

    def test_exhaustive_deterministic_policy_is_precheck_not_acceptance(self) -> None:
        checks = [
            {
                "id": "command-check",
                "kind": "command",
                "description": "Artifact content is exact.",
                "argv": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('artifact.txt').read_text() == 'ok\\n'",
                ],
            },
            {
                "id": "artifact-check",
                "kind": "artifact",
                "description": "Artifact exists.",
                "path": "artifact.txt",
            },
            {
                "id": "implementation-proof",
                "kind": "evidence",
                "description": "Implementation supplied reproducible evidence.",
            },
        ]
        self.initialize(task("A", policy="deterministic", checks=checks))
        implementation = reserve_ready_frontier(self.cfg)[0]
        self.activate(implementation, "implementation-thread")
        (self.root / "artifact.txt").write_text("ok\n", encoding="utf-8")
        self.evidence("A", "deterministic implementation", role="implementation-proof")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-thread",
            turn_id="implementation-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )

        self.assertEqual([item.task_id for item in outcome.descriptors], ["A"])
        self.assertEqual(outcome.descriptors[0].kind, "verifier")
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.VERIFYING.value)
        self.assertTrue(any(item["kind"] == "verifier" for item in state.worker_sessions))
        event = next(
            item
            for item in state.lifecycle_journal
            if item["event"] == "deterministic_verification_completed"
        )
        self.assertIn('"verdict": "PASS"', event["detail"])
        roles = {
            item["role"] for item in self.memory.milestone_evidence("A", limit=100)
        }
        self.assertTrue({"command-check", "artifact-check"}.issubset(roles))
        verification_results = self.memory.list_verification_results(
            task_id="A", limit=8
        ).records
        self.assertEqual(
            {item["check_id"] for item in verification_results},
            {"command-check", "artifact-check", "implementation-proof"},
        )
        self.assertTrue(all(item["verdict"] == "PASS" for item in verification_results))
        session = next(
            item
            for item in self.store.load().worker_sessions
            if item["thread_id"] == "implementation-thread"
        )
        self.assertEqual(len(session["memory_verification_ids"]), 3)

    def test_failed_deterministic_check_creates_structured_revision(self) -> None:
        checks = [
            {
                "id": "failing-check",
                "kind": "command",
                "description": "The deterministic assertion passes.",
                "argv": [sys.executable, "-c", "raise SystemExit(7)"],
            }
        ]
        self.initialize(
            task(
                "A",
                policy="deterministic",
                checks=checks,
                max_revisions=1,
            )
        )
        implementation = reserve_ready_frontier(self.cfg)[0]
        self.activate(implementation, "implementation-thread")
        self.evidence("A", "deterministic implementation")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-thread",
            turn_id="implementation-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        revision = outcome.descriptors[0]
        self.assertEqual(revision.kind, "revision")
        self.assertIn("CHECK-failing-check", revision.prompt)
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.REVISING.value)
        self.assertEqual(state.task_states["B"], TaskState.WAITING.value)
        self.assertEqual(state.task_revisions["A"], 1)
        self.assertFalse(any(item["kind"] == "verifier" for item in state.worker_sessions))
        failed_result = self.memory.list_verification_results(
            task_id="A", limit=8
        ).records
        self.assertEqual(
            [(item["check_id"], item["verdict"]) for item in failed_result],
            [("failing-check", "REVISE")],
        )

    def test_independent_policy_ignores_local_checks_and_routes_verifier_capability(self) -> None:
        checks = [
            {
                "id": "must-not-short-circuit",
                "kind": "command",
                "description": "Independent policy must not use this for promotion.",
                "argv": [sys.executable, "-c", "raise SystemExit(9)"],
            }
        ]
        self.initialize(
            task(
                "A",
                policy="independent",
                checks=checks,
                verifier_role="reviewer",
                verifier_mode="computer_use",
            )
        )
        implementation = reserve_ready_frontier(self.cfg)[0]
        self.activate(implementation, "implementation-thread")
        self.evidence("A", "implementation")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-thread",
            turn_id="implementation-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )

        verifier = outcome.descriptors[0]
        self.assertEqual(verifier.kind, "verifier")
        self.assertEqual(verifier.execution_mode, "computer_use")
        self.assertEqual(verifier.model, "gpt-6-astra")
        self.assertEqual(verifier.thinking, "high")
        self.assertIn("Independent Reviewer", verifier.prompt)
        state = self.store.load()
        self.assertFalse(
            any(
                item["event"] == "deterministic_verification_completed"
                for item in state.lifecycle_journal
            )
        )
        verifier_lock = next(
            item
            for item in state.resource_locks
            if item["owner"]["ownership_token"] == verifier.reservation_token
        )
        self.assertEqual(verifier_lock["computer_use_slot"], 0)

    def test_unavailable_verifier_capability_blocks_fail_closed(self) -> None:
        self.initialize(
            task(
                "A",
                policy="independent",
                verifier_role="reviewer",
                verifier_mode="computer_use",
            ),
            model_strategy="sol-only",
        )
        implementation = reserve_ready_frontier(self.cfg)[0]
        self.activate(implementation, "implementation-thread")
        self.evidence("A", "implementation")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-thread",
            turn_id="implementation-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        self.assertEqual(outcome.descriptors, ())
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.BLOCKED.value)
        self.assertEqual(state.task_states["B"], TaskState.WAITING.value)
        self.assertIn("requires Computer Use", state.last_error)

    def test_auto_without_checks_selects_fresh_independent_verifier(self) -> None:
        self.initialize(task("A", policy="auto", verifier_role="reviewer"))
        implementation = reserve_ready_frontier(self.cfg)[0]
        self.activate(implementation, "implementation-thread")
        self.evidence("A", "implementation")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-thread",
            turn_id="implementation-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        self.assertEqual(outcome.descriptors[0].kind, "verifier")
        self.assertEqual(outcome.descriptors[0].model, "gpt-5.6-sol")

    def test_auto_with_declared_checks_still_requires_independent_acceptance(self) -> None:
        checks = [
            {
                "id": "artifact-check",
                "kind": "artifact",
                "description": "The declared artifact exists.",
                "path": "auto.txt",
            }
        ]
        self.initialize(task("A", policy="auto", checks=checks))
        implementation = reserve_ready_frontier(self.cfg)[0]
        self.activate(implementation, "implementation-thread")
        (self.root / "auto.txt").write_text("verified\n", encoding="utf-8")
        self.evidence("A", "auto implementation")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-thread",
            turn_id="implementation-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        self.assertEqual([item.task_id for item in outcome.descriptors], ["A"])
        self.assertEqual(outcome.descriptors[0].kind, "verifier")
        self.assertTrue(
            any(item["kind"] == "verifier" for item in self.store.load().worker_sessions)
        )

    def test_revision_limit_blocks_without_unlocking_dependency(self) -> None:
        self.initialize(
            task(
                "A",
                policy="independent",
                verifier_role="reviewer",
                max_revisions=0,
            )
        )
        implementation = reserve_ready_frontier(self.cfg)[0]
        self.activate(implementation, "implementation-thread")
        self.evidence("A", "implementation")
        verifier = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-thread",
            turn_id="implementation-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        ).descriptors[0]
        self.activate(verifier, "verifier-thread")
        self.evidence("A", "independent rejection", role="independent_verification")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="verifier-thread",
            turn_id="verifier-turn",
            final_message=(
                VERIFICATION_PREFIX
                + '{"verdict":"REVISE","issues":['
                '{"code":"I-1","summary":"incorrect","details":"correct it","dod_refs":[1]}]}'
            ),
        )
        self.assertEqual(outcome.descriptors, ())
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.BLOCKED.value)
        self.assertEqual(state.task_states["B"], TaskState.WAITING.value)
        self.assertEqual(state.task_revisions["A"], 0)
        self.assertTrue(
            any(item["event"] == "revision_limit_reached" for item in state.lifecycle_journal)
        )


class VerificationProtocolTests(unittest.TestCase):
    def test_pass_and_revise_are_strictly_parsed(self) -> None:
        passed = parse_verifier_result(
            'review complete\nAUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}'
        )
        self.assertEqual(passed.verdict, "PASS")
        revised = parse_verifier_result(
            'AUTOPILOT_VERIFICATION: {"verdict":"REVISE","issues":['
            '{"code":"I-1","summary":"bad","details":"fix it","dod_refs":[1]}]}'
        )
        self.assertEqual(revised.issues[0].code, "I-1")

    def test_ambiguous_or_unstructured_results_are_rejected(self) -> None:
        invalid = [
            'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}\ntrailing',
            'AUTOPILOT_VERIFICATION: {"verdict":"REVISE","issues":[]}',
            'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":['
            '{"code":"I","summary":"bad","details":"bad","dod_refs":[]}]}',
            'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}\n'
            'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}',
        ]
        for message in invalid:
            with self.subTest(message=message), self.assertRaises(VerificationProtocolError):
                parse_verifier_result(message)


if __name__ == "__main__":
    unittest.main()
