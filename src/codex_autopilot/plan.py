from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable

from .department_acceptance import (
    DepartmentAcceptanceError,
    DepartmentContract,
    department_contract_from_raw,
    task_department_binding,
    resolve_task_department,
    validate_department_contracts,
)
from .models import EXECUTION_MODES, STRATEGIES
from .reasoning import normalize


PLAN_FILE = "plan.json"
PLAN_SCHEMA_VERSION = 3

EXECUTION_STRATEGIES = {"serial", "parallel", "auto"}
VERIFICATION_POLICIES = {"self", "deterministic", "independent", "auto"}
VERIFICATION_CHECK_KINDS = {"command", "artifact", "evidence"}
_CLEAN_IDENTITY_ASSIGNMENTS = {
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
_PYTEST_PARTIAL_SUITE_OPTIONS = frozenset(
    {
        "--co",
        "--collect-only",
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
)
RESOURCE_KINDS = {
    "path",
    "directory",
    "glob",
    "application",
    "environment",
    "browser",
    "device",
    "external_sandbox",
    "logical",
}
RESOURCE_ACCESS_MODES = {"read", "write", "exclusive"}

# Новый канонический schema-3 прогон по умолчанию допускает параллельное
# исполнение. Совместимость конфигов проектов, созданных до v0.9, хранится
# отдельно в COMPAT_* и не ослабляет контракт загружаемого плана.
DEFAULT_EXECUTION_STRATEGY = "auto"

# Значения для КОНФИГА БЕЗ секции [runtime], то есть для проекта,
# созданного до v0.9. Такой проект остаётся serial и одномерным явно,
# а не уезжает в параллельность из-за смены дефолта нового прогона.
COMPAT_EXECUTION_STRATEGY = "serial"
COMPAT_MAX_PARALLEL_WORKERS = 1
# Консервативный, но реально параллельный предел: два воркера дают
# настоящую параллельность при минимальном росте нагрузки и расхода.
# Решение пользователя от 14 сентября 2026. Двойка стояла здесь как
# умолчание и попала в шаблон плана, откуда планировщик копировал её не
# глядя: граф из 24 задач с четырьмя независимыми ветками исполнялся по
# две. Ограничение на Computer Use держится отдельным слотом и от этого
# числа не зависит.
DEFAULT_MAX_PARALLEL_WORKERS = 10
DEFAULT_COMPUTER_USE_SLOTS = 1
DEFAULT_MAX_MEMORY_RECORDS = 8
DEFAULT_MAX_DEPENDENCY_OUTPUTS = 8

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class RoleProfile:
    """Planner-defined specialist behavior; it never selects a model."""

    id: str
    name: str
    responsibilities: tuple[str, ...]
    domain_focus: tuple[str, ...] = ()
    preferred_tools: tuple[str, ...] = ()
    context_priorities: tuple[str, ...] = ()
    verification_expectations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    """A declarative check. Command checks use argv and never a shell string."""

    id: str
    kind: str
    description: str
    argv: tuple[str, ...] = ()
    path: str | None = None
    timeout_seconds: int = 300
    expected_exit_code: int = 0


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    policy: str
    required: bool
    deterministic_checks: tuple[VerificationCheck, ...] = ()
    verifier_role: str | None = None
    execution_mode: str | None = None
    execution_mode_reason: str | None = None
    reasoning: str | None = None
    max_revision_attempts: int = 2


@dataclass(frozen=True, slots=True)
class ResourceClaim:
    id: str
    kind: str
    target: str
    access: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class TaskOutput:
    id: str
    description: str
    path: str | None = None
    required: bool = True


@dataclass(frozen=True, slots=True)
class TaskContext:
    memory_queries: tuple[str, ...] = ()
    memory_record_ids: tuple[str, ...] = ()
    dependency_outputs: tuple[str, ...] = ()
    max_memory_records: int = DEFAULT_MAX_MEMORY_RECORDS
    max_dependency_outputs: int = DEFAULT_MAX_DEPENDENCY_OUTPUTS


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    title: str
    objective: str
    definition_of_done: tuple[str, ...]
    execution_mode: str
    execution_mode_reason: str
    reasoning: str | None
    role: str
    depends_on: tuple[str, ...]
    priority: int
    verification: VerificationPolicy
    resources: tuple[ResourceClaim, ...]
    required_capabilities: tuple[str, ...]
    context: TaskContext
    outputs: tuple[TaskOutput, ...]
    tags: tuple[str, ...]


# v0.8 callers use the old name. A milestone is now a graph task, not a second
# contract, so this alias is intentionally additive.
Milestone = Task


@dataclass(frozen=True, slots=True)
class Plan:
    goal: str
    user_request: str
    model_strategy: str
    tasks: tuple[Task, ...]
    roles: tuple[RoleProfile, ...]
    departments: tuple[DepartmentContract, ...] = ()
    graph_version: int = 1
    execution_strategy: str = DEFAULT_EXECUTION_STRATEGY
    max_parallel_workers: int = DEFAULT_MAX_PARALLEL_WORKERS
    computer_use_slots: int = DEFAULT_COMPUTER_USE_SLOTS

    @property
    def milestones(self) -> tuple[Task, ...]:
        """Compatibility alias for callers that use milestone terminology."""

        return self.tasks

    @property
    def task_map(self) -> dict[str, Task]:
        return {item.id: item for item in self.tasks}

    @property
    def role_map(self) -> dict[str, RoleProfile]:
        return {item.id: item for item in self.roles}


def validate_plan(data: dict[str, Any], profile: str) -> Plan:
    """Load a canonical schema-3 graph and enforce its acceptance floor.

    Older plan formats and caller-supplied ``compatibility`` claims fail closed.
    In particular, no persisted or replacement plan can use migration prose to
    bypass independent verification.
    """

    return _validate_plan_payload(data, profile)


def _validate_plan_payload(data: dict[str, Any], profile: str) -> Plan:
    if profile not in {"adaptive", "host-settings"}:
        raise ValueError("profile must be adaptive or host-settings")
    if not isinstance(data, dict):
        raise ValueError("plan must be an object")
    schema = data.get("schema_version")
    if schema != PLAN_SCHEMA_VERSION:
        raise ValueError(f"plan.schema_version must be {PLAN_SCHEMA_VERSION}")
    return _validate_graph_plan(
        data,
        profile,
    )


def validate_plan_change(current: Plan, data: dict[str, Any], profile: str) -> Plan:
    """Validate a complete replacement graph before any durable write."""

    # user_request переносится из текущего плана, а не берётся из ответа
    # реплэннера. Прежде требовалось дословное эхо, и промпт честно просил
    # "Дословно сохрани user_request" - но в живом прогоне это 35 234
    # символа. Модель, переписывающая граф, такую строку не воспроизводит,
    # и законная смена плана отклонялась целиком.
    #
    # Замерено на M11: ход реплэннера завершился успешно, результат отвергли
    # с "plan changes must not replace the original user request", прогон
    # встал, тикет открылся.
    #
    # Перенос строже прежней проверки: эхо можно было подделать, а поле,
    # которое не берётся из ответа, изменить нельзя вовсе. goal (542
    # символа) и model_strategy остаются строгими - их модель повторяет
    # надёжно, и расхождение там означает намерение, а не ошибку копии.
    data = dict(data)
    data["user_request"] = current.user_request
    if data.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ValueError("plan changes must use the canonical v0.9 schema")
    if data.get("compatibility") is not None:
        raise ValueError("plan.compatibility is not part of the canonical schema")
    candidate = _validate_plan_payload(data, profile)
    if candidate.graph_version != current.graph_version + 1:
        raise ValueError(
            "plan change graph_version must increment exactly once "
            f"({current.graph_version} -> {current.graph_version + 1})"
        )
    if candidate.goal != current.goal:
        raise ValueError("plan changes must not replace the run goal")
    if candidate.model_strategy != current.model_strategy:
        raise ValueError("plan changes must not replace model_strategy")
    # validate_plan already performs all role, output, dependency, and cycle
    # checks. Keeping this wrapper mandatory prevents a plan-change path from
    # accidentally treating initial-load validation as optional.
    return candidate


def load_plan(state_dir: Path, profile: str) -> Plan:
    data = json.loads((state_dir / PLAN_FILE).read_text(encoding="utf-8"))
    return validate_persisted_plan(data, profile, state_dir=state_dir)


def validate_persisted_plan(
    data: dict[str, Any],
    profile: str,
    *,
    state_dir: Path | None = None,
) -> Plan:
    """Проверить сохранённый план.

    Прежде здесь был отдельный путь для планов v0.8: их впускали, сверяя
    происхождение по файлам на диске. Файлы правятся руками, а впуск
    означал разрешённое самопринятие - задача принимала собственную
    работу, что запрещено правилом R8. Формат v0.8 снят целиком, и
    вместе с ним исчезла и эта дыра. state_dir остаётся в сигнатуре для
    совместимости вызовов и больше ни на что не влияет.
    """

    return validate_plan(data, profile)

def save_plan(state_dir: Path, plan: Plan) -> None:
    atomic_json(state_dir / PLAN_FILE, plan_to_dict(plan))


def plan_to_dict(plan: Plan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "graph_version": plan.graph_version,
        "goal": plan.goal,
        "user_request": plan.user_request,
        "model_strategy": plan.model_strategy,
        "execution_strategy": plan.execution_strategy,
        "max_parallel_workers": plan.max_parallel_workers,
        "computer_use_slots": plan.computer_use_slots,
        "roles": [_role_to_dict(item) for item in plan.roles],
        "tasks": [_task_to_dict(item) for item in plan.tasks],
    }
    if plan.departments:
        payload["departments"] = [item.to_dict() for item in plan.departments]
    return payload


def topological_order(plan: Plan) -> tuple[str, ...]:
    """Return a stable dependency order, using declaration order for ties."""

    rank = {task.id: index for index, task in enumerate(plan.tasks)}
    indegree = {task.id: len(task.depends_on) for task in plan.tasks}
    dependents: dict[str, list[str]] = {task.id: [] for task in plan.tasks}
    for task in plan.tasks:
        for dependency in task.depends_on:
            dependents[dependency].append(task.id)
    ready = sorted((task_id for task_id, count in indegree.items() if count == 0), key=rank.get)
    result: list[str] = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for dependent in sorted(dependents[current], key=rank.get):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=rank.get)
    if len(result) != len(plan.tasks):
        # This is defensive; all Plan instances created by validate_plan have
        # already passed the more descriptive DFS cycle validator.
        raise ValueError("plan task graph contains a cycle")
    return tuple(result)


def _validate_canonical_acceptance(plan: "Plan") -> None:
    """Enforce the acceptance floor for every new canonical task.

    R8/R29 make deterministic checks admission evidence for a fresh judge,
    never acceptance by themselves.  The argv gate can prove the identity
    environment is reset and that a direct command exists; executing the
    declared repository-wide check, not this lint, proves its outcome (R25).

    Исключений нет ни для кого. Прежде их имел мигрированный план v0.8:
    он выходил отсюда, ничего не проверив, и самопринятие проходило.
    Формат v0.8 снят целиком, вместе с ним снято и исключение.
    """

    for task in plan.tasks:
        verification = task.verification
        if verification.policy != "independent":
            raise ValueError(
                "R8/R29: canonical task "
                f"{task.id} verification.policy must be \"independent\"; "
                f"got {verification.policy!r}. Deterministic checks admit work "
                "to independent judgement and never replace it"
            )
        if not verification.required:
            raise ValueError(
                f"R8/R29: canonical task {task.id} verification.required must be true"
            )
        if verification.max_revision_attempts < 2:
            raise ValueError(
                "R29: canonical task "
                f"{task.id} verification.max_revision_attempts must be at least 2"
            )
        suite = next(
            (
                check
                for check in verification.deterministic_checks
                if _is_clean_suite_command(check)
            ),
            None,
        )
        if suite is None:
            names = ", ".join(f"{name}=" for name in _CLEAN_IDENTITY_ASSIGNMENTS)
            raise ValueError(
                "R29: canonical task "
                f"{task.id} must declare at least one full-suite deterministic "
                "check as a successful command argv launched through env, reset "
                f"{names}, and invoke the command directly"
            )


def _is_clean_suite_command(check: VerificationCheck) -> bool:
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
    clean_identity = all(
        assignments.get(name) == value
        for name, value in _CLEAN_IDENTITY_ASSIGNMENTS.items()
    )
    if not clean_identity:
        return False

    command = check.argv[command_index:]
    executable = Path(command[0]).name.lower()
    if executable in _SHELL_COMMANDS or executable in _NOOP_COMMANDS:
        return False
    # A suite command may legitimately be a project script, make target, or
    # language-specific runner.  Validation cannot prove its semantic
    # completeness (R25), but an inline interpreter expression is visibly not
    # a stable repository-wide runner and must not satisfy the declaration.
    if len(command) > 1 and command[1] in {"-c", "-e", "--eval"}:
        return False
    return _invokes_full_test_suite(command)


def _invokes_full_test_suite(command: tuple[str, ...]) -> bool:
    """Recognize direct repository-suite runners, not arbitrary commands.

    This remains a plan lint: execution supplies outcome evidence and the
    independent verifier judges whether the declared runner really covers the
    repository (R25/R29).  The lint nevertheless rejects argv that plainly
    cannot be a suite, such as ``uname`` renamed to check id ``suite``.
    """

    executable = Path(command[0]).name.lower()
    args = command[1:]
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", executable):
        if len(args) >= 3 and args[:3] == ("-m", "unittest", "discover"):
            return _unittest_discovers_test_root(args[3:])
        if len(args) >= 2 and args[:2] == ("-m", "pytest"):
            return _pytest_covers_test_root(args[2:])
        if args and not args[0].startswith("-"):
            stem = Path(args[0]).stem.lower()
            runner_args = args[1:]
            return "suite" in stem and not any(
                _unittest_option_selects_subset(arg)
                or _pytest_option_selects_subset(arg)
                for arg in runner_args
            )
        return False
    if executable in {"pytest", "py.test"}:
        return _pytest_covers_test_root(args)
    if executable in _PACKAGE_TEST_RUNNERS:
        return bool(args) and (
            args[0] == "test"
            or (len(args) >= 2 and args[0] == "run" and args[1] == "test")
        )
    if executable in _DIRECT_TEST_RUNNERS:
        return bool(args) and args[0] == "test"
    if "test" in executable and ("all" in executable or "suite" in executable):
        return True
    return False


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
        # Слитная форма argparse: `-ptest_plan*.py` - тот же фильтр, что и
        # `-p test_plan*.py`, но прежде она проверку не проходила и
        # «полным прогоном» засчитывался кусок набора. Проверка, которая
        # принимает подмножество за целое, не доказывает ничего.
        if _attached_short_option(arg, "-p"):
            if arg[2:] != "test*.py":
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
        _attached_short_option(arg, "-k")
        or arg == "--test-name-patterns"
        or arg.startswith("--test-name-patterns=")
    )


def _attached_short_option(arg: str, option: str) -> bool:
    """Match both ``-k value`` and argparse's compact ``-kvalue`` form."""

    return arg == option or (
        not arg.startswith("--")
        and len(arg) > len(option)
        and arg.startswith(option)
    )


# Единственный список допустимых полей плана. Он же называется модели в
# промпте реплэннера: иначе отказ "plan has unknown fields" не говорит,
# какие поля вообще существуют, и переделка идёт вслепую.
GRAPH_PLAN_FIELDS = frozenset(
    {
        "schema_version",
        "graph_version",
        "goal",
        "user_request",
        "model_strategy",
        "execution_strategy",
        "max_parallel_workers",
        "computer_use_slots",
        "roles",
        "departments",
        "tasks",
        "compatibility",
    }
)


def _validate_graph_plan(data: dict[str, Any], profile: str) -> Plan:
    _reject_unknown(data, set(GRAPH_PLAN_FIELDS), "plan")
    goal, user_request, strategy = _plan_header(
        data,
        profile,
        require_user_request=True,
    )
    graph_version = _positive_int(data.get("graph_version", 1), "plan.graph_version")
    execution_strategy = str(data.get("execution_strategy", DEFAULT_EXECUTION_STRATEGY)).strip()
    if execution_strategy not in EXECUTION_STRATEGIES:
        raise ValueError(f"plan.execution_strategy must be one of {sorted(EXECUTION_STRATEGIES)}")
    max_parallel_workers = _positive_int(
        data.get("max_parallel_workers", DEFAULT_MAX_PARALLEL_WORKERS),
        "plan.max_parallel_workers",
    )
    computer_use_slots = _positive_int(
        data.get("computer_use_slots", DEFAULT_COMPUTER_USE_SLOTS),
        "plan.computer_use_slots",
    )
    if data.get("compatibility") is not None:
        raise ValueError("plan.compatibility is not part of the canonical schema")

    raw_roles = data.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise ValueError("plan.roles must be a non-empty array")
    roles = tuple(_role_from_raw(raw, index) for index, raw in enumerate(raw_roles, 1))
    _validate_unique((role.id for role in roles), "role id")

    raw_departments = data.get("departments", [])
    if not isinstance(raw_departments, list):
        raise ValueError("plan.departments must be an array")
    try:
        departments = tuple(
            department_contract_from_raw(raw, f"department {index}")
            for index, raw in enumerate(raw_departments, 1)
        )
        validate_department_contracts(
            departments,
            role_ids=(role.id for role in roles),
        )
    except DepartmentAcceptanceError as exc:
        raise ValueError(str(exc)) from exc

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("plan.tasks must be a non-empty array")
    tasks: list[Task] = []
    for index, raw in enumerate(raw_tasks, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"task {index} must be an object")
        tasks.append(_task_from_raw(raw, profile, f"task {index}", canonical=True))
    _validate_unique((task.id for task in tasks), "task id")
    plan = Plan(
        goal=goal,
        user_request=user_request,
        model_strategy=strategy,
        tasks=tuple(tasks),
        roles=roles,
        departments=departments,
        graph_version=graph_version,
        execution_strategy=execution_strategy,
        max_parallel_workers=max_parallel_workers,
        computer_use_slots=computer_use_slots,
    )
    _validate_canonical_acceptance(plan)
    _validate_graph(plan)
    return plan


def _plan_header(
    data: dict[str, Any],
    profile: str,
    *,
    require_user_request: bool = False,
) -> tuple[str, str, str]:
    goal = _required_string(data.get("goal"), "plan.goal")
    user_request = _required_string(
        data.get("user_request") if require_user_request else data.get("user_request", goal),
        "plan.user_request",
    )
    strategy = str(data.get("model_strategy") or ("auto" if profile == "adaptive" else "host-settings"))
    if strategy not in STRATEGIES:
        raise ValueError(f"model_strategy must be one of {sorted(STRATEGIES)}")
    if profile == "adaptive" and strategy == "host-settings":
        raise ValueError("Adaptive profile requires auto, sol-only, or astra-only model_strategy")
    if profile == "host-settings" and strategy != "host-settings":
        raise ValueError("Host Settings profile requires model_strategy=host-settings")
    return goal, user_request, strategy


def _role_from_raw(raw: Any, index: int) -> RoleProfile:
    label = f"role {index}"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {
            "id",
            "name",
            "responsibilities",
            "domain_focus",
            "preferred_tools",
            "context_priorities",
            "verification_expectations",
        },
        label,
    )
    return RoleProfile(
        id=_identifier(raw.get("id"), f"{label}.id"),
        name=_required_string(raw.get("name"), f"{label}.name"),
        responsibilities=_nonempty_strings(raw.get("responsibilities"), f"{label}.responsibilities"),
        domain_focus=_strings(raw.get("domain_focus", []), f"{label}.domain_focus"),
        preferred_tools=_strings(raw.get("preferred_tools", []), f"{label}.preferred_tools"),
        context_priorities=_strings(raw.get("context_priorities", []), f"{label}.context_priorities"),
        verification_expectations=_strings(
            raw.get("verification_expectations", []),
            f"{label}.verification_expectations",
        ),
    )


def _task_from_raw(
    raw: dict[str, Any],
    profile: str,
    label: str,
    *,
    canonical: bool,
    task_id: str | None = None,
    role: str | None = None,
    depends_on: tuple[str, ...] | None = None,
) -> Task:
    if canonical:
        _reject_unknown(
            raw,
            {
                "id",
                "title",
                "objective",
                "definition_of_done",
                "execution_mode",
                "execution_mode_reason",
                "reasoning",
                "role",
                "depends_on",
                "priority",
                "verification",
                "resources",
                "required_capabilities",
                "context",
                "outputs",
                "tags",
            },
            label,
        )
    title = _required_string(raw.get("title"), f"{label}.title")
    objective = _required_string(raw.get("objective"), f"{label}.objective")
    done = _nonempty_strings(raw.get("definition_of_done"), f"{label}.definition_of_done")
    execution_mode = str(raw.get("execution_mode", "")).strip()
    if execution_mode not in EXECUTION_MODES:
        raise ValueError(f"{label}.execution_mode must be one of {sorted(EXECUTION_MODES)}")
    execution_mode_reason = _required_string(
        raw.get("execution_mode_reason"),
        f"{label}.execution_mode_reason",
    )
    reasoning = _reasoning(raw, profile, label)
    resolved_id = task_id or _identifier(raw.get("id"), f"{label}.id")
    resolved_role = role or _identifier(raw.get("role"), f"{label}.role")
    resolved_dependencies = depends_on
    if resolved_dependencies is None:
        resolved_dependencies = _identifiers(raw.get("depends_on", []), f"{label}.depends_on")
    priority = _bounded_int(raw.get("priority", 0), f"{label}.priority", -1_000_000, 1_000_000)
    verification = (
        _verification_from_raw(raw.get("verification"), profile, f"{label}.verification")
        if canonical
        else VerificationPolicy(policy="self", required=True, max_revision_attempts=0)
    )
    resources = tuple(
        _resource_from_raw(item, index, label)
        for index, item in enumerate(_array(raw.get("resources", []), f"{label}.resources"), 1)
    )
    _validate_unique((item.id for item in resources), f"{label} resource id")
    context = _context_from_raw(raw.get("context"), label) if canonical else TaskContext()
    outputs = tuple(
        _output_from_raw(item, index, label)
        for index, item in enumerate(_array(raw.get("outputs", []), f"{label}.outputs"), 1)
    )
    _validate_unique((item.id for item in outputs), f"{label} output id")
    return Task(
        id=resolved_id,
        title=title,
        objective=objective,
        definition_of_done=done,
        execution_mode=execution_mode,
        execution_mode_reason=execution_mode_reason,
        reasoning=reasoning,
        role=resolved_role,
        depends_on=resolved_dependencies,
        priority=priority,
        verification=verification,
        resources=resources,
        required_capabilities=_strings(
            raw.get("required_capabilities", []),
            f"{label}.required_capabilities",
        ),
        context=context,
        outputs=outputs,
        tags=_strings(raw.get("tags", []), f"{label}.tags"),
    )


def _verification_from_raw(raw: Any, profile: str, label: str) -> VerificationPolicy:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {
            "policy",
            "required",
            "deterministic_checks",
            "verifier_role",
            "execution_mode",
            "execution_mode_reason",
            "reasoning",
            "max_revision_attempts",
        },
        label,
    )
    policy = str(raw.get("policy", "")).strip()
    if policy not in VERIFICATION_POLICIES:
        raise ValueError(f"{label}.policy must be one of {sorted(VERIFICATION_POLICIES)}")
    if policy == "independent" and "max_revision_attempts" not in raw:
        raise ValueError(
            f"{label}.max_revision_attempts must be declared and be at least 2"
        )
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise ValueError(f"{label}.required must be a boolean")
    checks = tuple(
        _check_from_raw(item, index, label)
        for index, item in enumerate(
            _array(raw.get("deterministic_checks", []), f"{label}.deterministic_checks"),
            1,
        )
    )
    _validate_unique((item.id for item in checks), f"{label} check id")
    if policy == "deterministic" and required and not checks:
        raise ValueError(f"{label}.deterministic_checks is required for deterministic policy")
    verifier_role = raw.get("verifier_role")
    if verifier_role is not None:
        verifier_role = _identifier(verifier_role, f"{label}.verifier_role")
    execution_mode = raw.get("execution_mode")
    if execution_mode is not None:
        execution_mode = str(execution_mode).strip()
        if execution_mode not in EXECUTION_MODES:
            raise ValueError(f"{label}.execution_mode must be one of {sorted(EXECUTION_MODES)}")
    execution_mode_reason = raw.get("execution_mode_reason")
    if execution_mode_reason is not None:
        execution_mode_reason = _required_string(execution_mode_reason, f"{label}.execution_mode_reason")
    if bool(execution_mode) != bool(execution_mode_reason):
        raise ValueError(f"{label}.execution_mode and execution_mode_reason must be provided together")
    reasoning = None
    if "reasoning" in raw:
        if profile != "adaptive":
            raise ValueError("Host Settings verification policies must omit reasoning")
        reasoning = normalize(str(raw["reasoning"]))
    return VerificationPolicy(
        policy=policy,
        required=required,
        deterministic_checks=checks,
        verifier_role=verifier_role,
        execution_mode=execution_mode,
        execution_mode_reason=execution_mode_reason,
        reasoning=reasoning,
        max_revision_attempts=_bounded_int(
            raw.get("max_revision_attempts", 2),
            f"{label}.max_revision_attempts",
            0,
            100,
        ),
    )


def _check_from_raw(raw: Any, index: int, parent: str) -> VerificationCheck:
    label = f"{parent}.deterministic_checks[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {"id", "kind", "description", "argv", "path", "timeout_seconds", "expected_exit_code"},
        label,
    )
    kind = str(raw.get("kind", "")).strip()
    if kind not in VERIFICATION_CHECK_KINDS:
        raise ValueError(f"{label}.kind must be one of {sorted(VERIFICATION_CHECK_KINDS)}")
    argv = _strings(raw.get("argv", []), f"{label}.argv")
    path = raw.get("path")
    if path is not None:
        path = _required_string(path, f"{label}.path")
    if kind == "command" and not argv:
        raise ValueError(f"{label}.argv is required for command checks")
    if kind != "command" and argv:
        raise ValueError(f"{label}.argv is only valid for command checks")
    if kind == "artifact" and not path:
        raise ValueError(f"{label}.path is required for artifact checks")
    if kind != "artifact" and path:
        raise ValueError(f"{label}.path is only valid for artifact checks")
    return VerificationCheck(
        id=_identifier(raw.get("id"), f"{label}.id"),
        kind=kind,
        description=_required_string(raw.get("description"), f"{label}.description"),
        argv=argv,
        path=path,
        timeout_seconds=_bounded_int(raw.get("timeout_seconds", 300), f"{label}.timeout_seconds", 1, 86_400),
        expected_exit_code=_bounded_int(
            raw.get("expected_exit_code", 0),
            f"{label}.expected_exit_code",
            -255,
            255,
        ),
    )


def _resource_from_raw(raw: Any, index: int, parent: str) -> ResourceClaim:
    label = f"{parent}.resources[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(raw, {"id", "kind", "target", "access", "description"}, label)
    kind = str(raw.get("kind", "")).strip()
    if kind not in RESOURCE_KINDS:
        raise ValueError(f"{label}.kind must be one of {sorted(RESOURCE_KINDS)}")
    access = str(raw.get("access", "")).strip()
    if access not in RESOURCE_ACCESS_MODES:
        raise ValueError(f"{label}.access must be one of {sorted(RESOURCE_ACCESS_MODES)}")
    description = raw.get("description")
    if description is not None:
        description = _required_string(description, f"{label}.description")
    return ResourceClaim(
        id=_identifier(raw.get("id"), f"{label}.id"),
        kind=kind,
        target=_required_string(raw.get("target"), f"{label}.target"),
        access=access,
        description=description,
    )


def _output_from_raw(raw: Any, index: int, parent: str) -> TaskOutput:
    label = f"{parent}.outputs[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(raw, {"id", "description", "path", "required"}, label)
    path = raw.get("path")
    if path is not None:
        path = _required_string(path, f"{label}.path")
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise ValueError(f"{label}.required must be a boolean")
    return TaskOutput(
        id=_identifier(raw.get("id"), f"{label}.id"),
        description=_required_string(raw.get("description"), f"{label}.description"),
        path=path,
        required=required,
    )


def _context_from_raw(raw: Any, parent: str) -> TaskContext:
    label = f"{parent}.context"
    if raw is None:
        return TaskContext()
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown(
        raw,
        {
            "memory_queries",
            "memory_record_ids",
            "dependency_outputs",
            "max_memory_records",
            "max_dependency_outputs",
        },
        label,
    )
    return TaskContext(
        memory_queries=_strings(raw.get("memory_queries", []), f"{label}.memory_queries"),
        memory_record_ids=_strings(raw.get("memory_record_ids", []), f"{label}.memory_record_ids"),
        dependency_outputs=_identifiers(raw.get("dependency_outputs", []), f"{label}.dependency_outputs"),
        max_memory_records=_bounded_int(
            raw.get("max_memory_records", DEFAULT_MAX_MEMORY_RECORDS),
            f"{label}.max_memory_records",
            0,
            100,
        ),
        max_dependency_outputs=_bounded_int(
            raw.get("max_dependency_outputs", DEFAULT_MAX_DEPENDENCY_OUTPUTS),
            f"{label}.max_dependency_outputs",
            0,
            100,
        ),
    )


def _reasoning(raw: dict[str, Any], profile: str, label: str) -> str | None:
    if profile == "adaptive":
        if raw.get("reasoning") is None:
            raise ValueError(f"{label} requires reasoning in Adaptive profile")
        return normalize(str(raw["reasoning"]))
    if "reasoning" in raw:
        raise ValueError("Host Settings plans must omit task reasoning")
    return None


def _validate_graph(plan: Plan) -> None:
    task_map = plan.task_map
    role_ids = set(plan.role_map)
    try:
        validate_department_contracts(plan.departments, role_ids=role_ids)
    except DepartmentAcceptanceError as exc:
        raise ValueError(str(exc)) from exc
    for task in plan.tasks:
        if task.role not in role_ids:
            raise ValueError(f"task {task.id} references unknown role {task.role!r}")
        role = plan.role_map[task.role]
        if role.id == "legacy-worker" or role.name.casefold() == "legacy serial worker":
            raise ValueError(
                f"task {task.id} requires a concrete RoleProfile, not generic legacy-worker"
            )
        if task.verification.verifier_role and task.verification.verifier_role not in role_ids:
            raise ValueError(
                f"task {task.id} verification references unknown role "
                f"{task.verification.verifier_role!r}"
            )
        if task.verification.verifier_role:
            verifier_role = plan.role_map[task.verification.verifier_role]
            if (
                verifier_role.id == "legacy-worker"
                or verifier_role.name.casefold() == "legacy serial worker"
            ):
                raise ValueError(
                    f"task {task.id} verifier requires a concrete RoleProfile, "
                    "not generic legacy-worker"
                )
        try:
            department_binding = task_department_binding(task)
        except DepartmentAcceptanceError as exc:
            raise ValueError(f"task {task.id}: {exc}") from exc
        if department_binding is not None:
            try:
                department = resolve_task_department(
                    plan.departments,
                    task,
                    role_names={item.id: item.name for item in plan.roles},
                )
            except DepartmentAcceptanceError as exc:
                raise ValueError(f"task {task.id}: {exc}") from exc
            if department is None:
                raise ValueError(f"task {task.id}: department binding disappeared")
            if not task.context.dependency_outputs:
                raise ValueError(
                    f"task {task.id} department acceptance requires a selected "
                    "dependency output carrying the pinned rubric reference"
                )
        for dependency in task.depends_on:
            if dependency == task.id:
                raise ValueError(f"task {task.id} cannot depend on itself")
            if dependency not in task_map:
                raise ValueError(f"task {task.id} references unknown dependency {dependency!r}")
        invalid_outputs = set(task.context.dependency_outputs) - set(task.depends_on)
        if invalid_outputs:
            raise ValueError(
                f"task {task.id} context.dependency_outputs must be direct dependencies; "
                f"invalid={sorted(invalid_outputs)}"
            )
    _validate_cycles(plan.tasks)


def _validate_cycles(tasks: tuple[Task, ...]) -> None:
    dependencies = {task.id: task.depends_on for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            start = stack.index(task_id)
            cycle = stack[start:] + [task_id]
            raise ValueError(f"plan task graph contains a cycle: {' -> '.join(cycle)}")
        visiting.add(task_id)
        stack.append(task_id)
        for dependency in dependencies[task_id]:
            visit(dependency)
        stack.pop()
        visiting.remove(task_id)
        visited.add(task_id)

    for task in tasks:
        visit(task.id)


def _role_to_dict(role: RoleProfile) -> dict[str, Any]:
    return {
        "id": role.id,
        "name": role.name,
        "responsibilities": list(role.responsibilities),
        **({"domain_focus": list(role.domain_focus)} if role.domain_focus else {}),
        **({"preferred_tools": list(role.preferred_tools)} if role.preferred_tools else {}),
        **({"context_priorities": list(role.context_priorities)} if role.context_priorities else {}),
        **(
            {"verification_expectations": list(role.verification_expectations)}
            if role.verification_expectations
            else {}
        ),
    }


def _task_to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "objective": task.objective,
        "definition_of_done": list(task.definition_of_done),
        "execution_mode": task.execution_mode,
        "execution_mode_reason": task.execution_mode_reason,
        **({"reasoning": task.reasoning} if task.reasoning else {}),
        "role": task.role,
        "depends_on": list(task.depends_on),
        "priority": task.priority,
        "verification": _verification_to_dict(task.verification),
        "resources": [_resource_to_dict(item) for item in task.resources],
        "required_capabilities": list(task.required_capabilities),
        "context": _context_to_dict(task.context),
        "outputs": [_output_to_dict(item) for item in task.outputs],
        "tags": list(task.tags),
    }


def _verification_to_dict(policy: VerificationPolicy) -> dict[str, Any]:
    return {
        "policy": policy.policy,
        "required": policy.required,
        "deterministic_checks": [_check_to_dict(item) for item in policy.deterministic_checks],
        **({"verifier_role": policy.verifier_role} if policy.verifier_role else {}),
        **({"execution_mode": policy.execution_mode} if policy.execution_mode else {}),
        **(
            {"execution_mode_reason": policy.execution_mode_reason}
            if policy.execution_mode_reason
            else {}
        ),
        **({"reasoning": policy.reasoning} if policy.reasoning else {}),
        "max_revision_attempts": policy.max_revision_attempts,
    }


def _check_to_dict(check: VerificationCheck) -> dict[str, Any]:
    return {
        "id": check.id,
        "kind": check.kind,
        "description": check.description,
        **({"argv": list(check.argv)} if check.argv else {}),
        **({"path": check.path} if check.path else {}),
        "timeout_seconds": check.timeout_seconds,
        "expected_exit_code": check.expected_exit_code,
    }


def _resource_to_dict(resource: ResourceClaim) -> dict[str, Any]:
    return {
        "id": resource.id,
        "kind": resource.kind,
        "target": resource.target,
        "access": resource.access,
        **({"description": resource.description} if resource.description else {}),
    }


def _output_to_dict(output: TaskOutput) -> dict[str, Any]:
    return {
        "id": output.id,
        "description": output.description,
        **({"path": output.path} if output.path else {}),
        "required": output.required,
    }


def _context_to_dict(context: TaskContext) -> dict[str, Any]:
    return {
        "memory_queries": list(context.memory_queries),
        "memory_record_ids": list(context.memory_record_ids),
        "dependency_outputs": list(context.dependency_outputs),
        "max_memory_records": context.max_memory_records,
        "max_dependency_outputs": context.max_dependency_outputs,
    }


def _reject_unknown(raw: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, name: str) -> str:
    result = _required_string(value, name)
    if not _IDENTIFIER.fullmatch(result):
        raise ValueError(f"{name} must match {_IDENTIFIER.pattern}")
    return result


def _array(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    raw = _array(value, name)
    result = tuple(_required_string(item, f"{name} item") for item in raw)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _nonempty_strings(value: Any, name: str) -> tuple[str, ...]:
    result = _strings(value, name)
    if not result:
        raise ValueError(f"{name} must be a non-empty array")
    return result


def _identifiers(value: Any, name: str) -> tuple[str, ...]:
    raw = _array(value, name)
    result = tuple(_identifier(item, f"{name} item") for item in raw)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _positive_int(value: Any, name: str) -> int:
    return _bounded_int(value, name, 1, 1_000_000)


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validate_unique(values: Iterable[str], name: str) -> None:
    items = tuple(values)
    if len(set(items)) != len(items):
        raise ValueError(f"{name}s must be unique")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
