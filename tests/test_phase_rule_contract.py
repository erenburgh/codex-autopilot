"""M11-R16-R17-PHASE-CONTRACT: the rules contract is the same for every phase.

The rules block went to everyone, but the requirement to report the
applied ids stood only in the implementer and revision prompts. The
verifier and the replanner received the rules and were obliged to
report - though nobody asked them to: the completion audit is shared.
The on-call engineer received neither the rules block nor the audit at
all.

And the second half of R16, which existed nowhere: a divergence from the
recorded statement is filed as a Conflict and is not resolved by the one
who raised it.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import pathlib
import unittest

SRC = Path(__file__).resolve().parents[1] / "src" / "codex_autopilot"


class RuleConflictProtocolTests(unittest.TestCase):
    def _parse(self, message: str):
        from codex_autopilot.lifecycle_base import parse_rule_conflicts

        return parse_rule_conflicts(message)

    def test_a_disagreement_is_parsed_with_its_reason(self) -> None:
        parsed = self._parse(
            "итог\n"
            "AUTOPILOT_RULE_CONFLICT: R7 — область не покрывает сгенерированные файлы\n"
            "AUTOPILOT_RULES: R7\n"
            "AUTOPILOT_STATUS: ROTATE"
        )
        self.assertEqual(parsed, (("R7", "область не покрывает сгенерированные файлы"),))

    def test_a_disagreement_without_a_reason_is_not_a_disagreement(self) -> None:
        self.assertEqual(self._parse("AUTOPILOT_RULE_CONFLICT: R7 —"), ())

    def test_one_rule_is_reported_once(self) -> None:
        parsed = self._parse(
            "AUTOPILOT_RULE_CONFLICT: R7 - первая формулировка\n"
            "AUTOPILOT_RULE_CONFLICT: r7 - вторая формулировка"
        )
        self.assertEqual(len(parsed), 1)

    def test_silence_is_not_a_disagreement(self) -> None:
        self.assertEqual(self._parse("обычный отчёт без расхождений"), ())


class ConflictIsNotResolvedByTheWorkerTests(unittest.TestCase):
    """Declaring a conflict does not close it, or the rule is optional."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / ".git").mkdir()

    def test_the_conflict_opens_against_the_recorded_statement(self) -> None:
        from codex_autopilot.lifecycle_rule_audit import _rule_statement_record
        from codex_autopilot.memory import ProjectMemory
        from codex_autopilot.rules import rule

        memory = ProjectMemory(self.root)
        memory.initialize()
        canonical = rule("R7")
        first = _rule_statement_record(memory, "R7", canonical.statement)
        self.assertEqual(first["category"], "truth")
        again = _rule_statement_record(memory, "R7", canonical.statement)
        # A rule's statement is recorded once per project: otherwise the
        # history of divergences on the rule scatters across copies.
        self.assertEqual(first["id"], again["id"])

        reading = memory.add_observation(
            statement="R7: исполнитель T1 прочитал правило иначе — тест",
            created_by="task:T1",
        )
        conflict = memory.open_conflict(
            existing_record_id=str(first["id"]),
            incoming_record_id=str(reading["id"]),
            statement="R7: расхождение",
            created_by="task:T1",
        )
        # Memory opens the conflict in needs_review: it awaits review, and
        # the worker that raised it may not review it.
        self.assertEqual(
            memory.get_conflict(str(conflict["id"]))["status"], "needs_review"
        )


class EveryPhaseCarriesTheContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.studio = (SRC / "ai_studio.py").read_text(encoding="utf-8")
        self.completion = (SRC / "lifecycle_completion.py").read_text(encoding="utf-8")

    def test_the_verifier_is_asked_for_applied_rules(self) -> None:
        verifier_ru = self.studio[self.studio.index("AUTOPILOT_VERIFICATION"):]
        self.assertIn("AUTOPILOT_RULES", verifier_ru[:2000])

    def test_the_replanner_is_asked_for_applied_rules(self) -> None:
        for anchor in (
            "Верни только требуемый структурированный результат планирования.",
            "Return only the required structured planning result.",
        ):
            with self.subTest(anchor=anchor[:30]):
                tail = self.studio[self.studio.index(anchor):][:800]
                self.assertIn("AUTOPILOT_RULES", tail)

    def test_the_replanner_prompt_really_carries_the_rules(self) -> None:
        """Asking for applied rule ids is not the same as sending the rules.

        R17 is declared ENFORCED, and the replanner was asked to report the
        ids it applied - while its prompt was assembled with no rules block
        at all. It is the one phase that rewrites the whole graph, and the
        only phase that never saw the rules it is judged by. Measured by
        building the production prompt, not by reading the source: a check
        that greps for a line stays green when the line stops running.
        """

        import json
        import tempfile

        from codex_autopilot.config import load_config
        from codex_autopilot.lifecycle_prompts import _replanner_prompt
        from codex_autopilot.plan import load_plan
        from codex_autopilot.run_state import StateStore
        from codex_autopilot.rules import RULES
        from _plan_contract import (
            TEST_OUTCOME_ID,
            canonical_verification,
            canonicalize_plan,
            initialize_verified_project,
        )

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            (root / ".git").mkdir()
            skill = root / "SKILL.md"
            skill.write_text("# skill\n", encoding="utf-8")
            plan_file = root / "plan.json"
            plan_file.write_text(
                json.dumps(
                    canonicalize_plan(
                        {
                            "schema_version": 3,
                            "graph_version": 1,
                            "goal": "Exercise the replanner prompt.",
                            "user_request": "Exercise the replanner prompt.",
                            "model_strategy": "auto",
                            "execution_strategy": "parallel",
                            "max_parallel_workers": 1,
                            "computer_use_slots": 1,
                            "roles": [
                                {
                                    "id": "builder",
                                    "name": "Builder",
                                    "responsibilities": ["Build one task."],
                                }
                            ],
                            "tasks": [
                                {
                                    "id": "A",
                                    "title": "Task A",
                                    "objective": "Complete A.",
                                    "definition_of_done": ["A is verified."],
                                    "execution_mode": "code",
                                    "execution_mode_reason": "Files and tests suffice.",
                                    "reasoning": "medium",
                                    "role": "builder",
                                    "depends_on": [],
                                    "priority": 0,
                                    "verification": canonical_verification(),
                                    "resources": [],
                                    "required_capabilities": [],
                                    "context": {},
                                    "outputs": [],
                                    "tags": [],
                                    "produces_outcomes": [TEST_OUTCOME_ID],
                                    "acceptance_class": "mixed",
                                }
                            ],
                        }
                    )
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
            plan = load_plan(cfg.state_dir, cfg.profile)
            state = StateStore(cfg.state_dir).load()
            prompt = _replanner_prompt(
                cfg,
                plan,
                state,
                {
                    "id": "PC-01",
                    "request": {"purpose": "Add a task.", "evidence_ids": []},
                    "rejections": [],
                },
                "reservation-token",
            )

        enforced = [item.id for item in RULES if item.mode == "ENFORCED"]
        self.assertTrue(enforced)
        for rule_id in enforced:
            self.assertIn(rule_id, prompt)
        # R17: the rules stand before any specification they judge.
        self.assertLess(prompt.index('"rules"'), prompt.index('"current_plan"'))

    def test_the_engineer_receives_the_rules_block_not_just_a_promise(self) -> None:
        """A promise of "the same rules" without a rules block is a
        promise with nothing behind it."""

        self.assertIn('package["rules"] = rules_for_prompt(self.state_dir)', self.studio)

    def test_the_engineer_is_audited_like_a_worker(self) -> None:
        engineer = self.completion[self.completion.index("def _complete_pipeline_engineer"):]
        self.assertIn("_audit_rule_declaration(", engineer)
        self.assertIn("_record_rule_conflicts(", engineer)


if __name__ == "__main__":
    unittest.main()
