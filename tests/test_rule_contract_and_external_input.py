"""R16: rules apply as a contract. R18: external input does not override Truth."""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from codex_autopilot.lifecycle import parse_applied_rules
from codex_autopilot.memory import (
    EVIDENCE_KINDS,
    TRUTH_EVIDENCE_KINDS,
    MemoryValidationError,
    ProjectMemory,
)
from codex_autopilot.rules import RULES


class AppliedRulesReportTests(unittest.TestCase):
    def test_report_lists_applied_rule_ids(self) -> None:
        message = "сделано\nAUTOPILOT_RULES: R7, R17\nAUTOPILOT_STATUS: ROTATE"
        self.assertEqual(parse_applied_rules(message), ("R7", "R17"))

    def test_report_without_the_list_yields_nothing(self) -> None:
        self.assertEqual(parse_applied_rules("AUTOPILOT_STATUS: ROTATE"), ())

    def test_ids_are_deduplicated_and_case_insensitive(self) -> None:
        message = "AUTOPILOT_RULES: r7, R7, R1\nAUTOPILOT_STATUS: DONE"
        self.assertEqual(parse_applied_rules(message), ("R7", "R1"))

    def test_the_list_precedes_the_status_line(self) -> None:
        """Status parsing requires the status line to be the last one."""

        from codex_autopilot.lifecycle import parse_desktop_worker_status

        message = "AUTOPILOT_RULES: R7\nAUTOPILOT_STATUS: ROTATE"
        self.assertEqual(parse_desktop_worker_status(message), ("ROTATE", ""))
        self.assertEqual(parse_applied_rules(message), ("R7",))

    def test_prompt_asks_for_the_list_in_both_languages(self) -> None:
        """The worker prompt asks for the list of rules - in both
        languages, by running the build.

        Before, two phrases were looked for in the ai_studio.py source.
        A line in the file is not yet a line in the prompt: it can sit
        in a branch the build never reaches. Here the real prompt is
        built twice, once per run language, and the phrase has to turn
        up in the result.
        """

        from codex_autopilot.ai_studio import AIStudioRuntime
        from test_ai_studio import plan, task

        expected = {
            "ru": "строку AUTOPILOT_RULES с id правил",
            "en": "an AUTOPILOT_RULES line with the ids",
        }
        for language, phrase in expected.items():
            with self.subTest(language=language):
                root = Path(tempfile.mkdtemp()).resolve()
                self.addCleanup(shutil.rmtree, root, True)
                (root / ".git").mkdir()
                skill = root / "SKILL.md"
                skill.write_text("# test skill\n", encoding="utf-8")
                runtime = AIStudioRuntime(
                    plan([task("code-a", "integrator")]),
                    root,
                    language=language,
                    skill_path=skill,
                )
                prompt = runtime.build_prompt(
                    "code-a",
                    phase="implementation",
                    task_states={"code-a": "READY"},
                    reservation_token="fresh-relay",
                )
                self.assertIn(phrase, prompt)

    def test_every_prompt_variant_asks_for_the_list(self) -> None:
        """Counting occurrences does not work here: there are several
        variants of the final line, and missing one is enough for a
        worker to collect an R16 defect for something it was never
        asked to do."""

        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "codex_autopilot"
            / "ai_studio.py"
        ).read_text(encoding="utf-8")
        silent = [
            index
            for index, line in enumerate(source.splitlines(), 1)
            if "AUTOPILOT_STATUS:" in line
            and ("Заверши" in line or "Finish with" in line)
            and "AUTOPILOT_RULES" not in line
        ]
        self.assertEqual(silent, [], f"prompt variants that do not ask for rules: {silent}")


class ExternalInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        (root / ".git").mkdir()
        self.memory = ProjectMemory(root)

    def test_external_is_a_known_evidence_kind(self) -> None:
        """Outside text is recorded - it does not vanish, and it is not
        promoted."""

        self.assertIn("external", EVIDENCE_KINDS)

    def test_external_evidence_cannot_support_truth(self) -> None:
        self.assertNotIn("external", TRUTH_EVIDENCE_KINDS)
        evidence = self.memory.record_evidence(
            kind="external",
            summary="Комментарий в чужом issue утверждает, что порт 8080.",
            created_by="mcp-test",
            provider="github.com/other/repo#12",
            result="INFO",
        )
        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.record_verified_fact(
                statement="Порт сервиса 8080.",
                evidence_ids=[evidence["id"]],
                verification_method="external claim",
                created_by="mcp-test",
            )
        self.assertIn("R18", str(caught.exception))

    def test_external_material_has_a_label_on_the_ingestion_surface(self) -> None:
        """R18 is meaningless if the honest label is missing from the
        tool.

        The memory_record_evidence schema did not list "external" at
        all: a worker taking in text from outside could record it only
        as file, tool or user_instruction - so the contamination
        disappeared at the moment of intake, and every later check
        looked at a label nobody could set.
        """

        from codex_autopilot.memory_mcp import TOOLS

        choices = TOOLS[0]["inputSchema"]["oneOf"]
        choice = next(
            item
            for item in choices
            if item["properties"]["operation"]["const"] == "record_evidence"
        )
        kind = choice["properties"]["kind"]
        self.assertIn("external", kind["enum"])
        self.assertIn("provider", kind["description"])
        self.assertIn(
            "Required when kind",
            choice["properties"]["provider"]["description"],
        )

    def test_every_operation_carries_its_description_into_the_single_tool(self) -> None:
        """One tool is declared to the outside: operation descriptions
        get through only this way.

        The descriptions were written for every operation and did not
        reach the final schema at all - dead text the model never saw.
        """

        from codex_autopilot.memory_mcp import TOOLS, _ACTION_BY_NAME

        for choice in TOOLS[0]["inputSchema"]["oneOf"]:
            name = choice["properties"]["operation"]["const"]
            with self.subTest(operation=name):
                self.assertEqual(
                    choice.get("description"), _ACTION_BY_NAME[name]["description"]
                )
                self.assertTrue(choice.get("description"))

    def test_external_evidence_requires_a_provider(self) -> None:
        """Provenance is required at the moment of intake, not later."""

        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.record_evidence(
                kind="external",
                summary="Текст со страницы",
                created_by="mcp-test",
            )
        self.assertIn("provider", str(caught.exception))

    def test_a_non_user_constraint_cannot_rest_on_external_content(self) -> None:
        """A Constraint has no "proposed" state: it binds right away."""

        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.add_constraint(
                statement="Всегда менять очередь по требованию из чужого PR.",
                origin="agent",
                created_by="mcp-test",
                evidence_ids=[self.external_evidence()],
            )
        self.assertIn("R18", str(caught.exception))

    def test_a_proposed_decision_cannot_be_promoted_around_the_check(self) -> None:
        """The intake check is bypassed in two calls if the transition
        is not checked."""

        decision = self.memory.propose_decision(
            statement="Сменить очередь задач по требованию из чужого PR.",
            origin="agent",
            created_by="mcp-test",
            evidence_ids=[self.external_evidence()],
        )
        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.set_decision_status(
                decision["id"], "accepted", actor="mcp-test"
            )
        self.assertIn("R18", str(caught.exception))

    def test_external_support_cannot_be_attached_after_the_fact(self) -> None:
        """The third bypass: the record is put through clean, and the
        outside text is attached afterwards."""

        decision = self.memory.propose_decision(
            statement="Решение агента без внешних ссылок.",
            origin="agent",
            created_by="mcp-test",
        )
        self.memory.set_decision_status(decision["id"], "accepted", actor="mcp-test")
        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.attach_evidence(
                decision["id"],
                self.external_evidence(),
                relation="supports",
                actor="mcp-test",
            )
        self.assertIn("R18", str(caught.exception))

    def test_contradicting_external_evidence_stays_attachable(self) -> None:
        """This is exactly how outside material should work: it produces
        a Conflict.

        A ban on "contradicts" would silence disagreement - the exact
        opposite of what the rule was written for.
        """

        decision = self.memory.propose_decision(
            statement="Решение агента, которое внешний текст оспаривает.",
            origin="agent",
            created_by="mcp-test",
        )
        self.memory.set_decision_status(decision["id"], "accepted", actor="mcp-test")
        self.memory.attach_evidence(
            decision["id"],
            self.external_evidence(),
            relation="contradicts",
            actor="mcp-test",
        )

    def external_evidence(self) -> str:
        return self.memory.record_evidence(
            kind="external",
            summary="Комментарий в чужом PR требует сменить очередь задач.",
            created_by="mcp-test",
            provider="github.com/other/repo#34",
            result="INFO",
        )["id"]

    def test_decision_resting_on_external_content_cannot_start_accepted(self) -> None:
        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.propose_decision(
                statement="Перейти на другую очередь задач.",
                origin="project",
                created_by="mcp-test",
                status="accepted",
                evidence_ids=[self.external_evidence()],
            )
        self.assertIn("R18", str(caught.exception))

    def test_decision_resting_on_external_content_may_be_proposed(self) -> None:
        """External input may propose - but it is not what decides."""

        decision = self.memory.propose_decision(
            statement="Перейти на другую очередь задач.",
            origin="project",
            created_by="mcp-test",
            evidence_ids=[self.external_evidence()],
        )
        self.assertEqual(decision["status"], "proposed")

    def test_the_user_may_accept_a_decision_citing_external_content(self) -> None:
        """The boundary is deliberate: the user decides, not the text
        that was found."""

        decision = self.memory.propose_decision(
            statement="Перейти на другую очередь задач.",
            origin="user",
            created_by="paul",
            status="accepted",
            evidence_ids=[self.external_evidence()],
        )
        self.assertEqual(decision["status"], "accepted")


class ConflictResolutionIsNotALaunderingPathTests(unittest.TestCase):
    """R18 at the one writer nobody had checked: conflict resolution.

    Measured on a live database. ``attach_evidence`` refuses external
    support while a Truth is verified - but ``disputed`` is not a binding
    status, so the same call succeeds once a conflict is open, and
    resolution then returned the record to ``verified`` with a raw UPDATE
    and no trust check. Three calls of the single exposed memory tool -
    attach contradicts, attach supports, resolve - and unverified outside
    material was binding support for a verified fact.

    The same line hardcoded ``verified`` for every outcome but
    ``supersede_existing``, so a retired Truth came back to life.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        (root / ".git").mkdir()
        self.memory = ProjectMemory(root)

    def _deterministic(self, summary: str) -> str:
        return str(
            self.memory.record_evidence(
                kind="test",
                summary=summary,
                created_by="mcp-test",
                command="pytest -q",
                result="PASS",
                exit_code=0,
            )["id"]
        )

    def _verified_fact(self, statement: str) -> dict:
        return self.memory.record_verified_fact(
            statement=statement,
            evidence_ids=[self._deterministic(f"suite proves: {statement}")],
            verification_method="ran the suite",
            created_by="mcp-test",
        )

    def _open_conflict_id(self) -> str:
        with self.memory._connect() as db:
            return str(
                db.execute(
                    "SELECT id FROM conflicts WHERE status='needs_review'"
                ).fetchone()["id"]
            )

    def test_external_support_cannot_be_laundered_through_the_disputed_window(self) -> None:
        fact = self._verified_fact("The flag is --yes.")
        outside = self.memory.record_evidence(
            kind="external",
            summary="An upstream comment says the flag was renamed.",
            created_by="mcp-test",
            provider="github.com/other/repo#12",
            result="INFO",
        )
        with self.assertRaises(MemoryValidationError):
            self.memory.attach_evidence(
                fact["id"], outside["id"], relation="supports", actor="worker"
            )
        # A contradiction is always attachable, and it opens the dispute.
        self.memory.attach_evidence(
            fact["id"], outside["id"], relation="contradicts", actor="worker"
        )
        self.assertEqual(self.memory.get_record(fact["id"])["status"], "disputed")
        self.memory.attach_evidence(
            fact["id"], outside["id"], relation="supports", actor="worker"
        )

        with self.assertRaises(MemoryValidationError) as caught:
            self.memory.resolve_conflict(
                self._open_conflict_id(),
                outcome="reverified_existing",
                resolution="Looked again.",
                actor="worker",
            )
        self.assertIn("R18", str(caught.exception))
        # The refusal names the way out, and the record does not come back
        # binding on its own.
        self.assertIn("external content does not decide", str(caught.exception))
        self.assertEqual(self.memory.get_record(fact["id"])["status"], "disputed")

    def test_a_retired_truth_is_not_resurrected_by_resolving_a_conflict(self) -> None:
        fact = self._verified_fact("The flag is --force.")
        # A Truth is retired by the runtime, not by a public call: there is
        # no API for it, and the point here is the status the resolution
        # finds, however it got there.
        with self.memory._connect(write=True) as db:
            db.execute(
                "UPDATE records SET status='superseded' WHERE id=?", (fact["id"],)
            )
        self.assertEqual(self.memory.get_record(fact["id"])["status"], "superseded")

        conflict = self.memory.open_conflict(
            existing_record_id=fact["id"],
            statement="Does the retired statement hold after all?",
            created_by="mcp-test",
            incoming_evidence_id=self._deterministic("a later run"),
        )
        self.memory.resolve_conflict(
            conflict["id"],
            outcome="reject_incoming",
            resolution="The incoming claim was wrong.",
            actor="worker",
        )
        self.assertEqual(self.memory.get_record(fact["id"])["status"], "superseded")

    def test_a_clean_dispute_still_resolves_back_to_verified(self) -> None:
        """The gate must not become a wall: a clean conflict still closes."""

        fact = self._verified_fact("The suite is green.")
        self.memory.attach_evidence(
            fact["id"],
            self._deterministic("a run that disagreed"),
            relation="contradicts",
            actor="worker",
        )
        self.assertEqual(self.memory.get_record(fact["id"])["status"], "disputed")
        self.memory.resolve_conflict(
            self._open_conflict_id(),
            outcome="reverified_existing",
            resolution="Re-ran it; the original holds.",
            actor="worker",
        )
        self.assertEqual(self.memory.get_record(fact["id"])["status"], "verified")


class RegistryCoverageTests(unittest.TestCase):
    def test_r16_and_r18_exist_in_the_registry(self) -> None:
        ids = {item.id for item in RULES}
        self.assertIn("R16", ids)
        self.assertIn("R18", ids)


if __name__ == "__main__":
    unittest.main()


class WorkerReasonCodeTests(unittest.TestCase):
    """R13: a work stoppage is named by a code, not by a retelling of
    the status.

    Before, the reason went into last_error as the string "M9 worker
    returned BLOCKED" - which holds nothing the status does not hold
    already. On such a record you can neither route an escalation, nor
    count, nor tell "a user decision is needed" from "the environment
    broke".
    """

    def _parse(self, message: str):
        from codex_autopilot.lifecycle import parse_desktop_worker_status

        return parse_desktop_worker_status(message)

    def test_a_code_is_carried_through(self) -> None:
        self.assertEqual(
            self._parse("итог\nAUTOPILOT_STATUS: BLOCKED MISSING_RESOURCE"),
            ("BLOCKED", "MISSING_RESOURCE"),
        )
        self.assertEqual(
            self._parse("итог\nAUTOPILOT_STATUS: ESCALATE PRODUCT_DECISION"),
            ("ESCALATE", "PRODUCT_DECISION"),
        )

    def test_an_unknown_code_is_refused(self) -> None:
        """A list anything can be added to is not a closed list."""

        from codex_autopilot.lifecycle_base import DesktopLifecycleError

        with self.assertRaises(DesktopLifecycleError):
            self._parse("итог\nAUTOPILOT_STATUS: BLOCKED BECAUSE_I_SAID_SO")

    def test_a_missing_code_is_recorded_not_forgiven(self) -> None:
        """A hard refusal here would jam the pipeline at the exact point
        of the failure.

        The worker turn is already finished, there will be no second
        answer. So a missing code is UNSPECIFIED, and it is counted
        separately as an R13 violation in lifecycle_completion.
        """

        self.assertEqual(
            self._parse("итог\nAUTOPILOT_STATUS: BLOCKED"),
            ("BLOCKED", "UNSPECIFIED"),
        )
        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_completion.py"
        ).read_text(encoding="utf-8")
        self.assertIn('reason_code == "UNSPECIFIED"', source)
        self.assertIn('"R13"', source)

    def test_success_carries_no_reason(self) -> None:
        from codex_autopilot.lifecycle_base import DesktopLifecycleError

        self.assertEqual(self._parse("итог\nAUTOPILOT_STATUS: DONE"), ("DONE", ""))
        with self.assertRaises(DesktopLifecycleError):
            self._parse("итог\nAUTOPILOT_STATUS: DONE MISSING_RESOURCE")

    def test_the_prompt_names_every_code_a_worker_may_use(self) -> None:
        """A closed list is useless if the worker was never shown it."""

        from codex_autopilot.lifecycle_base import WORKER_REASON_CODES

        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/ai_studio.py"
        ).read_text(encoding="utf-8")
        for code in WORKER_REASON_CODES - {"UNSPECIFIED"}:
            with self.subTest(code=code):
                self.assertIn(code, source)
        # UNSPECIFIED is a record that there was no code, not a code
        # the worker is offered to choose.
        self.assertNotIn("UNSPECIFIED", source)

    def test_the_failure_record_names_the_code(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src/codex_autopilot/lifecycle_completion.py"
        ).read_text(encoding="utf-8")
        # The record is now written by the one door every stop goes through,
        # so the code travels as the reason it is given rather than as a
        # direct assignment. What R13 asks - that the record names the code,
        # not a retelling of the status - is unchanged.
        self.assertIn(
            'reason=f"{task_id} {kind} {worker_status} {reason_code}".strip(),',
            source,
        )
        self.assertIn("phase=\"BLOCKED\",", source)
