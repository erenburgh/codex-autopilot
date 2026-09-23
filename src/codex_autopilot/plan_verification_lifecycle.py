from __future__ import annotations

import json
from typing import Any

from .blocked_runs import stop_run as _stop_run
from .bootstrap import mark_roadmap, select_milestone
from .config import Config
from .lifecycle_base import (
    CompletionOutcome,
    DesktopLifecycleError,
    _active_session_by_thread,
    _append_event,
    _bind_resource_identity,
    _finish_global_state,
    _materialize,
    _sync_legacy_cursor,
    _verified_prefix,
)
from .lifecycle_reservations import _reserve_in_state
from .memory import ProjectMemory
from .plan import load_plan, validate_plan_change
from .plan_verification import (
    FULL_PLAN_REVALIDATION,
    PlanVerificationError,
    PlanVerificationVerdict,
    format_plan_verification_issues,
    make_plan_verification_receipt,
    plan_sha256,
    record_plan_verification,
    validate_verdict_references,
)
from .resilience import (
    active_plan_change,
    append_resilience_event,
    commit_plan_change,
    reconcile_plan_change_state,
)
from .resources import ResourceLockCoordinator, release_resources_in_state
from .run_state import StateStore, utc_now
from .task_state import TaskState, transition_task


MAX_PLAN_VERIFIER_PROTOCOL_REJECTIONS = 2
MAX_SEMANTIC_PLAN_REVISIONS = 2


def apply_legacy_plan_change_without_goal_contract(
    cfg: Config,
    *,
    current_plan: Any,
    candidate: Any,
    session: dict[str, Any],
    result: Any,
    thread_id: str,
    turn_id: str,
    at: str | None,
    now_epoch: int | None,
    dispatcher_authorized: bool = False,
) -> CompletionOutcome:
    """Preserve a pre-v1 run that has no authority for semantic plan review."""

    timestamp = at or utc_now()
    request_id = str(session.get("plan_change_id") or "")
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        current = _active_session_by_thread(state, thread_id)
        if current is None or current.get("reservation_token") != session.get(
            "reservation_token"
        ):
            return CompletionOutcome(False, None, (), state.status == "DONE")
        change = active_plan_change(state, request_id=request_id)
        _complete_session_identity(
            state,
            current,
            thread_id=thread_id,
            turn_id=turn_id,
            final_status="PLAN_CHANGE_APPLIED",
            timestamp=timestamp,
        )
        current["plan_change_result"] = {
            "request_id": result.request_id,
            "base_graph_version": result.base_graph_version,
            "target_graph_version": candidate.graph_version,
            "compatibility": "pre-v1 plan has no Goal Contract",
        }
        release_resources_in_state(
            state,
            str(current["resource_ownership_token"]),
            reason="authoritative legacy replanner completion",
            now=timestamp,
        )
        state.active_task_ids = [
            item for item in state.active_task_ids if item != current["task_id"]
        ]
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
        _record_successors(current, descriptors, dispatcher_authorized)
        _finish_global_state(
            candidate,
            state,
            descriptors,
            paused=store.pause_requested(),
            cfg=cfg,
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


def reject_plan_verifier_result(
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
    """Retry an unreadable verdict without pretending the plan was judged."""

    timestamp = at or utc_now()
    plan = load_plan(cfg.state_dir, cfg.profile)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        current = _active_session_by_thread(state, thread_id)
        if current is None or current.get("reservation_token") != session.get(
            "reservation_token"
        ):
            return CompletionOutcome(False, None, (), state.status == "DONE")
        if current.get("kind") != "plan_verifier":
            raise DesktopLifecycleError(
                "plan verification result came from a non-plan-verifier task"
            )
        change = active_plan_change(
            state, request_id=str(current.get("plan_change_id") or "")
        )
        rejections = list(change.get("plan_verifier_protocol_rejections") or [])
        rejections.append({"at": timestamp, "reason": reason})
        change["plan_verifier_protocol_rejections"] = rejections

        _complete_session_identity(
            state,
            current,
            thread_id=thread_id,
            turn_id=turn_id,
            final_status="PLAN_VERIFICATION_PROTOCOL_REJECTED",
            timestamp=timestamp,
        )
        current["plan_verification_rejection"] = reason
        release_resources_in_state(
            state,
            str(current["resource_ownership_token"]),
            reason="unreadable plan verifier result",
            now=timestamp,
        )
        task_id = str(current["task_id"])
        state.active_task_ids = [
            item for item in state.active_task_ids if item != task_id
        ]
        state.task_states = transition_task(
            plan, state.task_states, task_id, TaskState.BLOCKED
        )
        append_resilience_event(
            state,
            "plan_verifier_protocol_rejected",
            at=timestamp,
            task_id=task_id,
            plan_change_id=str(change["id"]),
            detail={"reason": reason, "attempt": len(rejections)},
        )
        exhausted = len(rejections) > MAX_PLAN_VERIFIER_PROTOCOL_REJECTIONS
        if exhausted:
            change["status"] = "REJECTED"
            change["completed_at"] = timestamp
            state.active_plan_change_id = None
            _stop_run(
                cfg,
                state,
                phase="PLAN_VERIFICATION_PROTOCOL_REJECTED",
                reason=reason,
                summary=(
                    "The plan verifier's answer could not be read every time it "
                    "was asked."
                ),
                at=timestamp,
                system_state={"plan_change_id": str(change.get("id") or "")},
            )
            descriptors = ()
        else:
            change["status"] = "PLAN_VERIFICATION_REQUIRED"
            descriptors = _reserve_in_state(
                cfg,
                plan,
                state,
                memory_audit_before=ProjectMemory(cfg.root).audit_highwater(),
                relay_owner_thread_id=thread_id,
                now_epoch=now_epoch,
            )
        _record_successors(current, descriptors, dispatcher_authorized)
        _finish_global_state(
            plan,
            state,
            descriptors,
            paused=store.pause_requested(),
            cfg=cfg,
        )
        store.save(state)
    _materialize(descriptors)
    return CompletionOutcome(
        True, "PLAN_VERIFICATION_PROTOCOL_REJECTED", descriptors, False
    )


def complete_plan_verifier(
    cfg: Config,
    *,
    session: dict[str, Any],
    thread_id: str,
    turn_id: str,
    verdict: PlanVerificationVerdict,
    at: str | None,
    now_epoch: int | None,
    dispatcher_authorized: bool = False,
) -> CompletionOutcome:
    """Accept or reject one proposed replacement graph after fresh judgment."""

    current_plan = load_plan(cfg.state_dir, cfg.profile)
    initial_state = StateStore(cfg.state_dir).load()
    request_id = str(session.get("plan_change_id") or "")
    change_snapshot = active_plan_change(initial_state, request_id=request_id)
    raw_candidate = change_snapshot.get("proposed_plan")
    if not isinstance(raw_candidate, dict):
        raise DesktopLifecycleError("active plan verification has no proposed plan")
    candidate = validate_plan_change(
        current_plan,
        raw_candidate,
        cfg.profile,
        promotion_evidence_store=ProjectMemory(cfg.root),
    )
    expected_digest = str(change_snapshot.get("proposed_plan_sha256") or "")
    if not expected_digest or expected_digest != plan_sha256(candidate):
        raise DesktopLifecycleError(
            "proposed plan digest changed while the verifier was running"
        )
    mode = str(change_snapshot.get("verification_mode") or "")
    try:
        validate_verdict_references(candidate, verdict)
    except PlanVerificationError as exc:
        return reject_plan_verifier_result(
            cfg,
            session=session,
            thread_id=thread_id,
            turn_id=turn_id,
            reason=str(exc),
            at=at,
            now_epoch=now_epoch,
            dispatcher_authorized=dispatcher_authorized,
        )

    base_receipt = None
    if verdict.verdict == "PASS":
        base_receipt = make_plan_verification_receipt(
            candidate,
            verdict,
            mode=mode,
            verifier_thread_id=thread_id,
            verifier_turn_id=turn_id,
            verified_at=at or utc_now(),
        )
    memory = ProjectMemory(cfg.root)
    receipt, evidence_id, verification_id = record_plan_verification(
        memory,
        candidate,
        verdict,
        mode=mode,
        verifier_thread_id=thread_id,
        verifier_turn_id=turn_id,
        receipt=base_receipt,
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
        if current.get("kind") != "plan_verifier":
            raise DesktopLifecycleError(
                "plan verification result came from a non-plan-verifier task"
            )
        change = active_plan_change(state, request_id=request_id)
        if (
            change.get("proposed_plan_sha256") != expected_digest
            or current.get("proposed_plan_sha256") != expected_digest
        ):
            raise DesktopLifecycleError(
                "plan verification proposal identity changed during completion"
            )
        _complete_session_identity(
            state,
            current,
            thread_id=thread_id,
            turn_id=turn_id,
            final_status=(
                "PLAN_VERIFIED"
                if verdict.verdict == "PASS"
                else "PLAN_REVISION_REQUIRED"
            ),
            timestamp=timestamp,
        )
        current["plan_verification_result"] = verdict.to_dict()
        current["completion_evidence_ids"] = [evidence_id]
        current["memory_verification_ids"] = [verification_id]
        release_resources_in_state(
            state,
            str(current["resource_ownership_token"]),
            reason="authoritative plan verifier completion",
            now=timestamp,
        )
        task_id = str(current["task_id"])
        state.active_task_ids = [
            item for item in state.active_task_ids if item != task_id
        ]

        if verdict.verdict == "REVISE":
            state.task_states = transition_task(
                current_plan,
                state.task_states,
                task_id,
                TaskState.BLOCKED,
            )
            issue_payload = format_plan_verification_issues(verdict.issues)
            rejections = list(change.get("rejections") or [])
            rejections.append(
                {
                    "at": timestamp,
                    "reason": "semantic plan verification rejected: "
                    + issue_payload,
                }
            )
            change["rejections"] = rejections
            history = list(change.get("plan_verification_history") or [])
            history.append(
                {
                    "at": timestamp,
                    "mode": mode,
                    "verdict": "REVISE",
                    "issues": [item.to_dict() for item in verdict.issues],
                    "evidence_id": evidence_id,
                    "verification_result_id": verification_id,
                    "proposed_plan_sha256": expected_digest,
                }
            )
            change["plan_verification_history"] = history
            exhausted = len(
                [
                    item
                    for item in history
                    if item.get("verdict") == "REVISE"
                ]
            ) > MAX_SEMANTIC_PLAN_REVISIONS
            if exhausted:
                change["status"] = "REJECTED"
                change["completed_at"] = timestamp
                state.active_plan_change_id = None
                _stop_run(
                    cfg,
                    state,
                    phase="PLAN_VERIFICATION_REJECTED",
                    reason=issue_payload,
                    summary="The plan verifier refused the proposed plan every time.",
                    at=timestamp,
                    system_state={"plan_change_id": str(change.get("id") or "")},
                )
                descriptors = ()
            else:
                change["status"] = "DRAINING"
                for key in (
                    "proposed_plan",
                    "proposed_plan_sha256",
                    "verification_mode",
                    "plan_verifier_session_token",
                ):
                    change.pop(key, None)
                descriptors = _reserve_in_state(
                    cfg,
                    current_plan,
                    state,
                    memory_audit_before=memory.audit_highwater(),
                    relay_owner_thread_id=thread_id,
                    now_epoch=now_epoch,
                )
            _record_successors(current, descriptors, dispatcher_authorized)
            _finish_global_state(
                current_plan,
                state,
                descriptors,
                paused=store.pause_requested(),
                cfg=cfg,
            )
            store.save(state)
            done = False
        else:
            if receipt is None:  # pragma: no cover - PASS guarantees it
                raise DesktopLifecycleError(
                    "PASS plan verdict did not produce a verification receipt"
                )
            reconcile_plan_change_state(
                current_plan,
                candidate,
                state,
                request_id=request_id,
                requester_task_id=str(change["requester_task_id"]),
                at=timestamp,
            )
            state.plan_verification = receipt.to_dict()
            if mode == FULL_PLAN_REVALIDATION:
                state.accepted_plan_patches_since_full_revalidation = 0
            else:
                state.accepted_plan_patches_since_full_revalidation += 1
            change["plan_verification"] = receipt.to_dict()
            history = list(change.get("plan_verification_history") or [])
            history.append(
                {
                    "at": timestamp,
                    "mode": mode,
                    "verdict": "PASS",
                    "evidence_id": evidence_id,
                    "verification_result_id": verification_id,
                    "proposed_plan_sha256": expected_digest,
                }
            )
            change["plan_verification_history"] = history
            append_resilience_event(
                state,
                "plan_verified",
                at=timestamp,
                task_id=task_id,
                plan_change_id=request_id,
                detail={
                    "mode": mode,
                    "graph_version": candidate.graph_version,
                    "plan_sha256": expected_digest,
                    "verification_result_id": verification_id,
                },
            )
            _sync_legacy_cursor(candidate, state)
            descriptors = _reserve_in_state(
                cfg,
                candidate,
                state,
                memory_audit_before=memory.audit_highwater(),
                relay_owner_thread_id=thread_id,
                now_epoch=now_epoch,
            )
            _record_successors(current, descriptors, dispatcher_authorized)
            _finish_global_state(
                candidate,
                state,
                descriptors,
                paused=store.pause_requested(),
                cfg=cfg,
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

    if verdict.verdict == "PASS":
        mark_roadmap(cfg.root, candidate, completed, language=cfg.language)
        if candidate.legacy_serial and not done:
            select_milestone(
                cfg.state_dir, candidate, next_index, language=cfg.language
            )
    _materialize(descriptors)
    return CompletionOutcome(
        True,
        "PLAN_VERIFIED" if verdict.verdict == "PASS" else "PLAN_REVISION_REQUIRED",
        descriptors,
        done,
    )


def _complete_session_identity(
    state: Any,
    session: dict[str, Any],
    *,
    thread_id: str,
    turn_id: str,
    final_status: str,
    timestamp: str,
) -> None:
    session["turn_id"] = turn_id
    session["status"] = "COMPLETED"
    session["final_status"] = final_status
    session["completed_at"] = timestamp
    _bind_resource_identity(
        state,
        str(session["resource_ownership_token"]),
        thread_id=thread_id,
        turn_id=turn_id,
    )
    _append_event(state, "turn_identity_bound", session, timestamp)
    _append_event(state, "turn_completed", session, timestamp, detail=final_status)


def _record_successors(
    session: dict[str, Any],
    descriptors: tuple[Any, ...],
    dispatcher_authorized: bool,
) -> None:
    if not dispatcher_authorized:
        return
    session["automatic_successor_tokens"] = [
        item.reservation_token for item in descriptors
    ]
    session["automatic_dispatch_state"] = (
        "ADVANCING" if descriptors else "COMPLETED"
    )
