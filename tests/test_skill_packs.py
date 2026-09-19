from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest import mock

from _appserver_fakes import activate_via_app_server
from _gates import patch_hook_trust_gates
from _handoff import bump_task_checkpoint
from _plan_contract import (
    TEST_OUTCOME_ID,
    canonicalize_plan,
    canonical_verification,
    initialize_verified_project,
)
from _relay import reserve_ready_frontier
from codex_autopilot.ai_studio import AIStudioRuntime, ContextBoundaryError, MAX_PROMPT_CHARS
from codex_autopilot.config import load_config
from codex_autopilot.lifecycle import complete_desktop_worker
from codex_autopilot.memory import MemoryValidationError, ProjectMemory
from codex_autopilot.memory_mcp import MemoryMcpServer
from codex_autopilot.plan import plan_to_dict, save_plan, validate_plan
from codex_autopilot.skill_packs import (
    SkillPackError,
    SkillReference,
    resolve_skill_stack,
    skill_pack_from_raw,
    skill_reference_from_raw,
)


def pack(
    skill_id: str,
    capability: str,
    *,
    version: str = "1.0.0",
    source: str = "vetted",
    status: str = "trusted",
    conflicts_with: list[dict[str, str]] | None = None,
    promotion_evidence: list[dict[str, object]] | None = None,
) -> dict:
    return {
        "id": skill_id,
        "version": version,
        "capability": capability,
        "source": source,
        "status": status,
        "procedures": [f"Apply the {skill_id} procedure."],
        "checklists": [f"Check the {skill_id} result."],
        "failure_modes": [f"Stop when {skill_id} prerequisites are absent."],
        "quality_criteria": [f"The {skill_id} result is reproducible."],
        "required_tools": ["repository"],
        "required_mcp_servers": [],
        "deterministic_checks": [
            {
                "id": f"{skill_id}-qualification",
                "description": f"Qualify use of {skill_id}.",
                "argv": [sys.executable, "-c", "raise SystemExit(0)"],
            }
        ],
        "evidence_roles": [f"{skill_id}_check"],
        **({"conflicts_with": conflicts_with} if conflicts_with is not None else {}),
        **(
            {"promotion_evidence": promotion_evidence}
            if promotion_evidence is not None
            else {}
        ),
    }


def role(role_id: str, requirements: list[dict[str, str]] | None = None) -> dict:
    return {
        "id": role_id,
        "name": role_id.replace("-", " ").title(),
        "version": "1.1.0" if requirements else "1.0.0",
        "responsibilities": [f"Own {role_id} work."],
        "skill_requirements": requirements or [],
    }


def canonical_plan(skill_packs: list[dict], loaded_skills: list[dict[str, str]]) -> dict:
    return canonicalize_plan(
        {
            "schema_version": 3,
            "goal": "Exercise exact, trusted skill composition.",
            "user_request": "Load the requested trusted skill stack and no other skills.",
            "model_strategy": "auto",
            "roles": [role("builder", loaded_skills), role("reviewer")],
            "skill_packs": skill_packs,
            "tasks": [
                {
                    "id": "M1",
                    "title": "Skill-backed task",
                    "objective": "Exercise the resolved skill stack.",
                    "definition_of_done": ["The stack is resolved and visible."],
                    "execution_mode": "code",
                    "execution_mode_reason": "Repository files and tests are sufficient.",
                    "reasoning": "medium",
                    "role": "builder",
                    "depends_on": [],
                    "priority": 0,
                    "verification": canonical_verification(verifier_role="reviewer"),
                    "resources": [],
                    "required_capabilities": ["python"],
                    "loaded_skills": loaded_skills,
                    "context": {},
                    "outputs": [],
                    "tags": [],
                    "produces_outcomes": [TEST_OUTCOME_ID],
                    "acceptance_class": "mixed",
                }
            ],
        }
    )


def attestation_plan(
    raw_pack: dict,
    kinds: tuple[str, ...],
    *,
    promotion_kind: str = "real_tool",
) -> dict:
    parsed = skill_pack_from_raw(raw_pack)
    reference = {"id": parsed.id, "version": parsed.version}
    tasks: list[dict[str, object]] = []
    previous: str | None = None
    for index, kind in enumerate(kinds, 1):
        attestation: dict[str, object] = {"kind": kind, "skill": reference}
        checks: list[dict[str, object]] = []
        if kind == "source":
            attestation["evidence_roles"] = ["source-origin"]
        elif kind == "promotion":
            attestation["promotion_evidence"] = [
                {"role": "promotion-proof", "kind": promotion_kind}
            ]
        else:
            checks = [
                {
                    "id": check.id,
                    "kind": "command",
                    "description": check.description,
                    "argv": list(check.argv),
                    "expected_exit_code": check.expected_exit_code,
                }
                for check in parsed.deterministic_checks
            ]
        task_id = f"A{index}"
        tasks.append(
            {
                "id": task_id,
                "title": f"{kind.title()} {parsed.id}",
                "objective": f"Produce the {kind} attestation for the exact Skill Pack revision.",
                "definition_of_done": [
                    f"The exact {parsed.id}@{parsed.version} revision passes {kind} review."
                ],
                "execution_mode": "code",
                "execution_mode_reason": "Repository files and deterministic tools are sufficient.",
                "reasoning": "medium",
                "role": "builder",
                "depends_on": [previous] if previous else [],
                "priority": 0,
                "verification": canonical_verification(
                    checks=checks,
                    verifier_role="reviewer",
                ),
                "resources": [],
                "required_capabilities": [],
                "context": {},
                "outputs": [],
                "tags": [],
                "produces_outcomes": [TEST_OUTCOME_ID],
                "acceptance_class": "mixed",
                "skill_attestation": attestation,
            }
        )
        previous = task_id
    return canonicalize_plan(
        {
            "schema_version": 3,
            "graph_version": 1,
            "goal": "Produce runtime-attested Skill Pack provenance and qualification.",
            "user_request": "Use the canonical lifecycle to attest one exact Skill Pack revision.",
            "model_strategy": "auto",
            "execution_strategy": "serial",
            "max_parallel_workers": 1,
            "computer_use_slots": 1,
            "roles": [role("builder"), role("reviewer")],
            "skill_packs": [raw_pack],
            "tasks": tasks,
        }
    )


def run_canonical_attestations(
    root: Path,
    raw_pack: dict,
    kinds: tuple[str, ...],
    *,
    promotion_kind: str = "real_tool",
    promotion_evidence_kind: str = "tool",
) -> tuple[dict[str, dict], dict[str, str]]:
    """Exercise the real reservation/completion path for test fixtures."""

    skill = root / "SKILL.md"
    skill.write_text("# test skill\n", encoding="utf-8")
    plan_file = root / "skill-attestation-plan.json"
    plan_file.write_text(
        json.dumps(
            attestation_plan(raw_pack, kinds, promotion_kind=promotion_kind)
        ),
        encoding="utf-8",
    )
    initialize_verified_project(
        root,
        plan_file,
        profile="adaptive",
        skill_path=skill,
        desktop_project_id="desktop-project",
    )
    cfg = load_config(root)
    memory = ProjectMemory(root)
    parsed = skill_pack_from_raw(raw_pack)
    descriptor = reserve_ready_frontier(cfg)[0]
    records: dict[str, dict] = {}
    implementation_ids: dict[str, str] = {}
    for index, kind in enumerate(kinds, 1):
        task_id = f"A{index}"
        assert descriptor.task_id == task_id
        activate_via_app_server(cfg, root, descriptor, f"implementation-{task_id}")
        bump_task_checkpoint(root, task_id, f"implemented {kind}")
        evidence_kwargs: dict[str, object] = {}
        evidence_kind = "tool"
        evidence_role = "qualification-setup"
        if kind == "source":
            evidence_role = "source-origin"
        elif kind == "promotion":
            evidence_role = "promotion-proof"
            evidence_kind = promotion_evidence_kind
        if evidence_kind == "tool":
            evidence_kwargs["tool_name"] = f"{kind}-probe"
        elif evidence_kind in {"test", "build"}:
            evidence_kwargs.update(command=f"verify {kind}", result="PASS", exit_code=0)
        evidence = memory.record_evidence(
            kind=evidence_kind,
            summary=f"Implementation evidence for {kind} review.",
            created_by="skill-attestation-worker",
            milestone_id=task_id,
            role=evidence_role,
            **evidence_kwargs,
        )
        implementation_ids[kind] = str(evidence["id"])
        implementation = complete_desktop_worker(
            cfg,
            thread_id=f"implementation-{task_id}",
            turn_id=f"implementation-turn-{task_id}",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        verifier = implementation.descriptors[0]
        assert verifier.kind == "verifier", (
            verifier.kind,
            verifier.task_id,
            json.loads((root / ".codex-autopilot" / "run-state.json").read_text())["worker_sessions"][-2:],
        )
        activate_via_app_server(cfg, root, verifier, f"verifier-{task_id}")
        bump_task_checkpoint(root, task_id, f"verified {kind}")
        memory.record_evidence(
            kind="test",
            summary=f"Fresh verifier reproduced {kind} evidence.",
            created_by="independent-reviewer",
            milestone_id=task_id,
            role="independent_verification",
            command=f"review {kind}",
            result="PASS",
            exit_code=0,
        )
        accepted = complete_desktop_worker(
            cfg,
            thread_id=f"verifier-{task_id}",
            turn_id=f"verifier-turn-{task_id}",
            final_message='AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}',
        )
        special_task_id = (
            f"SKILL-{kind.upper()}:{parsed.id}@{parsed.version}:"
            f"{parsed.revision_sha256}"
        )
        matches = memory.list_verification_results(
            task_id=special_task_id,
            limit=8,
        ).records
        assert len(matches) == 1
        records[kind] = memory.get_verification_result(matches[0]["id"])
        if index < len(kinds):
            descriptor = accepted.descriptors[0]
        else:
            assert accepted.descriptors == ()
    return records, implementation_ids


class SkillPackResolverTests(unittest.TestCase):
    def test_resolves_by_stable_id_and_exact_semantic_version(self) -> None:
        old = skill_pack_from_raw(pack("python-testing", "python", version="1.0.0"))
        current = skill_pack_from_raw(pack("python-testing", "python", version="2.1.0"))

        resolved = resolve_skill_stack(
            (old, current),
            (SkillReference("python-testing", "2.1.0"),),
            require_qualification=False,
        )

        self.assertEqual([(item.id, item.version) for item in resolved], [("python-testing", "2.1.0")])

    def test_candidate_cannot_be_loaded_as_trusted(self) -> None:
        candidate = skill_pack_from_raw(
            pack("generated-procedure", "novel-work", source="synthesized", status="candidate")
        )

        with self.assertRaisesRegex(SkillPackError, "CANDIDATE.*cannot be loaded as trusted"):
            resolve_skill_stack(
                (candidate,),
                (SkillReference("generated-procedure", "1.0.0"),),
                require_qualification=False,
            )

    def test_synthesized_pack_needs_independent_and_qualifying_evidence_to_be_trusted(self) -> None:
        with self.assertRaisesRegex(SkillPackError, "cannot be declared trusted"):
            skill_pack_from_raw(
                pack("generated-procedure", "novel-work", source="synthesized")
            )

        promoted = skill_pack_from_raw(
            pack(
                "generated-procedure",
                "novel-work",
                source="synthesized",
                promotion_evidence=[
                    {"id": "E-VERIFY", "kind": "independent_verification", "verified": True},
                    {"id": "E-TOOL", "kind": "real_tool", "verified": True},
                ],
            )
        )
        self.assertEqual(promoted.status, "trusted")

    def test_learned_pack_needs_verified_outcome_evidence_to_be_trusted(self) -> None:
        with self.assertRaisesRegex(SkillPackError, "learned.*verified_work_outcome"):
            skill_pack_from_raw(
                pack("learned-procedure", "repeatable-work", source="learned")
            )

    def test_semver_rejects_numeric_prerelease_identifiers_with_leading_zeroes(self) -> None:
        for version in ("1.0.0-01", "1.0.0-alpha.01", "2.3.4-00+build.7"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(SkillPackError, "leading zeroes"):
                    skill_reference_from_raw({"id": "python-testing", "version": version})
                with self.assertRaisesRegex(SkillPackError, "leading zeroes"):
                    skill_pack_from_raw(pack("python-testing", "python", version=version))

        self.assertEqual(
            skill_reference_from_raw(
                {"id": "python-testing", "version": "1.0.0-0.alpha+build.01"}
            ).version,
            "1.0.0-0.alpha+build.01",
        )

    def test_duplicate_and_conflicting_stacks_fail_closed(self) -> None:
        first = skill_pack_from_raw(pack("python-a", "python"))
        duplicate = skill_pack_from_raw(pack("python-b", "python"))
        with self.assertRaisesRegex(SkillPackError, "duplicate capability"):
            resolve_skill_stack(
                (first, duplicate),
                (first.reference, duplicate.reference),
                require_qualification=False,
            )

        guarded = skill_pack_from_raw(
            pack(
                "safe-export",
                "export",
                conflicts_with=[{"id": "fast-export", "version": "1.0.0"}],
            )
        )
        fast = skill_pack_from_raw(pack("fast-export", "fast-export"))
        with self.assertRaisesRegex(SkillPackError, "loaded skill packs conflict"):
            resolve_skill_stack(
                (guarded, fast),
                (guarded.reference, fast.reference),
                require_qualification=False,
            )

    def test_pack_cannot_select_a_model(self) -> None:
        raw = pack("python-testing", "python")
        raw["model"] = "gpt-6-astra"
        with self.assertRaisesRegex(SkillPackError, "cannot select a model"):
            skill_pack_from_raw(raw)

    def test_trusted_pack_without_authoritative_qualification_cannot_load(self) -> None:
        raw = pack("unsafe-check", "unsafe-check")
        raw["deterministic_checks"][0]["argv"] = ["false"]
        unqualified = skill_pack_from_raw(raw)

        with self.assertRaisesRegex(
            SkillPackError, "qualification requires Project Memory validation"
        ):
            resolve_skill_stack((unqualified,), (unqualified.reference,))


class SkillPackProductionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_hook_trust_gates(self)

    def _memory(self, root: Path) -> ProjectMemory:
        (root / ".git").mkdir()
        memory = ProjectMemory(root)
        memory.initialize()
        return memory

    def _verified_promotion(
        self,
        memory: ProjectMemory,
        raw_pack: dict,
        *,
        evidence_kind: str,
        promotion_kind: str,
    ) -> list[dict[str, object]]:
        candidate = dict(raw_pack)
        candidate["status"] = "candidate"
        candidate.pop("promotion_evidence", None)
        records, implementation = run_canonical_attestations(
            memory.root,
            candidate,
            ("promotion",),
            promotion_kind=promotion_kind,
            promotion_evidence_kind=evidence_kind,
        )
        return [
            {
                "id": str(records["promotion"]["id"]),
                "kind": "independent_verification",
                "verified": True,
            },
            {
                "id": implementation["promotion"],
                "kind": promotion_kind,
                "verified": True,
            },
        ]

    def _qualified_pack(
        self,
        memory: ProjectMemory,
        raw_pack: dict,
        *,
        evidence_kind: str = "tool",
        promotion_kind: str = "real_tool",
    ) -> dict:
        candidate = dict(raw_pack)
        candidate["status"] = "candidate"
        candidate.pop("source_verification_ids", None)
        candidate.pop("qualification_verification_ids", None)
        candidate.pop("promotion_evidence", None)
        source = str(candidate.get("source"))
        kinds = (
            ("source", "qualification")
            if source in {"vetted", "project_generated"}
            else ("promotion", "qualification")
        )
        records, implementation = run_canonical_attestations(
            memory.root,
            candidate,
            kinds,
            promotion_kind=promotion_kind,
            promotion_evidence_kind=evidence_kind,
        )
        trusted = dict(candidate)
        trusted["status"] = "trusted"
        if source in {"vetted", "project_generated"}:
            trusted["source_verification_ids"] = [records["source"]["id"]]
        else:
            trusted["promotion_evidence"] = [
                {
                    "id": records["promotion"]["id"],
                    "kind": "independent_verification",
                    "verified": True,
                },
                {
                    "id": implementation["promotion"],
                    "kind": promotion_kind,
                    "verified": True,
                },
            ]
        trusted["qualification_verification_ids"] = [
            records["qualification"]["id"]
        ]
        return trusted

    def _source_attested_pack(self, memory: ProjectMemory, raw_pack: dict) -> dict:
        candidate = dict(raw_pack)
        candidate["status"] = "candidate"
        candidate.pop("source_verification_ids", None)
        records, _implementation = run_canonical_attestations(
            memory.root,
            candidate,
            ("source",),
        )
        trusted = dict(candidate)
        trusted["status"] = "trusted"
        trusted["source_verification_ids"] = [records["source"]["id"]]
        return trusted

    def test_public_memory_api_cannot_self_attest_a_vetted_source(self) -> None:
        reference = {"id": "self-authored", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-source-") as temp:
            memory = self._memory(Path(temp))
            raw_pack = pack("self-authored", "novel-work", source="vetted")
            parsed = skill_pack_from_raw(raw_pack)
            evidence = memory.record_evidence(
                kind="tool",
                summary="Pack author claims its own source is vetted.",
                tool_name="self-authored-source-claim",
                created_by="pack-author",
            )
            with self.assertRaisesRegex(MemoryValidationError, "reserved"):
                memory.record_verification_result(
                    task_id=(
                        f"SKILL-SOURCE:{parsed.id}@{parsed.version}:"
                        f"{parsed.revision_sha256}"
                    ),
                    check_id="skill-source",
                    policy="independent",
                    verdict="PASS",
                    summary="Attempt to forge runtime provenance.",
                    evidence_ids=[str(evidence["id"])],
                    created_by="pack-author",
                    provider="codex-desktop",
                    provider_thread_id="self-declared-thread",
                    provider_turn_id="forged-attestation-turn",
                    details={"runtime_attestation": {"authority": "forged"}},
                )
            verification = memory.record_verification_result(
                task_id=(
                    f"SKILL-SOURCE:{parsed.id}@{parsed.version}:"
                    f"{parsed.revision_sha256}"
                ),
                check_id="skill-source",
                policy="independent",
                verdict="PASS",
                summary="Pack author claims an independent source PASS.",
                evidence_ids=[str(evidence["id"])],
                created_by="pack-author",
                provider="codex-desktop",
                provider_thread_id="self-declared-thread",
                provider_turn_id="self-declared-turn",
                details={
                    "skill_pack_revision": {
                        "id": parsed.id,
                        "version": parsed.version,
                        "sha256": parsed.revision_sha256,
                    },
                    "skill_source": {
                        "source": parsed.source,
                        "skill_pack_sha256": parsed.revision_sha256,
                        "evidence_ids": [str(evidence["id"])],
                    },
                },
            )
            raw_pack["source_verification_ids"] = [str(verification["id"])]

            with self.assertRaisesRegex(ValueError, "not runtime-attested"):
                validate_plan(
                    canonical_plan([raw_pack], [reference]),
                    "adaptive",
                    promotion_evidence_store=memory,
                )

    def test_public_memory_api_cannot_self_attest_skill_qualification(self) -> None:
        reference = {"id": "self-qualified", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-qual-") as temp:
            memory = self._memory(Path(temp))
            raw_pack = self._source_attested_pack(
                memory,
                pack("self-qualified", "novel-work", source="vetted"),
            )
            parsed = skill_pack_from_raw(raw_pack)
            check = parsed.deterministic_checks[0]
            evidence = memory.record_evidence(
                kind="test",
                summary="Pack author supplied a fabricated runner result.",
                command=shlex.join(check.argv),
                result="PASS",
                exit_code=check.expected_exit_code,
                created_by="pack-author",
            )
            verification = memory.record_verification_result(
                task_id=(
                    f"SKILL-QUALIFICATION:{parsed.id}@{parsed.version}:"
                    f"{parsed.revision_sha256}"
                ),
                check_id="skill-qualification",
                policy="deterministic",
                verdict="PASS",
                summary="Pack author claims its checks passed.",
                evidence_ids=[str(evidence["id"])],
                created_by="pack-author",
                provider="deterministic-runtime",
                provider_thread_id="self-declared-runner",
                provider_turn_id="self-declared-run",
                details={
                    "skill_pack_revision": {
                        "id": parsed.id,
                        "version": parsed.version,
                        "sha256": parsed.revision_sha256,
                    },
                    "skill_checks": [
                        {
                            "id": check.id,
                            "argv": list(check.argv),
                            "expected_exit_code": check.expected_exit_code,
                            "exit_code": check.expected_exit_code,
                            "evidence_id": str(evidence["id"]),
                        }
                    ],
                },
            )
            raw_pack["qualification_verification_ids"] = [str(verification["id"])]

            with self.assertRaisesRegex(ValueError, "not runtime-attested"):
                validate_plan(
                    canonical_plan([raw_pack], [reference]),
                    "adaptive",
                    promotion_evidence_store=memory,
                )

    def test_public_memory_api_enforces_loaded_pack_evidence_roles(self) -> None:
        reference = {"id": "role-guard", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-role-") as temp:
            root = Path(temp)
            memory = self._memory(root)
            raw_pack = self._qualified_pack(
                memory,
                pack("role-guard", "role-guard"),
            )
            plan = validate_plan(
                canonical_plan([raw_pack], [reference]),
                "adaptive",
                promotion_evidence_store=memory,
            )
            save_plan(memory.state_dir, plan)
            server = MemoryMcpServer(root)

            accepted = server.call(
                "memory",
                {
                    "operation": "record_evidence",
                    "kind": "tool",
                    "summary": "Evidence uses the role declared by the loaded pack.",
                    "milestone_id": "M1",
                    "role": "role-guard_check",
                    "tool_name": "role-probe",
                    "created_by": "skill-worker",
                },
            )
            self.assertRegex(str(accepted["id"]), r"^EVID-[0-9]+$")
            with self.assertRaisesRegex(
                MemoryValidationError, "forbidden-role.*allowed roles"
            ):
                server.call(
                    "memory",
                    {
                        "operation": "record_evidence",
                        "kind": "tool",
                        "summary": "Evidence uses an undeclared role.",
                        "milestone_id": "M1",
                        "role": "forbidden-role",
                        "tool_name": "role-probe",
                        "created_by": "skill-worker",
                    },
                )

    def test_plan_round_trip_and_worker_prompt_use_the_resolved_stack(self) -> None:
        reference = {"id": "python-testing", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-prompt-") as temp:
            root = Path(temp)
            memory = self._memory(root)
            qualified = self._qualified_pack(memory, pack("python-testing", "python"))
            plan = validate_plan(
                canonical_plan([qualified], [reference]),
                "adaptive",
                promotion_evidence_store=memory,
            )
            self.assertEqual(
                validate_plan(
                    plan_to_dict(plan),
                    "adaptive",
                    promotion_evidence_store=memory,
                ),
                plan,
            )
            skill = root / "SKILL.md"
            skill.write_text("# worker\n", encoding="utf-8")
            runtime = AIStudioRuntime(plan, root, language="en", skill_path=skill, memory=memory)
            prompt = runtime.build_prompt(
                "M1",
                phase="implementation",
                task_states={"M1": "READY"},
                reservation_token="skill-stack",
            )

        payload = json.loads(prompt.split("AUTOPILOT_CONTEXT: ", 1)[1].split("\n\n", 1)[0])
        self.assertEqual(payload["role"]["skill_requirements"], [reference])
        self.assertEqual(payload["task"]["loaded_skills"], [reference])
        self.assertEqual(
            [(item["id"], item["version"]) for item in payload["loaded_skills"]],
            [("python-testing", "1.0.0")],
        )
        self.assertNotIn("model", payload["loaded_skills"][0])
        self.assertEqual(payload["loaded_skills"][0]["provenance"]["status"], "trusted")
        self.assertEqual(
            payload["loaded_skills"][0]["provenance"]["source_verification_ids"],
            qualified["source_verification_ids"],
        )

    def test_twenty_role_competencies_load_only_the_two_needed_by_the_task(self) -> None:
        requirements = [
            {"id": f"competency-{index:02d}", "version": "1.0.0"}
            for index in range(20)
        ]
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-role-stack-") as temp:
            root = Path(temp)
            memory = self._memory(root)
            qualified = self._qualified_pack(
                memory,
                pack("competency-00", "capability-00"),
            )
            plan = validate_plan(
                canonical_plan([qualified], requirements[:1]),
                "adaptive",
                promotion_evidence_store=memory,
            )
            expanded = tuple(
                SkillReference(item["id"], item["version"]) for item in requirements
            )
            plan = replace(
                plan,
                roles=(
                    replace(plan.roles[0], skill_requirements=expanded),
                    *plan.roles[1:],
                ),
            )
            skill = root / "SKILL.md"
            skill.write_text("# worker\n", encoding="utf-8")
            prompt = AIStudioRuntime(
                plan, root, language="en", skill_path=skill, memory=memory
            ).build_prompt(
                "M1",
                phase="implementation",
                task_states={"M1": "READY"},
                reservation_token="twenty-competencies",
            )

        payload = json.loads(
            prompt.split("AUTOPILOT_CONTEXT: ", 1)[1].split("\n\n", 1)[0]
        )
        self.assertEqual(len(payload["role"]["skill_requirements"]), 20)
        self.assertEqual(
            [item["id"] for item in payload["loaded_skills"]],
            ["competency-00"],
        )
        self.assertNotIn("Apply the competency-19 procedure.", prompt)
        self.assertLess(len(prompt), MAX_PROMPT_CHARS)

    def test_canonical_plan_rejects_a_candidate_loaded_as_trusted(self) -> None:
        reference = {"id": "generated-procedure", "version": "1.0.0"}
        raw = canonical_plan(
            [pack("generated-procedure", "novel-work", source="synthesized", status="candidate")],
            [reference],
        )
        with self.assertRaisesRegex(ValueError, "CANDIDATE.*cannot be loaded as trusted"):
            validate_plan(raw, "adaptive")

    def test_worker_prompt_rechecks_source_provenance_for_the_exact_revision(self) -> None:
        reference = {"id": "python-testing", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-prompt-") as temp:
            root = Path(temp)
            memory = self._memory(root)
            qualified = self._qualified_pack(memory, pack("python-testing", "python"))
            plan = validate_plan(
                canonical_plan([qualified], [reference]),
                "adaptive",
                promotion_evidence_store=memory,
            )
            relabeled = replace(plan.skill_packs[0], source="project_generated")
            unsafe_plan = replace(plan, skill_packs=(relabeled,))
            skill = root / "SKILL.md"
            skill.write_text("# worker\n", encoding="utf-8")
            runtime = AIStudioRuntime(
                unsafe_plan, root, language="en", skill_path=skill, memory=memory
            )

            with self.assertRaisesRegex(
                ContextBoundaryError,
                "source verification.*not an independent PASS for this exact revision",
            ):
                runtime.build_prompt(
                    "M1",
                    phase="implementation",
                    task_states={"M1": "READY"},
                    reservation_token="source-spoof",
                )

    def test_canonical_plan_rejects_fabricated_promotion_evidence_ids(self) -> None:
        reference = {"id": "generated-procedure", "version": "1.0.0"}
        raw = canonical_plan(
            [
                pack(
                    "generated-procedure",
                    "novel-work",
                    source="synthesized",
                    promotion_evidence=[
                        {
                            "id": "DOES-NOT-EXIST-VERIFY",
                            "kind": "independent_verification",
                            "verified": True,
                        },
                        {
                            "id": "DOES-NOT-EXIST-TOOL",
                            "kind": "real_tool",
                            "verified": True,
                        },
                    ],
                )
            ],
            [reference],
        )
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-evidence-") as temp:
            memory = self._memory(Path(temp))
            with self.assertRaisesRegex(ValueError, "unknown Project Memory verification"):
                validate_plan(raw, "adaptive", promotion_evidence_store=memory)

    def test_source_label_substitution_fails_before_the_worker_prompt(self) -> None:
        reference = {"id": "self-authored", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-source-") as temp:
            memory = self._memory(Path(temp))
            spoofed = pack(
                "self-authored",
                "novel-work",
                source="vetted",
                promotion_evidence=[
                    {
                        "id": "DOES-NOT-EXIST-VERIFY",
                        "kind": "independent_verification",
                        "verified": True,
                    },
                    {
                        "id": "DOES-NOT-EXIST-TOOL",
                        "kind": "real_tool",
                        "verified": True,
                    },
                ],
            )
            parsed = skill_pack_from_raw(spoofed)
            evidence = memory.record_evidence(
                kind="test",
                summary="The self-authored pack passed its declared qualification check.",
                command=shlex.join(parsed.deterministic_checks[0].argv),
                result="PASS",
                exit_code=parsed.deterministic_checks[0].expected_exit_code,
                created_by="self-authored-skill",
            )
            evidence_id = str(evidence["id"])
            qualification = memory.record_verification_result(
                task_id=(
                    f"SKILL-QUALIFICATION:{parsed.id}@{parsed.version}:"
                    f"{parsed.revision_sha256}"
                ),
                check_id="skill-qualification",
                policy="deterministic",
                verdict="PASS",
                summary="Declared qualification check passed.",
                evidence_ids=[evidence_id],
                created_by="self-authored-skill",
                provider_thread_id="thread-spoofed-source",
                provider_turn_id="turn-spoofed-source",
                details={
                    "skill_pack_revision": {
                        "id": parsed.id,
                        "version": parsed.version,
                        "sha256": parsed.revision_sha256,
                    },
                    "skill_checks": [
                        {
                            "id": parsed.deterministic_checks[0].id,
                            "argv": list(parsed.deterministic_checks[0].argv),
                            "expected_exit_code": (
                                parsed.deterministic_checks[0].expected_exit_code
                            ),
                            "exit_code": parsed.deterministic_checks[0].expected_exit_code,
                            "evidence_id": evidence_id,
                        }
                    ],
                },
            )
            spoofed["qualification_verification_ids"] = [str(qualification["id"])]

            with self.assertRaisesRegex(
                ValueError,
                "source 'vetted'.*cannot claim promotion_evidence",
            ):
                validate_plan(
                    canonical_plan([spoofed], [reference]),
                    "adaptive",
                    promotion_evidence_store=memory,
                )

    def test_vetted_source_claim_needs_an_authoritative_source_pass(self) -> None:
        reference = {"id": "self-authored", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-source-") as temp:
            memory = self._memory(Path(temp))
            spoofed = pack("self-authored", "novel-work", source="vetted")
            spoofed["source_verification_ids"] = ["VERIFY-DOES-NOT-EXIST"]

            with self.assertRaisesRegex(ValueError, "unknown source verification"):
                validate_plan(
                    canonical_plan([spoofed], [reference]),
                    "adaptive",
                    promotion_evidence_store=memory,
                )

    def test_unrelated_pass_cannot_promote_a_skill_revision(self) -> None:
        reference = {"id": "generated-procedure", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-evidence-") as temp:
            memory = self._memory(Path(temp))
            raw_pack = pack(
                "generated-procedure", "novel-work", source="synthesized"
            )
            placeholder = dict(raw_pack)
            placeholder["promotion_evidence"] = [
                {"id": "VERIFY-X", "kind": "independent_verification", "verified": True},
                {"id": "EVID-X", "kind": "real_tool", "verified": True},
            ]
            parsed = skill_pack_from_raw(placeholder)
            evidence = memory.record_evidence(
                kind="tool",
                summary="Unrelated deployment probe.",
                tool_name="unrelated-tool",
                created_by="unrelated-worker",
            )
            verification = memory.record_verification_result(
                task_id="UNRELATED",
                check_id="skill-promotion",
                policy="independent",
                verdict="PASS",
                summary="An unrelated task passed.",
                evidence_ids=[str(evidence["id"])],
                created_by="unrelated-reviewer",
                provider_thread_id="thread-unrelated",
                provider_turn_id="turn-unrelated",
                details={
                    "skill_pack_revision": {
                        "id": parsed.id,
                        "version": parsed.version,
                        "sha256": parsed.revision_sha256,
                    },
                    "promotion_evidence": [
                        {
                            "id": str(evidence["id"]),
                            "skill_pack_sha256": parsed.revision_sha256,
                        }
                    ],
                },
            )
            raw_pack["promotion_evidence"] = [
                {
                    "id": str(verification["id"]),
                    "kind": "independent_verification",
                    "verified": True,
                },
                {"id": str(evidence["id"]), "kind": "real_tool", "verified": True},
            ]
            raw = canonical_plan([raw_pack], [reference])

            with self.assertRaisesRegex(ValueError, "not for this exact revision"):
                validate_plan(raw, "adaptive", promotion_evidence_store=memory)

    def test_promotion_verdict_is_invalidated_by_pack_content_change(self) -> None:
        reference = {"id": "generated-procedure", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-evidence-") as temp:
            memory = self._memory(Path(temp))
            raw_pack = pack(
                "generated-procedure", "novel-work", source="synthesized"
            )
            raw_pack["promotion_evidence"] = self._verified_promotion(
                memory,
                raw_pack,
                evidence_kind="tool",
                promotion_kind="real_tool",
            )
            raw_pack["procedures"] = ["A different, unreviewed procedure."]

            with self.assertRaisesRegex(ValueError, "not for this exact revision"):
                validate_plan(
                    canonical_plan([raw_pack], [reference]),
                    "adaptive",
                    promotion_evidence_store=memory,
                )

    def test_canonical_plan_resolves_synthesized_promotion_from_project_memory(self) -> None:
        reference = {"id": "generated-procedure", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-evidence-") as temp:
            memory = self._memory(Path(temp))
            raw_pack = pack(
                "generated-procedure",
                "novel-work",
                source="synthesized",
            )
            raw_pack = self._qualified_pack(
                memory,
                raw_pack,
                evidence_kind="tool",
                promotion_kind="real_tool",
            )
            raw = canonical_plan(
                [raw_pack],
                [reference],
            )
            plan = validate_plan(raw, "adaptive", promotion_evidence_store=memory)
            self.assertEqual(
                validate_plan(
                    plan_to_dict(plan),
                    "adaptive",
                    promotion_evidence_store=memory,
                ),
                plan,
            )

    def test_canonical_plan_rejects_untrusted_promotion_provenance(self) -> None:
        reference = {"id": "generated-procedure", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-evidence-") as temp:
            memory = self._memory(Path(temp))
            raw_pack = pack(
                "generated-procedure",
                "novel-work",
                source="synthesized",
            )
            trusted = self._verified_promotion(
                memory,
                raw_pack,
                evidence_kind="tool",
                promotion_kind="real_tool",
            )
            external = memory.record_evidence(
                kind="external",
                summary="Unverified documentation text.",
                created_by="skill-test",
                provider="example-docs",
            )
            raw = canonical_plan(
                [
                    pack(
                        "generated-procedure",
                        "novel-work",
                        source="synthesized",
                        promotion_evidence=[
                            trusted[0],
                            {
                                "id": str(external["id"]),
                                "kind": "authoritative_documentation",
                                "verified": True,
                            },
                        ],
                    )
                ],
                [reference],
            )
            with self.assertRaises(ValueError) as caught:
                validate_plan(raw, "adaptive", promotion_evidence_store=memory)

            # R31: the refusal names what IS accepted. The old message said
            # only "below deterministic trust", which is true of every
            # external record and told the reader nothing about the kind
            # that would have worked.
            self.assertIn("authoritative_documentation", str(caught.exception))
            self.assertIn("'file'", str(caught.exception))
            self.assertIn("external", str(caught.exception))

    def test_canonical_plan_resolves_learned_pack_only_with_verified_outcome(self) -> None:
        reference = {"id": "learned-procedure", "version": "1.0.0"}
        with tempfile.TemporaryDirectory(prefix="codex-autopilot-skill-evidence-") as temp:
            memory = self._memory(Path(temp))
            raw_pack = pack(
                "learned-procedure",
                "repeatable-work",
                source="learned",
            )
            raw_pack = self._qualified_pack(
                memory,
                raw_pack,
                evidence_kind="test",
                promotion_kind="verified_work_outcome",
            )
            raw = canonical_plan(
                [raw_pack],
                [reference],
            )
            self.assertEqual(
                validate_plan(
                    raw,
                    "adaptive",
                    promotion_evidence_store=memory,
                ).skill_packs[0].source,
                "learned",
            )


class SkillPackCanonicalLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hook_gate = mock.patch(
            "codex_autopilot.lifecycle_reservations.require_trusted_stop_hook_for_config"
        )
        self.hook_gate.start()
        self.addCleanup(self.hook_gate.stop)
        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")

    def _initialize(self, raw_pack: dict, kinds: tuple[str, ...]) -> None:
        plan_file = self.root / "plan-input.json"
        plan_file.write_text(
            json.dumps(attestation_plan(raw_pack, kinds)),
            encoding="utf-8",
        )
        initialize_verified_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.memory = ProjectMemory(self.root)

    def _implementation_evidence(self, task_id: str, kind: str) -> str:
        bump_task_checkpoint(self.root, task_id, f"implemented {kind}")
        role_name = {
            "source": "source-origin",
            "promotion": "promotion-proof",
            "qualification": "qualification-setup",
        }[kind]
        item = self.memory.record_evidence(
            kind="tool",
            summary=f"Implementation evidence for {kind} review.",
            created_by="skill-attestation-worker",
            milestone_id=task_id,
            role=role_name,
            tool_name=f"{kind}-probe",
        )
        return str(item["id"])

    def _verifier_evidence(self, task_id: str, kind: str) -> None:
        bump_task_checkpoint(self.root, task_id, f"verified {kind}")
        self.memory.record_evidence(
            kind="test",
            summary=f"Fresh verifier reproduced {kind} evidence.",
            created_by="independent-reviewer",
            milestone_id=task_id,
            role="independent_verification",
            command=f"review {kind}",
            result="PASS",
            exit_code=0,
        )

    def _run_attestations(
        self, raw_pack: dict, kinds: tuple[str, ...]
    ) -> tuple[dict[str, dict], dict[str, str]]:
        self._initialize(raw_pack, kinds)
        parsed = skill_pack_from_raw(raw_pack)
        descriptor = reserve_ready_frontier(self.cfg)[0]
        records: dict[str, dict] = {}
        implementation_ids: dict[str, str] = {}
        for index, kind in enumerate(kinds, 1):
            task_id = f"A{index}"
            self.assertEqual(descriptor.task_id, task_id)
            self.assertIn('"skill_attestation"', descriptor.prompt)
            self.assertIn(f'"kind":"{kind}"', descriptor.prompt)
            activate_via_app_server(
                self.cfg,
                self.root,
                descriptor,
                f"implementation-{task_id}",
            )
            implementation_ids[kind] = self._implementation_evidence(task_id, kind)
            implementation = complete_desktop_worker(
                self.cfg,
                thread_id=f"implementation-{task_id}",
                turn_id=f"implementation-turn-{task_id}",
                final_message="AUTOPILOT_STATUS: ROTATE",
            )
            self.assertEqual(len(implementation.descriptors), 1)
            verifier = implementation.descriptors[0]
            self.assertEqual(verifier.kind, "verifier")
            activate_via_app_server(
                self.cfg,
                self.root,
                verifier,
                f"verifier-{task_id}",
            )
            self._verifier_evidence(task_id, kind)
            accepted = complete_desktop_worker(
                self.cfg,
                thread_id=f"verifier-{task_id}",
                turn_id=f"verifier-turn-{task_id}",
                final_message='AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}',
            )
            special_task_id = (
                f"SKILL-{kind.upper()}:{parsed.id}@{parsed.version}:"
                f"{parsed.revision_sha256}"
            )
            matches = self.memory.list_verification_results(
                task_id=special_task_id,
                limit=8,
            ).records
            self.assertEqual(len(matches), 1)
            records[kind] = self.memory.get_verification_result(matches[0]["id"])
            if index < len(kinds):
                self.assertEqual(len(accepted.descriptors), 1)
                descriptor = accepted.descriptors[0]
            else:
                self.assertEqual(accepted.descriptors, ())
        return records, implementation_ids

    @staticmethod
    def _candidate(skill_id: str, source: str) -> dict:
        raw = pack(skill_id, f"{skill_id}-capability", source=source, status="candidate")
        raw["deterministic_checks"][0]["argv"] = [
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ]
        return raw

    def test_canonical_lifecycle_attests_vetted_source_and_qualification(self) -> None:
        raw = self._candidate("vetted-runtime", "vetted")
        records, _implementation = self._run_attestations(
            raw,
            ("source", "qualification"),
        )
        trusted = dict(raw)
        trusted["status"] = "trusted"
        trusted["source_verification_ids"] = [records["source"]["id"]]
        trusted["qualification_verification_ids"] = [records["qualification"]["id"]]
        reference = {"id": "vetted-runtime", "version": "1.0.0"}

        accepted = validate_plan(
            canonical_plan([trusted], [reference]),
            "adaptive",
            promotion_evidence_store=self.memory,
        )

        self.assertEqual(accepted.skill_packs[0].status, "trusted")
        self.assertEqual(records["source"]["check_id"], "skill-source")
        self.assertEqual(records["qualification"]["check_id"], "skill-qualification")
        self.assertEqual(
            records["qualification"]["details"]["runtime_attestation"]["authority_kind"],
            "fresh_verifier",
        )
        self.assertEqual(
            records["qualification"]["evidence"][0]["command"],
            shlex.join(skill_pack_from_raw(raw).deterministic_checks[0].argv),
        )

    def test_canonical_lifecycle_attests_promotion_and_qualification(self) -> None:
        raw = self._candidate("synthesized-runtime", "synthesized")
        records, implementation = self._run_attestations(
            raw,
            ("promotion", "qualification"),
        )
        trusted = dict(raw)
        trusted["status"] = "trusted"
        trusted["promotion_evidence"] = [
            {
                "id": records["promotion"]["id"],
                "kind": "independent_verification",
                "verified": True,
            },
            {
                "id": implementation["promotion"],
                "kind": "real_tool",
                "verified": True,
            },
        ]
        trusted["qualification_verification_ids"] = [records["qualification"]["id"]]
        reference = {"id": "synthesized-runtime", "version": "1.0.0"}

        accepted = validate_plan(
            canonical_plan([trusted], [reference]),
            "adaptive",
            promotion_evidence_store=self.memory,
        )

        self.assertEqual(accepted.skill_packs[0].source, "synthesized")
        self.assertEqual(records["promotion"]["check_id"], "skill-promotion")
        self.assertEqual(
            records["promotion"]["details"]["promotion_evidence"],
            [
                {
                    "id": implementation["promotion"],
                    "skill_pack_sha256": skill_pack_from_raw(raw).revision_sha256,
                }
            ],
        )

    def test_qualification_task_must_execute_the_exact_declared_skill_check(self) -> None:
        raw = self._candidate("mismatched-runtime", "vetted")
        payload = attestation_plan(raw, ("qualification",))
        checks = payload["tasks"][0]["verification"]["deterministic_checks"]
        skill_check = next(item for item in checks if item["id"] != "suite")
        skill_check["argv"] = [sys.executable, "-c", "raise SystemExit(9)"]

        with self.assertRaisesRegex(
            ValueError,
            "must exactly match the Skill Pack argv and expected exit code",
        ):
            validate_plan(payload, "adaptive")

    def test_revise_verdict_does_not_create_a_source_attestation(self) -> None:
        raw = self._candidate("rejected-runtime", "vetted")
        self._initialize(raw, ("source",))
        parsed = skill_pack_from_raw(raw)
        implementation = reserve_ready_frontier(self.cfg)[0]
        activate_via_app_server(
            self.cfg, self.root, implementation, "implementation-rejected"
        )
        self._implementation_evidence("A1", "source")
        completed = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-rejected",
            turn_id="implementation-rejected-turn",
            final_message="AUTOPILOT_STATUS: ROTATE",
        )
        verifier = completed.descriptors[0]
        activate_via_app_server(self.cfg, self.root, verifier, "verifier-rejected")
        self._verifier_evidence("A1", "source")
        complete_desktop_worker(
            self.cfg,
            thread_id="verifier-rejected",
            turn_id="verifier-rejected-turn",
            final_message=(
                'AUTOPILOT_VERIFICATION: {"verdict":"REVISE","issues":['
                '{"code":"BAD-SOURCE","summary":"Source is unproven",'
                '"details":"The supplied source evidence does not establish origin.",'
                '"dod_refs":[1]}]}'
            ),
        )
        task_id = (
            f"SKILL-SOURCE:{parsed.id}@{parsed.version}:{parsed.revision_sha256}"
        )

        self.assertEqual(
            self.memory.list_verification_results(task_id=task_id, limit=8).records,
            [],
        )


if __name__ == "__main__":
    unittest.main()
