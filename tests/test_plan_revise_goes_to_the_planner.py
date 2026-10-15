"""A REVISE of the initial plan goes back to the planner, not to the owner.

History. On 15 Oct a tester's fresh start stopped at preflight with "Plan
verification: FAIL"; the skill reads a non-zero exit as a finished answer,
so the agent printed it and stopped. Measured with the real verifier at the
production effort on one and the same good plan: two PASS, one REVISE with
substantive Definition of Done remarks. Nobody handed the plan back to the
one who wrote it - the initiating agent, the planner before run-state.

Now a REVISE is the planner's answer with its own exit code, rounds are
counted beside the plan and bound to its sha, and only the last round's
REVISE is the FAIL it always was. Everything here runs the real
``run_preflight`` (and the real CLI) against a fake App Server.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from codex_autopilot import cli
from codex_autopilot.appserver import TurnResult
from codex_autopilot.plan import plan_to_dict, validate_plan
from codex_autopilot.plan_revision_rounds import (
    MAX_INITIAL_PLAN_ROUNDS,
    PLAN_REVISION_EXIT_CODE,
    REVISE_MARKER,
    ROUNDS_FILE_NAME,
)
from codex_autopilot.plan_verification import PLAN_VERIFICATION_PREFIX, plan_sha256
from codex_autopilot.preflight import (
    PLAN_VERIFICATION_TITLE,
    PlanRevisionRequired,
    PreflightError,
    run_preflight,
)
from test_preflight import DESKTOP_PROJECT, HOST_SKILL, SKILL, PreflightClient, plan as preflight_plan, project


PASS = {"verdict": "PASS", "issues": []}


def revise(summary: str) -> dict[str, object]:
    return {
        "verdict": "REVISE",
        "issues": [
            {
                "category": "dod_sufficiency",
                "summary": summary,
                "task_ids": ["M1"],
                "outcome_ids": [],
            }
        ],
    }


def edition(number: int):
    """The same plan after ``number`` edits: one more Definition of Done item each."""

    raw = plan_to_dict(preflight_plan())
    raw["tasks"][0]["definition_of_done"] = ["UI tests pass"] + [
        f"Edit {index}: the verifier's remark {index} is closed by a named test."
        for index in range(1, number + 1)
    ]
    return validate_plan(raw, "adaptive")


class ScriptedVerifier(PreflightClient):
    """The fake App Server whose plan verifier answers from a script."""

    script: list[dict[str, object]] = []
    verifier_turns = 0

    def wait_for_turn(self, thread_id, turn_id, **kwargs):
        call = next(turn for turn in reversed(self.plain_turns) if turn["thread_id"] == thread_id)
        if "PLAN_VERIFICATION_CONTEXT:" not in call["prompt"]:
            return PreflightClient.wait_for_turn(self, thread_id, turn_id, **kwargs)
        type(self).verifier_turns += 1
        answer = type(self).script.pop(0)
        turn = {
            "id": turn_id,
            "status": "completed",
            "items": [
                {
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": f"{PLAN_VERIFICATION_PREFIX} {json.dumps(answer)}",
                }
            ],
        }
        return TurnResult(thread_id, turn, [])


class PlanReviseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        PreflightClient.instances.clear()
        PreflightClient.probe_instances.clear()
        ScriptedVerifier.script = []
        ScriptedVerifier.verifier_turns = 0
        self.root = project()
        self.lines: list[str] = []

    def preflight(self, plan, *answers: dict[str, object], root: Path | None = None):
        ScriptedVerifier.script.extend(answers)
        self.lines.clear()
        return run_preflight(
            root or self.root,
            plan=plan,
            profile="adaptive",
            skill_path=SKILL,
            binary="/bin/echo",
            client_factory=ScriptedVerifier,
            desktop_project_id=DESKTOP_PROJECT,
            emit=self.lines.append,
            plan_file=(root or self.root) / ".codex-autopilot/bootstrap-plan.json",
        )

    def revise_answer(self, plan, *answers: dict[str, object], root: Path | None = None) -> str:
        with self.assertRaises(PlanRevisionRequired) as caught:
            self.preflight(plan, *answers, root=root)
        return str(caught.exception)

    def rounds_file(self, root: Path | None = None) -> Path:
        return (root or self.root).resolve() / ".codex-autopilot" / ROUNDS_FILE_NAME

    def recorded(self, root: Path | None = None) -> list[dict[str, object]]:
        return json.loads(self.rounds_file(root).read_text(encoding="utf-8"))["rounds"]


class ReviseIsThePlannersAnswerTests(PlanReviseTestCase):
    def test_revise_prints_the_marker_every_issue_and_the_next_step(self) -> None:
        """Mutation: ``_run_initial_plan_verification`` raises the plain
        PlanVerificationError on REVISE again - PreflightError, no marker."""

        answer = self.revise_answer(edition(0), revise("DoD names no check for the empty state."))
        lines = answer.splitlines()
        self.assertEqual(lines[0], f"{REVISE_MARKER} (round 1 of {MAX_INITIAL_PLAN_ROUNDS})")
        self.assertEqual(lines[0], "Plan verification: REVISE (round 1 of 3)")
        issue = next(line for line in lines if line.startswith("Issue 1 of 1: "))
        self.assertEqual(
            json.loads(issue[len("Issue 1 of 1: "):]),
            revise("DoD names no check for the empty state.")["issues"][0],
        )
        step = next(line for line in lines if line.startswith("Next: "))
        self.assertIn(str(self.root / ".codex-autopilot/bootstrap-plan.json"), step)
        self.assertIn("run the same start-skill command again", step)
        self.assertIn("do not argue with the verifier", step)
        self.assertIn("do not drop, weaken or narrow any requirement", step)
        # A fresh verifier task, as before, archived when preflight exits.
        client = PreflightClient.instances[-1]
        self.assertIn(("preflight-thread-2", PLAN_VERIFICATION_TITLE), client.named)
        self.assertIn("preflight-thread-2", client.archived)
        self.assertNotIn("Plan verification: FAIL", "\n".join(self.lines))
        self.assertFalse((self.root / ".codex-autopilot/run-state.json").exists())
        (only,) = self.recorded()
        self.assertEqual((only["round"], only["plan_sha256"]), (1, plan_sha256(edition(0))))

    def test_the_exit_code_is_neither_success_nor_fail_nor_approval(self) -> None:
        """Mutation: PLAN_REVISION_EXIT_CODE = 2 - the FAIL's code."""

        self.assertNotIn(PLAN_REVISION_EXIT_CODE, {0, 2, 77})
        self.assertEqual(PlanRevisionRequired.exit_code, PLAN_REVISION_EXIT_CODE)
        self.assertTrue(issubclass(PlanRevisionRequired, PreflightError))

    def test_start_skill_exits_with_it_and_prints_the_marker_on_its_own_line(self) -> None:
        """The real CLI ``start-skill``, the real run_preflight, a fake App Server.

        Mutation: the CLI's own ``except PlanRevisionRequired`` removed - the
        generic handler answers 2 with a prefixed line on stderr.
        """

        plan_file = self.root / ".codex-autopilot/bootstrap-plan.json"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(json.dumps(plan_to_dict(edition(0))), encoding="utf-8")
        ScriptedVerifier.script.append(revise("DoD names no check for the empty state."))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "newest_codex_binary", return_value="/bin/echo"), mock.patch.object(
            cli, "run_preflight", functools.partial(run_preflight, client_factory=ScriptedVerifier)
        ), mock.patch.dict(os.environ, {"CODEX_AUTOPILOT_SKILL_PATH": str(SKILL)}), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            code = cli.main([
                "start-skill", "--project", str(self.root), "--plan-file", str(plan_file),
                "--replace", "--desktop-project-id", DESKTOP_PROJECT,
            ])
        self.assertEqual(code, PLAN_REVISION_EXIT_CODE)
        printed = out.getvalue().splitlines()
        self.assertIn("Plan verification: REVISE (round 1 of 3)", printed)
        self.assertTrue(printed[-1].startswith("Fresh verifier task "), printed[-3:])
        self.assertNotIn("Starting Autopilot...", printed)
        self.assertEqual(err.getvalue(), "")
        self.assertFalse((self.root / ".codex-autopilot/run-state.json").exists())


class RoundsAreBoundToThePlanTests(PlanReviseTestCase):
    def test_a_rerun_without_an_edit_starts_no_verifier(self) -> None:
        """Mutation: ``recorded_for`` always None - App Server starts again
        and a second verifier counts round 2 on the unchanged plan."""

        first = self.revise_answer(edition(0), revise("Remark one."))
        servers = len(PreflightClient.instances)
        again = self.revise_answer(edition(0))
        self.assertEqual(ScriptedVerifier.verifier_turns, 1)
        self.assertEqual(len(PreflightClient.instances), servers, "App Server was started again")
        self.assertEqual(again.splitlines()[0], "Plan verification: REVISE (round 1 of 3)")
        self.assertEqual(first.splitlines()[:-1], again.splitlines()[:-1])
        self.assertIn("no new verifier ran", again.splitlines()[-1])
        self.assertEqual(len(self.recorded()), 1)

    def test_an_edited_plan_is_the_next_round(self) -> None:
        """Mutation: ``continued`` returns () - every round is round 1."""

        self.revise_answer(edition(0), revise("Remark one."))
        answer = self.revise_answer(edition(1), revise("Remark two."))
        self.assertEqual(answer.splitlines()[0], "Plan verification: REVISE (round 2 of 3)")
        self.assertIn("Remark two.", answer)
        self.assertNotIn("Remark one.", answer)
        self.assertEqual(ScriptedVerifier.verifier_turns, 2)
        self.assertEqual(
            [(item["round"], item["plan_sha256"]) for item in self.recorded()],
            [(1, plan_sha256(edition(0))), (2, plan_sha256(edition(1)))],
        )

    def test_the_third_revise_is_the_fail_with_every_rounds_issues(self) -> None:
        """Mutation: ``>= MAX_INITIAL_PLAN_ROUNDS`` becomes ``>`` - round 3
        is one more REVISE and the owner never hears of it."""

        self.revise_answer(edition(0), revise("Remark one."))
        self.revise_answer(edition(1), revise("Remark two."))
        with self.assertRaises(PreflightError) as caught:
            self.preflight(edition(2), revise("Remark three."))
        self.assertNotIsInstance(caught.exception, PlanRevisionRequired)
        message = str(caught.exception)
        self.assertTrue(message.startswith("proposed plan was rejected by the fresh verifier: "))
        self.assertIn("verdict REVISE in round 3 of 3", message)
        for remark in ("Remark one.", "Remark two.", "Remark three."):
            self.assertIn(remark, message)
        report = "\n".join(self.lines)
        self.assertIn("Plan verification: FAIL", report)
        self.assertNotIn("Plan verification: REVISE", report)
        self.assertEqual(len(self.recorded()), 3)

        # The same plan again: the same FAIL, and no verifier is spent on it.
        with self.assertRaises(PreflightError) as again:
            self.preflight(edition(2))
        self.assertNotIsInstance(again.exception, PlanRevisionRequired)
        self.assertIn("Remark one.", str(again.exception))
        self.assertEqual(ScriptedVerifier.verifier_turns, 3)

        # A plan changed after the FAIL - the owner's word - is a new series.
        answer = self.revise_answer(edition(3), revise("Remark four."))
        self.assertEqual(answer.splitlines()[0], "Plan verification: REVISE (round 1 of 3)")
        self.assertEqual(len(self.recorded()), 1)

    def test_a_pass_in_round_two_starts_and_clears_the_count(self) -> None:
        """Mutation: the ``clear_plan_revision_rounds`` call dropped - the
        count outlives the PASS and the next plan starts at round 3."""

        self.revise_answer(edition(0), revise("Remark one."))
        result = self.preflight(edition(1), PASS)
        self.assertEqual(result.plan_verification.verdict, "PASS")
        self.assertEqual(result.plan_verification.plan_sha256, plan_sha256(edition(1)))
        self.assertIn("Preflight: PASS", self.lines)
        self.assertFalse(self.rounds_file().exists())
        answer = self.revise_answer(edition(2), revise("A later plan, a later remark."))
        self.assertEqual(answer.splitlines()[0], "Plan verification: REVISE (round 1 of 3)")


class TheCounterIsNeverAVerdictTests(PlanReviseTestCase):
    """A broken or foreign counter neither crashes preflight nor lets a plan by."""

    def foreign_record(self) -> str:
        """A real record, written by preflight for another project and this plan."""

        other = project()
        self.revise_answer(edition(0), revise("Another project's remark."), root=other)
        return self.rounds_file(other).read_text(encoding="utf-8")

    def counters(self) -> dict[str, str]:
        valid = json.loads(self.foreign_record())
        # Each record below is right for this project and this plan but in
        # one thing, so only the one check meant for it can stop it.
        here = {**valid, "project": str(self.root.resolve())}
        forged_pass = copy.deepcopy(here)
        forged_pass["rounds"][0]["verdict"] = "PASS"
        return {
            "not json": "{rounds: [",
            "not utf-8": "\udcff",
            "a list": "[1, 2, 3]",
            "another kind": json.dumps({**here, "kind": "something-else"}),
            "another schema": json.dumps({**here, "schema_version": 2}),
            "another project": json.dumps(valid),
            "a forged PASS for this plan": json.dumps(forged_pass),
        }

    def plant(self, content: str) -> None:
        path = self.rounds_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8", "surrogateescape"))

    def test_a_broken_or_foreign_counter_is_ignored_and_a_verifier_still_judges(self) -> None:
        """Mutations: the project check dropped (the other project's REVISE
        is replayed, no verifier); the kind or the schema check dropped (a
        record of another kind is replayed); the REVISE-only check dropped
        (the forged entry is replayed); the JSON error not caught (a crash)."""

        for name, content in self.counters().items():
            with self.subTest(counter=name):
                ScriptedVerifier.verifier_turns = 0
                self.plant(content)
                answer = self.revise_answer(edition(0), revise("This project's remark."))
                self.assertEqual(ScriptedVerifier.verifier_turns, 1, "no verifier judged the plan")
                self.assertEqual(answer.splitlines()[0], "Plan verification: REVISE (round 1 of 3)")
                self.assertIn("This project's remark.", answer)
                self.assertTrue(
                    any(line.startswith("Plan verification rounds: WARN") for line in self.lines),
                    self.lines,
                )
                (only,) = self.recorded()
                self.assertEqual(only["issues"][0]["summary"], "This project's remark.")

    def test_a_broken_counter_never_stands_in_for_a_pass(self) -> None:
        """PASS is still the verifier's: with a forged record the start
        passes only through a fresh PASS, and the record is cleared."""

        for name, content in self.counters().items():
            with self.subTest(counter=name):
                ScriptedVerifier.verifier_turns = 0
                self.plant(content)
                result = self.preflight(edition(0), PASS)
                self.assertEqual(ScriptedVerifier.verifier_turns, 1)
                self.assertEqual(result.plan_verification.verdict, "PASS")
                self.assertFalse(self.rounds_file().exists())

    def test_a_directory_in_place_of_the_counter_is_a_clean_refusal(self) -> None:
        """Not a file at all: nothing can be counted, so the cap cannot hold.
        Preflight refuses with its own error, never a traceback."""

        self.rounds_file().mkdir(parents=True)
        with self.assertRaises(PreflightError) as caught:
            self.preflight(edition(0), revise("Remark one."))
        self.assertNotIsInstance(caught.exception, PlanRevisionRequired)
        self.assertIn("the round could not be recorded", str(caught.exception))
        self.assertEqual(ScriptedVerifier.verifier_turns, 1)


class TheSkillsReadTheMarkerTests(unittest.TestCase):
    """Both skills: REVISE is the planner's to fix, every other exit stays final.

    Mutation: the REVISE paragraph removed from either SKILL.md.
    """

    def test_both_skills_send_revise_back_to_the_planner(self) -> None:
        for skill in (SKILL, HOST_SKILL):
            with self.subTest(skill=skill.parent.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("Every other non-zero exit is a finished answer", text)
                self.assertNotIn("A non-zero exit is a finished answer", text)
                self.assertIn(f"Exit code {PLAN_REVISION_EXIT_CODE}", text)
                self.assertIn("`Plan verification: REVISE (round <k> of 3)`", text)
                for duty in (
                    "run the same `start-skill` command again",
                    "do not argue with the verifier",
                    "never drop, weaken or narrow a requirement",
                    "an unchanged plan returns the same answer",
                ):
                    self.assertIn(duty, text)


if __name__ == "__main__":
    unittest.main()
