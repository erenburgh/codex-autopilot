from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from codex_autopilot.ai_studio import (
    AIStudioRuntime,
    HARD_MAX_MEMORY_RECORDS,
    MAX_INLINE_USER_REQUEST_CHARS,
    MAX_OUTPUT_EXCERPT_CHARS,
    MAX_PROMPT_CHARS,
    ContextBoundaryError,
)
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.plan import validate_plan
from codex_autopilot.verification import VerificationIssue
from _plan_contract import TEST_OUTCOME_ID, canonicalize_plan, canonical_verification


def role(role_id: str, name: str) -> dict:
    return {
        "id": role_id,
        "name": name,
        "responsibilities": [f"Own {name} results."],
        "domain_focus": [f"{role_id} domain"],
        "preferred_tools": [f"{role_id}-tool"],
        "context_priorities": [f"{role_id} facts first"],
        "verification_expectations": [f"Verify {role_id} independently."],
    }


def task(
    task_id: str,
    role_id: str,
    *,
    mode: str = "code",
    dependencies: list[str] | None = None,
    queries: list[str] | None = None,
    record_ids: list[str] | None = None,
    verifier_role: str | None = None,
    verifier_mode: str | None = None,
) -> dict:
    dependency_ids = dependencies or []
    verification = canonical_verification(verifier_role=verifier_role)
    if verifier_mode:
        verification.update(
            {
                "execution_mode": verifier_mode,
                "execution_mode_reason": "The verification capability requires this mode.",
                "reasoning": "high",
            }
        )
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Produce the structured {task_id} result.",
        "definition_of_done": [f"{task_id} result is evidenced."],
        "execution_mode": mode,
        "execution_mode_reason": (
            "Browser interaction is required."
            if mode == "computer_use"
            else "Repository and programmatic tools are sufficient."
        ),
        "reasoning": "high",
        "role": role_id,
        "depends_on": dependency_ids,
        "priority": 7,
        "verification": verification,
        "resources": [
            {
                "id": f"resource-{task_id}",
                "kind": "logical",
                "target": f"studio:{task_id}",
                "access": "write",
            }
        ],
        "required_capabilities": ["research"] if role_id == "researcher" else ["python"],
        "context": {
            "memory_queries": queries or [],
            "memory_record_ids": record_ids or [],
            "dependency_outputs": dependency_ids,
            "max_memory_records": 100,
            "max_dependency_outputs": 20,
        },
        "outputs": [
            {
                "id": f"output-{task_id}",
                "description": f"Bounded result from {task_id}.",
                "path": f"outputs/{task_id}.txt",
                "required": True,
            }
        ],
        "tags": [role_id, mode],
        "produces_outcomes": [TEST_OUTCOME_ID],
    }


def plan(tasks: list[dict]):
    return validate_plan(
        canonicalize_plan({
            "schema_version": 3,
            "goal": "Exercise one AI Studio role/task runtime.",
            "user_request": (
                "Deliver the requested behavior and judge it independently of "
                "implementation-authored tests."
            ),
            "model_strategy": "auto",
            "execution_strategy": "parallel",
            "max_parallel_workers": 3,
            "computer_use_slots": 1,
            "roles": [
                role("integrator", "Release Integrator"),
                role("researcher", "Fact Verification Specialist"),
                role("operator", "Desktop Workflow Operator"),
                role("reviewer", "Independent Evidence Reviewer"),
            ],
            "tasks": tasks,
        }),
        "adaptive",
    )


def context_payload(prompt: str) -> dict:
    raw = prompt.split("AUTOPILOT_CONTEXT: ", 1)[1].split("\n\n", 1)[0]
    return json.loads(raw)


class RunbookIsExecutableTests(unittest.TestCase):
    """Команду из рантбука дежурный инженер исполняет дословно.

    ``--failure-code`` стал обязательным у ``relay-fail``, а строка в
    рантбуке осталась прежней. Инженер выполнил бы её как написано и
    получил "error: the following arguments are required: --failure-code",
    exit 2. Состояние при этом цело - argparse падает до любой работы, -
    но ход сгорает целиком, а на прогоне инженер поднимается первым.

    Проверяется класс, а не случай: у каждой команды, названной в
    рантбуке, каждый обязательный флаг обязан стоять в его тексте. Тогда
    следующий обязательный флаг не разойдётся с промптом молча.

    Промпт здесь строится, а не читается из исходника: сверять текст
    файла значит проверять, как написано, вместо того что выполнится.
    """

    def prompt(self) -> str:
        from codex_autopilot.pipeline_engineer import FORBIDDEN_ACTIONS

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        (root / ".git").mkdir()
        skill = root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        runtime = AIStudioRuntime(
            plan([task("code-a", "integrator")]),
            root,
            language="en",
            skill_path=skill,
        )
        return runtime.build_pipeline_engineer_prompt(
            {
                "incident": {
                    "incident_id": "INC-1",
                    "classification": "PIPELINE",
                    "phase": "PIPELINE_ENGINEER",
                    "summary": "транспорт сорвался",
                    "affected_task_ids": ["A"],
                },
                "forbidden_actions": sorted(FORBIDDEN_ACTIONS),
                "allowed_actions": ["read_state"],
            },
            reservation_token="token-1",
        )

    def test_every_required_flag_of_a_runbook_command_is_named_in_it(self) -> None:
        import argparse
        import re

        from codex_autopilot.cli import parser

        text = self.prompt()
        # The flag is searched for INSIDE the command itself, not anywhere in the prompt.
        # The explanation next to the command also names the flag, and a search
        # over the whole text would stay green even with the flag removed from
        # the command - the same substring blindness that hid dead code.
        invocations = re.findall(r"`scripts/codex-autopilot (\S+)([^`]*)`", text)
        self.assertTrue(invocations, "рантбук не называет ни одной команды")

        subparsers = {}
        for action in parser()._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparsers.update(action.choices)

        missing = []
        for command, arguments in invocations:
            sub = subparsers.get(command)
            self.assertIsNotNone(sub, f"рантбук называет несуществующую команду {command}")
            for item in sub._actions:
                if not item.option_strings or not item.required:
                    continue
                flag = max(item.option_strings, key=len)
                if flag not in arguments:
                    missing.append(f"{command} {flag}")
        self.assertEqual(
            missing,
            [],
            "обязательный флаг есть в CLI и отсутствует в рантбуке — инженер "
            "получит exit 2 и потеряет ход: " + ", ".join(missing),
        )


class AIStudioRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-autopilot-ai-studio-")
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")
        (self.root / "outputs").mkdir()
        self.memory = ProjectMemory(self.root)
        self.memory.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def runtime(self, raw_tasks: list[dict]) -> AIStudioRuntime:
        return AIStudioRuntime(
            plan(raw_tasks),
            self.root,
            language="en",
            skill_path=self.skill,
            memory=self.memory,
        )

    def test_arbitrary_role_profile_is_complete_but_model_route_is_capability_only(self):
        runtime = self.runtime(
            [
                task("code-a", "integrator"),
                task("code-b", "researcher"),
                task("gui", "integrator", mode="computer_use"),
            ]
        )
        states = {"code-a": "READY", "code-b": "READY", "gui": "READY"}

        self.assertEqual(runtime.route("code-a").model_id, "gpt-5.6-sol")
        self.assertEqual(runtime.route("code-b").model_id, "gpt-5.6-sol")
        self.assertEqual(runtime.route("gui").model_id, "gpt-6-astra")
        payload = context_payload(
            runtime.build_prompt(
                "code-b",
                phase="implementation",
                task_states=states,
                reservation_token="fresh-1",
            )
        )
        self.assertEqual(payload["role"]["name"], "Fact Verification Specialist")
        self.assertEqual(payload["role"]["domain_focus"], ["researcher domain"])
        self.assertEqual(payload["role"]["preferred_tools"], ["researcher-tool"])
        self.assertEqual(payload["role"]["context_priorities"], ["researcher facts first"])
        self.assertEqual(
            payload["role"]["verification_expectations"],
            ["Verify researcher independently."],
        )
        self.assertNotIn("model", payload["role"])

    def test_worker_prompt_preserves_dispatcher_owned_run_authorization(self):
        runtime = self.runtime([task("code-a", "integrator")])
        prompt = runtime.build_prompt(
            "code-a",
            phase="implementation",
            task_states={"code-a": "READY"},
            reservation_token="fresh-relay",
        )

        self.assertIn(
            "never create, start, or message other tasks",
            prompt,
        )
        self.assertIn(
            "already-running local dispatcher consumes the authoritative App Server completion",
            prompt,
        )
        self.assertIn(
            "closes this task's App Server process",
            prompt,
        )
        self.assertIn("starts the exact successor", prompt)
        self.assertIn("Stop hook is only an observer", prompt)
        self.assertIn("DevOps only re-arms the causal dispatcher", prompt)

    def test_russian_worker_prompt_uses_the_same_dispatcher_contract(self):
        runtime = AIStudioRuntime(
            self.runtime([task("code-a", "integrator")]).plan,
            self.root,
            language="ru",
            skill_path=self.skill,
            memory=self.memory,
        )
        prompt = runtime.build_prompt(
            "code-a",
            phase="implementation",
            task_states={"code-a": "READY"},
            reservation_token="fresh-relay",
        )

        self.assertIn("уже работающий локальный dispatcher", prompt)
        self.assertIn("полностью закрывает App Server-процесс", prompt)
        self.assertIn("Stop hook автоматически управляемого turn", prompt)
        self.assertNotIn("короткое продолжение в этом же causal task", prompt)

    def test_verifier_acceptance_gate_uses_original_request_independently(self):
        runtime = self.runtime(
            [task("code-a", "integrator", verifier_role="reviewer")]
        )
        prompt = runtime.build_prompt(
            "code-a",
            phase="verification",
            task_states={"code-a": "VERIFYING"},
            reservation_token="fresh-acceptance",
            verification_round=1,
        )
        payload = context_payload(prompt)

        # The original request is fixed for the whole run and cannot be narrowed.
        # A verbatim copy in every prompt ate the budget: 51 475 of
        # 62 635 characters against 395 for the task itself. Now it is a
        # verifiable reference, and the text comes from Project Memory.
        request = (
            "Deliver the requested behavior and judge it independently of "
            "implementation-authored tests."
        )
        reference = payload["acceptance_gate"]["original_user_request"]
        self.assertFalse(reference["verbatim_in_prompt"])
        self.assertEqual(reference["chars"], len(request))
        self.assertEqual(
            reference["sha256"], hashlib.sha256(request.encode("utf-8")).hexdigest()
        )
        self.assertEqual(
            reference["retrieval"],
            {
                "server": "codex_autopilot_memory",
                "tool": "memory",
                "arguments": {
                    "operation": "current",
                    "task_id": "code-a",
                    "expect_user_request_sha256": hashlib.sha256(
                        runtime.plan.user_request.encode("utf-8")
                    ).hexdigest(),
                },
                "field": "user_request",
                "verified_by": "runtime",
            },
        )
        self.assertNotIn(request, prompt)
        # The reference is useless if the worker was not told how to
        # use it: before this fix the prompt did not mention the memory
        # server once.
        self.assertIn("codex_autopilot_memory", prompt)
        self.assertIn('"operation":"current"', prompt)
        self.assertTrue(
            payload["acceptance_gate"]["implementation_tests_are_evidence_only"]
        )
        self.assertEqual(
            payload["acceptance_gate"]["task_definition_of_done"],
            payload["definition_of_done"],
        )
        self.assertIn("Independently compare the result", prompt)
        self.assertIn("Implementer-authored tests are evidence only", prompt)
        self.assertIn("PASS is allowed only after this independent check", prompt)

    def test_a_recorded_human_decision_reaches_the_worker(self):
        """Решение, которого никто не читает, ничем не лучше реплики.

        Причина разблокировки ложилась в `user_unblocks` и никуда больше:
        поле встречалось только там, где записывается. Владелец за сутки
        сняла шесть остановок, каждый раз объясняя почему, и ни одно
        объяснение не дошло до воркера, который продолжал задачу. R32
        требует записанного решения - но решение обязано ещё и дойти.
        """

        import json as _json

        from codex_autopilot.run_state import StateStore

        store = StateStore(self.root / ".codex-autopilot")
        state = store.load()
        state.user_unblocks = [
            {"task_id": "code-a", "reason": "Вынести путь аттестации отдельной задачей.", "at": "2026-09-16T12:00:00+00:00"},
            {"task_id": "other", "reason": "Чужое решение.", "at": "2026-09-16T12:01:00+00:00"},
        ]
        store.save(state)

        runtime = self.runtime([task("code-a", "integrator")])
        prompt = runtime.build_prompt(
            "code-a",
            phase="implementation",
            task_states={"code-a": "RUNNING"},
            reservation_token="fresh-decisions",
        )
        payload = context_payload(prompt)
        decisions = payload["recorded_human_decisions"]
        self.assertEqual(len(decisions), 1)
        self.assertIn("отдельной задачей", decisions[0]["decision"])
        self.assertNotIn("Чужое решение", _json.dumps(payload, ensure_ascii=False))

    def test_no_phase_ever_asks_the_worker_to_hash_the_request(self):
        """Ни одна фаза не требует считать хэш самому.

        Требование заверить длину и sha256 своими силами останавливало
        задачи наглухо: в изоляте постобработки Codex нет ни `crypto`, ни
        `TextEncoder`, а контракт разрешает ровно один вызов Project
        Memory и повторить его нельзя. На живом прогоне так встали M4,
        M11 и доработка M11 - последняя уже после того, как я закрыла
        первое из четырёх мест. Класс держится тестом, а не памятью.
        """

        raw_task = task("code-a", "integrator", verifier_role="reviewer")
        raw_plan = {
            "schema_version": 3,
            "graph_version": 1,
            "goal": "Большой запрос не должен решать, может ли задача начаться.",
            "user_request": "complete request\n" * MAX_INLINE_USER_REQUEST_CHARS,
            "model_strategy": "auto",
            "execution_strategy": "auto",
            "max_parallel_workers": 2,
            "computer_use_slots": 1,
            "roles": [
                role("integrator", "Systems Integrator"),
                role("reviewer", "Independent Reviewer"),
            ],
            "tasks": [raw_task],
        }
        runtime = AIStudioRuntime(
            validate_plan(canonicalize_plan(raw_plan), "adaptive"),
            self.root,
            language="en",
            skill_path=self.skill,
            memory=self.memory,
        )

        for phase, states in (
            ("implementation", {"code-a": "RUNNING"}),
            ("verification", {"code-a": "VERIFYING"}),
        ):
            with self.subTest(phase=phase):
                prompt = runtime.build_prompt(
                    "code-a",
                    phase=phase,
                    task_states=states,
                    reservation_token=f"fresh-{phase}",
                    **({"verification_round": 1} if phase == "verification" else {}),
                )
                self.assertNotIn("verify chars and sha256", prompt)
                self.assertNotIn("chars и sha256", prompt)
                self.assertIn("Never hash it yourself", prompt)

    def test_large_original_request_uses_lossless_canonical_reference(self):
        raw_task = task("code-a", "integrator", verifier_role="reviewer")
        raw_plan = {
            "schema_version": 3,
            "graph_version": 1,
            "goal": "Verify the complete request without duplicating it in every prompt.",
            "user_request": "complete request\n" * MAX_INLINE_USER_REQUEST_CHARS,
            "model_strategy": "auto",
            "execution_strategy": "auto",
            "max_parallel_workers": 2,
            "computer_use_slots": 1,
            "roles": [
                role("integrator", "Systems Integrator"),
                role("reviewer", "Independent Reviewer"),
            ],
            "tasks": [raw_task],
        }
        runtime = AIStudioRuntime(
            validate_plan(canonicalize_plan(raw_plan), "adaptive"),
            self.root,
            language="en",
            skill_path=self.skill,
            memory=self.memory,
        )

        prompt = runtime.build_prompt(
            "code-a",
            phase="verification",
            task_states={"code-a": "VERIFYING"},
            reservation_token="fresh-large-acceptance",
            verification_round=1,
        )
        payload = context_payload(prompt)
        reference = payload["acceptance_gate"]["original_user_request"]
        canonical_request = runtime.plan.user_request

        self.assertLess(len(prompt), MAX_PROMPT_CHARS)
        self.assertFalse(reference["verbatim_in_prompt"])
        self.assertEqual(reference["chars"], len(canonical_request))
        self.assertEqual(
            reference["sha256"],
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(
            reference["retrieval"],
            {
                "server": "codex_autopilot_memory",
                "tool": "memory",
                "arguments": {
                    "operation": "current",
                    "task_id": "code-a",
                    "expect_user_request_sha256": hashlib.sha256(
                        runtime.plan.user_request.encode("utf-8")
                    ).hexdigest(),
                },
                "field": "user_request",
                "verified_by": "runtime",
            },
        )
        self.assertNotIn(canonical_request, prompt)
        self.assertIn("single specified Project Memory call", prompt)

        implementation_prompt = runtime.build_prompt(
            "code-a",
            phase="implementation",
            task_states={"code-a": "READY"},
            reservation_token="fresh-large-implementation",
        )
        self.assertIn("exactly one Project Memory call", implementation_prompt)
        # The runtime attests: the worker passes the arguments verbatim and
        # hashes nothing - the post-processing isolate has neither crypto nor
        # TextEncoder, and the requirement to compute a hash stopped tasks.
        self.assertIn("the runtime verifies the text for you", implementation_prompt)
        self.assertIn("Never hash it yourself", implementation_prompt)
        self.assertNotIn("verify chars and sha256", implementation_prompt)

    def test_selective_verified_memory_and_dependency_output_are_bounded(self):
        evidence = self.memory.record_evidence(
            kind="file",
            summary="Verified alpha protocol file.",
            path="SKILL.md",
            milestone_id="source",
            created_by="test",
        )
        fact = self.memory.record_verified_fact(
            statement="Alpha protocol is the verified dependency interface.",
            evidence_ids=[evidence["id"]],
            verification_method="file inspection",
            created_by="test",
        )
        self.memory.add_observation(
            statement="UNRELATED-CONCURRENT-TRANSCRIPT alpha guess",
            created_by="other worker",
        )
        for index in range(30):
            item = self.memory.record_evidence(
                kind="file",
                summary=f"Verified alpha memory item {index}.",
                path="SKILL.md",
                created_by="test",
            )
            self.memory.record_verified_fact(
                statement=f"Alpha bounded fact {index} " + ("x" * 1_000),
                evidence_ids=[item["id"]],
                verification_method="file inspection",
                created_by="test",
            )
        (self.root / "outputs" / "source.txt").write_text(
            "DEPENDENCY-PAYLOAD:" + ("z" * 5_000),
            encoding="utf-8",
        )
        raw_source = task("source", "integrator")
        raw_target = task(
            "target",
            "researcher",
            dependencies=["source"],
            queries=["Alpha"],
            record_ids=[fact["id"]],
        )
        runtime = self.runtime([raw_source, raw_target])
        prompt = runtime.build_prompt(
            "target",
            phase="implementation",
            task_states={"source": "VERIFIED", "target": "READY"},
            reservation_token="fresh-2",
        )
        payload = context_payload(prompt)

        self.assertLessEqual(len(payload["context"]["verified_state"]), HARD_MAX_MEMORY_RECORDS)
        self.assertEqual(payload["context"]["verified_state"][0]["id"], fact["id"])
        self.assertNotIn("UNRELATED-CONCURRENT-TRANSCRIPT", prompt)
        output = payload["context"]["dependency_outputs"][0]
        self.assertEqual(output["dependency_task_id"], "source")
        self.assertEqual(output["dependency_state"], "VERIFIED")
        self.assertTrue(output["truncated"])
        self.assertEqual(len(output["content_excerpt"]), MAX_OUTPUT_EXCERPT_CHARS)
        self.assertLess(len(prompt), MAX_PROMPT_CHARS)

    def test_unverified_dependency_output_and_unknown_explicit_memory_fail_closed(self):
        source = task("source", "integrator")
        target = task("target", "researcher", dependencies=["source"])
        runtime = self.runtime([source, target])
        with self.assertRaisesRegex(ContextBoundaryError, "unverified dependency"):
            runtime.select_context(
                "target",
                task_states={"source": "IMPLEMENTED", "target": "READY"},
            )

        target["context"]["memory_record_ids"] = ["FACT-999"]
        runtime = self.runtime([source, target])
        with self.assertRaisesRegex(ContextBoundaryError, "unavailable Project Memory"):
            runtime.select_context(
                "target",
                task_states={"source": "VERIFIED", "target": "READY"},
            )

    def test_all_five_phases_share_one_fresh_structured_boundary(self):
        raw = task(
            "work",
            "integrator",
            verifier_role="reviewer",
            verifier_mode="computer_use",
        )
        runtime = self.runtime([raw])
        states = {"work": "RUNNING"}
        issue = VerificationIssue(
            code="ISSUE-1",
            summary="Correct the artifact",
            details="Re-run the exact check.",
            dod_refs=(1,),
        )
        evidence = ({"id": "EVID-101", "kind": "test", "role": "implementation"},)
        prompts = {
            phase: runtime.build_prompt(
                "work",
                phase=phase,
                task_states=states,
                reservation_token=f"fresh-{phase}",
                verification_round=1,
                revision_number=1,
                issues=(issue,) if phase in {"revision", "replanning"} else (),
                evidence=evidence if phase == "verification" else (),
            )
            for phase in ("implementation", "verification", "revision", "planning", "replanning")
        }

        for phase, prompt in prompts.items():
            payload = context_payload(prompt)
            self.assertEqual(payload["phase"], phase)
            self.assertEqual(payload["task"]["id"], "work")
            self.assertIn("role", payload)
            self.assertIn("definition_of_done", payload)
            self.assertIn("resources", payload)
            self.assertNotIn("transcript", payload)
            self.assertNotIn("conversation", payload)
        self.assertIn("ISSUE-1", prompts["revision"])
        self.assertNotIn("ISSUE-1", prompts["implementation"])
        self.assertIn("EVID-101", prompts["verification"])
        self.assertEqual(runtime.route("work", phase="verification").model_id, "gpt-6-astra")
        self.assertFalse(any("session" in slot for slot in AIStudioRuntime.__slots__))
        with self.assertRaises(TypeError):
            runtime.build_prompt(
                "work",
                phase="implementation",
                task_states=states,
                reservation_token="fresh",
                transcript="forbidden",  # type: ignore[call-arg]
            )

    def test_code_research_and_mixed_computer_use_shapes_use_same_runtime(self):
        runtime = self.runtime(
            [
                task("integration", "integrator"),
                task("facts", "researcher", dependencies=["integration"]),
                task(
                    "desktop",
                    "operator",
                    mode="computer_use",
                    dependencies=["facts"],
                ),
            ]
        )
        self.assertEqual(runtime.route("integration").model_id, "gpt-5.6-sol")
        self.assertEqual(runtime.route("facts").model_id, "gpt-5.6-sol")
        self.assertEqual(runtime.route("desktop").model_id, "gpt-6-astra")
        self.assertEqual(runtime.plan.task_map["facts"].required_capabilities, ("research",))
        self.assertEqual(runtime.plan.task_map["desktop"].execution_mode, "computer_use")


if __name__ == "__main__":
    unittest.main()
