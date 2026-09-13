from __future__ import annotations

import json
from typing import Any, Callable

from .bootstrap import mark_roadmap, select_milestone
from .config import Config
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
    SUCCESS_STATUSES,
    CompletionOutcome,
    DesktopLifecycleError,
    _active_session_by_thread,
    _append_event,
    _bind_resource_identity,
    _block_if_revision_limit_reached,
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
    if kind == "replanner":
        try:
            replanner_result = parse_plan_change_result(final_message)
        except PlanChangeProtocolError as exc:
            raise DesktopLifecycleError(str(exc)) from exc
        return _complete_replanner(
            cfg,
            session=session,
            thread_id=thread_id,
            turn_id=turn_id,
            result=replanner_result,
            at=at,
            now_epoch=now_epoch,
        )
    try:
        plan_change_request = parse_plan_change_request(final_message)
    except PlanChangeProtocolError as exc:
        raise DesktopLifecycleError(str(exc)) from exc
    if plan_change_request is not None:
        worker_status = "PLAN_CHANGE_REQUEST"
    elif kind == "verifier":
        try:
            verdict = parse_verifier_result(final_message)
        except VerificationProtocolError as exc:
            raise DesktopLifecycleError(str(exc)) from exc
        worker_status = verdict.verdict
    else:
        worker_status = parse_desktop_worker_status(final_message)
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
    if verdict is not None:
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
        and task.verification.policy in {"deterministic", "auto"}
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
                _block_if_revision_limit_reached(plan, state, task_id, current, timestamp)
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
                    _block_if_revision_limit_reached(
                        plan, state, task_id, current, timestamp
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
            state.last_error = f"{task_id} {kind} returned {worker_status}"

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
        completed = _verified_prefix(plan, state)
        next_index = state.milestone_index
    mark_roadmap(cfg.root, plan, completed, language=cfg.language)
    if plan.legacy_serial and not done:
        select_milestone(cfg.state_dir, plan, next_index, language=cfg.language)
    _materialize(descriptors)
    return CompletionOutcome(True, worker_status, descriptors, done)

def _complete_replanner(
    cfg: Config,
    *,
    session: dict[str, Any],
    thread_id: str,
    turn_id: str,
    result: Any,
    at: str | None,
    now_epoch: int | None,
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
        raise DesktopLifecycleError(str(exc)) from exc

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
    if candidate.legacy_serial and not done:
        select_milestone(cfg.state_dir, candidate, next_index, language=cfg.language)
    _materialize(descriptors)
    return CompletionOutcome(True, "PLAN_CHANGE_APPLIED", descriptors, done)
