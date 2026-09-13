"""Сборка промптов фаз Desktop-owned жизненного цикла.

Выделено из lifecycle.py: эти функции принимают конфиг, план и задачу
и возвращают текст. Они не трогают состояние и не выполняют транспорт,
поэтому живут отдельно от машины жизненного цикла.
"""

from __future__ import annotations

import json
from typing import Any

from .ai_studio import AIStudioRuntime
from .config import Config
from .language import is_russian
from .memory import ProjectMemory
from .plan import Plan, Task, plan_to_dict
from .resilience import PLAN_CHANGE_RESULT_PREFIX
from .run_state import RunState
from .task_state import TaskState
from .verification import VerificationIssue


def _replanner_prompt(
    cfg: Config,
    plan: Plan,
    state: RunState,
    change: dict[str, Any],
    token: str,
) -> str:
    """Build one bounded, transcript-free graph-replacement request."""

    memory = ProjectMemory(cfg.root)
    request = dict(change["request"])
    evidence_ids = list(request.get("evidence_ids") or [])
    for evidence_id in evidence_ids:
        memory.get_evidence(str(evidence_id))
    verified_state = []
    for task in plan.tasks:
        if state.task_states.get(task.id) != TaskState.VERIFIED.value:
            continue
        verified_state.append(
            {
                "task_id": task.id,
                "state": TaskState.VERIFIED.value,
                "evidence_ids": [
                    str(item["id"])
                    for item in memory.milestone_evidence(task.id, limit=20)[:8]
                ],
            }
        )
    envelope = {
        "phase": "replanning",
        "request_id": change["id"],
        "base_graph_version": plan.graph_version,
        "request": request,
        "current_plan": plan_to_dict(plan),
        "verified_state": verified_state,
        "constraints": {
            "goal_immutable": True,
            "user_request_immutable": True,
            "model_strategy_immutable": True,
            "verified_task_contracts_immutable": True,
            "existing_task_ids_must_remain": True,
            "next_graph_version": plan.graph_version + 1,
        },
    }
    payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    finish = (
        f'{PLAN_CHANGE_RESULT_PREFIX} '
        '{"request_id":"'
        + str(change["id"])
        + '","base_graph_version":'
        + str(plan.graph_version)
        + ',"plan":{...complete schema-3 plan...}}'
    )
    if is_russian(cfg.language):
        prompt = f"""Codex Autopilot AI Studio Runtime — свежий replanner.

Выполни только короткую перепланировку {change['id']} для канонического каталога {cfg.root}. Ниже расположен полный разрешённый контекст: текущий валидный граф, структурированный запрос и селекторы подтверждённого состояния. Не запрашивай транскрипты, HANDOFF prose или параллельные разговоры.

AUTOPILOT_CONTEXT: {payload}

Сначала полностью прочитай {cfg.skill_path}. При необходимости получи только перечисленные evidence ID через Project Memory. Не изменяй файлы, не запускай production и не становись manager: верни один полный schema-3 replacement graph. Дословно сохрани user_request, а также goal, model_strategy, контракты VERIFIED задач, структурированные RoleProfile и все существующие task ID; установи graph_version={plan.graph_version + 1}. Runtime заново проверит все ссылки, состояния и циклы и выполнит crash-safe commit. Reservation token: {token}.

Последняя непустая строка должна быть единственной protocol line в точном формате:
{finish}"""
    else:
        prompt = f"""Codex Autopilot AI Studio Runtime — fresh replanner.

Perform only the short {change['id']} replan for canonical directory {cfg.root}. The bounded context below is complete: the current validated graph, typed request, and verified-state selectors. Do not request transcripts, HANDOFF prose, or concurrent conversations.

AUTOPILOT_CONTEXT: {payload}

Read {cfg.skill_path} completely first. Retrieve only listed evidence IDs from Project Memory if needed. Do not modify files, start production, or become a manager: return one complete schema-3 replacement graph. Preserve user_request verbatim, plus the goal, model_strategy, VERIFIED task contracts, structured RoleProfiles, and every existing task ID; set graph_version={plan.graph_version + 1}. The runtime will revalidate every reference, state, and cycle and perform the crash-safe commit. Reservation token: {token}.

The final non-empty line must be the only protocol line in this exact format:
{finish}"""
    if len(prompt) > 64_000:
        raise DesktopLifecycleError("replanner prompt exceeds 64000 characters")
    return prompt


def _worker_prompt(
    cfg: Config,
    plan: Plan,
    state: RunState,
    task_id: str,
    token: str,
    *,
    kind: str = "implementation",
    verification_round: int = 0,
    revision_number: int = 0,
    verification_issues: tuple[VerificationIssue, ...] = (),
    verification_evidence: tuple[dict[str, Any], ...] = (),
    deterministic_results: tuple[dict[str, Any], ...] = (),
) -> str:
    phase = {
        "implementation": "implementation",
        "worker": "implementation",
        "verifier": "verification",
        "revision": "revision",
    }.get(kind)
    if phase is None:
        raise DesktopLifecycleError(f"unsupported worker prompt kind: {kind}")
    runtime = AIStudioRuntime(
        plan,
        cfg.root,
        language=cfg.language,
        skill_path=cfg.skill_path,
    )
    return runtime.build_prompt(
        task_id,
        phase=phase,
        task_states=state.task_states,
        reservation_token=token,
        verification_round=verification_round,
        revision_number=revision_number,
        issues=verification_issues,
        evidence=verification_evidence,
        deterministic_results=deterministic_results,
    )


def _verifier_prompt(
    cfg: Config,
    plan: Plan,
    task_id: str,
    token: str,
    *,
    verification_round: int,
    evidence: tuple[dict[str, Any], ...],
    deterministic_results: tuple[dict[str, Any], ...],
) -> str:
    task = plan.task_map[task_id]
    route = verifier_route(plan, task)
    role = plan.role_map[route.role_id]
    dod = "\n".join(
        f"{index}. {item}" for index, item in enumerate(task.definition_of_done, 1)
    )
    evidence_json = json.dumps(
        _evidence_selectors(evidence), ensure_ascii=False, separators=(",", ":")
    )
    checks_json = json.dumps(
        list(deterministic_results), ensure_ascii=False, separators=(",", ":")
    )
    role_json = json.dumps(
        {
            "id": role.id,
            "name": role.name,
            "responsibilities": list(role.responsibilities),
            "verification_expectations": list(role.verification_expectations),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    revise_example = (
        f'{VERIFICATION_PREFIX}{{"verdict":"REVISE","issues":['
        '{"code":"ISSUE-1","summary":"short issue","details":"specific evidence and required correction","dod_refs":[1]}]}'
    )
    pass_example = f'{VERIFICATION_PREFIX}{{"verdict":"PASS","issues":[]}}'
    if is_russian(cfg.language):
        return f"""Codex Autopilot Desktop-owned independent verifier.

Независимо проверь {task.id}: {task.title} в каноническом каталоге {cfg.root}. Это свежий verifier V{verification_round}; не изменяй реализацию и не выполняй работу implementer/revision worker.
Исходное пользовательское ТЗ: {plan.user_request}
Цель run: {plan.goal}
Цель задачи: {task.objective}
Роль verifier: {role_json}
Требуемая capability: {route.execution_mode}. Причина: {route.execution_mode_reason}

Критерии готовности:
{dod}

Селективные идентификаторы evidence текущей реализации: {evidence_json}
Результаты deterministic checks, если policy передала их: {checks_json}

Acceptance gate: независимо сопоставь фактический результат с исходным пользовательским ТЗ, целью run, структурированным контрактом задачи и каждым критерием готовности. Тесты implementer являются только evidence и не определяют критерии приёмки.

В prompt намеренно нет ответа implementer, его самооценки, transcript history, HANDOFF prose или параллельных разговоров. Не запрашивай их и не считай утверждения другого worker доказательством. Полностью прочитай {cfg.skill_path}, получи перечисленные evidence через Project Memory, самостоятельно проверь файлы/команды/артефакты и зафиксируй новое evidence для {task.id} с ролью independent_verification. Обнови свой задачный файл передачи .codex-autopilot/handoff/{task.id}.md — обязательный чекпойнт завершения, принадлежащий этой задаче. Не создавай commit, tag, push, publish, reset или clean. Не запускай production через App Server. Reservation token: {token}.

Верни PASS только если каждый критерий подтверждён. Иначе верни REVISE с уникальными структурированными issues. Свободный краткий отчёт разрешён перед protocol line. Последняя непустая строка должна быть ровно одним JSON-результатом одного из форматов:
{pass_example}
{revise_example}"""
    return f"""Codex Autopilot Desktop-owned independent verifier.

Independently verify {task.id}: {task.title} in canonical directory {cfg.root}. This is fresh verifier V{verification_round}; do not modify the implementation or perform implementer/revision work.
Original user request: {plan.user_request}
Run goal: {plan.goal}
Task objective: {task.objective}
Verifier role: {role_json}
Required capability: {route.execution_mode}. Reason: {route.execution_mode_reason}

Definition of Done:
{dod}

Selective evidence identifiers for the current implementation: {evidence_json}
Deterministic check results, when supplied by policy: {checks_json}

Acceptance gate: independently compare the actual result with the original user request, run goal, structured task contract, and every Definition of Done item. Implementer-authored tests are evidence only and do not define the acceptance criteria.

The prompt deliberately contains no implementer response, self-assessment, transcript history, HANDOFF prose, or concurrent conversation. Do not request them or treat another worker's claims as evidence. Read {cfg.skill_path} completely, retrieve the listed evidence through Project Memory, independently inspect the files/commands/artifacts, and record new evidence for {task.id} with role independent_verification. Update your own task handoff file .codex-autopilot/handoff/{task.id}.md - the required completion checkpoint owned by this task. Do not commit, tag, push, publish, reset, or clean. Never start production through App Server. Reservation token: {token}.

Return PASS only when every criterion is evidenced. Otherwise return REVISE with unique structured issues. A concise free-form report may precede the protocol line. The final non-empty line must be exactly one JSON result in one of these forms:
{pass_example}
{revise_example}"""


def _revision_prompt(
    cfg: Config,
    plan: Plan,
    task_id: str,
    token: str,
    *,
    revision_number: int,
    issues: tuple[VerificationIssue, ...],
) -> str:
    task = plan.task_map[task_id]
    role = plan.role_map[task.role]
    dod = "\n".join(
        f"{index}. {item}" for index, item in enumerate(task.definition_of_done, 1)
    )
    issues_json = json.dumps(
        [item.to_dict() for item in issues],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    role_json = json.dumps(
        {
            "id": role.id,
            "name": role.name,
            "responsibilities": list(role.responsibilities),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    verification_contract = json.dumps(
        _verification_contract(task), ensure_ascii=False, separators=(",", ":")
    )
    if is_russian(cfg.language):
        return f"""Codex Autopilot Desktop-owned revision worker.

Выполни только revision R{revision_number} для {task.id}: {task.title} в каноническом каталоге {cfg.root}.
Цель задачи: {task.objective}
Роль: {role_json}

Критерии готовности:
{dod}
Verification policy: {verification_contract}

Структурированные issues verifier/deterministic policy:
{issues_json}

Это свежий worker: prompt содержит только контракт задачи и issues, без verifier transcript, implementer transcript или параллельных разговоров. Полностью прочитай {cfg.skill_path}, исправь перечисленные issues, заново проверь затронутые критерии и зафиксируй новое evidence для {task.id}. Обнови свой задачный файл передачи .codex-autopilot/handoff/{task.id}.md — обязательный чекпойнт завершения, принадлежащий этой задаче. Сохраняй чужие изменения; не создавай commit, tag, push, publish, reset или clean. Не запускай production через App Server. Reservation token: {token}.

Заверши кратким проверенным итогом и ровно одной последней строкой:
AUTOPILOT_STATUS: ROTATE
Используй BLOCKED только при реальном блокере, ESCALATE — только после исчерпания текущего уровня рассуждения."""
    return f"""Codex Autopilot Desktop-owned revision worker.

Perform only revision R{revision_number} for {task.id}: {task.title} in canonical directory {cfg.root}.
Task objective: {task.objective}
Role: {role_json}

Definition of Done:
{dod}
Verification policy: {verification_contract}

Structured verifier/deterministic-policy issues:
{issues_json}

This is a fresh worker: the prompt contains only the task contract and issues, with no verifier transcript, implementer transcript, or concurrent conversation. Read {cfg.skill_path} completely, correct the listed issues, re-check the affected criteria, and record new evidence for {task.id}. Update your own task handoff file .codex-autopilot/handoff/{task.id}.md - the required completion checkpoint owned by this task. Preserve unrelated changes; do not commit, tag, push, publish, reset, or clean. Never start production through App Server. Reservation token: {token}.

Finish with a concise verified result and exactly one final line:
AUTOPILOT_STATUS: ROTATE
Use BLOCKED only for a real blocker and ESCALATE only after exhausting the current reasoning level."""


def _evidence_selectors(evidence: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    selectors: list[dict[str, Any]] = []
    for item in evidence[:20]:
        selector = {
            key: item[key]
            for key in ("id", "kind", "role", "path", "artifact_path")
            if item.get(key) is not None
        }
        if selector.get("id"):
            selectors.append(selector)
    return selectors


def _verification_contract(task: Task) -> dict[str, Any]:
    policy = task.verification
    return {
        "policy": policy.policy,
        "required": policy.required,
        "deterministic_checks": [
            {
                "id": check.id,
                "kind": check.kind,
                "description": check.description,
                **({"argv": list(check.argv)} if check.argv else {}),
                **({"path": check.path} if check.path else {}),
                "timeout_seconds": check.timeout_seconds,
                "expected_exit_code": check.expected_exit_code,
            }
            for check in policy.deterministic_checks
        ],
        "max_revision_attempts": policy.max_revision_attempts,
    }
