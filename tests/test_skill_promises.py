"""The skill may not promise what the runtime does not do.

Two promises were costly. First: the report section told the model to
keep the turn open and stream the ladder until the task started - while
the dispatcher waits for exactly that turn to complete, and the launch
never came. Second: after the fix the same place kept "the [✓] lines the
user already sees come from there", though the hook moved to continue
and its message is not shown.

The third promise was the DevOps path: seven paragraphs on how DevOps
repairs and restarts, with no code at all that creates the engineer.
"""

from __future__ import annotations

from pathlib import Path
import unittest

SKILL = (
    Path(__file__).resolve().parents[1]
    / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md"
)


class LaunchReportPromiseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL.read_text(encoding="utf-8")

    def test_the_skill_does_not_claim_the_launch_report_is_visible(self) -> None:
        self.assertNotIn("the user already sees", self.text)
        self.assertIn("not visible", self.text)

    def test_the_skill_names_the_way_to_look(self) -> None:
        """An invisible report is allowed, silence about it is not."""

        self.assertIn("статус", self.text)

    def test_the_skill_forbids_polling_inside_the_initiating_turn(self) -> None:
        self.assertIn("never run it inside the initiating turn", self.text)


class PipelineEngineerPromiseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SKILL.read_text(encoding="utf-8")

    def test_the_skill_says_the_engineer_is_created_as_a_worker(self) -> None:
        """Before, the skill said honestly that nobody creates the
        worker.

        Now something does - and the promise has to match the runtime
        again, this time in the other direction.
        """

        self.assertIn("creates it as a visible worker task", self.text)
        self.assertNotIn("Nothing creates an engineer worker", self.text)

    def test_the_skill_states_the_engineer_repair_authority(self) -> None:
        """R13: the user takes no part in choosing how the fix is made."""

        self.assertIn("full authority to repair", self.text)
        self.assertIn("The user does not choose the repair", self.text)

    def test_the_skill_requires_a_code_for_escalation(self) -> None:
        self.assertIn("RECOVERY_EXHAUSTED", self.text)
        self.assertIn("A bare escalation is refused", self.text)

    def test_the_procedure_names_real_commands(self) -> None:
        for command in ("relay-status", "relay-complete", "relay-fail",
                        "devops-rearm-relay-owner"):
            with self.subTest(command=command):
                self.assertIn(command, self.text)

    def test_the_procedure_keeps_ambiguous_outcomes_stopped(self) -> None:
        """An unknown side effect is the one case where standing still
        is right. Replacing the task here would split the work in two."""

        self.assertIn("AMBIGUOUS", self.text)
        self.assertIn("never guess", self.text)


class CommandsInTheSkillExistTests(unittest.TestCase):
    """A procedure is useless if it names a command that does not exist."""

    def test_every_named_helper_command_is_a_real_subcommand(self) -> None:
        import re

        from codex_autopilot.cli import parser

        available = set()
        for action in parser()._subparsers._group_actions:
            available.update(action.choices)
        named = set(re.findall(r"scripts/codex-autopilot (\S+)", SKILL.read_text(encoding="utf-8")))
        named |= {
            match
            for match in re.findall(r"`(relay-\w+|devops-[\w-]+)", SKILL.read_text(encoding="utf-8"))
        }
        missing = sorted(named - available)
        self.assertEqual(missing, [], f"the skill names commands that do not exist: {missing}")


if __name__ == "__main__":
    unittest.main()


class EntrypointDefaultsTests(unittest.TestCase):
    """M11-ENTRYPOINT-DEFAULTS: the skill template is the actual default
    of a run.

    plan.py declares auto and two workers as the schema-3 default, but
    the plan is written by the planner, from the sample in SKILL.md, not
    by plan.py. The sample carried execution_strategy="serial" and
    max_parallel_workers=1, so every new run entered serial explicitly
    and never reached the default. That is stronger than a default: an
    explicit value in the file cannot be overridden.
    """

    SKILLS = (
        Path(__file__).resolve().parents[1]
        / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md",
        Path(__file__).resolve().parents[1]
        / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md",
    )

    def test_both_templates_emit_the_declared_v09_defaults(self) -> None:
        from codex_autopilot.plan import (
            DEFAULT_EXECUTION_STRATEGY,
            DEFAULT_MAX_PARALLEL_WORKERS,
        )

        expected = (
            f'"execution_strategy":"{DEFAULT_EXECUTION_STRATEGY}",'
            f'"max_parallel_workers":{DEFAULT_MAX_PARALLEL_WORKERS}'
        )
        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIn(expected, text)
                self.assertNotIn('"execution_strategy":"serial"', text)

    def test_both_templates_explain_that_siblings_are_the_parallelism(self) -> None:
        """The auto default gives nothing to a graph built as a chain."""

        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("declared as siblings", text)
                self.assertIn("legacy_serial", text)

    def test_both_templates_warn_that_a_shared_write_serializes_siblings(self) -> None:
        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                self.assertIn(
                    "the resource lock",
                    skill.read_text(encoding="utf-8"),
                )


class RoleNameLanguageTests(unittest.TestCase):
    """A role is a profession, and professions across this environment
    are named in English.

    The language rule said to write everything in the run language
    except protocol identifiers, and the planner dutifully translated
    role names. But the branch title format appends the English Verifier
    and Verify - what came out was "Инженер основания Verifier | M1 |
    Verify ...", half and half. This is not taste: the mixed title is
    produced by the code itself, not by a person.
    """

    SKILLS = (
        Path(__file__).resolve().parents[1]
        / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md",
        Path(__file__).resolve().parents[1]
        / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md",
    )

    def test_both_skills_exempt_role_names_from_the_run_language(self) -> None:
        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("stay in English always", text)
                self.assertIn("Resilience Engineer", text)

    def test_both_skills_say_why_rather_than_only_what(self) -> None:
        """A rule without its reason is the one the planner bends at the
        first conflict."""

        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = skill.read_text(encoding="utf-8")
                self.assertIn("`Verifier` and `Verify`", text)
                self.assertIn("half-translated title", text)

    def test_the_title_format_really_appends_english_words(self) -> None:
        """The reason behind the rule is checked, not taken on trust."""

        from codex_autopilot.thread_titles import verifier_thread_title

        title = verifier_thread_title("M1", "Create the foundation", role_name="Foundation Engineer")
        self.assertIn("Verifier", title)
        self.assertIn("Verify", title)


def _flat(path: Path) -> str:
    """Text without line breaks: the rule is checked by meaning, not by
    layout."""

    return " ".join(path.read_text(encoding="utf-8").split())


class LiveVerificationRuleTests(unittest.TestCase):
    """The DoD has to require at least one real check.

    Measured on codex-thread-tools: 28 green tests on a fake transport,
    an independent verifier accepted the work, and the projects command
    fell over on the first live call -
    "project/list requires experimentalApi capability". The capability
    was not announced in the handshake. The fake could not catch that,
    and neither could the verifier - the contract did not require it.
    """

    SKILLS = (
        Path(__file__).resolve().parents[1]
        / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md",
        Path(__file__).resolve().parents[1]
        / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md",
    )

    def test_both_skills_require_a_live_item(self) -> None:
        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = _flat(skill)
                self.assertIn("verified against that real", text)
                self.assertIn("not only against a double", text)

    def test_both_skills_keep_the_measured_reason(self) -> None:
        """A rule without the case that produced it is the first one to
        be bent."""

        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = _flat(skill)
                self.assertIn("requires experimentalApi capability", text)

    def test_both_skills_bound_the_live_item(self) -> None:
        """A "check it live" rule without bounds is permission to break
        other people's systems."""

        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = _flat(skill)
                self.assertIn("read-only whenever", text)
                self.assertIn("never a destructive call", text)

    def test_both_skills_name_the_honest_way_out(self) -> None:
        """An unreachable system is a named gap, not a second fake."""

        for skill in self.SKILLS:
            with self.subTest(skill=skill.name):
                text = _flat(skill)
                self.assertIn("explicit gap", text)
