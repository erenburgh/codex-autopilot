"""Инструкция дежурного инженера не расходится с самим CLI.

Так уже ломалось. `--failure-code` сделали обязательным у `relay-fail`,
а строку в рантбуке не поправили: инженер выполнил бы её дословно и
получил бы отказ argparse с кодом 2 - ход сгорает, тикет остаётся, а
прогон стоит. Проверка ловит не тот случай, а класс: любой обязательный
флаг любой команды, названной в рантбуке, обязан быть в её строке.
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
