"""Hiring: the screener's requisition and the stack the runtime resolves from it.

The gap this closes was measured on 20 Sep 2026 against a plan with no
``skill_packs`` field: every assembled worker prompt carried
``"loaded_skills":[]``.  The resolver was never broken - its only catalog
source is ``plan.skill_packs`` and its only selection source is
``task.loaded_skills``, a plan field no planner fills, because no planner
can know which skills a task will need.

These tests execute the real resolver rather than re-stating its rules, so a
rule that stops running here stops passing here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re
import shlex
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _appserver_fakes import activate_via_app_server
from _gates import patch_hook_trust_gates
from _handoff import bump_task_checkpoint
from _plan_contract import initialize_verified_project
from _relay import reserve_ready_frontier
from codex_autopilot.ai_studio import AIStudioRuntime, ContextBoundaryError
from codex_autopilot.config import load_config
from codex_autopilot.lifecycle import complete_desktop_worker
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.plan import load_plan
from codex_autopilot.skill_packs import SkillPackError, SkillReference, skill_pack_from_raw
from codex_autopilot.skill_screening import (
    SCREENING_PREFIX,
    SKILL_LIBRARY_DIRNAME,
    ScreeningProtocolError,
    SkillLibraryError,
    HiringDecision,
    HiringOutcome,
    SkillRequisition,
    hiring_decision_from_raw,
    load_skill_library,
    parse_screening_result,
    record_hiring,
    recorded_hiring,
    resolve_requisition,
    skill_catalog,
)
from codex_autopilot.lifecycle_screening import screening_applies
from test_skill_packs import attestation_plan, pack, run_canonical_attestations


def set_screening_mode(root: Path, mode: str) -> None:
    """Rewrite the mode in an initialized project's config.

    The line is always present now - bootstrap writes it - so this replaces
    it rather than inserting a second one, which would make the file invalid
    TOML.
    """

    config = root / ".codex-autopilot" / "config.toml"
    text = config.read_text(encoding="utf-8")
    replaced, count = re.subn(
        r'skill_screening = "[a-z]+"', f'skill_screening = "{mode}"', text, count=1
    )
    assert count == 1, "initialized config must declare skill_screening"
    config.write_text(replaced, encoding="utf-8")


def screening_message(payload: dict, *, prose: str = "Looked at the task.") -> str:
    return f"{prose}\n\n{SCREENING_PREFIX}{json.dumps(payload)}"


def item(
    capability: str,
    *,
    rationale: str = "The task edits this framework's templates.",
    necessity: str = "required",
    candidates: list[dict[str, str]] | None = None,
    search_intent: str | None = None,
) -> dict:
    body: dict[str, object] = {
        "capability": capability,
        "rationale": rationale,
        "necessity": necessity,
    }
    if candidates is not None:
        body["candidates"] = candidates
    if search_intent is not None:
        body["search_intent"] = search_intent
    return body


class ScreeningProtocolTests(unittest.TestCase):
    def test_parses_the_final_protocol_line(self) -> None:
        message = screening_message(
            {
                "task_id": "M1",
                "items": [
                    item(
                        "python",
                        candidates=[{"id": "python-testing", "version": "2.1.0"}],
                    )
                ],
            }
        )

        requisition = parse_screening_result(message, task_id="M1")

        self.assertEqual(requisition.task_id, "M1")
        self.assertEqual(len(requisition.items), 1)
        self.assertEqual(requisition.items[0].capability, "python")
        self.assertEqual(requisition.items[0].necessity, "required")
        self.assertEqual(
            requisition.items[0].candidates,
            (SkillReference("python-testing", "2.1.0"),),
        )

    def test_a_screener_may_honestly_hire_nobody(self) -> None:
        """An empty requisition is an answer, not a protocol failure."""

        requisition = parse_screening_result(
            screening_message({"task_id": "M1", "items": []}), task_id="M1"
        )

        self.assertEqual(requisition.items, ())

    def test_refuses_a_protocol_line_that_is_not_last(self) -> None:
        message = (
            f'{SCREENING_PREFIX}{{"task_id":"M1","items":[]}}\n'
            "On reflection I would add one more skill."
        )

        with self.assertRaisesRegex(ScreeningProtocolError, "final"):
            parse_screening_result(message, task_id="M1")

    def test_refuses_two_protocol_lines(self) -> None:
        """A quoted example on its own line cannot stand beside the answer."""

        payload = {"task_id": "M1", "items": []}
        message = (
            "The shape I was asked for:\n"
            f"{SCREENING_PREFIX}{json.dumps(payload)}\n"
            "and here is my answer:\n"
            f"{SCREENING_PREFIX}{json.dumps(payload)}"
        )

        with self.assertRaisesRegex(ScreeningProtocolError, "exactly one"):
            parse_screening_result(message, task_id="M1")

    def test_an_example_inside_a_prose_line_is_not_the_answer(self) -> None:
        """Only a line that begins with the prefix counts, so prose that
        mentions the protocol cannot become the requisition."""

        message = (
            f'Autopilot expects a line like {SCREENING_PREFIX}{{"task_id":"M1","items":[]}}.\n'
            f'{SCREENING_PREFIX}{{"task_id":"M1","items":['
            f'{json.dumps(item("python", candidates=[{"id": "a-skill", "version": "1.0.0"}]))}]}}'
        )

        requisition = parse_screening_result(message, task_id="M1")

        self.assertEqual([entry.capability for entry in requisition.items], ["python"])

    def test_refuses_a_requisition_for_another_task(self) -> None:
        message = screening_message({"task_id": "M2", "items": []})

        with self.assertRaisesRegex(ScreeningProtocolError, "M1"):
            parse_screening_result(message, task_id="M1")

    def test_refuses_an_item_without_a_rationale(self) -> None:
        """The rationale is the record the choice is later judged against."""

        message = screening_message(
            {
                "task_id": "M1",
                "items": [
                    {
                        "capability": "python",
                        "necessity": "required",
                        "candidates": [{"id": "python-testing", "version": "2.1.0"}],
                    }
                ],
            }
        )

        with self.assertRaisesRegex(ScreeningProtocolError, "rationale"):
            parse_screening_result(message, task_id="M1")

    def test_refuses_duplicate_capabilities(self) -> None:
        message = screening_message(
            {
                "task_id": "M1",
                "items": [
                    item("python", candidates=[{"id": "a-skill", "version": "1.0.0"}]),
                    item("python", candidates=[{"id": "b-skill", "version": "1.0.0"}]),
                ],
            }
        )

        with self.assertRaisesRegex(ScreeningProtocolError, "capability"):
            parse_screening_result(message, task_id="M1")

    def test_refuses_an_item_with_neither_a_candidate_nor_a_search_intent(self) -> None:
        message = screening_message(
            {"task_id": "M1", "items": [item("python", candidates=[])]}
        )

        with self.assertRaisesRegex(ScreeningProtocolError, "search_intent"):
            parse_screening_result(message, task_id="M1")

    def test_an_unmatched_capability_may_carry_only_a_search_intent(self) -> None:
        requisition = parse_screening_result(
            screening_message(
                {
                    "task_id": "M1",
                    "items": [
                        item(
                            "svelte",
                            candidates=[],
                            search_intent="A procedure for Svelte 5 runes.",
                        )
                    ],
                }
            ),
            task_id="M1",
        )

        self.assertEqual(requisition.items[0].candidates, ())
        self.assertEqual(
            requisition.items[0].search_intent, "A procedure for Svelte 5 runes."
        )

    def test_refuses_unknown_item_fields_and_names_what_is_accepted(self) -> None:
        message = screening_message(
            {
                "task_id": "M1",
                "items": [
                    {
                        "capability": "python",
                        "rationale": "Needed.",
                        "necessity": "required",
                        "install": "https://example.invalid/skill.zip",
                    }
                ],
            }
        )

        with self.assertRaises(ScreeningProtocolError) as caught:
            parse_screening_result(message, task_id="M1")

        self.assertIn("install", str(caught.exception))
        self.assertIn("capability", str(caught.exception))
        self.assertIn("search_intent", str(caught.exception))

    def test_refuses_a_necessity_outside_the_declared_set(self) -> None:
        message = screening_message(
            {
                "task_id": "M1",
                "items": [
                    item(
                        "python",
                        necessity="mandatory",
                        candidates=[{"id": "a-skill", "version": "1.0.0"}],
                    )
                ],
            }
        )

        with self.assertRaisesRegex(ScreeningProtocolError, "necessity"):
            parse_screening_result(message, task_id="M1")


class HiringResolutionTests(unittest.TestCase):
    """Selection only.  The qualification gate is exercised end to end below."""

    def requisition(self, *items: dict) -> SkillRequisition:
        return parse_screening_result(
            screening_message({"task_id": "M1", "items": list(items)}), task_id="M1"
        )

    def test_hires_a_candidate_that_resolves(self) -> None:
        catalog = (skill_pack_from_raw(pack("python-testing", "python")),)

        decision = resolve_requisition(
            self.requisition(
                item("python", candidates=[{"id": "python-testing", "version": "1.0.0"}])
            ),
            catalog,
            require_qualification=False,
        )

        self.assertEqual(decision.hired, (SkillReference("python-testing", "1.0.0"),))
        self.assertEqual(decision.outcomes[0].status, "hired")
        self.assertEqual(decision.outcomes[0].reason, "")

    def test_reports_unmet_when_nothing_in_the_catalog_matches(self) -> None:
        decision = resolve_requisition(
            self.requisition(
                item(
                    "svelte",
                    candidates=[{"id": "svelte-runes", "version": "1.0.0"}],
                    search_intent="A procedure for Svelte 5 runes.",
                )
            ),
            (),
            require_qualification=False,
        )

        self.assertEqual(decision.hired, ())
        self.assertEqual(decision.outcomes[0].status, "unmet")
        self.assertIn("svelte-runes@1.0.0", decision.outcomes[0].reason)
        self.assertEqual(
            decision.outcomes[0].search_intent, "A procedure for Svelte 5 runes."
        )

    def test_withholds_a_candidate_status_pack_and_keeps_the_resolver_reason(self) -> None:
        catalog = (
            skill_pack_from_raw(
                pack("draft-procedure", "python", source="synthesized", status="candidate")
            ),
        )

        decision = resolve_requisition(
            self.requisition(
                item("python", candidates=[{"id": "draft-procedure", "version": "1.0.0"}])
            ),
            catalog,
            require_qualification=False,
        )

        self.assertEqual(decision.hired, ())
        self.assertEqual(decision.outcomes[0].status, "withheld")
        self.assertIn("CANDIDATE", decision.outcomes[0].reason)

    def test_falls_through_to_the_next_candidate(self) -> None:
        catalog = (skill_pack_from_raw(pack("python-testing", "python")),)

        decision = resolve_requisition(
            self.requisition(
                item(
                    "python",
                    candidates=[
                        {"id": "absent-skill", "version": "3.0.0"},
                        {"id": "python-testing", "version": "1.0.0"},
                    ],
                )
            ),
            catalog,
            require_qualification=False,
        )

        self.assertEqual(decision.hired, (SkillReference("python-testing", "1.0.0"),))

    def test_withholds_a_pack_that_conflicts_with_one_already_hired(self) -> None:
        """The conflict rule is the resolver's, and it runs against the
        stack being assembled - not against each candidate alone."""

        catalog = (
            skill_pack_from_raw(pack("strict-tdd", "testing")),
            skill_pack_from_raw(
                pack(
                    "fast-prototyping",
                    "prototyping",
                    conflicts_with=[{"id": "strict-tdd", "version": "1.0.0"}],
                )
            ),
        )

        decision = resolve_requisition(
            self.requisition(
                item("testing", candidates=[{"id": "strict-tdd", "version": "1.0.0"}]),
                item(
                    "prototyping",
                    candidates=[{"id": "fast-prototyping", "version": "1.0.0"}],
                ),
            ),
            catalog,
            require_qualification=False,
        )

        self.assertEqual(decision.hired, (SkillReference("strict-tdd", "1.0.0"),))
        self.assertEqual(decision.outcomes[1].status, "withheld")
        self.assertIn("conflict", decision.outcomes[1].reason)

    def test_the_decision_survives_a_round_trip_through_run_state(self) -> None:
        catalog = (skill_pack_from_raw(pack("python-testing", "python")),)
        decision = resolve_requisition(
            self.requisition(
                item("python", candidates=[{"id": "python-testing", "version": "1.0.0"}]),
                item("svelte", candidates=[], search_intent="Svelte 5 runes."),
            ),
            catalog,
            require_qualification=False,
        )

        restored = hiring_decision_from_raw(
            json.loads(json.dumps(decision.to_dict()))
        )

        self.assertEqual(restored, decision)
        self.assertEqual(restored.hired, (SkillReference("python-testing", "1.0.0"),))
        self.assertEqual(
            [outcome.rationale for outcome in restored.outcomes],
            [outcome.rationale for outcome in decision.outcomes],
        )


class SkillLibraryTests(unittest.TestCase):
    """What this machine has installed is the second half of the catalog.

    Without it the hiring layer can only choose from ``plan.skill_packs``,
    which is exactly the empty field the measurement above found.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Path(self.temp.name) / SKILL_LIBRARY_DIRNAME

    def install(self, raw: dict, *, name: str | None = None) -> Path:
        self.library.mkdir(parents=True, exist_ok=True)
        path = self.library / (name or f"{raw['id']}-{raw['version']}.json")
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path

    def test_a_missing_directory_is_an_empty_library(self) -> None:
        self.assertEqual(load_skill_library(self.library), ())

    def test_reads_installed_manifests_in_a_stable_order(self) -> None:
        self.install(pack("b-skill", "beta"))
        self.install(pack("a-skill", "alpha"))

        installed = load_skill_library(self.library)

        self.assertEqual([item.id for item in installed], ["a-skill", "b-skill"])

    def test_an_invalid_manifest_is_refused_by_file_name(self) -> None:
        broken = dict(pack("a-skill", "alpha"))
        broken["procedures"] = []
        self.install(broken, name="a-skill.json")

        with self.assertRaises(SkillLibraryError) as caught:
            load_skill_library(self.library)

        self.assertIn("a-skill.json", str(caught.exception))
        self.assertIn("procedures", str(caught.exception))

    def test_unreadable_json_names_the_file(self) -> None:
        self.library.mkdir(parents=True, exist_ok=True)
        (self.library / "truncated.json").write_text("{\"id\":", encoding="utf-8")

        with self.assertRaisesRegex(SkillLibraryError, "truncated.json"):
            load_skill_library(self.library)

    def test_the_library_may_promote_the_identical_plan_revision(self) -> None:
        """A candidate is tested and then promoted without changing what it
        proved, so the same revision sha256 in both places is one pack."""

        candidate = skill_pack_from_raw(
            pack("a-skill", "alpha", source="vetted", status="candidate")
        )
        promoted_raw = dict(pack("a-skill", "alpha", source="vetted", status="candidate"))
        promoted_raw["status"] = "trusted"
        promoted_raw["source_verification_ids"] = ["v-1"]
        promoted_raw["qualification_verification_ids"] = ["v-2"]
        promoted = skill_pack_from_raw(promoted_raw)
        self.assertEqual(candidate.revision_sha256, promoted.revision_sha256)

        catalog = skill_catalog((candidate,), (promoted,))

        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0].status, "trusted")

    def test_the_library_may_not_redefine_a_plan_declared_revision(self) -> None:
        """Different behaviour under the same id@version is ambiguous, and
        the trust records point at a digest that no longer describes it."""

        declared = skill_pack_from_raw(pack("a-skill", "alpha"))
        shadow_raw = dict(pack("a-skill", "alpha"))
        shadow_raw["procedures"] = ["Do something else entirely."]
        shadow = skill_pack_from_raw(shadow_raw)

        with self.assertRaises(SkillLibraryError) as caught:
            skill_catalog((declared,), (shadow,))

        self.assertIn("a-skill@1.0.0", str(caught.exception))
        self.assertIn(declared.revision_sha256[:12], str(caught.exception))

    def test_a_trusted_library_claim_still_fails_the_production_gate(self) -> None:
        """Dropping a manifest in the directory is not a way to be trusted."""

        forged = dict(pack("a-skill", "alpha"))
        forged["status"] = "trusted"
        forged["source_verification_ids"] = ["no-such-record"]
        forged["qualification_verification_ids"] = ["no-such-record"]
        self.install(forged)
        catalog = skill_catalog((), load_skill_library(self.library))

        decision = resolve_requisition(
            parse_screening_result(
                screening_message(
                    {
                        "task_id": "M1",
                        "items": [
                            item(
                                "alpha",
                                candidates=[{"id": "a-skill", "version": "1.0.0"}],
                            )
                        ],
                    }
                ),
                task_id="M1",
            ),
            catalog,
            qualification_evidence_store=_EmptyEvidenceStore(),
        )

        self.assertEqual(decision.hired, ())
        self.assertEqual(decision.outcomes[0].status, "withheld")


class _EmptyEvidenceStore:
    """A Project Memory that has never recorded anything."""

    path = Path("/nonexistent/memory.sqlite3")


class AttestedProjectCase(unittest.TestCase):
    """A real project whose Project Memory holds a qualified Skill Pack."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patch_hook_trust_gates(self)

    def _qualified_pack(
        self, *, trailing_task: dict | None = None
    ) -> tuple[dict, ProjectMemory]:
        """Drive the canonical attestation lifecycle for one exact revision."""

        raw = pack("vetted-runtime", "runtime-capability", status="candidate")
        raw["deterministic_checks"][0]["argv"] = [
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ]
        import subprocess

        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.root / "skill-attestation-plan.json"
        source_plan = attestation_plan(raw, ("source", "qualification"))
        if trailing_task is not None:
            source_plan["tasks"].append(trailing_task)
        plan_file.write_text(json.dumps(source_plan), encoding="utf-8")
        initialize_verified_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        cfg = load_config(self.root)
        memory = ProjectMemory(self.root)
        parsed = skill_pack_from_raw(raw)
        descriptor = reserve_ready_frontier(cfg)[0]
        records: dict[str, dict] = {}
        for index, kind in enumerate(("source", "qualification"), 1):
            task_id = f"A{index}"
            activate_via_app_server(cfg, self.root, descriptor, f"implementation-{task_id}")
            bump_task_checkpoint(self.root, task_id, f"implemented {kind}")
            if kind == "source":
                memory.record_evidence(
                    kind="tool",
                    summary="Implementation evidence for source review.",
                    created_by="skill-attestation-worker",
                    milestone_id=task_id,
                    role="source-origin",
                    tool_name="source-probe",
                )
            else:
                memory.record_evidence(
                    kind="tool",
                    summary="Implementation evidence for qualification setup.",
                    created_by="skill-attestation-worker",
                    milestone_id=task_id,
                    role="qualification-setup",
                    tool_name="qualification-probe",
                )
            implementation = complete_desktop_worker(
                cfg,
                thread_id=f"implementation-{task_id}",
                turn_id=f"implementation-turn-{task_id}",
                final_message="AUTOPILOT_STATUS: ROTATE",
            )
            verifier = implementation.descriptors[0]
            activate_via_app_server(cfg, self.root, verifier, f"verifier-{task_id}")
            bump_task_checkpoint(self.root, task_id, f"verified {kind}")
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
            if index == 2:
                self._before_last_acceptance()
                # Config is read once per call, so a hook that edits
                # config.toml has no effect on an object loaded earlier.
                cfg = load_config(self.root)
            accepted = complete_desktop_worker(
                cfg,
                thread_id=f"verifier-{task_id}",
                turn_id=f"verifier-turn-{task_id}",
                final_message='AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}',
            )
            self.last_descriptors = accepted.descriptors
            special = (
                f"SKILL-{kind.upper()}:{parsed.id}@{parsed.version}:"
                f"{parsed.revision_sha256}"
            )
            matches = memory.list_verification_results(task_id=special, limit=8).records
            self.assertEqual(len(matches), 1)
            records[kind] = memory.get_verification_result(matches[0]["id"])
            if index == 1:
                descriptor = accepted.descriptors[0]

        trusted = dict(raw)
        trusted["status"] = "trusted"
        trusted["source_verification_ids"] = [records["source"]["id"]]
        trusted["qualification_verification_ids"] = [records["qualification"]["id"]]
        return trusted, memory

    def _before_last_acceptance(self) -> None:
        """A hook for a subclass that needs the project changed mid-run."""

    def _requisition(self) -> SkillRequisition:
        return parse_screening_result(
            screening_message(
                {
                    "task_id": "A1",
                    "items": [
                        item(
                            "runtime-capability",
                            candidates=[{"id": "vetted-runtime", "version": "1.0.0"}],
                        )
                    ],
                }
            ),
            task_id="A1",
        )


class QualificationGateTests(AttestedProjectCase):
    """A hired skill still passes every production gate that already exists."""

    def test_a_qualified_pack_is_hired_through_the_production_gate(self) -> None:
        trusted, memory = self._qualified_pack()

        decision = resolve_requisition(
            self._requisition(),
            (skill_pack_from_raw(trusted),),
            qualification_evidence_store=memory,
        )

        self.assertEqual(decision.hired, (SkillReference("vetted-runtime", "1.0.0"),))
        self.assertEqual(
            shlex.join(skill_pack_from_raw(trusted).deterministic_checks[0].argv),
            memory.get_verification_result(
                trusted["qualification_verification_ids"][0]
            )["evidence"][0]["command"],
        )

    def test_an_unqualified_trusted_pack_is_withheld_not_hired(self) -> None:
        """Hiring cannot skip the gate that a plan-declared skill passes."""

        trusted, memory = self._qualified_pack()
        unqualified = dict(trusted)
        unqualified.pop("qualification_verification_ids")

        decision = resolve_requisition(
            self._requisition(),
            (skill_pack_from_raw(unqualified),),
            qualification_evidence_store=memory,
        )

        self.assertEqual(decision.hired, ())
        self.assertEqual(decision.outcomes[0].status, "withheld")
        self.assertIn("qualification", decision.outcomes[0].reason)


if __name__ == "__main__":
    unittest.main()


class PromptBindingTests(AttestedProjectCase):
    """The end of the chain: a hired skill is in the worker's prompt.

    The plan here declares the pack as a CANDIDATE and neither the role nor
    the task lists it, which is the shape of every real run.  Only the hire
    and the installed promotion can put it in front of the worker.
    """

    def _runtime_and_decision(self) -> tuple[AIStudioRuntime, object, dict]:
        trusted, memory = self._qualified_pack()
        library = self.root / ".codex-autopilot" / SKILL_LIBRARY_DIRNAME
        library.mkdir(parents=True, exist_ok=True)
        (library / "vetted-runtime.json").write_text(
            json.dumps(trusted), encoding="utf-8"
        )
        cfg = load_config(self.root)
        plan = load_plan(cfg.state_dir, cfg.profile)
        runtime = AIStudioRuntime(
            plan, cfg.root, language=cfg.language, skill_path=cfg.skill_path
        )
        decision = resolve_requisition(
            parse_screening_result(
                screening_message(
                    {
                        "task_id": "A1",
                        "items": [
                            item(
                                "runtime-capability",
                                rationale="A1 exercises this runtime procedure directly.",
                                candidates=[{"id": "vetted-runtime", "version": "1.0.0"}],
                            )
                        ],
                    }
                ),
                task_id="A1",
            ),
            skill_catalog(plan.skill_packs, load_skill_library(library)),
            qualification_evidence_store=memory,
        )
        return runtime, decision, trusted

    def test_a_hired_skill_reaches_the_worker_prompt(self) -> None:
        runtime, decision, trusted = self._runtime_and_decision()
        self.assertEqual(
            decision.hired, (SkillReference("vetted-runtime", "1.0.0"),)
        )
        self.assertEqual(runtime.plan.role_map["builder"].skill_requirements, ())
        self.assertEqual(runtime.plan.task_map["A1"].loaded_skills, ())

        prompt = runtime.build_prompt(
            "A1",
            phase="implementation",
            task_states={},
            reservation_token="token",
            hiring=decision,
        )

        self.assertIn(trusted["procedures"][0], prompt)
        self.assertIn(trusted["quality_criteria"][0], prompt)
        self.assertIn(skill_pack_from_raw(trusted).revision_sha256, prompt)

    def test_without_a_hire_the_same_task_gets_an_empty_stack(self) -> None:
        runtime, _decision, trusted = self._runtime_and_decision()

        prompt = runtime.build_prompt(
            "A1", phase="implementation", task_states={}, reservation_token="token"
        )

        self.assertIn('"loaded_skills":[]', prompt)
        self.assertNotIn(trusted["procedures"][0], prompt)

    def test_an_unhired_skill_is_still_refused_at_the_prompt_boundary(self) -> None:
        """Widening the requirement set for a hire does not open it for
        anything else: an undeclared, unhired reference is still refused."""

        runtime, _decision, _trusted = self._runtime_and_decision()
        forged = HiringDecision(
            task_id="A1",
            outcomes=(
                HiringOutcome(
                    capability="runtime-capability",
                    rationale="A1 exercises this runtime procedure directly.",
                    necessity="required",
                    status="hired",
                    skill=SkillReference("vetted-runtime", "9.9.9"),
                ),
            ),
        )

        with self.assertRaisesRegex(ContextBoundaryError, "9.9.9"):
            runtime.build_prompt(
                "A1",
                phase="implementation",
                task_states={},
                reservation_token="token",
                hiring=forged,
            )


class ScreeningLifecycleTests(AttestedProjectCase):
    """The whole chain through the real lifecycle: screen, resolve, bind.

    Screening is off by default because each one is a Codex thread out of
    the user's limits, so these runs turn it on the way a project would.
    """

    def _enable_screening(self, mode: str) -> None:
        set_screening_mode(self.root, mode)

    def _install(self, manifest: dict) -> None:
        library = self.root / ".codex-autopilot" / SKILL_LIBRARY_DIRNAME
        library.mkdir(parents=True, exist_ok=True)
        (library / "vetted-runtime.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def _before_last_acceptance(self) -> None:
        # The pack is installed as the CANDIDATE it still is: the
        # qualification record it needs to be trusted is written by the very
        # completion this hook runs before. The screener therefore sees it
        # exactly as a screener would - present, not yet trusted.
        self._install(_candidate_manifest())
        self._enable_screening(self.screening_mode)

    def _run(self, *, screening_mode: str) -> tuple[Any, dict]:
        """Attest a pack, then let the trailing task be hired for."""

        self.screening_mode = screening_mode
        trusted, _memory = self._qualified_pack(trailing_task=_trailing_task())
        return self.last_descriptors, trusted

    def test_the_trailing_task_is_screened_before_it_gets_a_worker(self) -> None:
        descriptors, _trusted = self._run(screening_mode="always")

        self.assertEqual([item.kind for item in descriptors], ["screening"])
        self.assertEqual(descriptors[0].task_id, "M1")
        self.assertTrue(descriptors[0].title.startswith("Screening | Hire M1"))
        self.assertIn("vetted-runtime", descriptors[0].prompt)
        self.assertIn(SCREENING_PREFIX, descriptors[0].prompt)

    def test_the_hire_reaches_the_worker_the_screening_hired_for(self) -> None:
        descriptors, trusted = self._run(screening_mode="always")
        # The attestations are on record now, so the installed manifest can
        # carry the trust they prove.
        self._install(trusted)
        cfg = load_config(self.root)
        activate_via_app_server(cfg, self.root, descriptors[0], "screening-M1")

        completed = complete_desktop_worker(
            cfg,
            thread_id="screening-M1",
            turn_id="screening-turn-M1",
            final_message=screening_message(
                {
                    "task_id": "M1",
                    "items": [
                        item(
                            "runtime-capability",
                            rationale="M1 runs the procedure this pack describes.",
                            candidates=[{"id": "vetted-runtime", "version": "1.0.0"}],
                        ),
                        item(
                            "svelte",
                            rationale="M1 has no Svelte in it, but ask anyway.",
                            necessity="helpful",
                            candidates=[],
                            search_intent="A procedure for Svelte 5 runes.",
                        ),
                    ],
                }
            ),
        )

        record = json.loads(
            (self.root / ".codex-autopilot" / "run-state.json").read_text(
                encoding="utf-8"
            )
        )["task_hiring"]["M1"]
        self.assertEqual(record["graph_version"], 1)
        self.assertEqual(record["screened_by"]["thread_id"], "screening-M1")
        self.assertEqual(
            [outcome["status"] for outcome in record["decision"]["outcomes"]],
            ["hired", "unmet"],
        )
        self.assertEqual(
            record["decision"]["outcomes"][0]["rationale"],
            "M1 runs the procedure this pack describes.",
        )

        worker = completed.descriptors[0]
        self.assertEqual((worker.kind, worker.task_id), ("implementation", "M1"))
        self.assertIn(trusted["procedures"][0], worker.prompt)
        self.assertIn("unfilled_skill_needs", worker.prompt)
        self.assertIn("M1 has no Svelte in it, but ask anyway.", worker.prompt)

    def test_an_unreadable_requisition_does_not_stop_the_task(self) -> None:
        """Skills fail closed; the task does not fail with them."""

        descriptors, trusted = self._run(screening_mode="always")
        cfg = load_config(self.root)
        for attempt in range(1, 3):
            self.assertEqual(descriptors[0].kind, "screening")
            thread = f"screening-M1-{attempt}"
            activate_via_app_server(cfg, self.root, descriptors[0], thread)
            completed = complete_desktop_worker(
                cfg,
                thread_id=thread,
                turn_id=f"screening-turn-M1-{attempt}",
                final_message="I could not decide.",
            )
            descriptors = completed.descriptors

        worker = descriptors[0]
        self.assertEqual((worker.kind, worker.task_id), ("implementation", "M1"))
        self.assertNotIn(trusted["procedures"][0], worker.prompt)
        record = json.loads(
            (self.root / ".codex-autopilot" / "run-state.json").read_text(
                encoding="utf-8"
            )
        )["task_hiring"]["M1"]
        self.assertIn("did not produce a readable requisition", record["unscreened"])
        self.assertEqual(record["decision"]["outcomes"], [])

    def test_a_run_switched_off_is_not_screened_at_all(self) -> None:
        """The switch survives the default being on."""

        self.screening_mode = "never"
        trusted, _memory = self._qualified_pack(trailing_task=_trailing_task())

        self.assertEqual(
            [item.kind for item in self.last_descriptors], ["implementation"]
        )
        self.assertNotIn(trusted["procedures"][0], self.last_descriptors[0].prompt)


def _candidate_manifest() -> dict:
    """The revision as it stands before its attestations are on record."""

    raw = pack("vetted-runtime", "runtime-capability", status="candidate")
    raw["deterministic_checks"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(0)"]
    return raw


def _trailing_task() -> dict:
    """A plain task after the attestations, so there is someone left to hire."""

    from _plan_contract import TEST_OUTCOME_ID, canonical_verification

    return {
        "id": "M1",
        "title": "Use the attested runtime procedure",
        "objective": "Apply the procedure the attested pack describes.",
        "definition_of_done": ["The procedure is applied and the result is reproducible."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files and deterministic tools are sufficient.",
        "reasoning": "medium",
        "role": "builder",
        "depends_on": ["A2"],
        "priority": 0,
        "verification": canonical_verification(verifier_role="reviewer"),
        "resources": [],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
        "produces_outcomes": [TEST_OUTCOME_ID],
        "acceptance_class": "mixed",
    }


class ScreeningModeTests(unittest.TestCase):
    """When a run screens at all, and why the default spends nothing."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / ".codex-autopilot"
        self.state_dir.mkdir(parents=True)

    def cfg(self, mode: str) -> Any:
        from codex_autopilot.config import Config, DesktopConfig, RetryConfig, RuntimeConfig

        root = self.state_dir.parent
        return Config(
            root=root,
            state_dir=self.state_dir,
            roadmap=root / "ROADMAP.md",
            profile="adaptive",
            language="en",
            skill_name="codex-autopilot-adaptive",
            skill_path=root / "SKILL.md",
            desktop=DesktopConfig(),
            retry=RetryConfig(),
            runtime=RuntimeConfig(skill_screening=mode),
        )

    @staticmethod
    def plan(packs: tuple = ()) -> Any:
        class _Plan:
            skill_packs = packs

        return _Plan()

    def install(self) -> None:
        library = self.state_dir / SKILL_LIBRARY_DIRNAME
        library.mkdir(parents=True, exist_ok=True)
        (library / "a-skill.json").write_text(
            json.dumps(pack("a-skill", "alpha")), encoding="utf-8"
        )

    def test_hiring_ships_enabled(self) -> None:
        """The owner chose it on, knowing the cost: one extra Codex thread
        per task, and +25 on her own 25-task run with no role reuse."""

        from codex_autopilot.config import DEFAULT_SKILL_SCREENING, RuntimeConfig

        self.assertEqual(DEFAULT_SKILL_SCREENING, "always")
        self.assertEqual(RuntimeConfig().skill_screening, "always")
        self.assertTrue(screening_applies(self.cfg(DEFAULT_SKILL_SCREENING), self.plan()))

    def test_never_switches_it_off_even_with_a_library_installed(self) -> None:
        self.install()

        self.assertFalse(screening_applies(self.cfg("never"), self.plan()))

    def test_auto_spends_nothing_when_there_is_nothing_to_hire(self) -> None:
        self.assertFalse(screening_applies(self.cfg("auto"), self.plan()))

    def test_auto_screens_once_the_machine_has_a_pack(self) -> None:
        self.install()

        self.assertTrue(screening_applies(self.cfg("auto"), self.plan()))

    def test_auto_screens_when_only_the_plan_declares_a_pack(self) -> None:
        declared = (skill_pack_from_raw(pack("a-skill", "alpha")),)

        self.assertTrue(screening_applies(self.cfg("auto"), self.plan(declared)))

    def test_always_screens_so_unmet_needs_are_recorded_from_the_first_run(self) -> None:
        self.assertTrue(screening_applies(self.cfg("always"), self.plan()))

    def test_an_unknown_mode_is_refused_by_the_config(self) -> None:
        from codex_autopilot.config import load_config

        with self.assertRaisesRegex(ValueError, "skill_screening"):
            load_config(_config_with(self.state_dir, 'skill_screening = "sometimes"'))


def _config_with(state_dir: Path, runtime_line: str) -> Path:
    path = state_dir / "config.toml"
    path.write_text(
        'profile = "adaptive"\nlanguage = "en"\n\n'
        f'[project]\nroot = "{state_dir.parent}"\n\n'
        '[desktop]\nskill_path = "SKILL.md"\n\n'
        f"[runtime]\n{runtime_line}\n",
        encoding="utf-8",
    )
    return path


class HiringRecordTests(unittest.TestCase):
    """The record is keyed by task contract, not by task id alone."""

    def decision(self) -> HiringDecision:
        return HiringDecision(
            task_id="M1",
            outcomes=(
                HiringOutcome(
                    capability="python",
                    rationale="M1 edits Python.",
                    necessity="required",
                    status="hired",
                    skill=SkillReference("python-testing", "1.0.0"),
                ),
            ),
        )

    def test_a_hire_made_for_an_older_graph_version_is_not_reused(self) -> None:
        """A replan rewrites task contracts under the same ids. Reusing the
        old hire would put skills chosen for vanished work in front of a
        worker doing different work."""

        records: dict[str, Any] = {}
        record_hiring(
            records,
            task_id="M1",
            graph_version=1,
            decision=self.decision(),
            requisition=None,
            screened_by={"thread_id": "screening-M1"},
            at="2026-09-20T00:00:00+00:00",
        )

        self.assertEqual(
            recorded_hiring(records, task_id="M1", graph_version=1),
            self.decision(),
        )
        self.assertIsNone(recorded_hiring(records, task_id="M1", graph_version=2))

    def test_a_record_that_names_another_task_is_refused_at_the_write(self) -> None:
        with self.assertRaisesRegex(ScreeningProtocolError, "M2"):
            record_hiring(
                {},
                task_id="M2",
                graph_version=1,
                decision=self.decision(),
                requisition=None,
                screened_by={},
                at="2026-09-20T00:00:00+00:00",
            )


class ScreeningInFlightTests(AttestedProjectCase):
    """A frontier pass while a screening is still in flight.

    Reachable whenever anything else raises the frontier before the screener
    has answered - another task completing in a parallel run, a wake-up, a
    re-arm. Without the wait branch the task falls through to the ordinary
    reservation check, which sees the pending screening session and refuses
    the whole pass with "already has a pending reservation".
    """

    def _before_last_acceptance(self) -> None:
        library = self.root / ".codex-autopilot" / SKILL_LIBRARY_DIRNAME
        library.mkdir(parents=True, exist_ok=True)
        (library / "vetted-runtime.json").write_text(
            json.dumps(_candidate_manifest()), encoding="utf-8"
        )
        set_screening_mode(self.root, "always")

    def test_the_frontier_waits_instead_of_refusing_the_whole_pass(self) -> None:
        self._qualified_pack(trailing_task=_trailing_task())
        pending = self.last_descriptors
        self.assertEqual([item.kind for item in pending], ["screening"])
        cfg = load_config(self.root)

        again = reserve_ready_frontier(cfg)

        self.assertEqual(again, ())
        sessions = json.loads(
            (self.root / ".codex-autopilot" / "run-state.json").read_text(
                encoding="utf-8"
            )
        )["worker_sessions"]
        screenings = [item for item in sessions if item["kind"] == "screening"]
        self.assertEqual(len(screenings), 1, "the pass must not re-hire the screener")
        self.assertEqual(
            screenings[0]["reservation_token"], pending[0].reservation_token
        )


class ExternalProvenanceTests(unittest.TestCase):
    """R18 at the pack boundary: a pack that came from outside is marked.

    A pack may be written from text somebody published. That text is
    external content: it may shape HOW the work is done and must never
    become the authority for WHETHER the work is accepted.
    """

    @staticmethod
    def sourced(**overrides) -> dict:
        raw = dict(pack("svelte-runes", "svelte", source="synthesized", status="candidate"))
        raw["external_sources"] = [
            {
                "provider": "github.com/example/skills",
                "locator": "docs/runes.md",
                "digest": "sha256:" + "0" * 64,
            }
        ]
        raw.update(overrides)
        return raw

    def test_a_pack_records_where_its_text_came_from(self) -> None:
        parsed = skill_pack_from_raw(self.sourced())

        self.assertEqual(len(parsed.external_sources), 1)
        self.assertEqual(
            parsed.external_sources[0].provider, "github.com/example/skills"
        )
        self.assertTrue(parsed.is_externally_sourced)

    def test_a_source_needs_a_provider(self) -> None:
        """The trust ladder refuses external evidence without one, so a
        pack may not carry a source that could never be recorded."""

        with self.assertRaisesRegex(SkillPackError, "provider"):
            skill_pack_from_raw(
                self.sourced(external_sources=[{"locator": "docs/runes.md"}])
            )

    def test_vetted_cannot_launder_an_outside_origin(self) -> None:
        """`vetted` claims an independent review of the origin. Fetching
        text is not that review."""

        with self.assertRaisesRegex(SkillPackError, "external_sources"):
            skill_pack_from_raw(self.sourced(source="vetted"))

    def test_declaring_a_source_changes_the_revision_digest(self) -> None:
        plain = skill_pack_from_raw(
            pack("svelte-runes", "svelte", source="synthesized", status="candidate")
        )

        self.assertNotEqual(
            plain.revision_sha256, skill_pack_from_raw(self.sourced()).revision_sha256
        )

    def test_a_pack_without_sources_keeps_the_digest_it_already_proved(self) -> None:
        """Adding the field must not invalidate every qualification on
        record in a live project."""

        parsed = skill_pack_from_raw(pack("python-testing", "python"))

        self.assertEqual(
            parsed.revision_sha256,
            "b80373831f1902f2bf28d445adf9e61e67b56a09bd7c50aa64eace7e8590f124",
        )


class VerifierIsNotToldByTheMarketTests(AttestedProjectCase):
    """R18: the worker may follow a market procedure; the acceptor may not.

    build_prompt resolves the same stack for every phase, so without this a
    pack written from somebody's published text would reach the session that
    decides whether the work passes.
    """

    def _runtime(self, *, external: bool) -> tuple[Any, dict, Any]:
        """Promote one pack for real, with or without an outside origin.

        The origin is declared BEFORE the attestations run: external_sources
        is inside revision_sha256, so a pack that gains one afterwards is a
        different revision and its records no longer describe it.
        """

        import subprocess

        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        raw = pack(
            "market-runtime",
            "runtime-capability",
            source="synthesized",
            status="candidate",
        )
        raw["deterministic_checks"][0]["argv"] = [
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ]
        if external:
            raw["external_sources"] = [
                {"provider": "github.com/example/skills", "locator": "docs/runes.md"}
            ]
        records, implementation = run_canonical_attestations(
            self.root, raw, ("promotion", "qualification")
        )
        manifest = dict(raw)
        manifest["status"] = "trusted"
        manifest["promotion_evidence"] = [
            {
                "id": records["promotion"]["id"],
                "kind": "independent_verification",
                "verified": True,
            },
            {"id": implementation["promotion"], "kind": "real_tool", "verified": True},
        ]
        manifest["qualification_verification_ids"] = [records["qualification"]["id"]]
        library = self.root / ".codex-autopilot" / SKILL_LIBRARY_DIRNAME
        library.mkdir(parents=True, exist_ok=True)
        (library / "market-runtime.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        cfg = load_config(self.root)
        plan = load_plan(cfg.state_dir, cfg.profile)
        runtime = AIStudioRuntime(
            plan, cfg.root, language=cfg.language, skill_path=cfg.skill_path
        )
        decision = HiringDecision(
            task_id="A1",
            outcomes=(
                HiringOutcome(
                    capability="runtime-capability",
                    rationale="A1 runs this procedure.",
                    necessity="required",
                    status="hired",
                    skill=SkillReference("market-runtime", "1.0.0"),
                ),
            ),
        )
        return runtime, manifest, decision

    def test_a_locally_sourced_pack_reaches_both_phases(self) -> None:
        """The control: without an outside origin nothing is withheld."""

        runtime, manifest, decision = self._runtime(external=False)

        for phase in ("implementation", "verification"):
            with self.subTest(phase=phase):
                prompt = runtime.build_prompt(
                    "A1",
                    phase=phase,
                    task_states={},
                    reservation_token="token",
                    hiring=decision,
                )
                self.assertIn(manifest["quality_criteria"][0], prompt)

    def test_a_market_sourced_pack_never_reaches_the_verifier(self) -> None:
        runtime, manifest, decision = self._runtime(external=True)

        worker = runtime.build_prompt(
            "A1",
            phase="implementation",
            task_states={},
            reservation_token="token",
            hiring=decision,
        )
        verifier = runtime.build_prompt(
            "A1",
            phase="verification",
            task_states={},
            reservation_token="token",
            hiring=decision,
        )

        self.assertIn(manifest["quality_criteria"][0], worker)
        self.assertIn("github.com/example/skills", worker)
        self.assertNotIn(manifest["quality_criteria"][0], verifier)
        self.assertNotIn(manifest["procedures"][0], verifier)

    def test_the_verifier_is_told_what_was_withheld_from_it(self) -> None:
        """Informed, not blinded: it knows which capability the worker
        carried, without reading the text that shaped the work."""

        runtime, _manifest, decision = self._runtime(external=True)

        verifier = runtime.build_prompt(
            "A1",
            phase="verification",
            task_states={},
            reservation_token="token",
            hiring=decision,
        )

        self.assertIn("withheld_external_skills", verifier)
        self.assertIn("runtime-capability", verifier)
        self.assertIn("market-runtime", verifier)


class ScreeningFailureReachesAWorkerTests(AttestedProjectCase):
    """The fail-open half, driven rather than asserted in isolation.

    Skills fail closed; the task does not fail with them. The predictable
    part - a task is created, handed over, runs to DONE - is the core, and
    hiring is superstructure that may not stop it.
    """

    def _before_last_acceptance(self) -> None:
        library = self.root / ".codex-autopilot" / SKILL_LIBRARY_DIRNAME
        library.mkdir(parents=True, exist_ok=True)
        (library / "vetted-runtime.json").write_text(
            json.dumps(_candidate_manifest()), encoding="utf-8"
        )
        set_screening_mode(self.root, "always")

    def test_a_screener_that_errors_every_time_still_yields_a_worker(self) -> None:
        self._qualified_pack(trailing_task=_trailing_task())
        descriptors = self.last_descriptors
        cfg = load_config(self.root)
        # Each turn ends in a way the protocol cannot read: no final line at
        # all, then a line that is valid JSON for the wrong task.
        replies = (
            "I looked at the project and could not decide.",
            screening_message({"task_id": "A2", "items": []}),
        )
        seen_kinds = []
        for attempt, reply in enumerate(replies, 1):
            seen_kinds.append(descriptors[0].kind)
            thread = f"screening-M1-{attempt}"
            activate_via_app_server(cfg, self.root, descriptors[0], thread)
            descriptors = complete_desktop_worker(
                cfg,
                thread_id=thread,
                turn_id=f"screening-turn-M1-{attempt}",
                final_message=reply,
            ).descriptors

        self.assertEqual(seen_kinds, ["screening", "screening"])
        worker = descriptors[0]
        self.assertEqual((worker.kind, worker.task_id), ("implementation", "M1"))
        self.assertIn('"loaded_skills":[]', worker.prompt)

        state = json.loads(
            (self.root / ".codex-autopilot" / "run-state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            "did not produce a readable requisition",
            state["task_hiring"]["M1"]["unscreened"],
        )
        # The two stopped screenings keep their own reason. They must not be
        # relabelled RETIRED_SUPERSEDED by the worker they failed to hire
        # for: the journal would then say they were superseded, when their
        # failure is exactly why that worker has no skills.
        stopped = [
            item
            for item in state["worker_sessions"]
            if item["kind"] == "screening" and item["status"] == "BLOCKED"
        ]
        self.assertEqual(len(stopped), 2)
        self.assertIn("exactly one", stopped[0]["failure_reason"])
        self.assertIn("A2", stopped[1]["failure_reason"])
        self.assertNotIn(
            "RETIRED_SUPERSEDED",
            [item["status"] for item in state["worker_sessions"]],
        )
        self.assertNotEqual(state["status"], "BLOCKED")


class ScreeningCostIsVisibleTests(AttestedProjectCase):
    """A cost she can see is a cost she can decide about.

    Screening spends one Codex thread per task out of the user's limits.
    The card says how many it has spent and how many tasks went without.
    """

    def _card(self) -> str:
        from codex_autopilot.plan import load_plan as _load_plan
        from codex_autopilot.run_state import StateStore
        from codex_autopilot.status import render_project_status

        cfg = load_config(self.root)
        return render_project_status(
            cfg,
            StateStore(cfg.state_dir).load(),
            _load_plan(cfg.state_dir, cfg.profile),
            dispatcher_running=False,
        )

    def test_a_run_that_never_screens_says_so_and_says_it_spent_nothing(self) -> None:
        self.screening_mode = "never"
        self._qualified_pack(trailing_task=_trailing_task())

        card = self._card()

        self.assertIn("Screening:", card)
        self.assertIn("off", card)
        self.assertIn("never", card)
        self.assertIn("0 threads", card)

    def test_the_card_counts_the_threads_spent_and_what_they_bought(self) -> None:
        self.screening_mode = "always"
        _trusted, _memory = self._qualified_pack(trailing_task=_trailing_task())
        cfg = load_config(self.root)
        activate_via_app_server(cfg, self.root, self.last_descriptors[0], "screening-M1")
        complete_desktop_worker(
            cfg,
            thread_id="screening-M1",
            turn_id="screening-turn-M1",
            final_message=screening_message(
                {
                    "task_id": "M1",
                    "items": [
                        item(
                            "svelte",
                            rationale="M1 does not need it, but ask.",
                            candidates=[],
                            search_intent="A procedure for Svelte 5 runes.",
                        )
                    ],
                }
            ),
        )

        card = self._card()

        self.assertIn("Screening: on (always)", card)
        self.assertIn("1 thread", card)
        self.assertIn("1 task screened", card)
        self.assertIn("1 need unfilled", card)

    def _before_last_acceptance(self) -> None:
        if getattr(self, "screening_mode", "never") == "never":
            return
        library = self.root / ".codex-autopilot" / SKILL_LIBRARY_DIRNAME
        library.mkdir(parents=True, exist_ok=True)
        (library / "vetted-runtime.json").write_text(
            json.dumps(_candidate_manifest()), encoding="utf-8"
        )
        set_screening_mode(self.root, self.screening_mode)


class BundleRequisitionTests(unittest.TestCase):
    """A screener may hand over the skill itself, not a paraphrase of it."""

    @staticmethod
    def message(**bundle) -> str:
        body = {
            "name": "taste",
            "staged_path": "staged-skills/taste",
            "provider": "github.com/example/skills",
            "locator": "skills/taste",
        }
        body.update(bundle)
        return screening_message(
            {
                "task_id": "M1",
                "items": [
                    {
                        "capability": "frontend-taste",
                        "rationale": "M1 builds the landing page and this is the house procedure.",
                        "necessity": "required",
                        "bundle": body,
                    }
                ],
            }
        )

    def test_a_bundle_item_needs_no_candidate_and_no_search_intent(self) -> None:
        requisition = parse_screening_result(self.message(), task_id="M1")

        bundle = requisition.items[0].bundle
        self.assertIsNotNone(bundle)
        self.assertEqual(bundle.name, "taste")
        self.assertEqual(bundle.provider, "github.com/example/skills")
        self.assertEqual(bundle.staged_path, "staged-skills/taste")

    def test_a_bundle_must_name_where_it_came_from(self) -> None:
        """It is external content; the trust ladder needs the provider."""

        with self.assertRaisesRegex(ScreeningProtocolError, "provider"):
            parse_screening_result(self.message(provider=""), task_id="M1")

    def test_a_staged_path_cannot_be_absolute_or_climb_out(self) -> None:
        for path in ("/etc", "../../.codex/plugins", "~/.codex/skills"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ScreeningProtocolError, "staged_path"):
                    parse_screening_result(self.message(staged_path=path), task_id="M1")

    def test_a_bundle_item_resolves_to_unmet_until_it_is_admitted(self) -> None:
        """resolve_requisition knows packs, not the filesystem. Admission is
        the runtime's own step, and until it happens nothing is claimed."""

        decision = resolve_requisition(
            parse_screening_result(self.message(), task_id="M1"),
            (),
            require_qualification=False,
        )

        self.assertEqual(decision.outcomes[0].status, "unmet")
        self.assertEqual(decision.hired, ())


class InstalledBundleReachesTheWorkerTests(AttestedProjectCase):
    """The worker gets the skill itself, by path, and the verifier does not."""

    def _before_last_acceptance(self) -> None:
        set_screening_mode(self.root, "always")

    def _hire_with_a_bundle(self) -> tuple[Any, dict]:
        self._qualified_pack(trailing_task=_trailing_task())
        cfg = load_config(self.root)
        staged = cfg.state_dir / "staged-skills" / "taste"
        staged.mkdir(parents=True)
        (staged / "SKILL.md").write_text(
            "---\nname: taste\n---\n\n# Taste\n\nWrite less code.\n",
            encoding="utf-8",
        )
        activate_via_app_server(cfg, self.root, self.last_descriptors[0], "screening-M1")
        completed = complete_desktop_worker(
            cfg,
            thread_id="screening-M1",
            turn_id="screening-turn-M1",
            final_message=screening_message(
                {
                    "task_id": "M1",
                    "items": [
                        {
                            "capability": "frontend-taste",
                            "rationale": "M1 builds a page and this is the house procedure.",
                            "necessity": "required",
                            "bundle": {
                                "name": "taste",
                                "staged_path": "staged-skills/taste",
                                "provider": "github.com/example/skills",
                                "locator": "skills/taste",
                            },
                        }
                    ],
                }
            ),
        )
        record = json.loads(
            (cfg.state_dir / "run-state.json").read_text(encoding="utf-8")
        )["task_hiring"]["M1"]
        return completed.descriptors[0], record

    def test_the_bundle_is_admitted_into_the_project_and_recorded(self) -> None:
        from codex_autopilot.hired_skills import hired_skill_records

        _worker, record = self._hire_with_a_bundle()
        cfg = load_config(self.root)

        outcome = record["decision"]["outcomes"][0]
        self.assertEqual(outcome["status"], "installed")
        self.assertEqual(outcome["bundle"]["provider"], "github.com/example/skills")
        installed = hired_skill_records(cfg.state_dir)
        self.assertEqual(len(installed), 1)
        self.assertEqual(installed[0]["name"], "taste")
        self.assertTrue(
            Path(installed[0]["path"]).is_relative_to(cfg.state_dir),
            "a bundle lives in the project, never in the user's Codex home",
        )

    def test_the_worker_is_told_the_exact_path_to_read(self) -> None:
        worker, record = self._hire_with_a_bundle()

        self.assertEqual(worker.kind, "implementation")
        self.assertIn("hired_skill_bundles", worker.prompt)
        self.assertIn(record["decision"]["outcomes"][0]["bundle"]["path"], worker.prompt)
        self.assertIn("frontend-taste", worker.prompt)

    def test_the_verifier_is_not_given_the_bundle(self) -> None:
        """R18 again: a bundle is external content by construction."""

        _worker, record = self._hire_with_a_bundle()
        cfg = load_config(self.root)
        runtime = AIStudioRuntime(
            load_plan(cfg.state_dir, cfg.profile),
            cfg.root,
            language=cfg.language,
            skill_path=cfg.skill_path,
        )
        decision = hiring_decision_from_raw(record["decision"])

        verifier = runtime.build_prompt(
            "M1",
            phase="verification",
            task_states={},
            reservation_token="token",
            hiring=decision,
        )

        self.assertNotIn("hired_skill_bundles", verifier)
        self.assertNotIn(
            record["decision"]["outcomes"][0]["bundle"]["path"], verifier
        )
        self.assertIn("withheld_external_skills", verifier)
        self.assertIn("frontend-taste", verifier)
