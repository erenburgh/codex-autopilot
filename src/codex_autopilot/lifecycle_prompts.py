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
