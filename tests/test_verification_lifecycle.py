from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from _gates import patch_hook_trust_gates

from codex_autopilot.appserver import TurnResult
from _plan_contract import initialize_verified_project as initialize_project
from codex_autopilot.config import DESKTOP_OWNED_SURFACE, load_config
from _handoff import bump_task_checkpoint
from _plan_contract import TEST_OUTCOME_ID, canonicalize_plan, canonical_verification
from _appserver_fakes import activate_via_app_server
from _relay import reserve_ready_frontier  # R21: no dependency on the environment
from codex_autopilot.lifecycle import (
    DESKTOP_SLOT_READY,
    WORKSPACE_HANDOFF_OK,
    acknowledge_desktop_send,
    complete_desktop_worker,
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
    policy: str = "independent",
    required: bool = True,
    checks: list[dict[str, object]] | None = None,
    verifier_role: str | None = None,
    verifier_mode: str | None = None,
    max_revisions: int = 2,
) -> dict[str, object]:
    verification = canonical_verification(
        checks=checks or (),
        verifier_role=verifier_role,
        max_revision_attempts=max_revisions,
    )
    verification["policy"] = policy
    verification["required"] = required
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
        "produces_outcomes": [TEST_OUTCOME_ID],
        "acceptance_class": "mixed",
    }


def graph(first: dict[str, object]) -> dict[str, object]:
    return canonicalize_plan({
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
            task("B", depends_on=("A",)),
        ],
    })


class VerificationLifecycleTests(unittest.TestCase):

    def activate(self, descriptor, thread_id: str):
        """The live path: how the production dispatcher raises a task."""
        return activate_via_app_server(self.cfg, self.root, descriptor, thread_id)

    def setUp(self) -> None:
        self.hook_gate = mock.patch(
            "codex_autopilot.lifecycle_reservations.require_trusted_stop_hook_for_config"
        )
        self.hook_gate.start()
        self.addCleanup(self.hook_gate.stop)
        # The hook-trust gate reads the machine's REAL App Server. Without this
        # substitution the suite passed only because the developer's hooks
        # happened to be trusted, and it collapsed right after reinstalling the plugin.
        patch_hook_trust_gates(self)
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
        payload: dict[str, object] | None = None,
    ) -> None:
        plan_file = self.root / "plan-input.json"
        payload = payload if payload is not None else graph(first)
        payload["model_strategy"] = model_strategy
        plan_file.write_text(json.dumps(payload), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.root / ".codex-autopilot")
        self.memory = ProjectMemory(self.root)


    def evidence(self, task_id: str, label: str, *, role: str = "verification") -> str:
        bump_task_checkpoint(self.root, task_id, f"Completed: {label}")
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

    def test_self_policy_is_rejected_before_any_task_exists(self) -> None:
        """R8: self-acceptance is impossible not because a verifier
        starts anyway, but because such a plan is not accepted at all.

        This test used to check something weaker: that with policy="self"
        a verifier is created all the same. That is exactly what did not
        work on the real run - eight tasks out of nine got VERIFIED in
        the same second as IMPLEMENTED.
        """
        with self.assertRaises(ValueError) as caught:
            self.initialize(task("A", policy="self"))
        message = str(caught.exception)
        self.assertIn("R8", message)
        self.assertIn("A", message)
        self.assertIn("self", message)

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
        recorded_rejection = [
            item
            for item in recorded_rejection
            if item["check_id"] == "independent-acceptance"
        ]
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
            [
                item["verdict"]
                for item in recorded_verdicts
                if item["check_id"] == "independent-acceptance"
            ],
            ["REVISE", "PASS"],
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

    def test_deterministic_checks_are_admission_not_acceptance(self) -> None:
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
        self.initialize(task("A", checks=checks))
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
            {"suite", "command-check", "artifact-check", "implementation-proof"},
        )
        self.assertTrue(all(item["verdict"] == "PASS" for item in verification_results))
        session = next(
            item
            for item in self.store.load().worker_sessions
            if item["thread_id"] == "implementation-thread"
        )
        self.assertEqual(len(session["memory_verification_ids"]), 4)

    def test_correctly_labelled_outside_material_does_not_make_the_task_unacceptable(self) -> None:
        """A worker that obeys the memory tool must not break its own completion.

        The tool refuses evidence without a milestone_id while a task is
        active, and its own description says outside material MUST be
        recorded with kind "external". The acceptance then cited the whole
        milestone evidence list, and citing one external item refuses the
        entire set under R18 - the exception escaped complete_desktop_worker
        after the Stop hook had already fired, so the turn was lost and the
        task could not be accepted at all.

        External material stays recorded on the milestone. It is simply not
        cited as support for the verdict: external content does not decide.
        """

        self.initialize(task("A"))
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
        self.activate(verifier, "verifier-thread")
        self.evidence("A", "independent verification")
        outside = self.memory.record_evidence(
            kind="external",
            summary="Upstream issue quoted while verifying.",
            created_by="verification-lifecycle-test",
            milestone_id="A",
            provider="https://example.invalid/issue/1",
        )

        accepted = complete_desktop_worker(
            self.cfg,
            thread_id="verifier-thread",
            turn_id="verifier-turn",
            final_message='AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}',
        )
        self.assertEqual(self.store.load().task_states["A"], TaskState.VERIFIED.value)

        # The record keeps the outside material, and the acceptance does not
        # rest on it.
        roles = {
            str(item["id"]) for item in self.memory.milestone_evidence("A", limit=100)
        }
        self.assertIn(str(outside["id"]), roles)
        acceptance = next(
            item
            for item in self.memory.list_verification_results(task_id="A", limit=16).records
            if item["check_id"] == "independent-acceptance"
        )
        with self.memory._connect() as db:
            cited = {
                str(row["evidence_id"])
                for row in db.execute(
                    "SELECT evidence_id FROM verification_result_evidence WHERE verification_id=?",
                    (str(acceptance["id"]),),
                ).fetchall()
            }
        self.assertNotIn(str(outside["id"]), cited)
        self.assertTrue(cited, "the acceptance must still cite the deterministic evidence")
        del accepted

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
                checks=checks,
                max_revisions=2,
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
            [("suite", "PASS"), ("failing-check", "REVISE")],
        )

    def test_independent_policy_runs_checks_then_routes_verifier_capability(self) -> None:
        checks = [
            {
                "id": "admission-check",
                "kind": "command",
                "description": "Independent policy runs this before judgement.",
                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
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
        self.assertTrue(
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

    def test_auto_policy_is_rejected_before_initialization(self) -> None:
        with self.assertRaisesRegex(ValueError, 'must be "independent"'):
            self.initialize(task("A", policy="auto", verifier_role="reviewer"))

    def test_deterministic_policy_is_rejected_even_with_declared_checks(self) -> None:
        checks = [
            {
                "id": "artifact-check",
                "kind": "artifact",
                "description": "The declared artifact exists.",
                "path": "auto.txt",
            }
        ]
        with self.assertRaisesRegex(ValueError, 'must be "independent"'):
            self.initialize(task("A", policy="deterministic", checks=checks))

    def _reject_once(self, descriptor, round_index: int):
        """One acceptance round: work -> fresh verifier -> refusal."""
        self.activate(descriptor, f"work-thread-{round_index}")
        self.evidence("A", f"work {round_index}")
        verifier = complete_desktop_worker(
            self.cfg,
            thread_id=f"work-thread-{round_index}",
            turn_id=f"work-turn-{round_index}",
            final_message="AUTOPILOT_STATUS: ROTATE",
        ).descriptors[0]
        self.assertEqual(verifier.kind, "verifier")
        self.activate(verifier, f"verifier-thread-{round_index}")
        self.evidence("A", f"rejection {round_index}", role="independent_verification")
        return complete_desktop_worker(
            self.cfg,
            thread_id=f"verifier-thread-{round_index}",
            turn_id=f"verifier-turn-{round_index}",
            final_message=(
                VERIFICATION_PREFIX
                + '{"verdict":"REVISE","issues":['
                '{"code":"I-1","summary":"incorrect","details":"correct it","dod_refs":[1]}]}'
            ),
        )

    def test_exhausted_revision_budget_rehires_instead_of_stalling(self) -> None:
        """An acceptance refusal has no right to kill the run.

        Before, exhausting the revision budget put the task into BLOCKED
        and that was the end of it: neither the replanner nor the
        Pipeline Engineer started, and the CLI had no command that lifted
        BLOCKED. Now the task gets a fresh worker at the next effort
        step. The plan, the graph and the Definition of Done are not
        touched.
        """

        self.initialize(
            task("A", policy="independent", verifier_role="reviewer", max_revisions=2)
        )
        # The revision budget is two attempts: the re-hire comes on the third
        # refusal, not the first. The test used to set the budget to zero, but
        # the canonical acceptance contract requires at least two.
        descriptor = reserve_ready_frontier(self.cfg)[0]
        for index in range(3):
            outcome = self._reject_once(descriptor, index)
            if outcome.descriptors:
                descriptor = outcome.descriptors[0]

        self.assertEqual([item.kind for item in outcome.descriptors], ["revision"])
        self.assertEqual(outcome.descriptors[0].thinking, "high")
        state = self.store.load()
        self.assertEqual(state.task_rehires["A"], 1)
        self.assertEqual(state.task_effort["A"], "high")
        self.assertNotEqual(state.task_states["A"], TaskState.BLOCKED.value)
        self.assertEqual(state.task_states["B"], TaskState.WAITING.value)
        rehired = [
            json.loads(item["detail"])
            for item in state.lifecycle_journal
            if item["event"] == "task_rehired"
        ]
        self.assertEqual(rehired[-1]["effort_from"], "medium")
        self.assertEqual(rehired[-1]["effort_to"], "high")

    def test_definition_of_done_survives_every_rehire(self) -> None:
        """The worker and the method change, not the bar."""

        self.initialize(
            task("A", policy="independent", verifier_role="reviewer", max_revisions=2)
        )
        plan_file = self.cfg.state_dir / "plan.json"
        before = plan_file.read_text(encoding="utf-8")
        descriptor = reserve_ready_frontier(self.cfg)[0]
        for index in range(2):
            descriptor = self._reject_once(descriptor, index).descriptors[0]
        self.assertEqual(plan_file.read_text(encoding="utf-8"), before)
        self.assertEqual(self.store.load().graph_version, 1)

    def test_hiring_ladder_ends_at_the_owner_without_unlocking_dependency(self) -> None:
        """The ladder is finite: at its top the task really does stop.

        By the incident taxonomy the PRODUCTION class belongs to the
        product owner, and automatic quality repair is forbidden here.
        But the task has to stop at the top of the ladder, not on the
        first refusal.
        """

        self.initialize(
            task("A", policy="independent", verifier_role="reviewer", max_revisions=2)
        )
        descriptor = reserve_ready_frontier(self.cfg)[0]
        # Every ladder step costs three refusals: two revisions within the
        # budget plus the one on which the budget is exhausted.
        efforts: list[str] = []
        for index in range(16):
            outcome = self._reject_once(descriptor, index)
            if not outcome.descriptors:
                break
            descriptor = outcome.descriptors[0]
            if descriptor.thinking not in efforts:
                efforts.append(descriptor.thinking)
        else:
            self.fail("лестница найма не закончилась")

        # The first laps run at the task's own effort, and only an
        # exhausted budget raises it one step.
        self.assertEqual(efforts, ["medium", "high", "xhigh", "max"])
        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.BLOCKED.value)
        self.assertEqual(state.task_states["B"], TaskState.WAITING.value)
        self.assertEqual(state.task_rehires["A"], 3)
        self.assertTrue(
            any(item["event"] == "hiring_ladder_exhausted" for item in state.lifecycle_journal)
        )
        self.assertIn("hiring ladder", state.last_error)

        # The product owner must see exactly what acceptance rejected and
        # how many executors have already changed - otherwise they have nothing to decide with.
        from codex_autopilot.plan import load_plan
        from codex_autopilot.status import _waiting_reason

        reason = _waiting_reason(
            load_plan(self.cfg.state_dir, "adaptive"),
            state,
            "A",
            TaskState.BLOCKED,
            self.root,
        )
        self.assertIn("4 hire(s)", reason)
        self.assertIn("effort max", reason)
        self.assertIn("I-1", reason)
        self.assertIn("incorrect", reason)


    def test_a_task_at_the_top_of_the_ladder_does_not_freeze_its_neighbours(self) -> None:
        """One task stops - the neighbours that do not depend on it go on.

        The old hole had two halves: the task died on the first
        acceptance refusal and stopped the run along with itself.
        Rehiring closes the first half, this check closes the second.
        """

        payload = graph(task("A", policy="independent", verifier_role="reviewer", max_revisions=2))
        payload["execution_strategy"] = "parallel"
        payload["max_parallel_workers"] = 2
        payload["tasks"] = [
            payload["tasks"][0],
            task("C", policy="independent", verifier_role="reviewer"),
        ]
        self.initialize({}, payload=payload)

        frontier = {item.task_id: item for item in reserve_ready_frontier(self.cfg)}
        self.assertEqual(sorted(frontier), ["A", "C"])

        descriptor = frontier["A"]
        # Every ladder step costs three refusals: two revisions within the
        # budget plus the one on which the budget is exhausted.
        for index in range(16):
            outcome = self._reject_once(descriptor, index)
            if not outcome.descriptors:
                break
            descriptor = next(
                (item for item in outcome.descriptors if item.task_id == "A"), None
            )
            if descriptor is None:
                self.fail("A перестала получать исполнителей до вершины лестницы")
        else:
            self.fail("лестница найма не закончилась")

        state = self.store.load()
        self.assertEqual(state.task_states["A"], TaskState.BLOCKED.value)
        # The run does not declare itself BLOCKED while live work goes on: the
        # BLOCKED status rises only when no active task is left.
        self.assertEqual(state.status, "RUNNING")

        # The point: a run with A stalled keeps moving C.
        self.activate(frontier["C"], "c-thread")
        self.evidence("C", "c implementation")
        outcome = complete_desktop_worker(
            self.cfg,
            thread_id="c-thread",
            turn_id="c-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        self.assertEqual(
            [(item.task_id, item.kind) for item in outcome.descriptors],
            [("C", "verifier")],
        )
        self.assertEqual(self.store.load().task_states["A"], TaskState.BLOCKED.value)


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
