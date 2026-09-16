from __future__ import annotations

from pathlib import Path
import re
from typing import Protocol


class VerificationCheckLike(Protocol):
    """Structural input needed to recognize a canonical suite command."""

    kind: str
    argv: tuple[str, ...]
    expected_exit_code: int


CLEAN_IDENTITY_ASSIGNMENTS = {
    "CODEX_THREAD_ID": "",
    "CODEX_TURN_ID": "",
    "CODEX_SESSION_ID": "",
}
_SHELL_COMMANDS = frozenset(
    {
        "bash",
        "cmd",
        "cmd.exe",
        "dash",
        "fish",
        "ksh",
        "powershell",
        "pwsh",
        "sh",
        "zsh",
    }
)
_NOOP_COMMANDS = frozenset({":", "echo", "false", "printf", "true"})
_DIRECT_TEST_RUNNERS = frozenset(
    {
        "cargo",
        "dotnet",
        "go",
        "gradle",
        "gradlew",
        "make",
        "mvn",
        "mvnw",
        "py.test",
        "pytest",
    }
)
_PACKAGE_TEST_RUNNERS = frozenset({"bun", "npm", "pnpm", "yarn"})
_NO_TEST_EXECUTION_OPTIONS = frozenset(
    {
        "--collect-only",
        "--dry-run",
        "--fixtures",
        "--fixtures-per-test",
        "--help",
        "--ignore-scripts",
        "--just-print",
        "--list",
        "--list-tests",
        "--listTests",
        "--no-run",
        "--question",
        "--recon",
        "--setup-only",
        "--setup-plan",
        "--touch",
        "--version",
        "-V",
        "-h",
    }
)
_PYTEST_PARTIAL_SUITE_OPTIONS = frozenset(
    {
        "--co",
        "--deselect",
        "--failed-first",
        "--ff",
        "--ignore",
        "--ignore-glob",
        "--last-failed",
        "--lf",
        "--m",
        "--new-first",
        "--nf",
        "--pyargs",
        "--stepwise",
        "--sw",
        "-k",
        "-m",
    }
) | _NO_TEST_EXECUTION_OPTIONS


def is_clean_suite_command(check: VerificationCheckLike) -> bool:
    """Recognize a direct successful suite command in a clean identity env."""

    if (
        check.kind != "command"
        or check.expected_exit_code != 0
        or len(check.argv) < 2
        or check.argv[0] not in {"env", "/usr/bin/env"}
    ):
        return False

    assignments: dict[str, str] = {}
    command_index = 1
    while command_index < len(check.argv):
        token = check.argv[command_index]
        if "=" not in token:
            break
        name, value = token.split("=", 1)
        if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return False
        assignments[name] = value
        command_index += 1
    if command_index >= len(check.argv):
        return False
    if not all(
        assignments.get(name) == value
        for name, value in CLEAN_IDENTITY_ASSIGNMENTS.items()
    ):
        return False

    command = check.argv[command_index:]
    executable = Path(command[0]).name.lower()
    if executable in _SHELL_COMMANDS or executable in _NOOP_COMMANDS:
        return False
    # Validation cannot prove semantic completeness (R25), but an inline
    # expression is visibly not a stable repository-wide runner.
    if len(command) > 1 and command[1] in {"-c", "-e", "--eval"}:
        return False
    return _invokes_full_test_suite(command)


def _invokes_full_test_suite(command: tuple[str, ...]) -> bool:
    """Recognize direct repository-suite runners, not arbitrary commands."""

    executable = Path(command[0]).name.lower()
    args = command[1:]
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable):
        if len(args) >= 3 and args[:3] == ("-m", "unittest", "discover"):
            return _unittest_discovers_test_root(args[3:])
        if len(args) >= 2 and args[:2] == ("-m", "pytest"):
            return _pytest_covers_test_root(args[2:])
        # Произвольный скрипт полным набором не засчитывается. Прежде
        # хватало слова "suite" в имени файла: хелпер с одним тестом
        # проходил как весь репозиторий. Имя - не доказательство, а
        # намерение; выполнение доказывает исход, но лишь тогда, когда
        # объявлен настоящий запускальщик (R25). Полный прогон
        # объявляется вызовом запускальщика, а не названием файла.
        return False
    if executable in {"pytest", "py.test"}:
        return _pytest_covers_test_root(args)
    if executable in _PACKAGE_TEST_RUNNERS:
        if not args:
            return False
        if args[0] == "test":
            return not _runner_args_select_subset(args[1:])
        if len(args) >= 2 and args[0] == "run" and args[1] == "test":
            return not _runner_args_select_subset(args[2:])
        return False
    if executable == "go":
        return _go_covers_module(args)
    if executable == "make" and "-q" in args:
        return False
    if executable in _DIRECT_TEST_RUNNERS:
        if not args or args[0] != "test":
            return False
        return not _runner_args_select_subset(args[1:])
    # An executable name is a claim, not a contract.  A project-controlled
    # no-op called ``full-test-suite`` used to pass this gate solely because
    # its filename contained the right words.  Unknown runners have no
    # syntax the validator can inspect for whole-suite versus filtered work,
    # so they fail closed until the runtime has a typed runner contract.
    return False


_RUNNER_SUBSET_OPTIONS = frozenset(
    {
        "--bin",
        "--bins",
        "--changed",
        "--benches",
        "--doc",
        "--example",
        "--examples",
        "--exclude",
        "--exclude-task",
        "--filter",
        "--findRelatedTests",
        "--grep",
        "--lastFailed",
        "--lib",
        "--onlyChanged",
        "--only",
        "--ignored",
        "--package",
        "--related",
        "--run",
        "--shard",
        "--short",
        "--skip",
        "--spec",
        "--test",
        "--test-name-pattern",
        "--testFile",
        "--testNamePattern",
        "--testPathPattern",
        "--testPathPatterns",
        "--tests",
        "-Dit.test",
        "-Dtest",
        "-list",
        "-n",
        "-run",
        "-short",
        "-skip",
        "-x",
    }
) | _NO_TEST_EXECUTION_OPTIONS
_RUNNER_SUBSET_SHORT_OPTIONS = ("-g", "-k", "-m", "-p", "-t")


def _runner_args_select_subset(args: tuple[str, ...]) -> bool:
    for arg in args:
        if arg == "--":
            continue
        if arg.startswith("-"):
            lowered = arg.casefold()
            if lowered.startswith(("-dskiptests", "-dmaven.test.skip")):
                return True
            if arg.split("=", 1)[0] in _RUNNER_SUBSET_OPTIONS:
                return True
            if any(
                _attached_short_option(arg, option)
                for option in _RUNNER_SUBSET_SHORT_OPTIONS
            ):
                return True
            continue
        if "=" in arg and not arg.startswith("="):
            continue
        return True
    return False


def _go_covers_module(args: tuple[str, ...]) -> bool:
    """Go needs the explicit recursive package pattern for a module-wide run."""

    if not args or args[0] != "test":
        return False
    runner_args = args[1:]
    if runner_args.count("./...") != 1:
        return False
    remaining = tuple(arg for arg in runner_args if arg != "./...")
    return not _runner_args_select_subset(remaining)


def _unittest_discovers_test_root(args: tuple[str, ...]) -> bool:
    if any(_unittest_option_selects_subset(arg) for arg in args):
        return False
    for flag in ("-p", "--pattern"):
        if flag in args:
            try:
                if args[args.index(flag) + 1] != "test*.py":
                    return False
            except IndexError:
                return False
    for arg in args:
        if arg.startswith("-p=") or arg.startswith("--pattern="):
            if arg.split("=", 1)[1] != "test*.py":
                return False
        if _attached_short_option(arg, "-p") and arg[2:] != "test*.py":
            return False
    for flag in ("-s", "--start-directory"):
        try:
            root = args[args.index(flag) + 1]
        except (ValueError, IndexError):
            continue
        if Path(root).name.lower() in {"test", "tests"}:
            return True
    return False


def _pytest_covers_test_root(args: tuple[str, ...]) -> bool:
    if any(_pytest_option_selects_subset(arg) or "::" in arg for arg in args):
        return False
    positional = [arg for arg in args if not arg.startswith("-")]
    return not positional or all(
        Path(arg).name.lower() in {"test", "tests"} for arg in positional
    )


def _pytest_option_selects_subset(arg: str) -> bool:
    name = arg.split("=", 1)[0]
    return name in _PYTEST_PARTIAL_SUITE_OPTIONS or any(
        _attached_short_option(arg, option) for option in ("-k", "-m")
    )


def _unittest_option_selects_subset(arg: str) -> bool:
    return (
        arg.split("=", 1)[0] in _NO_TEST_EXECUTION_OPTIONS
        or _attached_short_option(arg, "-k")
        or arg == "--test-name-patterns"
        or arg.startswith("--test-name-patterns=")
    )


def _attached_short_option(arg: str, option: str) -> bool:
    return arg == option or (
        not arg.startswith("--")
        and len(arg) > len(option)
        and arg.startswith(option)
    )
