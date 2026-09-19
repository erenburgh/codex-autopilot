"""The on-call engineer's instructions do not diverge from the CLI itself.

This has broken before. `--failure-code` was made mandatory on
`relay-fail`, and the runbook line was not updated: the engineer would
have run it verbatim and got an argparse refusal with exit code 2 - the
turn burns, the ticket remains, the run stands. The check catches not
that case but the class: any mandatory flag of any command named in the
runbook must be in its line.
"""

from __future__ import annotations

import argparse
import inspect
import re
import unittest

from codex_autopilot.ai_studio import AIStudioRuntime
from codex_autopilot.cli import parser


def runbook_text() -> str:
    """The instruction text is the function source, not a built prompt.

    Building the prompt would mean bringing up run state; what matters
    here is the instruction itself, and it lies in the template.
    """

    return inspect.getsource(AIStudioRuntime.build_pipeline_engineer_prompt)


def subcommands() -> dict[str, argparse.ArgumentParser]:
    root = parser()
    action = next(
        item for item in root._actions if isinstance(item, argparse._SubParsersAction)
    )
    return dict(action.choices)


def invocations(text: str, name: str) -> list[str]:
    """The calls of the command themselves, not lines that mention it.

    A runbook line is a call in backticks plus an explanation after a
    dash, and the explanation often names the same flag in prose. What
    must be checked is the call: the engineer copies that, not the
    explanation. The command name is taken whole - "arm" is inside
    "devops-rearm-relay-owner".
    """

    pattern = re.compile(
        r"`[^`\n]*codex-autopilot " + re.escape(name) + r"(?![\w-])[^`\n]*`"
    )
    return pattern.findall(text)


def required_flags(command: argparse.ArgumentParser) -> list[str]:
    return [
        item.option_strings[0]
        for item in command._actions
        if item.required and item.option_strings
    ]


class RunbookMatchesTheCliTests(unittest.TestCase):
    def test_every_named_command_carries_its_required_flags(self) -> None:
        text = runbook_text()
        checked = 0
        for name, command in subcommands().items():
            calls = invocations(text, name)
            if not calls:
                continue
            checked += 1
            for flag in required_flags(command):
                self.assertTrue(
                    any(flag in call for call in calls),
                    f"рантбук зовёт {name} без обязательного {flag}: "
                    "инженер выполнит строку дословно и получит отказ argparse",
                )
        self.assertGreaterEqual(
            checked,
            6,
            "the instruction no longer names the recovery commands -"
            " there is nothing to check",
        )

    def test_the_check_would_catch_the_drift_that_happened(self) -> None:
        """The mutation in reverse: drop the flag from the line and the
        check must fail."""

        text = runbook_text().replace("--failure-code <kind> ", "")
        command = subcommands()["relay-fail"]
        call = invocations(text, "relay-fail")[0]
        missing = [flag for flag in required_flags(command) if flag not in call]
        self.assertEqual(
            missing,
            ["--failure-code"],
            "the check would not have noticed that very drift",
        )


if __name__ == "__main__":
    unittest.main()
