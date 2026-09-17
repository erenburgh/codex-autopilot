"""Prompt assembly for the phases of the Desktop-owned lifecycle.

Extracted from lifecycle.py: these functions take the config, the plan and
the task and return text. They touch no state and perform no transport,
so they live apart from the lifecycle machine.
"""

from __future__ import annotations

import json
from typing import Any

from .ai_studio import MAX_PROMPT_CHARS, AIStudioRuntime
from .config import Config
from .lifecycle_base import DesktopLifecycleError
from .language import is_russian
from .memory import ProjectMemory
from .plan import GRAPH_PLAN_FIELDS, Plan, Task, plan_to_dict
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
    # The refusals of earlier attempts. Without them the model redoes the
    # work blind and returns the same error: measured on a departments
    # field absent from the plan schema.
    rejections = [
        {"reason": str(item.get("reason") or "")}
        for item in (change.get("rejections") or [])
    ]
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
            "allowed_plan_fields": sorted(GRAPH_PLAN_FIELDS),
        },
    }
    if rejections:
        envelope["rejected_attempts"] = rejections
    declared_workers = (
        cfg.runtime.max_parallel_workers
        if getattr(cfg.runtime, "max_parallel_workers_declared", False)
        and cfg.runtime.max_parallel_workers != plan.max_parallel_workers
        else None
    )
    if declared_workers is not None:
        envelope["constraints"]["required_max_parallel_workers"] = declared_workers
    payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    finish = (
        f'{PLAN_CHANGE_RESULT_PREFIX} '
        '{"request_id":"'
        + str(change["id"])
        + '","base_graph_version":'
        + str(plan.graph_version)
        + ',"plan":{...complete schema-3 plan...}}'
    )
    # The worker count lives in the plan, and only the replanner may
    # rewrite the plan. The user changes it in their config.toml, and
    # without this hand-over the change went nowhere: the replanner copied
    # the old number from the current graph, and the ceiling stayed forever
    # what the run was created with.
    retry_ru = ""
    retry_en = ""
    workers_ru = ""
    workers_en = ""
    if declared_workers is not None:
        workers_ru = (
            f"\n\nThe user set the number of parallel workers: "
            f"{declared_workers}. Set max_parallel_workers={declared_workers} "
            f"in the returned graph; it currently holds {plan.max_parallel_workers}."
        )
        workers_en = (
            f"\n\nThe user set the parallel worker count to {declared_workers}. "
            f"Set max_parallel_workers={declared_workers} in the graph you return; "
            f"it currently holds {plan.max_parallel_workers}."
        )
    if rejections:
        last = rejections[-1]["reason"]
        allowed = ", ".join(sorted(GRAPH_PLAN_FIELDS))
        retry_ru = (
            f"\n\nThe previous attempt was rejected by the runtime: {last}. "
            f"The graph is unchanged. The top-level plan fields are limited to this "
            f"list and it cannot be extended: {allowed}. Anything outside it "
            f"is expressed inside tasks and roles."
        )
        retry_en = (
            f"\n\nThe previous attempt was rejected by the runtime: {last}. "
            f"The graph is unchanged. Top-level plan fields are limited to this "
            f"list and it cannot be extended: {allowed}. Anything else belongs "
            f"inside tasks and roles."
        )
    if is_russian(cfg.language):
        prompt = f"""Codex Autopilot AI Studio Runtime — fresh replanner.

Выполни только короткую перепланировку {change['id']} для канонического каталога {cfg.root}. Ниже расположен полный разрешённый контекст: текущий валидный граф, структурированный запрос и селекторы подтверждённого состояния. Не запрашивай транскрипты, HANDOFF prose или параллельные разговоры.

AUTOPILOT_CONTEXT: {payload}

Сначала полностью прочитай {cfg.skill_path}. При необходимости получи только перечисленные evidence ID через Project Memory. Не изменяй файлы, не запускай production и не становись manager: верни один полный schema-3 replacement graph. user_request переносит runtime - его возвращать не нужно. Дословно сохрани goal, Goal Contract, model_strategy, контракты VERIFIED задач, структурированные RoleProfile и все существующие task ID; установи graph_version={plan.graph_version + 1}. Runtime заново проверит все ссылки, состояния и циклы и выполнит crash-safe commit. Reservation token: {token}.{workers_ru}{retry_ru}

Последняя непустая строка должна быть единственной protocol line в точном формате:
{finish}"""
    else:
        prompt = f"""Codex Autopilot AI Studio Runtime — fresh replanner.

Perform only the short {change['id']} replan for canonical directory {cfg.root}. The bounded context below is complete: the current validated graph, typed request, and verified-state selectors. Do not request transcripts, HANDOFF prose, or concurrent conversations.

AUTOPILOT_CONTEXT: {payload}

Read {cfg.skill_path} completely first. Retrieve only listed evidence IDs from Project Memory if needed. Do not modify files, start production, or become a manager: return one complete schema-3 replacement graph. The runtime carries user_request over; do not return it. Preserve the goal, Goal Contract, model_strategy, VERIFIED task contracts, structured RoleProfiles, and every existing task ID; set graph_version={plan.graph_version + 1}. The runtime will revalidate every reference, state, and cycle and perform the crash-safe commit. Reservation token: {token}.{workers_en}{retry_en}

The final non-empty line must be the only protocol line in this exact format:
{finish}"""
    # A second copy of the same ceiling. In the morning the number was
    # derived from the model window in ai_studio, and this copy stayed
    # bare: the planner prompt embeds the whole 23-task graph, passed
    # 64 000 and killed the relay mid-run - with a NameError instead of a
    # clear refusal, because the exception was never imported here since
    # the split of lifecycle.py.
    if len(prompt) > MAX_PROMPT_CHARS:
        raise DesktopLifecycleError(
            f"replanner prompt is {len(prompt)} characters against a "
            f"{MAX_PROMPT_CHARS} budget derived from the model context window"
        )
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
    prompt = runtime.build_prompt(
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
    # The reason the previous verdict could not be read. Without it a fresh
    # verifier rewrites blind and repeats the same error: measured on the
    # `rubric` field the previous task itself introduced.
    rejections = (state.verification_rejections or {}).get(task_id) or []
    if phase == "verification" and rejections:
        last = str(rejections[-1].get("reason") or "")
        note = (
            f"\n\nThe previous verdict was rejected by the runtime: {last}. The acceptance "
            "counted in no direction - the verdict was not read. Return "
            'AUTOPILOT_VERIFICATION ровно с двумя полями верхнего уровня: '
            '"verdict" and "issues". Any other field rejects the verdict entirely.'
            if is_russian(cfg.language)
            else f"\n\nThe previous verdict was rejected by the runtime: {last}. "
            "Acceptance was not recorded either way - the verdict was not read. "
            'Return AUTOPILOT_VERIFICATION with exactly two top-level fields: '
            '"verdict" and "issues". Any other field rejects the whole verdict.'
        )
        if len(prompt) + len(note) <= MAX_PROMPT_CHARS:
            prompt += note
    return prompt


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
        "acceptance_class": task.acceptance_class.value,
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
