from __future__ import annotations

import json
import re
from typing import Any, Callable

from .ai_studio import AIStudioRuntime, ContextBoundaryError
from .bootstrap import mark_roadmap, select_milestone
from .config import Config
from .department_acceptance import (
    DepartmentAcceptanceError,
    LoadedDepartmentAcceptance,
    load_task_department_acceptance,
    require_rubric_attestation,
    task_department_binding,
)
from .hook_trust import require_trusted_stop_hook_for_config
from .lifecycle_base import parse_applied_rules
from .memory import ProjectMemory
from .rules import record_violation
from .scope import (
    ScopeNotObservable,
    audit_declared_scope,
    observe_changed_paths,
)
from .plan import Plan, load_plan
from .resilience import (
    PlanChangeConflictError,
    PlanChangeProtocolError,
    active_plan_change,
    append_resilience_event,
    commit_plan_change,
    parse_plan_change_request,
    parse_plan_change_result,
    reconcile_plan_change_state,
    register_plan_change_request,
    validate_replanner_result,
)
from .resources import (
    ResourceLockCoordinator,
    release_resources_in_state,
)
from .run_state import RunState, StateStore, utc_now
from .scheduler import schedule
from .task_state import (
    TaskState,
    transition_task,
)
from .thread_titles import department_verifier_thread_title
from .verification import (
    DeterministicCheckResult,
    VerificationProtocolError,
    VerificationVerdict,
    deterministic_issues,
    parse_verifier_result,
    run_deterministic_checks,
    verifier_route,
)

from .lifecycle_base import (
    IMPLEMENTATION_SESSION_KINDS,
    PENDING_SESSION_STATUSES,
    SUCCESS_STATUSES,
    CompletionOutcome,
    DesktopLifecycleError,
    _active_session_by_thread,
    _append_event,
    _bind_resource_identity,
    _dispatcher_owns_reservation,
    _finish_global_state,
    _latest_implementation_thread_id,
    _materialize,
    _record_deterministic_evidence,
    _record_deterministic_verification_results,
    _require_desktop_owned,
    _session_kind,
    _sync_legacy_cursor,
    _verified_prefix,
    parse_desktop_worker_status,
    task_checkpoint,
    task_checkpoint_path,
)
from .lifecycle_failures import reconcile_desktop_thread_identity
from .lifecycle_reservations import _reserve_in_state


def _audit_rule_declaration(
    cfg: Config,
    state: RunState,
    session: dict[str, Any],
    final_message: str,
    at: str,
) -> None:
    """Правило R16: отчёт обязан перечислить применённые id правил.

    Правило в режиме CHECKED: отсутствие перечня записывается как дефект
    и поднимает R16 в приоритете правил следующего воркера, но завершение
    не рушит. Ссылка на несуществующий id - тоже дефект: так правило
    "соблюдается" цитированием того, чего нет.
    """

    from .rules import RULES

    declared = parse_applied_rules(final_message)
    known = {item.id for item in RULES}
    if not declared:
        detail = (
            f"R16: отчёт задачи {session.get('task_id')} не перечислил применённые "
            "правила; ожидается строка AUTOPILOT_RULES перед AUTOPILOT_STATUS"
        )
    else:
        unknown = [item for item in declared if item not in known]
        if not unknown:
            return
        detail = (
            f"R16: отчёт задачи {session.get('task_id')} ссылается на несуществующие "
            f"правила: {', '.join(unknown)}"
        )
    record_violation(cfg.state_dir, "R16", detail=detail)
    _append_event(state, "rule_declaration_missing", session, at, detail=detail)


def _record_rule_conflicts(
    cfg: Config,
    state: RunState,
    session: dict[str, Any],
    final_message: str,
    at: str,
    memory,
) -> None:
    """Правило R16: расхождение с формулировкой уходит в Conflict.

    Воркер не разрешает его сам. Конфликт открывается между записанной
    формулировкой правила и тем, как её прочитал исполнитель, и остаётся
    открытым: разрешает его человек или отдельная задача, но не тот, кто
    его заявил.

    Формулировка правила заводится наблюдением один раз на проект -
    конфликту нужна существующая запись, а правило живёт в коде, не в
    памяти. Дальше все расхождения по этому правилу спорят с той же
    записью, и историю по правилу видно целиком.
    """

    from .lifecycle_base import parse_rule_conflicts
    from .rules import rule as rule_by_id

    conflicts = parse_rule_conflicts(final_message)
    if not conflicts:
        return
    task_id = str(session.get("task_id") or "")
    for rule_id, detail in conflicts:
        try:
            canonical = rule_by_id(rule_id)
        except KeyError:
            record_violation(
                cfg.state_dir,
                "R16",
                detail=f"R16: {task_id} оспорил несуществующее правило {rule_id}",
            )
            continue
        try:
            recorded = _rule_statement_record(memory, rule_id, canonical.statement)
            reading = memory.add_observation(
                statement=f"{rule_id}: исполнитель {task_id} прочитал правило иначе — {detail}",
                created_by=f"task:{task_id}",
                confidence="medium",
            )
            conflict = memory.open_conflict(
                existing_record_id=str(recorded["id"]),
                incoming_record_id=str(reading["id"]),
                statement=(
                    f"{rule_id}: записанная формулировка и прочтение задачи "
                    f"{task_id} расходятся; разрешает не исполнитель"
                ),
                created_by=f"task:{task_id}",
            )
        except Exception as exc:
            _append_event(
                state,
                "rule_conflict_not_recorded",
                session,
                at,
                detail=f"{rule_id}: {exc}",
            )
            continue
        _append_event(
            state,
            "rule_conflict_opened",
            session,
            at,
            detail=f"{rule_id}: {conflict.get('id')}",
        )


def _rule_statement_record(memory, rule_id: str, statement: str) -> dict[str, Any]:
    """Каноническая формулировка правила как Truth, одна на проект.

    Конфликт открывается только против Truth - и это правильно: спорить
    можно с установленным, а не с чьим-то мнением. Формулировка правила
    установлена: она прочитана из работающего рантайма, и это
    доказательство вида environment_probe. Файлом её не подтвердить -
    rules.py лежит в автопилоте, а не в проекте пользователя.
    """

    marker = f"{rule_id} (записанная формулировка)"
    page = memory.search(query=rule_id, categories=["truth"], limit=20)
    for record in page.records:
        if str(record.get("statement", "")).startswith(marker):
            return record
    evidence = memory.record_evidence(
        kind="environment_probe",
        summary=f"Формулировка {rule_id}, прочитанная из установленного рантайма.",
        created_by="codex-autopilot",
        environment_probe=statement,
        role="rule_statement",
    )
    return memory.record_verified_fact(
        statement=f"{marker}: {statement}",
        created_by="codex-autopilot",
        verification_method="прочитано из блока правил установленного рантайма",
        evidence_ids=[str(evidence["id"])],
    )


def _audit_task_scope(
    cfg: Config,
    plan: Plan,
    state: RunState,
    session: dict[str, Any],
    at: str,
) -> None:
    """Правило R7: сверить фактически изменённые пути с объявленной областью.

    Правило в режиме CHECKED: расхождение записывается как дефект и
    поднимает R7 в приоритете правил следующего воркера, но не рушит
    завершение. Невозможность наблюдать пути записывается отдельно -
    "не проверено" не должно выглядеть как "нарушений нет".
    """

    task = plan.task_map.get(str(session.get("task_id") or ""))
    if task is None:
        return
    try:
        changed = observe_changed_paths(cfg.root, session.get("scope_baseline"))
    except ScopeNotObservable as error:
        _append_event(state, "scope_not_observed", session, at, detail=str(error))
        return
    violations = audit_declared_scope(task, changed, project_root=cfg.root)
    for detail in violations:
        record_violation(cfg.state_dir, "R7", detail=detail)
        _append_event(state, "scope_violation_recorded", session, at, detail=detail)


def complete_desktop_worker(
    cfg: Config,
    *,
    thread_id: str,
    turn_id: str,
    final_message: str,
    source_thread_id: str | None = None,
    at: str | None = None,
    now_epoch: int | None = None,
    hook_gate: Callable[[Config], Any] | None = None,
    dispatcher_reservation_token: str | None = None,
    dispatcher_pid: int | None = None,
) -> CompletionOutcome:
    """Consume an authoritative Desktop Stop event and schedule the next frontier."""

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    initial = store.load()
    session = _active_session_by_thread(initial, thread_id)
    gate = hook_gate or require_trusted_stop_hook_for_config
    gate_checked = False
    dispatcher_authorized = bool(
        session
        and dispatcher_reservation_token == session.get("reservation_token")
        and _dispatcher_owns_reservation(session, dispatcher_pid=dispatcher_pid)
    )
    source_identity = str(source_thread_id or "").strip()
    if session is None and source_identity and source_identity != thread_id:
        source_session = _active_session_by_thread(initial, source_identity)
        if source_session is not None:
            gate(cfg)
            gate_checked = True
            reconcile_desktop_thread_identity(
                cfg,
                str(source_session["reservation_token"]),
                previous_thread_id=source_identity,
                current_thread_id=thread_id,
                expected_task_id=str(source_session["task_id"]),
                at=at,
                hook_gate=lambda _cfg: None,
            )
            initial = store.load()
            session = _active_session_by_thread(initial, thread_id)
    if session is None:
        return CompletionOutcome(False, None, (), initial.status == "DONE")
    if not gate_checked and not dispatcher_authorized:
        gate(cfg)
    kind = _session_kind(session)
    verdict: VerificationVerdict | None = None
    plan_change_request = None
    if kind == "pipeline_engineer":
        return _complete_pipeline_engineer(
            cfg,
            session=session,
            thread_id=thread_id,
            turn_id=turn_id,
            final_message=final_message,
            at=at,
            now_epoch=now_epoch,
            dispatcher_authorized=dispatcher_authorized,
            dispatcher_pid=dispatcher_pid,
        )
    if kind == "replanner":
        try:
            replanner_result = parse_plan_change_result(final_message)
        except PlanChangeProtocolError as exc:
            raise DesktopLifecycleError(str(exc)) from exc
        # Владение переходом передаётся и сюда. Инженеру и воркеру его
        # чинили по отдельности, реплэннера пропустили: сторона
        # вызываемого была готова, а вызывающий флаг не передавал. Из-за
        # этого весь учёт преемника у реплэннера был недостижим из
        # продакшена, и следующий шаг отвечал "current dispatcher does
        # not own the completed-to-successor transition" - на первой же
        # смене плана.
        return _complete_replanner(
            cfg,
            session=session,
            thread_id=thread_id,
            turn_id=turn_id,
            result=replanner_result,
            at=at,
            now_epoch=now_epoch,
            dispatcher_authorized=dispatcher_authorized,
            dispatcher_pid=dispatcher_pid,
        )
    try:
        plan_change_request = parse_plan_change_request(final_message)
    except PlanChangeProtocolError as exc:
        raise DesktopLifecycleError(str(exc)) from exc
    reason_code = ""
    if plan_change_request is not None:
        worker_status = "PLAN_CHANGE_REQUEST"
    elif kind == "verifier":
        try:
            verdict = parse_verifier_result(final_message)
        except VerificationProtocolError as exc:
            # Нечитаемый вердикт - ошибка модели, а не поломка рантайма.
            # Замерено: верифаер приложил к вердикту поле `rubric` -
            # рубрику отдела, которую предыдущая задача сама и создала, -
            # ход завершился успешно, а диспетчер умер на разборе ответа.
            # Работа осталась сделанной, приёмка не записана, поверх
            # неё открылся тикет о падении диспетчера, и прогон простоял
            # полтора часа. Тот же класс уже закрыт для реплэннера.
            return _reject_verifier_result(
                cfg,
                session=session,
                thread_id=thread_id,
                turn_id=turn_id,
                reason=str(exc),
                at=at,
                now_epoch=now_epoch,
                dispatcher_authorized=dispatcher_authorized,
            )
        worker_status = verdict.verdict
    else:
        worker_status, reason_code = parse_desktop_worker_status(final_message)
    task_id = str(session["task_id"])
    checkpoint_before = str(session.get("checkpoint_before") or "")
    checkpoint_path = task_checkpoint_path(cfg.state_dir, task_id)
    if task_checkpoint(cfg.state_dir, task_id) == checkpoint_before:
        raise DesktopLifecycleError(
            "worker did not update its own checkpoint file: "
            f"{checkpoint_path.relative_to(cfg.state_dir.parent)}"
        )
    memory = ProjectMemory(cfg.root)
    evidence = memory.milestone_evidence(
        task_id,
        after_audit_id=int(session.get("memory_audit_before") or 0),
        limit=100,
    )
    needs_evidence = kind == "verifier" or worker_status in SUCCESS_STATUSES
    if needs_evidence and not evidence:
        raise DesktopLifecycleError(
            f"{task_id} returned completion without new Project Memory evidence"
        )
    plan = load_plan(cfg.state_dir, cfg.profile)
    task = plan.task_map[task_id]
    loaded_department_acceptance: LoadedDepartmentAcceptance | None = None
    if verdict is not None:
        try:
            department_binding = task_department_binding(task)
            if department_binding is not None:
                dependency_outputs = AIStudioRuntime(
                    plan,
                    cfg.root,
                    language=cfg.language,
                    skill_path=cfg.skill_path,
                    memory=memory,
                ).select_context(
                    task.id,
                    task_states=initial.task_states,
                ).dependency_outputs
                loaded_department_acceptance = load_task_department_acceptance(
                    memory,
                    departments=plan.departments,
                    task=task,
                    role_names={item.id: item.name for item in plan.roles},
                    dependency_outputs=dependency_outputs,
                )
                department = loaded_department_acceptance.department
                require_rubric_attestation(department.rubric, verdict.rubric)
                expected_title = department_verifier_thread_title(
                    task.id,
                    task.title,
                    lead_role_name=plan.role_map[department.lead_role_id].name,
                )
                actual_title = str((session.get("descriptor") or {}).get("title") or "")
                if actual_title != expected_title:
                    raise DepartmentAcceptanceError(
                        "department verifier title does not identify the pinned Lead Role: "
                        f"expected {expected_title!r}, observed {actual_title!r}"
                    )
            elif verdict.rubric is not None:
                raise DepartmentAcceptanceError(
                    "verifier attested a department rubric for a task without "
                    "department-binding and rubric-binding resources"
                )
        except (DepartmentAcceptanceError, ContextBoundaryError) as exc:
            raise DesktopLifecycleError(str(exc)) from exc
        invalid_refs = sorted(
            {
                ref
                for issue in verdict.issues
                for ref in issue.dod_refs
                if ref > len(task.definition_of_done)
            }
        )
        if invalid_refs:
            raise DesktopLifecycleError(
                f"verifier issues reference unknown Definition of Done items: {invalid_refs}"
            )
    deterministic_results: tuple[DeterministicCheckResult, ...] = ()
    if (
        kind in IMPLEMENTATION_SESSION_KINDS | {"revision"}
        and worker_status in SUCCESS_STATUSES
        and task.verification.deterministic_checks
    ):
        deterministic_results = run_deterministic_checks(
            cfg.root,
            task.verification.deterministic_checks,
            evidence,
        )
        _record_deterministic_evidence(
            memory,
            task_id,
            task.verification.deterministic_checks,
            deterministic_results,
            provider_thread_id=thread_id,
        )
        evidence = memory.milestone_evidence(
            task_id,
            after_audit_id=int(session.get("memory_audit_before") or 0),
            limit=100,
        )
    memory_verification_ids: list[str] = []
    if deterministic_results:
        memory_verification_ids.extend(
            _record_deterministic_verification_results(
                memory,
                task_id,
                deterministic_results,
                evidence,
                provider_thread_id=thread_id,
                provider_turn_id=turn_id,
            )
        )
    if verdict is not None:
        verifier_role = plan.role_map[verifier_route(plan, task).role_id].name
        verification = memory.record_verification_result(
            task_id=task_id,
            check_id="independent-acceptance",
            policy="independent",
            verdict=verdict.verdict,
            summary=(
                "Fresh independent verifier accepted every Definition of Done item."
                if verdict.verdict == "PASS"
                else f"Fresh independent verifier requested revision with {len(verdict.issues)} issue(s)."
            ),
            evidence_ids=[str(item["id"]) for item in evidence],
            created_by=verifier_role,
            provider="codex-desktop",
            provider_thread_id=thread_id,
            provider_turn_id=turn_id,
            details={
                "verification_round": int(session.get("verification_round") or 0),
                "issues": [item.to_dict() for item in verdict.issues],
                **(
                    {
                        "department_rubric": loaded_department_acceptance.rubric.to_dict(),
                        "department_acceptance": loaded_department_acceptance.to_dict(),
                    }
                    if loaded_department_acceptance is not None
                    else {}
                ),
            },
        )
        memory_verification_ids.append(str(verification["id"]))
    timestamp = at or utc_now()
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        current = _active_session_by_thread(state, thread_id)
        if current is None:
            return CompletionOutcome(False, None, (), state.status == "DONE")
        if current["reservation_token"] != session["reservation_token"]:
            raise DesktopLifecycleError("Desktop completion identity changed during reconciliation")
        current["turn_id"] = turn_id
        current["final_status"] = worker_status
        current["completed_at"] = timestamp
        current["status"] = (
            "COMPLETED"
            if kind == "verifier" or worker_status in SUCCESS_STATUSES
            else worker_status
        )
        current["completion_evidence_ids"] = [str(item["id"]) for item in evidence]
        if deterministic_results:
            current["deterministic_results"] = [item.to_dict() for item in deterministic_results]
        if verdict is not None:
            current["verification_result"] = verdict.to_dict()
        if memory_verification_ids:
            current["memory_verification_ids"] = memory_verification_ids
        _bind_resource_identity(
            state,
            str(current["reservation_token"]),
            thread_id=thread_id,
            turn_id=turn_id,
        )
        _append_event(state, "turn_identity_bound", current, timestamp)
        _append_event(state, "turn_completed", current, timestamp, detail=worker_status)
        _audit_task_scope(cfg, plan, state, current, timestamp)
        _audit_rule_declaration(cfg, state, current, final_message, timestamp)
        _record_rule_conflicts(cfg, state, current, final_message, timestamp, memory)

        release_resources_in_state(
            state,
            str(current["resource_ownership_token"]),
            reason="authoritative Desktop turn completion",
            now=timestamp,
        )
        state.active_task_ids = [item for item in state.active_task_ids if item != task_id]

        if plan_change_request is not None:
            source = TaskState(state.task_states[task_id])
            if source not in {
                TaskState.RUNNING,
                TaskState.VERIFYING,
                TaskState.REVISING,
            }:
                raise DesktopLifecycleError(
                    "plan change request requires an active implementation phase"
                )
            state.task_states = transition_task(
                plan,
                state.task_states,
                task_id,
                TaskState.BLOCKED,
            )
            current["status"] = "PLAN_CHANGE_REQUESTED"
            current["final_status"] = "PLAN_CHANGE_REQUEST"
            record = register_plan_change_request(
                state,
                plan_change_request,
                requester_task_id=task_id,
                requester_session_token=str(current["reservation_token"]),
                at=timestamp,
            )
            current["plan_change_id"] = record["id"]
            descriptors = _reserve_in_state(
                cfg,
                plan,
                state,
                memory_audit_before=memory.audit_highwater(),
                relay_owner_thread_id=thread_id,
                now_epoch=now_epoch,
            )
            if dispatcher_authorized:
                current["automatic_successor_tokens"] = [
                    item.reservation_token for item in descriptors
                ]
                current["automatic_dispatch_state"] = (
                    "ADVANCING" if descriptors else "COMPLETED"
                )
            _finish_global_state(
                plan,
                state,
                descriptors,
                paused=store.pause_requested(),
            )
            store.save(state)
            _materialize(descriptors)
            return CompletionOutcome(True, worker_status, descriptors, False)

        if kind == "verifier":
            if state.task_states[task_id] != TaskState.VERIFYING.value:
                raise DesktopLifecycleError("verifier completion requires VERIFYING state")
            assert verdict is not None
            if verdict.verdict == "PASS":
                state.task_states = transition_task(
                    plan, state.task_states, task_id, TaskState.VERIFIED
                )
                accepted_owner = _latest_implementation_thread_id(state, task_id)
                current["accepted_implementation_thread_id"] = accepted_owner
                _append_event(
                    state,
                    "verification_passed",
                    current,
                    timestamp,
                    detail=json.dumps(
                        {
                            "verdict": "PASS",
                            "accepted_implementation_thread_id": accepted_owner,
                        },
                        sort_keys=True,
                    ),
                )
            else:
                state.task_states = transition_task(
                    plan, state.task_states, task_id, TaskState.REVISION_REQUIRED
                )
                current["verification_issues"] = [item.to_dict() for item in verdict.issues]
                _append_event(
                    state,
                    "verification_revise",
                    current,
                    timestamp,
                    detail=json.dumps(verdict.to_dict(), ensure_ascii=False, sort_keys=True),
                )
                # Решение о перенайме принимается один раз - на резервации,
                # где бюджет ревизий реально тратится и известен номер
                # следующей ревизии. Второй вызов здесь поднимал ступень
                # дважды за один отказ приёмки.
        elif worker_status in SUCCESS_STATUSES:
            expected = (
                TaskState.REVISING.value
                if kind == "revision"
                else TaskState.RUNNING.value
            )
            if state.task_states[task_id] != expected:
                raise DesktopLifecycleError(
                    f"{kind} completion requires {expected} state"
                )
            state.task_states = transition_task(
                plan, state.task_states, task_id, TaskState.IMPLEMENTED
            )
            _append_event(
                state,
                "revision_completed" if kind == "revision" else "implementation_completed",
                current,
                timestamp,
                detail=TaskState.IMPLEMENTED.value,
            )
            if deterministic_results:
                passed = all(item.passed for item in deterministic_results)
                _append_event(
                    state,
                    "deterministic_verification_completed",
                    current,
                    timestamp,
                    detail=json.dumps(
                        {
                            "verdict": "PASS" if passed else "REVISE",
                            "checks": [item.to_dict() for item in deterministic_results],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
                if not passed:
                    state.task_states = transition_task(
                        plan, state.task_states, task_id, TaskState.VERIFYING
                    )
                    issues = deterministic_issues(deterministic_results)
                    current["verification_issues"] = [item.to_dict() for item in issues]
                    state.task_states = transition_task(
                        plan, state.task_states, task_id, TaskState.REVISION_REQUIRED
                    )
        else:
            source = TaskState.REVISING if kind == "revision" else TaskState.RUNNING
            if state.task_states[task_id] != source.value:
                raise DesktopLifecycleError(
                    f"{kind} failure requires {source.value} state"
                )
            state.task_states = transition_task(
                plan, state.task_states, task_id, TaskState.BLOCKED
            )
            # R13: причина остановки - код из закрытого списка, а не
            # пересказ статуса. Прежняя строка "M9 worker returned
            # BLOCKED" не сообщала ничего сверх самого статуса.
            state.last_error = f"{task_id} {kind} {worker_status} {reason_code}".strip()
            current["reason_code"] = reason_code
            if reason_code == "UNSPECIFIED":
                record_violation(
                    cfg.state_dir,
                    "R13",
                    detail=f"{task_id} {kind} reported {worker_status} without a reason code",
                )

        if state.task_states[task_id] == TaskState.VERIFIED.value:
            memory.mark_milestone_complete(
                milestone_id=task_id,
                run_id=state.run_id,
                worker_sequence=int(current["worker_sequence"]),
                source=(
                    "independent_verifier"
                    if kind == "verifier"
                    else "deterministic_verification"
                    if deterministic_results
                    else "desktop_stop"
                ),
            )
        state.task_retry_at.pop(task_id, None)
        _sync_legacy_cursor(plan, state)
        next_relay_owner = thread_id
        if kind == "verifier" and verdict is not None and verdict.verdict == "PASS":
            next_relay_owner = str(
                current.get("accepted_implementation_thread_id") or ""
            )
            if not next_relay_owner:
                raise DesktopLifecycleError(
                    "verified task has no exact causal implementation predecessor"
                )
        descriptors = _reserve_in_state(
            cfg,
            plan,
            state,
            memory_audit_before=memory.audit_highwater(),
            relay_owner_thread_id=next_relay_owner,
            now_epoch=now_epoch,
        )
        if dispatcher_authorized:
            current["automatic_successor_tokens"] = [
                item.reservation_token for item in descriptors
            ]
            current["automatic_dispatch_state"] = (
                "ADVANCING" if descriptors else "COMPLETED"
            )
        _finish_global_state(
            plan,
            state,
            descriptors,
            paused=store.pause_requested(),
        )
        store.save(state)
        done = state.status == "DONE"
        task_state_after = state.task_states.get(task_id, "")
        completed = _verified_prefix(plan, state)
        next_index = state.milestone_index
    mark_roadmap(cfg.root, plan, completed, language=cfg.language)
    _materialize(descriptors)
    _notify_completion(cfg, plan, task_id, state_after=task_state_after, done=done)
    return CompletionOutcome(True, worker_status, descriptors, done)


def _notify_completion(cfg, plan, task_id: str, *, state_after: str, done: bool) -> None:
    """Сказать человеку, что работа закончилась.

    Единственный доступный способ: состояние "непрочитано" принадлежит
    интерфейсу Desktop, и снаружи оно не наше - замерено, см. notify.py.
    Здесь один банер на переход, а не на каждое событие: поток
    уведомлений человек выключит на второй задаче.

    Вызов не вправе ничего сломать: он стоит после сохранения состояния
    и не бросает.
    """

    from .notify import notify

    if done:
        notify(cfg, "Codex Autopilot", cfg.root.name, "Прогон завершён.")
        return
    if state_after not in {TaskState.VERIFIED.value, TaskState.BLOCKED.value}:
        return
    title = next((item.title for item in plan.tasks if item.id == task_id), task_id)
    word = "проверена" if state_after == TaskState.VERIFIED.value else "встала"
    notify(cfg, "Codex Autopilot", cfg.root.name, f"{task_id} {word}: {title}")

# R13: DevOps решает инфраструктурные баги от имени пользователя, и
# пользователь не участвует в выборе способа фикса. Поэтому эскалация -
# не второй равноправный выход, а исключение, и она обязана назвать
# причину кодом из закрытого списка.
ESCALATION_CODES = frozenset({
    "DANGEROUS_PERMISSION",
    "GLOBAL_CONFIG_CHANGE",
    "PROJECT_DAMAGE_RISK",
    "RECOVERY_EXHAUSTED",
    "PRODUCT_DECISION",
    "ARCHITECTURE_DECISION",
})
PIPELINE_ENGINEER_STATUS = re.compile(
    r"(?m)^PIPELINE_ENGINEER_STATUS:\s*(RESOLVED|ESCALATE_TO_USER(?:\s+\S+)?)\s*$"
)


def parse_pipeline_engineer_status(message: str) -> tuple[str, str]:
    """Финальная строка инженера: итог и, для эскалации, код причины."""

    matches = PIPELINE_ENGINEER_STATUS.findall(message or "")
    last = next(
        (line.strip() for line in reversed((message or "").splitlines()) if line.strip()),
        "",
    )
    if len(matches) != 1 or last != f"PIPELINE_ENGINEER_STATUS: {matches[0]}":
        raise DesktopLifecycleError(
            "дежурный инженер обязан закончить ровно одной строкой "
            "PIPELINE_ENGINEER_STATUS: RESOLVED или "
            "PIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER <КОД>"
        )
    parts = matches[0].split()
    if parts[0] == "RESOLVED":
        return "RESOLVED", ""
    if len(parts) != 2 or parts[1] not in ESCALATION_CODES:
        raise DesktopLifecycleError(
            "эскалация требует кода причины из закрытого списка (R13): "
            + ", ".join(sorted(ESCALATION_CODES))
        )
    return "ESCALATE_TO_USER", parts[1]


def _orphaned_pending_descriptors(state: RunState) -> tuple[Any, ...]:
    """Зарезервированная работа, которую некому поднять.

    После инцидента остаются сессии в состояниях, пригодных к релею:
    ветка ещё не создавалась, дублировать нечего. Их владелец - задача,
    завершившая свой ход до инцидента, - поднять их уже не может: его
    процесс вышел. Возврат их дескрипторов и есть продолжение прогона
    без оператора.
    """

    from .lifecycle_base import RELAYABLE_SESSION_STATUSES, LaunchDescriptor

    return tuple(
        LaunchDescriptor.from_dict(dict(item["descriptor"]))
        for item in state.worker_sessions
        if item.get("status") in RELAYABLE_SESSION_STATUSES
        and isinstance(item.get("descriptor"), dict)
        and not str(item.get("thread_id") or "")
    )


def _would_idle_forever(state: RunState) -> bool:
    """Прогон встал бы навсегда: работа готова, а делать её некому.

    Пустой список преемников законен сам по себе - например, когда всё
    упёрлось в заблокированную задачу. Признак беды другой: есть задача
    в READY и при этом ни одной живой сессии, то есть никто не придёт и
    ничего не сдвинет.
    """

    active = any(
        item.get("status") in PENDING_SESSION_STATUSES
        for item in state.worker_sessions
    )
    if active:
        return False
    return any(
        value == TaskState.READY.value for value in (state.task_states or {}).values()
    )


def _complete_pipeline_engineer(
    cfg: Config,
    *,
    session: dict[str, Any],
    thread_id: str,
    turn_id: str,
    final_message: str,
    at: str | None,
    now_epoch: int | None = None,
    dispatcher_authorized: bool = False,
    dispatcher_pid: int | None = None,
) -> CompletionOutcome:
    """Принять итог инженера, ничего не принимая на слово.

    RESOLVED засчитывается только если тикет действительно закрыт - через
    devops-resolve-incident, с пройденной проверкой здоровья. Слово в
    финальной строке заявлением о починке не является: ровно эта подмена
    наблюдения заявлением и стоила прогону ночи.
    """

    from .pipeline_engineer import IncidentPhase, PipelineIncidentStore

    status, escalation_code = parse_pipeline_engineer_status(final_message)
    timestamp = at or utc_now()
    incident_id = str(session.get("incident_id") or "")
    incidents = PipelineIncidentStore(cfg.state_dir).load()
    incident = next(
        (
            item
            for item in incidents.get("incidents", [])
            if str(item.get("incident_id")) == incident_id
        ),
        None,
    )
    if incident is None:
        raise DesktopLifecycleError(
            f"инцидент {incident_id} дежурного инженера не найден"
        )
    resolved = str(incident.get("phase")) == IncidentPhase.RESOLVED.value
    if status == "RESOLVED" and not resolved:
        raise DesktopLifecycleError(
            f"инженер объявил RESOLVED, а тикет {incident_id} остался в фазе "
            f"{incident.get('phase')}: закрытие выполняется devops-resolve-incident "
            "с пройденной проверкой здоровья"
        )

    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        current = _active_session_by_thread(state, thread_id)
        if current is None or current.get("reservation_token") != session.get(
            "reservation_token"
        ):
            return CompletionOutcome(False, None, (), state.status == "DONE")
        current["turn_id"] = turn_id
        current["status"] = "COMPLETED"
        current["final_status"] = status
        if escalation_code:
            current["escalation_code"] = escalation_code
        current["completed_at"] = timestamp
        # Ход инженера завершился так же, как любой другой, и барьер
        # причинности читает именно это событие. Прежде инженер писал
        # только своё `pipeline_engineer_completed`: его завершённый ход
        # оставался для барьера невидимым, и преемника некому было
        # поднять - "automatic relay has no completed causal predecessor".
        _append_event(state, "turn_completed", current, timestamp, detail=status)
        _append_event(
            state,
            "pipeline_engineer_completed",
            current,
            timestamp,
            detail=f"{incident_id}: {status}",
        )
        # Контракт фаз одинаков для всех: инженер отчитывается о
        # применённых правилах и о расхождениях так же, как воркер.
        _audit_rule_declaration(cfg, state, current, final_message, timestamp)
        _record_rule_conflicts(
            cfg, state, current, final_message, timestamp, ProjectMemory(cfg.root)
        )
        descriptors: tuple[Any, ...] = ()
        if status == "ESCALATE_TO_USER":
            # Тикет обязан узнать об эскалации вместе с прогоном. Прежде
            # прогон уходил в BLOCKED, а тикет оставался в
            # PIPELINE_ENGINEER: хранилище считало инженера работающим,
            # задача висела приостановленной, и закрыть тикет было
            # нечем ни ему, ни пользователю.
            PipelineIncidentStore(cfg.state_dir).escalate_incident_to_user(
                incident_id,
                reason_code=escalation_code,
                at=timestamp,
                detail="Pipeline Engineer handed the incident to the user",
            )
            state.status = "BLOCKED"
            state.phase = "PIPELINE_ENGINEER_ESCALATED"
            state.last_error = (
                f"дежурный инженер передал инцидент {incident_id} пользователю: "
                f"{escalation_code}"
            )
        else:
            state.status = "READY"
            state.phase = "PREPARING"
            state.last_error = None
            # Починка без преемника завершением не является. Прежде здесь
            # возвращался пустой список, прогон уходил в READY/PREPARING,
            # и на этом всё кончалось: инженер закрывал инцидент, его
            # процесс штатно выходил, а запускать M1 становилось некому.
            # Причинный предшественник к этому моменту мёртв - именно его
            # смерть и была инцидентом, - поэтому причинным звеном служит
            # сам ход инженера: его Stop-хук выполняет релей, как у
            # любого воркера.
            descriptors = _reserve_in_state(
                cfg,
                load_plan(cfg.state_dir, cfg.profile),
                state,
                memory_audit_before=ProjectMemory(cfg.root).audit_highwater(),
                relay_owner_thread_id=thread_id,
                now_epoch=now_epoch,
            )
            # Резервация, созданная ДО инцидента, новой не является, и
            # `_reserve_in_state` её не вернёт. Прежде она так и оставалась
            # висеть в CREATE_REQUESTED: инженер чинил причину, выходил, а
            # прогон стоял до тех пор, пока человек не возобновит его
            # руками. Именно это и делало пайплайн неавтоматическим -
            # каждая починка требовала оператора.
            if not descriptors:
                descriptors = _orphaned_pending_descriptors(state)
            if dispatcher_authorized:
                # Тот же учёт владения переходом, что и у обычного воркера.
                # Прежде инженер назначал преемника и не отмечал его у себя:
                # диспетчер отказывался вести цепочку дальше словами
                # "current dispatcher does not own the completed-to-successor
                # transition", резервация висела в CREATE_REQUESTED, и поверх
                # закрытого инцидента открывался новый - о падении самого
                # диспетчера.
                current["automatic_successor_tokens"] = [
                    item.reservation_token for item in descriptors
                ]
                current["automatic_dispatch_state"] = (
                    "ADVANCING" if descriptors else "COMPLETED"
                )
            if not descriptors and _would_idle_forever(state):
                # Исключение здесь потеряло бы саму запись о завершении
                # инженера, поэтому прогон останавливается громко, а не
                # падает: задача готова к работе, но назначить её некому.
                state.status = "BLOCKED"
                state.phase = "PIPELINE_ENGINEER_NO_SUCCESSOR"
                state.last_error = (
                    f"инженер закрыл инцидент {incident_id}, но преемник не назначен: "
                    "есть готовая задача и ни одной активной сессии"
                )
                _append_event(
                    state,
                    "pipeline_engineer_left_no_successor",
                    current,
                    timestamp,
                    detail=incident_id,
                )
        store.save(state)
    return CompletionOutcome(True, status, descriptors, False)


# Сколько раз реплэннеру возвращают его же граф с причиной отказа.
# Три попытки всего: одна исходная и две с текстом ошибки на руках. Если
# модель трижды не попала в схему, дело не в случайности, и следующий ход
# будет жечь лимиты впустую - прогон должен остановиться громко и назвать
# человеку причину, а не молча крутиться.
MAX_PLAN_CHANGE_REJECTIONS = 2


def _reject_replanner_result(
    cfg: Config,
    *,
    session: dict[str, Any],
    thread_id: str,
    turn_id: str,
    reason: str,
    request_id: str,
    current_plan: Plan,
    at: str | None,
    now_epoch: int | None,
    dispatcher_authorized: bool = False,
) -> CompletionOutcome:
    """Вернуть реплэннеру его граф с причиной отказа и дать переделать.

    План не меняется: отвергнутый граф не пишется никуда. Меняется
    только запись смены плана - в ней копится список отказов, который
    попадает в следующий промпт. Задача-заказчик уходит в BLOCKED, и
    обычный путь резервирования поднимает из него свежего реплэннера:
    он уже умеет BLOCKED -> READY для этого случая.
    """

    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    descriptors: tuple[Any, ...] = ()
    with coordinator.transaction():
        state = store.load()
        current = _active_session_by_thread(state, thread_id)
        if current is None or current.get("reservation_token") != session.get(
            "reservation_token"
        ):
            return CompletionOutcome(False, None, (), state.status == "DONE")
        if _session_kind(current) != "replanner":
            raise DesktopLifecycleError("plan change result came from a non-replanner task")
        change = active_plan_change(state, request_id=request_id)
        rejections = list(change.get("rejections") or [])
        rejections.append({"at": timestamp, "reason": reason})
        change["rejections"] = rejections
        exhausted = len(rejections) > MAX_PLAN_CHANGE_REJECTIONS

        current["turn_id"] = turn_id
        current["final_status"] = "PLAN_CHANGE_REJECTED"
        current["completed_at"] = timestamp
        current["status"] = "COMPLETED"
        current["plan_change_rejection"] = reason
        _bind_resource_identity(
            state,
            str(current["reservation_token"]),
            thread_id=thread_id,
            turn_id=turn_id,
        )
        _append_event(state, "turn_identity_bound", current, timestamp)
        _append_event(
            state,
            "turn_completed",
            current,
            timestamp,
            detail="PLAN_CHANGE_REJECTED",
        )
        release_resources_in_state(
            state,
            str(current["resource_ownership_token"]),
            reason="replanner returned an invalid graph",
            now=timestamp,
        )
        task_id = str(current["task_id"])
        state.active_task_ids = [
            item for item in state.active_task_ids if item != task_id
        ]
        state.task_states = transition_task(
            current_plan,
            state.task_states,
            task_id,
            TaskState.BLOCKED,
        )
        append_resilience_event(
            state,
            "plan_change_rejected",
            at=timestamp,
            task_id=task_id,
            plan_change_id=request_id,
            detail={"reason": reason, "attempt": len(rejections)},
        )
        if exhausted:
            # Бюджет исчерпан. Молчаливое ожидание здесь и есть та дыра,
            # из-за которой прогон стоит без объяснения: остановка должна
            # называть причину в статусе.
            change["status"] = "REJECTED"
            state.active_plan_change_id = None
            state.status = "BLOCKED"
            state.phase = "PLAN_CHANGE_REJECTED"
            if dispatcher_authorized:
                current["automatic_successor_tokens"] = []
                current["automatic_dispatch_state"] = "COMPLETED"
            store.save(state)
            return CompletionOutcome(True, "PLAN_CHANGE_REJECTED", (), False)

        change["status"] = "DRAINING"
        descriptors = _reserve_in_state(
            cfg,
            current_plan,
            state,
            memory_audit_before=ProjectMemory(cfg.root).audit_highwater(),
            relay_owner_thread_id=thread_id,
            now_epoch=now_epoch,
        )
        if dispatcher_authorized:
            current["automatic_successor_tokens"] = [
                item.reservation_token for item in descriptors
            ]
            current["automatic_dispatch_state"] = (
                "ADVANCING" if descriptors else "COMPLETED"
            )
        _finish_global_state(
            current_plan,
            state,
            descriptors,
            paused=store.pause_requested(),
        )
        store.save(state)
    _materialize(descriptors)
    return CompletionOutcome(True, "PLAN_CHANGE_REJECTED", descriptors, False)


# Сколько раз вердикт возвращают верифаеру с причиной. Три попытки
# всего: одна исходная и две с текстом отказа на руках.
MAX_VERIFICATION_REJECTIONS = 2


def _reject_verifier_result(
    cfg: Config,
    *,
    session: dict[str, Any],
    thread_id: str,
    turn_id: str,
    reason: str,
    at: str | None,
    now_epoch: int | None,
    dispatcher_authorized: bool = False,
) -> CompletionOutcome:
    """Вернуть верифаеру его вердикт с причиной и дать переписать.

    Приёмка не засчитывается ни в какую сторону: непрочитанный вердикт
    не PASS и не REVISE. Задача возвращается в IMPLEMENTED - работа
    сделана и по-прежнему ждёт приёмки, - и обычный путь резервирования
    поднимает свежего верифаера.
    """

    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    descriptors: tuple[Any, ...] = ()
    plan = load_plan(cfg.state_dir, cfg.profile)
    with coordinator.transaction():
        state = store.load()
        current = _active_session_by_thread(state, thread_id)
        if current is None or current.get("reservation_token") != session.get(
            "reservation_token"
        ):
            return CompletionOutcome(False, None, (), state.status == "DONE")
        task_id = str(current["task_id"])
        rejections = list(state.verification_rejections.get(task_id) or [])
        rejections.append({"at": timestamp, "reason": reason})
        state.verification_rejections[task_id] = rejections
        exhausted = len(rejections) > MAX_VERIFICATION_REJECTIONS

        current["turn_id"] = turn_id
        current["final_status"] = "VERIFICATION_REJECTED"
        current["completed_at"] = timestamp
        current["status"] = "COMPLETED"
        current["verification_rejection"] = reason
        _bind_resource_identity(
            state,
            str(current["reservation_token"]),
            thread_id=thread_id,
            turn_id=turn_id,
        )
        _append_event(state, "turn_identity_bound", current, timestamp)
        _append_event(
            state, "turn_completed", current, timestamp, detail="VERIFICATION_REJECTED"
        )
        release_resources_in_state(
            state,
            str(current["resource_ownership_token"]),
            reason="verifier returned an unreadable verdict",
            now=timestamp,
        )
        state.active_task_ids = [
            item for item in state.active_task_ids if item != task_id
        ]
        state.task_states = transition_task(
            plan,
            state.task_states,
            task_id,
            TaskState.BLOCKED if exhausted else TaskState.IMPLEMENTED,
        )
        if exhausted:
            # Три нечитаемых вердикта подряд - это не случайность. Дальше
            # жечь ходы бессмысленно: прогон встаёт громко и называет
            # причину, а не крутится молча.
            state.status = "BLOCKED"
            state.phase = "VERIFICATION_PROTOCOL_BLOCKED"
            state.last_error = (
                f"верифаер {task_id} трижды вернул нечитаемый вердикт: {reason}"
            )
            if dispatcher_authorized:
                current["automatic_successor_tokens"] = []
                current["automatic_dispatch_state"] = "COMPLETED"
            store.save(state)
            return CompletionOutcome(True, "VERIFICATION_REJECTED", (), False)

        descriptors = _reserve_in_state(
            cfg,
            plan,
            state,
            memory_audit_before=ProjectMemory(cfg.root).audit_highwater(),
            relay_owner_thread_id=thread_id,
            now_epoch=now_epoch,
        )
        if dispatcher_authorized:
            current["automatic_successor_tokens"] = [
                item.reservation_token for item in descriptors
            ]
            current["automatic_dispatch_state"] = (
                "ADVANCING" if descriptors else "COMPLETED"
            )
        _finish_global_state(
            plan, state, descriptors, paused=store.pause_requested()
        )
        store.save(state)
    _materialize(descriptors)
    return CompletionOutcome(True, "VERIFICATION_REJECTED", descriptors, False)


def _complete_replanner(
    cfg: Config,
    *,
    session: dict[str, Any],
    thread_id: str,
    turn_id: str,
    result: Any,
    at: str | None,
    now_epoch: int | None,
    dispatcher_authorized: bool = False,
    dispatcher_pid: int | None = None,
) -> CompletionOutcome:
    current_plan = load_plan(cfg.state_dir, cfg.profile)
    request_id = str(session.get("plan_change_id") or "")
    try:
        candidate = validate_replanner_result(
            current_plan,
            result,
            request_id=request_id,
            profile=cfg.profile,
        )
    except (PlanChangeProtocolError, PlanChangeConflictError, ValueError) as exc:
        # Негодный граф - ошибка модели, а не поломка инфраструктуры.
        # Прежде она поднималась как DesktopLifecycleError: диспетчер
        # падал, открывался PIPELINE-тикет, и прогон вставал навсегда -
        # дежурному инженеру чинить нечего, сломан не рантайм, а ответ.
        # Замерено: реплэннер вернул поле departments, которого нет в
        # схеме, и прогон из 24 задач простоял с нулём выполненных.
        # Верифаер в такой ситуации возвращает работу воркеру с
        # причиной; у реплэннера этого пути не было.
        return _reject_replanner_result(
            cfg,
            session=session,
            thread_id=thread_id,
            turn_id=turn_id,
            reason=str(exc),
            request_id=request_id,
            current_plan=current_plan,
            at=at,
            now_epoch=now_epoch,
            dispatcher_authorized=dispatcher_authorized,
        )

    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        current = _active_session_by_thread(state, thread_id)
        if current is None or current.get("reservation_token") != session.get(
            "reservation_token"
        ):
            return CompletionOutcome(False, None, (), state.status == "DONE")
        if _session_kind(current) != "replanner":
            raise DesktopLifecycleError("plan change result came from a non-replanner task")
        change = active_plan_change(state, request_id=request_id)
        if int(change["base_graph_version"]) != current_plan.graph_version:
            raise DesktopLifecycleError("active plan change base version changed")
        current["turn_id"] = turn_id
        current["final_status"] = "PLAN_CHANGE_APPLIED"
        current["completed_at"] = timestamp
        current["status"] = "COMPLETED"
        current["plan_change_result"] = {
            "request_id": result.request_id,
            "base_graph_version": result.base_graph_version,
            "target_graph_version": candidate.graph_version,
        }
        _bind_resource_identity(
            state,
            str(current["reservation_token"]),
            thread_id=thread_id,
            turn_id=turn_id,
        )
        _append_event(state, "turn_identity_bound", current, timestamp)
        _append_event(
            state,
            "turn_completed",
            current,
            timestamp,
            detail="PLAN_CHANGE_APPLIED",
        )
        release_resources_in_state(
            state,
            str(current["resource_ownership_token"]),
            reason="authoritative replanner completion",
            now=timestamp,
        )
        state.active_task_ids = [
            task_id for task_id in state.active_task_ids if task_id != current["task_id"]
        ]
        change["status"] = "REPLANNING"
        reconcile_plan_change_state(
            current_plan,
            candidate,
            state,
            request_id=request_id,
            requester_task_id=str(change["requester_task_id"]),
            at=timestamp,
        )
        _sync_legacy_cursor(candidate, state)
        descriptors = _reserve_in_state(
            cfg,
            candidate,
            state,
            memory_audit_before=ProjectMemory(cfg.root).audit_highwater(),
            relay_owner_thread_id=thread_id,
            now_epoch=now_epoch,
        )
        if dispatcher_authorized:
            # Третий путь завершения, которому не передавали владение
            # переходом. Планировщик менял план, резервировал преемника и
            # не отмечал его у себя: следующий шаг отвечал "current
            # dispatcher does not own the completed-to-successor
            # transition". Тот же пробел уже был у дежурного инженера и
            # чинился отдельно - путей три, а закрыт был один.
            current["automatic_successor_tokens"] = [
                item.reservation_token for item in descriptors
            ]
            current["automatic_dispatch_state"] = (
                "ADVANCING" if descriptors else "COMPLETED"
            )
        _finish_global_state(
            candidate,
            state,
            descriptors,
            paused=store.pause_requested(),
        )
        commit_plan_change(
            cfg.state_dir,
            profile=cfg.profile,
            current=current_plan,
            candidate=candidate,
            state=state,
            request_id=request_id,
        )
        done = state.status == "DONE"
        completed = _verified_prefix(candidate, state)
        next_index = state.milestone_index
    mark_roadmap(cfg.root, candidate, completed, language=cfg.language)
    _materialize(descriptors)
    return CompletionOutcome(True, "PLAN_CHANGE_APPLIED", descriptors, done)
