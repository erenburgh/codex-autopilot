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
    """Текст инструкции - исходник функции, а не собранный промпт.

    Собирать промпт значило бы поднимать состояние прогона; здесь важна
    сама инструкция, а она лежит в шаблоне.
    """

    return inspect.getsource(AIStudioRuntime.build_pipeline_engineer_prompt)


def subcommands() -> dict[str, argparse.ArgumentParser]:
    root = parser()
    action = next(
        item for item in root._actions if isinstance(item, argparse._SubParsersAction)
    )
    return dict(action.choices)


def invocations(text: str, name: str) -> list[str]:
    """Сами вызовы команды, а не строки с упоминанием.

    Строка рантбука состоит из вызова в обратных кавычках и пояснения
    после тире, и пояснение часто называет тот же флаг прозой. Проверять
    надо вызов: инженер копирует его, а не объяснение. Имя команды
    берётся целиком - "arm" входит в "devops-rearm-relay-owner".
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
            "инструкция перестала называть команды восстановления - проверять нечего",
        )

    def test_the_check_would_catch_the_drift_that_happened(self) -> None:
        """Мутация наоборот: убираем флаг из строки - проверка обязана упасть."""

        text = runbook_text().replace("--failure-code <kind> ", "")
        command = subcommands()["relay-fail"]
        call = invocations(text, "relay-fail")[0]
        missing = [flag for flag in required_flags(command) if flag not in call]
        self.assertEqual(
            missing,
            ["--failure-code"],
            "проверка не заметила бы того самого расхождения",
        )


if __name__ == "__main__":
    unittest.main()
