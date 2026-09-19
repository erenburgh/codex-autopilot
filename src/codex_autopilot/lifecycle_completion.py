from __future__ import annotations

import json
import re
from typing import Any, Callable

from .ai_studio import AIStudioRuntime, ContextBoundaryError
from .artifact_staging_lifecycle import (
    audit_completed_task_scope,
    bind_completion_artifact_gate,
)
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
from .memory import ProjectMemory
from .rules import record_violation
from .memory_verification import evidence_that_may_support
from .plan import Plan, load_plan, plan_to_dict, validate_plan_change
from .lifecycle_screening import complete_screening_session
from .skill_packs import record_runtime_skill_attestation
from .plan_verification import (
    PlanVerificationError,
    deterministic_plan_issues,
    format_plan_verification_issues,
    parse_plan_verification_result,
    plan_change_verification_mode,
    plan_sha256,
)
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
    WorkerProtocolError,
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
    _latest_completion_context,
    _latest_implementation_thread_id,
    _materialize,
    _record_deterministic_evidence,
    _record_deterministic_verification_results,
    _require_desktop_owned,
    _session_kind,
    _sync_legacy_cursor,
    _verified_prefix,
    parse_desktop_worker_status,
)
from .lifecycle_failures import reconcile_desktop_thread_identity
from .lifecycle_reservations import _reserve_in_state
from .lifecycle_rule_audit import _audit_rule_declaration, _record_rule_conflicts
from .plan_verification_lifecycle import (
    apply_legacy_plan_change_without_goal_contract,
    complete_plan_verifier as _complete_plan_verifier,
    reject_plan_verifier_result as _reject_plan_verifier_result,
)


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
    if kind == "screening":
        return complete_screening_session(
            cfg,
            session=session,
            thread_id=thread_id,
            turn_id=turn_id,
            final_message=final_message,
            at=at,
            now_epoch=now_epoch,
            dispatcher_authorized=dispatcher_authorized,
        )
    if kind == "replanner":
        try:
            replanner_result = parse_plan_change_result(final_message)
        except PlanChangeProtocolError as exc:
            raise WorkerProtocolError(str(exc)) from exc
        # Ownership of the transition is passed here too. It was fixed for
        # the engineer and the worker separately and the replanner was
        # missed: the callee side was ready, the caller never passed the
        # flag. So the replanner's whole successor bookkeeping was
        # unreachable from production, and the next step answered "current
        # dispatcher does not own the completed-to-successor transition" -
        # on the very first plan change.
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
    if kind == "plan_verifier":
        try:
            plan_verdict = parse_plan_verification_result(final_message)
        except PlanVerificationError as exc:
            return _reject_plan_verifier_result(
                cfg,
                session=session,
                thread_id=thread_id,
                turn_id=turn_id,
                reason=str(exc),
                at=at,
                now_epoch=now_epoch,
                dispatcher_authorized=dispatcher_authorized,
            )
        return _complete_plan_verifier(
            cfg,
            session=session,
            thread_id=thread_id,
            turn_id=turn_id,
            verdict=plan_verdict,
            at=at,
            now_epoch=now_epoch,
            dispatcher_authorized=dispatcher_authorized,
        )
    try:
        plan_change_request = parse_plan_change_request(final_message)
    except PlanChangeProtocolError as exc:
        raise WorkerProtocolError(str(exc)) from exc
    reason_code = ""
    if plan_change_request is not None:
        worker_status = "PLAN_CHANGE_REQUEST"
    elif kind == "verifier":
        try:
            verdict = parse_verifier_result(final_message)
        except VerificationProtocolError as exc:
            # An unreadable verdict is a model error, not a runtime fault.
            # Measured: the verifier attached a `rubric` field to the verdict
            # - the department rubric the previous task itself created - the
            # turn completed successfully, and the dispatcher died parsing
            # the reply. The work stayed done, the acceptance was not
            # recorded, a dispatcher-crash ticket opened on top of it, and
            # the run stood for an hour and a half. The same class is
            # already closed for the replanner.
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
    artifact_gate = bind_completion_artifact_gate(
        cfg, session, task_id, checkpoint_before
    )
    memory = ProjectMemory(cfg.root)
    evidence = memory.milestone_evidence(
        task_id,
        after_audit_id=int(session.get("memory_audit_before") or 0),
        limit=100,
    )
    needs_evidence = kind == "verifier" or worker_status in SUCCESS_STATUSES
    if needs_evidence and not evidence:
        raise WorkerProtocolError(
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
            raise WorkerProtocolError(str(exc)) from exc
        invalid_refs = sorted(
            {
                ref
                for issue in verdict.issues
                for ref in issue.dod_refs
                if ref > len(task.definition_of_done)
            }
        )
        if invalid_refs:
            raise WorkerProtocolError(
                f"verifier issues reference unknown Definition of Done items: {invalid_refs}"
            )
    deterministic_results: tuple[DeterministicCheckResult, ...] = ()
    if (
        kind in IMPLEMENTATION_SESSION_KINDS | {"revision"}
        and worker_status in SUCCESS_STATUSES
        and task.verification.deterministic_checks
    ):
        deterministic_results = run_deterministic_checks(
            artifact_gate.workspace,
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
    if (
        artifact_gate.staged
        and kind in IMPLEMENTATION_SESSION_KINDS | {"revision"}
        and worker_status in SUCCESS_STATUSES
    ):
        artifact_gate.seal_and_record(
            task_id, memory, provider_thread_id=thread_id
        )
        evidence = memory.milestone_evidence(
            task_id,
            after_audit_id=int(session.get("memory_audit_before") or 0),
            limit=100,
        )
    memory_verification_ids: list[str] = []
    independent_verification_id: str | None = None
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
        supporting_evidence = evidence_that_may_support(evidence)
        if not supporting_evidence:
            raise WorkerProtocolError(
                f"{task_id} has no evidence that can support an acceptance: "
                "every recorded item is below the deterministic threshold. "
                "Record a command, test, artifact or filesystem observation "
                "before returning a verdict; external and instruction "
                "material stays on the record but cannot support it."
            )
        verification = memory._record_runtime_verification_result(
            task_id=task_id,
            check_id="independent-acceptance",
            policy="independent",
            verdict=verdict.verdict,
            summary=(
                "Fresh independent verifier accepted every Definition of Done item."
                if verdict.verdict == "PASS"
                else f"Fresh independent verifier requested revision with {len(verdict.issues)} issue(s)."
            ),
            # R18: an acceptance rests only on what may support it. Outside
            # material stays on the milestone record and is not cited.
            evidence_ids=supporting_evidence,
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
        independent_verification_id = str(verification["id"])
        memory_verification_ids.append(independent_verification_id)
        if verdict.verdict == "PASS" and task.skill_attestation is not None:
            implementation_evidence, implementation_checks = _latest_completion_context(
                memory, initial, task_id
            )
            skill_verification = record_runtime_skill_attestation(
                memory, plan, task, evidence=implementation_evidence,
                check_results=implementation_checks, created_by=verifier_role,
                provider_thread_id=thread_id, provider_turn_id=turn_id,
            )
            assert skill_verification is not None
            memory_verification_ids.append(str(skill_verification["id"]))
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
        audit_completed_task_scope(cfg, plan, state, current, timestamp)
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
                artifact_gate.promote(
                    task_id,
                    independent_verification_id,
                    state=state,
                    session=current,
                    at=timestamp,
                )
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
                artifact_gate.require_revision(task_id)
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
                # The re-hire decision is taken once - at reservation, where
                # the revision budget is actually spent and the next revision
                # number is known. A second call here raised the step twice
                # for one acceptance refusal.
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
                    artifact_gate.require_revision(task_id)
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
            # R13: the stop reason is a code from the closed list, not a
            # retelling of the status. The old line "M9 worker returned
            # BLOCKED" said nothing beyond the status itself.
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
    if plan.legacy_serial and not done:
        select_milestone(cfg.state_dir, plan, next_index, language=cfg.language)
    _materialize(descriptors)
    _notify_completion(cfg, plan, task_id, state_after=task_state_after, done=done)
    return CompletionOutcome(True, worker_status, descriptors, done)


def _notify_completion(cfg, plan, task_id: str, *, state_after: str, done: bool) -> None:
    """Tell the human that the work is finished.

    The only available way: the "unread" state belongs to the Desktop
    interface and is not ours from outside - measured, see notify.py. One
    banner per transition, not per event: a stream of notifications gets
    switched off by the second task.

    The call may break nothing: it stands after the state is saved and does
    not raise.
    """

    from .notify import notify

    if done:
        notify(cfg, "Codex Autopilot", cfg.root.name, "Run finished.")
        return
    if state_after not in {TaskState.VERIFIED.value, TaskState.BLOCKED.value}:
        return
    title = next((item.title for item in plan.tasks if item.id == task_id), task_id)
    word = "verified" if state_after == TaskState.VERIFIED.value else "stopped"
    notify(cfg, "Codex Autopilot", cfg.root.name, f"{task_id} {word}: {title}")

# R13: DevOps resolves infrastructure bugs on the user's behalf, and the
# user takes no part in choosing the fix. So an escalation is not a second
# equal exit but an exception, and it must name its reason with a code from
# the closed list.
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
    """The engineer's final line: the outcome and, for an escalation, the reason code."""

    matches = PIPELINE_ENGINEER_STATUS.findall(message or "")
    last = next(
        (line.strip() for line in reversed((message or "").splitlines()) if line.strip()),
        "",
    )
    if len(matches) != 1 or last != f"PIPELINE_ENGINEER_STATUS: {matches[0]}":
        raise DesktopLifecycleError(
            "the on-call engineer must finish with exactly one line "
            "PIPELINE_ENGINEER_STATUS: RESOLVED or "
            "PIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER <CODE>"
        )
    parts = matches[0].split()
    if parts[0] == "RESOLVED":
        return "RESOLVED", ""
    if len(parts) != 2 or parts[1] not in ESCALATION_CODES:
        raise DesktopLifecycleError(
            "an escalation requires a reason code from the closed list (R13): "
            + ", ".join(sorted(ESCALATION_CODES))
        )
    return "ESCALATE_TO_USER", parts[1]


def _relayable_descriptors_without_a_thread(state: RunState) -> tuple[Any, ...]:
    """Reserved work that nobody is left to raise.

    After an incident, sessions remain in states fit for a relay: the thread
    was never created, so there is nothing to duplicate. Their owner - the
    task that completed its turn before the incident - can no longer raise
    them: its process has exited. Returning their descriptors is how the run
    continues without an operator.
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
    """The run would stand forever: work is ready and nobody is there to do it.

    An empty successor list is legitimate in itself - when everything hangs
    on a blocked task, say. The sign of trouble is different: a task in READY
    and not one live session, so nobody will come and nothing will move.
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
    """Accept the engineer's outcome, taking nothing on its word.

    RESOLVED counts only if the ticket is really closed - through
    devops-resolve-incident, with a passing healthcheck. The word in the
    final line is not a statement of repair: exactly that substitution of a
    claim for an observation cost the run a night.
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
            f"the on-call engineer's incident {incident_id} was not found"
        )
    resolved = str(incident.get("phase")) == IncidentPhase.RESOLVED.value
    if status == "RESOLVED" and not resolved:
        raise DesktopLifecycleError(
            f"the engineer declared RESOLVED, but ticket {incident_id} stayed in phase "
            f"{incident.get('phase')}: closing is done by devops-resolve-incident "
            "with a passing healthcheck"
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
        # The engineer's turn completed like any other, and the causality
        # barrier reads exactly this event. The engineer used to write only
        # its own `pipeline_engineer_completed`: its completed turn stayed
        # invisible to the barrier, and nobody was left to raise the
        # successor - "automatic relay has no completed causal predecessor".
        _append_event(state, "turn_completed", current, timestamp, detail=status)
        _append_event(
            state,
            "pipeline_engineer_completed",
            current,
            timestamp,
            detail=f"{incident_id}: {status}",
        )
        # The phase contract is the same for all: the engineer reports the
        # applied rules and the disagreements exactly like a worker.
        _audit_rule_declaration(cfg, state, current, final_message, timestamp)
        _record_rule_conflicts(
            cfg, state, current, final_message, timestamp, ProjectMemory(cfg.root)
        )
        descriptors: tuple[Any, ...] = ()
        if status == "ESCALATE_TO_USER":
            # The ticket must learn of the escalation together with the run.
            # The run used to go to BLOCKED while the ticket stayed in
            # PIPELINE_ENGINEER: the store believed the engineer was working,
            # the task hung paused, and neither it nor the user had anything
            # to close the ticket with.
            PipelineIncidentStore(cfg.state_dir).escalate_incident_to_user(
                incident_id,
                reason_code=escalation_code,
                at=timestamp,
                detail="Pipeline Engineer handed the incident to the user",
            )
            state.status = "BLOCKED"
            state.phase = "PIPELINE_ENGINEER_ESCALATED"
            state.last_error = (
                f"the on-call engineer handed incident {incident_id} to the user: "
                f"{escalation_code}"
            )
        else:
            state.status = "READY"
            state.phase = "PREPARING"
            state.last_error = None
            # A repair without a successor is not a completion. An empty
            # list used to be returned here, the run went to READY/PREPARING,
            # and that was the end: the engineer closed the incident, its
            # process exited normally, and nobody was left to launch M1. The
            # causal predecessor is dead by then - its death was the incident
            # - so the causal link is the engineer's own turn: its Stop hook
            # performs the relay, as for any worker.
            descriptors = _reserve_in_state(
                cfg,
                load_plan(cfg.state_dir, cfg.profile),
                state,
                memory_audit_before=ProjectMemory(cfg.root).audit_highwater(),
                relay_owner_thread_id=thread_id,
                now_epoch=now_epoch,
            )
            # A reservation created BEFORE the incident is not new, and
            # `_reserve_in_state` will not return it. It used to stay hanging
            # in CREATE_REQUESTED: the engineer fixed the cause, exited, and
            # the run stood until a human resumed it by hand. Exactly this
            # made the pipeline non-automatic - every repair needed an
            # operator.
            if not descriptors:
                descriptors = _relayable_descriptors_without_a_thread(state)
            if dispatcher_authorized:
                # The same transition-ownership bookkeeping as for an
                # ordinary worker. The engineer used to assign a successor
                # without marking it on itself: the dispatcher refused to
                # carry the chain on with "current dispatcher does not own
                # the completed-to-successor transition", the reservation
                # hung in CREATE_REQUESTED, and a new incident - about the
                # dispatcher's own crash - opened on top of the closed one.
                current["automatic_successor_tokens"] = [
                    item.reservation_token for item in descriptors
                ]
                current["automatic_dispatch_state"] = (
                    "ADVANCING" if descriptors else "COMPLETED"
                )
            if not descriptors and _would_idle_forever(state):
                # An exception here would lose the very record of the
                # engineer's completion, so the run stops loudly rather than
                # crashing: the task is ready, but nobody can be assigned.
                state.status = "BLOCKED"
                state.phase = "PIPELINE_ENGINEER_NO_SUCCESSOR"
                state.last_error = (
                    f"the engineer closed incident {incident_id}, but no successor was assigned: "
                    "there is a ready task and not one active session"
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


# How many times the replanner gets its own graph back with the reason.
# Three attempts in all: one original and two with the error text in hand.
# If the model missed the schema three times, it is no accident, and the
# next turn would burn limits for nothing - the run must stop loudly and
# name the reason to the human, not spin silently.
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
    """Return the graph to the replanner with the reason and let it redo it.

    The plan does not change: a rejected graph is written nowhere. Only the
    plan-change record changes - it accumulates the list of refusals that
    goes into the next prompt. The requesting task goes to BLOCKED, and the
    ordinary reservation path raises a fresh replanner from it: it already
    knows BLOCKED -> READY for this case.
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
            # The budget is exhausted. A silent wait here is precisely the
            # hole that leaves a run standing unexplained: a stop must name
            # its reason in the status.
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


# How many times a verdict is returned to the verifier with the reason.
# Three attempts in all: one original and two with the refusal in hand.
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
    """Return the verdict to the verifier with the reason and let it rewrite it.

    The acceptance counts in no direction: an unreadable verdict is neither
    PASS nor REVISE. The task returns to IMPLEMENTED - the work is done and
    still awaits acceptance - and the ordinary reservation path raises a
    fresh verifier.
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
            # Three unreadable verdicts in a row are no accident. Burning
            # more turns is pointless: the run stops loudly and names the
            # reason instead of spinning silently.
            state.status = "BLOCKED"
            state.phase = "VERIFICATION_PROTOCOL_BLOCKED"
            state.last_error = (
                f"the verifier of {task_id} returned an unreadable verdict three times: {reason}"
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
            promotion_evidence_store=ProjectMemory(cfg.root),
        )
    except (PlanChangeProtocolError, PlanChangeConflictError, ValueError) as exc:
        # An invalid graph is a model error, not an infrastructure fault.
        # It used to be raised as DesktopLifecycleError: the dispatcher
        # crashed, a PIPELINE ticket opened, and the run stood forever - the
        # on-call engineer had nothing to repair; the reply was broken, not
        # the runtime. Measured: the replanner returned a departments field
        # absent from the schema, and a 24-task run stood with zero done. A
        # verifier in that situation returns the work to the worker with the
        # reason; the replanner had no such path.
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

    admission_issues = deterministic_plan_issues(candidate)
    if admission_issues:
        return _reject_replanner_result(
            cfg,
            session=session,
            thread_id=thread_id,
            turn_id=turn_id,
            reason=(
                "proposed plan failed deterministic coverage admission: "
                + format_plan_verification_issues(admission_issues)
            ),
            request_id=request_id,
            current_plan=current_plan,
            at=at,
            now_epoch=now_epoch,
            dispatcher_authorized=dispatcher_authorized,
        )
    if current_plan.goal_contract is None:
        return apply_legacy_plan_change_without_goal_contract(
            cfg, current_plan=current_plan, candidate=candidate, session=session,
            result=result, thread_id=thread_id, turn_id=turn_id, at=at,
            now_epoch=now_epoch, dispatcher_authorized=dispatcher_authorized,
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
        verification_mode = plan_change_verification_mode(
            current_plan,
            candidate,
            accepted_patches_since_full=(
                state.accepted_plan_patches_since_full_revalidation
            ),
            full_revalidation_patches=(
                cfg.runtime.full_plan_revalidation_patches
            ),
        )
        current["final_status"] = "PLAN_CHANGE_PROPOSED"
        current["completed_at"] = timestamp
        current["status"] = "COMPLETED"
        current["plan_change_result"] = {
            "request_id": result.request_id,
            "base_graph_version": result.base_graph_version,
            "target_graph_version": candidate.graph_version,
            "proposed_plan_sha256": plan_sha256(candidate),
            "verification_mode": verification_mode,
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
            detail="PLAN_CHANGE_PROPOSED",
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
        requester_task_id = str(change["requester_task_id"])
        state.task_states = transition_task(
            current_plan,
            state.task_states,
            requester_task_id,
            TaskState.BLOCKED,
        )
        change["status"] = "PLAN_VERIFICATION_REQUIRED"
        change["proposed_plan"] = plan_to_dict(candidate)
        change["proposed_plan_sha256"] = plan_sha256(candidate)
        change["verification_mode"] = verification_mode
        change["proposed_at"] = timestamp
        descriptors = _reserve_in_state(
            cfg,
            current_plan,
            state,
            memory_audit_before=ProjectMemory(cfg.root).audit_highwater(),
            relay_owner_thread_id=thread_id,
            now_epoch=now_epoch,
        )
        if dispatcher_authorized:
            # The third completion path that was never handed ownership of
            # the transition. The planner changed the plan, reserved a
            # successor and did not mark it on itself: the next step answered
            # "current dispatcher does not own the completed-to-successor
            # transition". The on-call engineer had the same gap and it was
            # fixed separately - three paths, one closed.
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
        done = False
    _materialize(descriptors)
    return CompletionOutcome(True, "PLAN_CHANGE_PROPOSED", descriptors, done)
