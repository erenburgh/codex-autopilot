from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shlex
import time
import uuid
from typing import Any, Callable, Mapping

from .config import Config, DESKTOP_OWNED_SURFACE
from .language import is_russian
from .memory import ProjectMemory
from .models import next_effort_step
from .plan import Plan, VerificationCheck, atomic_json, load_plan
from .resilience import (
    RuntimeReconciliation,
    active_plan_change,
    append_resilience_event,
    reconcile_running_work,
    recover_plan_change_transaction,
)
from .resources import (
    DurableResourceLock,
    LockOwner,
    ResourceLockCoordinator,
)
from .run_state import RunState, StateStore, utc_now
from .task_state import (
    TaskState,
    transition_task,
)
from .verification import (
    DeterministicCheckResult,
    VerificationIssue,
    VerificationProtocolError,
)


DESCRIPTOR_SCHEMA_VERSION = 2
DESKTOP_SLOT_READY = "AUTOPILOT_SLOT_READY"
WORKSPACE_HANDOFF_OK = "AUTOPILOT_WORKSPACE_READY"
WORKSPACE_HANDOFF_PROMPT = (
    "Codex Autopilot workspace handoff. Do not inspect or modify files and do not call tools. "
    f"Reply exactly: {WORKSPACE_HANDOFF_OK}"
)
PENDING_SESSION_STATUSES = frozenset(
    {
        "RESERVED",
        "CREATE_REQUESTED",
        "RELAYING",
        "CREATED",
        "PREPARING",
        "PREPARED",
        "SEND_RELAYING",
        "ACTIVE",
        "AMBIGUOUS",
    }
)
RELAYABLE_SESSION_STATUSES = frozenset(
    {"CREATE_REQUESTED", "CREATED", "PREPARING", "PREPARED"}
)
SUCCESS_STATUSES = frozenset({"ROTATE", "DONE"})
ALLOWED_STATUSES = frozenset({"ROTATE", "DONE", "BLOCKED", "ESCALATE"})
IMPLEMENTATION_SESSION_KINDS = frozenset({"worker", "implementation"})
SESSION_KINDS = IMPLEMENTATION_SESSION_KINDS | frozenset(
    {"verifier", "revision", "replanner", "plan_verifier", "pipeline_engineer", "screening"}
)


class DesktopLifecycleError(RuntimeError):
    pass


class WorkerProtocolError(DesktopLifecycleError):
    """The worker finished its turn but shaped the reply against the protocol.

    A model error, not a machine fault. The difference is not cosmetic: on
    the hook path such a refusal goes back to the worker as a
    `decision: block` line and is fixed within the same turn - for free.
    On the automatic path the turn is already over and cannot be returned
    to, and the exception used to propagate: the dispatcher crashed, a
    PIPELINE ticket opened, an engineer was raised.

    Measured on 16 Sep 2026 on a live run: worker M8 did its work, got the
    final line wrong - and the whole run stood. Nobody was left to accept
    the completion, the session hung active, and a human was needed. The
    on-call engineer, working it out, opened an R31 rule conflict: the
    runtime rejected an already finished worker at a late check, i.e. threw
    away done work at a formatting gate.

    A separate class lets the automatic path tell "the model shaped the
    reply badly" from "the transport broke" and treat the first as a failed
    attempt of the task - record the reason, grant a retry - rather than as
    an infrastructure emergency.
    """



@dataclass(frozen=True, slots=True)
class LaunchDescriptor:
    schema_version: int
    surface: str
    run_id: str
    graph_version: int
    task_id: str
    task_title: str
    kind: str
    attempt: int
    worker_sequence: int
    reservation_token: str
    operation_id: str
    client_user_message_id: str
    desktop_project_id: str
    cwd: str
    title: str
    prompt: str
    model: str | None
    thinking: str | None
    execution_mode: str
    created_at: str
    prep_app_server_exited_at: str
    descriptor_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LaunchDescriptor":
        return cls(**{name: raw[name] for name in cls.__dataclass_fields__})

    def create_thread_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": self.slot_prompt(),
            "title": self.title,
            "target": {
                "type": "project",
                "projectId": self.desktop_project_id,
                "environment": {"type": "local"},
            },
        }
        if self.model:
            payload["model"] = self.model
        if self.thinking:
            payload["thinking"] = self.thinking
        return payload

    def slot_prompt(self) -> str:
        return (
            "Codex Autopilot Desktop slot reservation. During this no-op turn, do not "
            "inspect or modify files and do not call tools. Reply exactly: "
            f"{DESKTOP_SLOT_READY}. After that exact reply, a trusted Codex Autopilot "
            "Stop hook may record the result and durable causal provenance. The user's "
            "authorization for the complete Autopilot run covers the fixed scheduler-selected "
            "task chain. Pipeline Engineer may repair a failed transport and re-arm the causal "
            "predecessor, but never creates, starts, forks, or messages the next task itself."
        )


@dataclass(frozen=True, slots=True)
class CompletionOutcome:
    matched: bool
    worker_status: str | None
    descriptors: tuple[LaunchDescriptor, ...]
    run_done: bool

# R13: a stop of the work is always named with a code from the closed
# list. A free string will not do: it can neither be routed on, nor
# counted, nor tell "the user must decide" from "the environment broke".
# ROTATE and DONE carry no code - success needs no reason.
WORKER_REASON_CODES = frozenset(
    {
        # A permission or access the worker may not take is required.
        "DANGEROUS_PERMISSION",
        # The needed resource, tool or credentials are missing.
        "MISSING_RESOURCE",
        # A verified dependency's output is unfit for this task.
        "DEPENDENCY_DEFECT",
        # The task contract contradicts itself or the plan.
        "CONTRADICTORY_CONTRACT",
        # The environment is broken beyond the task's authority.
        "ENVIRONMENT_FAILURE",
        # The decision belongs to the user: a product one.
        "PRODUCT_DECISION",
        # The decision belongs to the user: an architectural one.
        "ARCHITECTURE_DECISION",
        # The means of repair are exhausted.
        "RECOVERY_EXHAUSTED",
        # There was no code. That too is a fact, and is recorded as one.
        "UNSPECIFIED",
    }
)

_WORKER_STATUS_PATTERN = re.compile(
    r"(?m)^AUTOPILOT_STATUS:\s*(ROTATE|DONE|BLOCKED|ESCALATE)(?:\s+([A-Z_]+))?\s*$"
)


def parse_desktop_worker_status(message: str) -> tuple[str, str]:
    """Return (status, reason code) from the worker's final reply.

    R13. The function used to return only the status, and the stop reason
    went into ``last_error`` as a free string like "M9 worker returned
    BLOCKED" - that is, went nowhere. Now BLOCKED and ESCALATE carry a code
    from the closed list.

    A missing code does not fail the completion: the worker's turn is over,
    and a hard refusal here would jam the pipeline at exactly the moment
    something has already gone wrong. Such a case is recorded as
    ``UNSPECIFIED`` - an honest record, not a quiet pardon. An unknown code
    is another matter: a closed list anything can be appended to is not
    closed.
    """

    matches = _WORKER_STATUS_PATTERN.findall(message)
    last = next(
        (line.strip() for line in reversed(message.splitlines()) if line.strip()), ""
    )
    if len(matches) != 1:
        raise WorkerProtocolError(
            "Desktop worker final response must end with exactly one allowed "
            "AUTOPILOT_STATUS line"
        )
    status, raw_code = matches[0]
    expected = f"AUTOPILOT_STATUS: {status}"
    if raw_code:
        expected = f"{expected} {raw_code}"
    if last != expected:
        raise WorkerProtocolError(
            "Desktop worker final response must end with exactly one allowed "
            "AUTOPILOT_STATUS line"
        )
    if status in SUCCESS_STATUSES:
        if raw_code:
            raise WorkerProtocolError(
                "AUTOPILOT_STATUS ROTATE and DONE carry no reason code"
            )
        return status, ""
    code = raw_code or "UNSPECIFIED"
    if code not in WORKER_REASON_CODES:
        raise DesktopLifecycleError(
            f"unknown AUTOPILOT_STATUS reason code {code!r}; allowed: "
            + ", ".join(sorted(WORKER_REASON_CODES))
        )
    return status, code

RULE_CONFLICT_PATTERN = re.compile(
    r"(?mi)^AUTOPILOT_RULE_CONFLICT:\s*(R\d+)\s*[-—:]\s*(.+?)\s*$"
)


def parse_rule_conflicts(message: str) -> tuple[tuple[str, str], ...]:
    """Rule R16: disagreement with a rule's wording is a Conflict.

    The worker may not resolve the disagreement itself: it either applies
    the rule as written or names the disagreement, which goes to Project
    Memory as a conflict. Silent reinterpretation is precisely how a rule
    stops being a rule.

    Returns (rule id, disagreement wording) pairs in order of appearance,
    without repeats by id.
    """

    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rule_id, detail in RULE_CONFLICT_PATTERN.findall(message):
        key = rule_id.upper()
        if key in seen or not detail.strip():
            continue
        seen.add(key)
        found.append((key, detail.strip()))
    return tuple(found)


APPLIED_RULES_PATTERN = re.compile(r"(?mi)^AUTOPILOT_RULES:\s*(.+?)\s*$")


def parse_applied_rules(message: str) -> tuple[str, ...]:
    """Rule R16: the report lists the ids of the rules applied to the task.

    Rules reach the worker as a structure with stable ids (R17), and the
    report must cite them by the same ids. A report without the list is a
    defect: without it "rule applied" cannot be told from "rule not read".

    Returns the ids found in order of appearance, without repeats. An empty
    tuple means there is no list.
    """

    found: list[str] = []
    for line in APPLIED_RULES_PATTERN.findall(message):
        for token in re.findall(r"\bR\d+\b", line.upper()):
            if token not in found:
                found.append(token)
    return tuple(found)


def pause_desktop_run(cfg: Config, *, at: str | None = None) -> None:
    """Persist a drain pause before returning control to the user.

    Existing Desktop turns continue and retain their locks. Their Stop hooks may
    record completion, but no new reservation is admitted while the pause marker
    exists. An explicit Interrupt remains authoritative and moves only that task
    to RETRY_WAIT.
    """

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    store.request_pause()
    coordinator = ResourceLockCoordinator(store, cfg.root)
    timestamp = at or utc_now()
    with coordinator.transaction():
        state = store.load()
        append_resilience_event(
            state,
            "pause_requested",
            at=timestamp,
            detail={"semantics": "drain", "active_task_ids": list(state.active_task_ids)},
        )
        if state.status != "DONE":
            state.status = "PAUSED"
            state.phase = "PAUSED_DRAINING" if state.active_task_ids else "PAUSED"
        store.save(state)

def relay_session_status(cfg: Config, reservation_token: str) -> dict[str, Any]:
    """Read the durable phase that determines the relay's next fixed action."""

    _require_desktop_owned(cfg)
    state = StateStore(cfg.state_dir).load()
    session = _session_by_token(state, reservation_token)
    status = str(session["status"])
    prep_failure = str(session.get("prep_failure_reason") or "")
    release_attempts = int(session.get("writer_release_attempts") or 0)
    created_action = "await_trusted_hook_cwd_prep"
    if "already has an active writer" in prep_failure:
        created_action = (
            "release_active_writer"
            if release_attempts == 0
            else "active_writer_release_exhausted"
        )
    action = {
        "CREATE_REQUESTED": "create_with_app_server",
        "CREATED": created_action,
        "PREPARING": "await_trusted_hook_cwd_prep",
        "PREPARED": "claim_production_send",
        "ACTIVE": "done",
        "COMPLETED": "done",
        "RELAYING": "reconcile_create_do_not_repeat",
        "SEND_RELAYING": "reconcile_send_do_not_repeat",
        "AMBIGUOUS": "reconcile_do_not_repeat",
    }.get(status, "stop")
    result = {
        "reservation_token": reservation_token,
        "task_id": session["task_id"],
        "status": status,
        "next_action": action,
        "thread_id": session.get("thread_id"),
        "actual_cwd": session.get("actual_cwd"),
        "prep_app_server_exited_at": session.get("prep_app_server_exited_at"),
        "writer_release_attempts": release_attempts,
        "relay_owner_thread_id": session.get("relay_owner_thread_id"),
    }
    if status == "ACTIVE":
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        result["visible_report"] = str(
            session.get("visible_launch_report")
            or relay_success_report(
                descriptor,
                thread_id=str(session.get("thread_id") or ""),
                status="ACTIVE",
                language=cfg.language,
            )
        )
    return result

def _dispatcher_owns_reservation(
    session: dict[str, Any],
    *,
    dispatcher_pid: int | None,
) -> bool:
    """Prove this process is the dispatcher already spawned for this reservation."""

    return bool(
        dispatcher_pid
        and dispatcher_pid == os.getpid()
        and session.get("automatic_dispatch_state") == "RUNNING"
        and session.get("automatic_dispatch_pid") == dispatcher_pid
    )

def _wait_for_dispatcher_ownership(
    cfg: Config,
    reservation_token: str,
    *,
    owner_thread_id: str,
    timeout: float = 5.0,
) -> bool:
    """Wait for the spawning parent to durably bind this child PID."""

    deadline = time.monotonic() + timeout
    while True:
        state = StateStore(cfg.state_dir).load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, owner_thread_id)
        if _dispatcher_owns_reservation(session, dispatcher_pid=os.getpid()):
            return True
        if (
            session.get("automatic_dispatch_state") != "SCHEDULED"
            or session.get("automatic_dispatch_pid") is not None
            or time.monotonic() >= deadline
        ):
            return False
        time.sleep(0.02)

def acknowledge_desktop_send(
    cfg: Config,
    reservation_token: str,
    *,
    thread_id: str,
    at: str | None = None,
    relay_executor_thread_id: str | None = None,
) -> LaunchDescriptor:
    """Acknowledge production turn/start and register Desktop Stop waiting."""

    _require_desktop_owned(cfg)
    timestamp = at or utc_now()
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    with coordinator.transaction():
        state = store.load()
        session = _session_by_token(state, reservation_token)
        _require_relay_executor(session, relay_executor_thread_id)
        if session.get("thread_id") != thread_id:
            raise DesktopLifecycleError("production send acknowledged an unexpected Desktop task")
        if session["status"] == "ACTIVE":
            return LaunchDescriptor.from_dict(dict(session["descriptor"]))
        if session["status"] != "SEND_RELAYING":
            raise DesktopLifecycleError("reservation is not awaiting production-send acknowledgement")
        session["status"] = "ACTIVE"
        session["start_acknowledged_at"] = timestamp
        for event in ("start_acknowledged", "wait_registered"):
            _append_event(state, event, session, timestamp)
        descriptor = LaunchDescriptor.from_dict(dict(session["descriptor"]))
        session["visible_launch_report"] = relay_success_report(
            descriptor,
            thread_id=thread_id,
            status="ACTIVE",
            language=cfg.language,
        )
        _append_event(state, "visible_launch_report_ready", session, timestamp)
        if _session_kind(session) == "replanner":
            change = active_plan_change(
                state,
                request_id=str(session.get("plan_change_id") or ""),
            )
            change["status"] = "REPLANNING"
            append_resilience_event(
                state,
                "replanner_active",
                at=timestamp,
                task_id=str(session["task_id"]),
                plan_change_id=str(change["id"]),
                detail={"thread_id": thread_id},
            )
        state.current_thread_id = thread_id
        state.phase = "DESKTOP_WORKERS_ACTIVE"
        state.status = "RUNNING"
        store.save(state)
        return descriptor

def relay_success_report(
    descriptor: LaunchDescriptor,
    *,
    thread_id: str,
    status: str,
    language: str,
) -> str:
    """Render the durable user-visible result of a successful production relay."""

    current_thread = str(thread_id or "").strip()
    if not current_thread:
        raise DesktopLifecycleError("visible relay report requires a Desktop thread ID")
    if status != "ACTIVE":
        raise DesktopLifecycleError("visible relay report requires ACTIVE launch status")
    if is_russian(language):
        return "\n".join(
            (
                "Codex Autopilot: the next task has been launched.",
                f"Next task: {descriptor.task_id}",
                f"Title: {descriptor.title}",
                f"Thread ID: {current_thread}",
                f"Launch status: {status}",
            )
        )
    return "\n".join(
        (
            "Codex Autopilot: next task launched.",
            f"Next task: {descriptor.task_id}",
            f"Title: {descriptor.title}",
            f"Thread ID: {current_thread}",
            f"Launch status: {status}",
        )
    )

def _record_deterministic_evidence(
    memory: ProjectMemory,
    task_id: str,
    checks: tuple[VerificationCheck, ...],
    results: tuple[DeterministicCheckResult, ...],
    *,
    provider_thread_id: str,
) -> None:
    by_id = {item.id: item for item in checks}
    for result in results:
        check = by_id[result.check_id]
        outcome = "PASS" if result.passed else "REVISE"
        common = {
            "summary": (
                f"Deterministic verification check {result.check_id} {outcome}: "
                f"{result.description}"
            ),
            "created_by": "codex-autopilot deterministic verifier",
            "milestone_id": task_id,
            "role": result.check_id,
            "provider": "deterministic-runtime",
            "provider_thread_id": provider_thread_id,
        }
        if result.kind == "command":
            memory.record_evidence(
                kind="test",
                command=shlex.join(check.argv),
                result=json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True),
                exit_code=result.exit_code if result.exit_code is not None else -1,
                **common,
            )
        elif result.kind == "artifact" and result.passed and result.path:
            memory.record_evidence(kind="artifact", path=result.path, **common)
        elif result.kind == "artifact":
            memory.record_evidence(
                kind="environment_probe",
                environment_probe=json.dumps(
                    result.to_dict(), ensure_ascii=False, sort_keys=True
                ),
                **common,
            )
        elif result.kind == "evidence" and not result.passed:
            memory.record_evidence(
                kind="environment_probe",
                environment_probe=json.dumps(
                    result.to_dict(), ensure_ascii=False, sort_keys=True
                ),
                **common,
            )

def _record_deterministic_verification_results(
    memory: ProjectMemory,
    task_id: str,
    results: tuple[DeterministicCheckResult, ...],
    evidence: list[dict[str, Any]],
    *,
    provider_thread_id: str,
    provider_turn_id: str,
) -> tuple[str, ...]:
    """Persist each deterministic check as an evidence-linked audit outcome."""

    verification_ids: list[str] = []
    for result in results:
        evidence_ids = [
            str(item["id"])
            for item in evidence
            if item.get("role") == result.check_id
        ]
        if not evidence_ids:
            raise DesktopLifecycleError(
                f"deterministic check {result.check_id} has no evidence with its exact check ID role"
            )
        verification = memory._record_runtime_verification_result(
            task_id=task_id,
            check_id=result.check_id,
            policy="deterministic",
            verdict="PASS" if result.passed else "REVISE",
            summary=(
                f"Deterministic verification check {result.check_id} "
                f"{'PASS' if result.passed else 'REVISE'}: {result.description}"
            ),
            evidence_ids=evidence_ids,
            created_by="codex-autopilot deterministic verifier",
            provider="deterministic-runtime",
            provider_thread_id=provider_thread_id,
            provider_turn_id=provider_turn_id,
            details=result.to_dict(),
        )
        verification_ids.append(str(verification["id"]))
    return tuple(verification_ids)

def _latest_completion_context(
    memory: ProjectMemory,
    state: RunState,
    task_id: str,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    completed = next(
        (
            item
            for item in reversed(state.worker_sessions)
            if item.get("task_id") == task_id
            and _session_kind(item) in IMPLEMENTATION_SESSION_KINDS | {"revision"}
            and item.get("status") == "COMPLETED"
        ),
        None,
    )
    if completed is None:
        raise DesktopLifecycleError(
            f"task {task_id} has no completed implementation for verification"
        )
    evidence_ids = {
        str(item) for item in completed.get("completion_evidence_ids", []) if item
    }
    records = tuple(
        item
        for item in memory.milestone_evidence(task_id, limit=100)
        if str(item.get("id")) in evidence_ids
    )
    checks = completed.get("deterministic_results", [])
    if not isinstance(checks, list) or not all(isinstance(item, dict) for item in checks):
        raise DesktopLifecycleError("persisted deterministic results are malformed")
    return records, tuple(dict(item) for item in checks)

def _latest_verification_issues(
    state: RunState,
    task_id: str,
) -> tuple[VerificationIssue, ...]:
    raw = next(
        (
            item.get("verification_issues")
            for item in reversed(state.worker_sessions)
            if item.get("task_id") == task_id and item.get("verification_issues")
        ),
        None,
    )
    if not isinstance(raw, list):
        return ()
    try:
        return tuple(VerificationIssue.from_dict(item) for item in raw)
    except VerificationProtocolError as exc:
        raise DesktopLifecycleError("persisted verification issues are malformed") from exc

def _latest_task_session(state: RunState, task_id: str) -> dict[str, Any]:
    session = next(
        (item for item in reversed(state.worker_sessions) if item.get("task_id") == task_id),
        None,
    )
    if session is None:
        raise DesktopLifecycleError(f"task {task_id} has no lifecycle session")
    return session

def task_effort(plan: Plan, state: RunState, task_id: str) -> str:
    """The task's effort step, re-hires included."""

    assigned = state.task_effort.get(task_id)
    if assigned:
        return assigned
    return plan.task_map[task_id].reasoning or "medium"


def _rehire_or_block_on_revision_limit(
    plan: Plan,
    state: RunState,
    task_id: str,
    session: dict[str, Any],
    at: str,
) -> bool:
    """The revision budget is exhausted - re-hire the executor, do not stop.

    Neither the plan nor the bar changes; what changes is the way the result
    is reached and who reaches it: the task gets a fresh worker at the next
    effort step and the whole accumulated list of verification complaints.
    The Definition of Done, the deterministic checks and the graph stay the
    same - or acceptance would move to meet the work, not the other way.

    Returns True only when the hiring ladder has run out. That is the only
    case where the task really stops: by the incident taxonomy the
    PRODUCTION class belongs to the product owner, and automatic quality
    repair is forbidden here. Neighbouring tasks that do not depend on this
    one keep going - a re-hire merges nothing and touches no shared graph.
    """

    task = plan.task_map[task_id]
    used = int(state.task_revisions.get(task_id, 0))
    hires = int(state.task_rehires.get(task_id, 0))
    maximum = task.verification.max_revision_attempts
    # The budget is issued afresh to every hire, while the revision counter
    # stays continuous: otherwise the attempt history and R{n} numbering
    # are lost.
    if used < maximum * (hires + 1):
        return False

    current = task_effort(plan, state, task_id)
    promoted = next_effort_step(current)
    if promoted is not None:
        state.task_rehires[task_id] = hires + 1
        state.task_effort[task_id] = promoted
        _append_event(
            state,
            "task_rehired",
            session,
            at,
            detail=json.dumps(
                {
                    "hire": hires + 1,
                    "effort_from": current,
                    "effort_to": promoted,
                    "revision_attempts": used,
                    "max_revision_attempts": maximum,
                },
                sort_keys=True,
            ),
        )
        return False

    if state.task_states[task_id] == TaskState.REVISION_REQUIRED.value:
        state.task_states = transition_task(
            plan, state.task_states, task_id, TaskState.BLOCKED
        )
    state.last_error = (
        f"{task_id} exhausted the hiring ladder: {hires + 1} hire(s) up to "
        f"effort {current}, {used} revision attempt(s)"
    )
    _append_event(
        state,
        "hiring_ladder_exhausted",
        session,
        at,
        detail=json.dumps(
            {
                "revision_attempts": used,
                "max_revision_attempts": maximum,
                "hires": hires + 1,
                "effort": current,
            },
            sort_keys=True,
        ),
    )
    return True

def _session_kind(session: dict[str, Any]) -> str:
    kind = str(session.get("kind") or "worker")
    if kind not in SESSION_KINDS:
        raise DesktopLifecycleError(f"unknown Desktop session kind: {kind}")
    return kind

def _append_event(
    state: RunState,
    event: str,
    session: dict[str, Any],
    at: str,
    *,
    detail: str | None = None,
    turn_id: str | None = None,
) -> None:
    state.lifecycle_journal_sequence += 1
    state.lifecycle_journal.append(
        {
            "sequence": state.lifecycle_journal_sequence,
            "event": event,
            "operation_id": str(session["operation_id"]),
            "task_id": str(session["task_id"]),
            "attempt": int(session["attempt"]),
            "reservation_token": str(session["reservation_token"]),
            "thread_id": session.get("thread_id"),
            "relay_owner_thread_id": session.get("relay_owner_thread_id"),
            "turn_id": turn_id if turn_id is not None else session.get("turn_id"),
            "client_user_message_id": session.get("client_user_message_id"),
            "at": at,
            "detail": detail,
        }
    )

def _synthetic_session(state: RunState) -> dict[str, Any]:
    task_id = state.milestone_id or next(iter(state.task_states), "PREP")
    attempt = max(1, int(state.task_attempts.get(task_id, 0) or 1))
    token = _stable_id(state, f"prep:{state.lifecycle_journal_sequence + 1}")
    return {
        "operation_id": _stable_id(state, f"prep-operation:{token}"),
        "task_id": task_id,
        "attempt": attempt,
        "reservation_token": token,
        "thread_id": None,
        "turn_id": None,
        "client_user_message_id": None,
    }

def _session_by_token(state: RunState, token: str) -> dict[str, Any]:
    matches = [item for item in state.worker_sessions if item.get("reservation_token") == token]
    if len(matches) != 1:
        raise DesktopLifecycleError("unknown or non-unique reservation token")
    return matches[0]

def _require_relay_executor(
    session: dict[str, Any],
    relay_executor_thread_id: str | None,
) -> None:
    """Bind every relay mutation to the causal predecessor task.

    Direct in-process callers may omit the identity at the deterministic test
    boundary. Production CLI commands always supply ``CODEX_THREAD_ID`` and may
    mutate only the reservation owned by that exact Desktop task.
    """

    if relay_executor_thread_id is None:
        return
    owner = str(session.get("relay_owner_thread_id") or "")
    executor = str(relay_executor_thread_id or "")
    if not owner:
        raise DesktopLifecycleError(
            "relay reservation has no bound owner thread; reconcile fail-closed"
        )
    if not executor:
        raise DesktopLifecycleError(
            "relay mutation requires the current Codex thread identity"
        )
    if executor != owner:
        raise DesktopLifecycleError(
            "relay executor thread does not match the reservation owner"
        )

# M10-REV-006: the status of a session displaced by a replacement of the same task.
RETIRED_SUPERSEDED = "RETIRED_SUPERSEDED"

# Statuses after which a session can no longer change anything.
_TERMINAL_SESSION_STATUSES = {"COMPLETED", "BLOCKED", RETIRED_SUPERSEDED}

# M11-PRE-SIDE-EFFECT-FENCE. A retired Desktop task stays addressable: its
# thread is still there, one can write into it, and the model would keep
# working as a worker on a reservation that no longer exists.
#
# The set is wider than session_is_fenced's on purpose. That one takes part
# in turn completion and changes the semantics of a finished path; this one
# only refuses at the entrance, before a single side effect, and so can
# list every kind of retirement, not one.
RETIRED_SESSION_STATUSES = frozenset(
    {
        RETIRED_SUPERSEDED,
        "RETIRED_USER_ARCHIVED_RECREATE",
        "RETIRED_INCOMPATIBLE_TRANSPORT",
        "CANCELLED_TRANSPORT_MIGRATION",
    }
)


def retired_session_for_thread(
    state: RunState, thread_id: str
) -> dict[str, Any] | None:
    """The latest retired session of this thread, if any.

    An active session outranks it: the same thread may have been retired
    and taken up again, and an active task must not be refused because of
    its own past.
    """

    if not thread_id:
        return None
    live = [
        item
        for item in state.worker_sessions
        if item.get("thread_id") == thread_id
        and item.get("status") not in RETIRED_SESSION_STATUSES
        and item.get("status") in PENDING_SESSION_STATUSES
    ]
    if live:
        return None
    retired = [
        item
        for item in state.worker_sessions
        if item.get("thread_id") == thread_id
        and item.get("status") in RETIRED_SESSION_STATUSES
    ]
    return retired[-1] if retired else None


def fence_superseded_sessions(
    state: RunState,
    task_id: str,
    *,
    at: str,
    reason: str,
) -> list[dict[str, Any]]:
    """Fence the task's previous sessions before a replacement takes resources.

    Observed during the M10 audit itself: the original reservation stayed
    in RETRY_WAIT, the new one became ACTIVE, and the interrupted Desktop
    task kept changing the same working tree - source and test mtimes
    changed during the audit.
    """
    fenced: list[dict[str, Any]] = []
    for session in state.worker_sessions:
        if session.get("task_id") != task_id:
            continue
        if session.get("status") in _TERMINAL_SESSION_STATUSES:
            continue
        # What must be fenced is what can really continue producing: an
        # addressable Desktop task. A session with no bound thread was never
        # created, has nothing to continue, and stays available for normal
        # DevOps repair and relaunch.
        if not str(session.get("thread_id") or "").strip():
            continue
        session["status"] = RETIRED_SUPERSEDED
        session["retired_at"] = at
        session["retired_reason"] = reason
        session["automatic_dispatch_state"] = RETIRED_SUPERSEDED
        session["automatic_dispatch_pid"] = None
        session["automatic_dispatch_connection_pid"] = None
        fenced.append(session)
        _append_event(state, "session_retired_superseded", session, at, detail=reason)
    return fenced

def session_is_fenced(session: dict[str, Any]) -> bool:
    return session.get("status") == RETIRED_SUPERSEDED

def _active_session_by_thread(state: RunState, thread_id: str) -> dict[str, Any] | None:
    matches = [
        item
        for item in state.worker_sessions
        if item.get("thread_id") == thread_id and item.get("status") == "ACTIVE"
    ]
    if len(matches) > 1:
        raise DesktopLifecycleError("Desktop thread has multiple active reservations")
    if matches:
        return matches[0]
    # M10-REV-006: a displaced session does not stay silent, it fails
    # closed. Its Desktop task stays addressable, and without this it would
    # keep producing next to the active attempt of the same task.
    superseded = [
        item
        for item in state.worker_sessions
        if item.get("thread_id") == thread_id and session_is_fenced(item)
    ]
    if superseded:
        retired = superseded[-1]
        raise DesktopLifecycleError(
            f"Desktop task {thread_id} was superseded by a replacement for "
            f"{retired.get('task_id')} and must not continue production: "
            f"{retired.get('retired_reason') or 'retired'}"
        )
    return None

def _latest_implementation_thread_id(state: RunState, task_id: str) -> str:
    candidates = [
        item
        for item in state.worker_sessions
        if item.get("task_id") == task_id
        and _session_kind(item) in IMPLEMENTATION_SESSION_KINDS | {"revision"}
        and item.get("status") == "COMPLETED"
        and item.get("final_status") in SUCCESS_STATUSES
        and item.get("thread_id")
    ]
    if not candidates:
        raise DesktopLifecycleError(
            f"task {task_id} has no completed implementation predecessor"
        )
    latest = max(candidates, key=lambda item: int(item.get("worker_sequence") or 0))
    return str(latest["thread_id"])

def _bind_resource_identity(
    state: RunState,
    ownership_token: str,
    *,
    thread_id: str,
    turn_id: str | None = None,
) -> None:
    updated: list[dict[str, Any]] = []
    for raw in state.resource_locks:
        lock = DurableResourceLock.from_dict(raw)
        if lock.owner.ownership_token == ownership_token:
            owner = LockOwner.create(
                run_id=lock.owner.run_id,
                task_id=lock.owner.task_id,
                attempt=lock.owner.attempt,
                worker_id=lock.owner.worker_id,
                thread_id=thread_id,
                turn_id=turn_id or lock.owner.turn_id,
                ownership_token=lock.owner.ownership_token,
            )
            lock = DurableResourceLock(
                lock_id=lock.lock_id,
                owner=owner,
                claims=lock.claims,
                acquired_at=lock.acquired_at,
                heartbeat_at=lock.heartbeat_at,
                computer_use_slot=lock.computer_use_slot,
            )
        updated.append(lock.to_dict())
    state.resource_locks = updated

def _finish_global_state(
    plan: Plan,
    state: RunState,
    descriptors: tuple[LaunchDescriptor, ...],
    *,
    paused: bool = False,
) -> None:
    if all(value == TaskState.VERIFIED.value for value in state.task_states.values()):
        state.status = "DONE"
        state.phase = "DONE"
        state.completed_at = utc_now()
        return
    if paused:
        state.status = "PAUSED"
        state.phase = "PAUSED_DRAINING" if state.active_task_ids else "PAUSED"
        return
    if state.active_plan_change_id is not None:
        if descriptors:
            state.status = "RUNNING"
            state.phase = "AWAITING_DESKTOP_CREATE"
        elif state.active_task_ids:
            replanning = any(
                item.get("kind") == "replanner"
                and item.get("task_id") in state.active_task_ids
                and item.get("status") in PENDING_SESSION_STATUSES
                for item in state.worker_sessions
            )
            state.status = "RUNNING"
            state.phase = "PLAN_CHANGE_REPLANNING" if replanning else "PLAN_CHANGE_DRAINING"
        elif state.rate_limit_until is not None:
            state.status = "WAITING"
            state.phase = "WAITING_RATE_LIMIT"
        else:
            state.status = "WAITING"
            state.phase = "PLAN_CHANGE_WAITING_LOCKS"
        return
    if descriptors:
        state.status = "RUNNING"
        state.phase = "AWAITING_DESKTOP_CREATE"
    elif state.active_task_ids:
        state.status = "RUNNING"
        state.phase = "DESKTOP_WORKERS_ACTIVE"
    elif state.rate_limit_until is not None or any(
        value == TaskState.RETRY_WAIT.value for value in state.task_states.values()
    ):
        state.status = "WAITING"
        state.phase = "WAITING_RATE_LIMIT"
    elif any(value == TaskState.BLOCKED.value for value in state.task_states.values()):
        state.status = "BLOCKED"
        state.phase = "BLOCKED"
    else:
        state.status = "WAITING"
        state.phase = "WAITING_DEPENDENCIES"

def _sync_legacy_cursor(plan: Plan, state: RunState) -> None:
    for index, task in enumerate(plan.tasks):
        if state.task_states[task.id] != TaskState.VERIFIED.value:
            state.milestone_index = index
            state.milestone_id = task.id
            return
    state.milestone_index = len(plan.tasks) - 1
    state.milestone_id = plan.tasks[-1].id

def _verified_prefix(plan: Plan, state: RunState) -> int:
    count = 0
    for task in plan.tasks:
        if state.task_states[task.id] != TaskState.VERIFIED.value:
            break
        count += 1
    return count

def _checkpoint(path: Path) -> str:
    if not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()

def task_checkpoint_path(state_dir: Path, task_id: str) -> Path:
    """The per-task handoff file (M10-REV-005).

    The completion gate used to be the shared .codex-autopilot/HANDOFF.md:
    all tasks reserved in parallel got ONE hash of that file, and the first
    worker to write it closed the gate for everyone else. Parallel writes
    to one file also lost changes, although the tasks' resources did not
    overlap.

    Now every task has its own file, and the gate checks exactly that one.
    HANDOFF.md remains a shared note for the human and is not a gate.
    """
    return state_dir / "handoff" / f"{_checkpoint_slug(task_id)}.md"

def _checkpoint_slug(task_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(task_id)).strip("-")
    if not slug:
        raise DesktopLifecycleError(f"task id has no usable checkpoint name: {task_id!r}")
    return slug

def task_checkpoint(state_dir: Path, task_id: str) -> str:
    return _checkpoint(task_checkpoint_path(state_dir, task_id))

def _thread_cwd(thread: dict[str, Any]) -> Path | None:
    raw = thread.get("cwd")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).expanduser().resolve()

def _contains_exact_text(value: Any, expected: str) -> bool:
    if isinstance(value, str):
        return _text_matches_expected(value, expected)
    if isinstance(value, dict):
        return any(_contains_exact_text(item, expected) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_exact_text(item, expected) for item in value)
    return False

def _text_matches_expected(value: str, expected: str) -> bool:
    actual = value.strip()
    wanted = expected.strip()
    if actual == wanted:
        return True
    redacted = re.fullmatch(
        r"<redacted chars=(\d+) sha256=([0-9a-fA-F]{64})>", actual
    )
    if redacted:
        return int(redacted.group(1)) == len(wanted) and redacted.group(2).lower() == (
            hashlib.sha256(wanted.encode("utf-8")).hexdigest()
        )
    for encoded in re.findall(r"<input>(.*?)</input>", actual, flags=re.DOTALL):
        if html.unescape(encoded).strip() == wanted:
            return True
    return False

def _client_process_exited(client: Any) -> bool:
    marker = getattr(client, "process_exited", None)
    if callable(marker):
        return bool(marker())
    if marker is not None:
        return bool(marker)
    proc = getattr(client, "proc", None)
    return proc is not None and proc.poll() is not None


def _stable_id(state: RunState, value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"codex-autopilot:{state.run_id}:{value}"))

def _materialize(descriptors: tuple[LaunchDescriptor, ...]) -> None:
    for descriptor in descriptors:
        payload = descriptor.to_dict()
        payload["create_thread_payload"] = descriptor.create_thread_payload()
        atomic_json(Path(descriptor.descriptor_path), payload)

def _require_desktop_owned(cfg: Config) -> None:
    if cfg.runtime.worker_surface != DESKTOP_OWNED_SURFACE:
        # Unreachable through load_config: there is one surface and it is
        # checked while parsing the config. The refusal is kept for a Config
        # built around the parser, so it names the value, not a removed
        # surface.
        raise DesktopLifecycleError(
            "Desktop lifecycle requires worker_surface="
            f"{DESKTOP_OWNED_SURFACE}; this run declares "
            f"{cfg.runtime.worker_surface!r}"
        )
    if not cfg.desktop.desktop_project_id:
        raise DesktopLifecycleError("desktop_owned requires desktop.desktop_project_id")

def _pid_alive(pid: int | None) -> bool:
    """Is the recorded dispatcher alive. A broken value is a refusal, not a guess.

    Both callers of this check are guards: they refuse to work while the
    dispatcher is alive. So an error in either direction is expensive.

    A negative pid went into os.kill(-N, 0), which signals a process GROUP:
    an unrelated live process in the group meant "dispatcher alive", and
    the run stood forever. A non-integer raised a TypeError that is not
    caught here.

    Treating such a value as dead is not allowed either: a second
    dispatcher would rise on top of a live one. The only honest answer is
    to name the corrupt record and stop.
    """

    if pid is None or pid == 0:
        return False
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 0:
        raise DesktopLifecycleError(
            f"the run state holds an unusable dispatcher_pid: {pid!r}"
        )
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def reconcile_desktop_runtime(
    cfg: Config,
    *,
    authoritative_states: dict[str, str] | None = None,
    now_epoch: int | None = None,
    at: str | None = None,
) -> RuntimeReconciliation:
    """Reconcile local plan commits, worker attempts, and resource ownership.

    Unknown or omitted external worker states remain locked. Only an explicit
    terminal/absent observation retires an attempt, and it retries rather than
    advancing the graph because no trusted completion protocol was observed.
    """
    # late import: breaks a circular module dependency
    from .lifecycle_reservations import _prepare_state

    _require_desktop_owned(cfg)
    store = StateStore(cfg.state_dir)
    coordinator = ResourceLockCoordinator(store, cfg.root)
    epoch = int(time.time()) if now_epoch is None else now_epoch
    with coordinator.transaction():
        recover_plan_change_transaction(cfg.state_dir, cfg.profile)
        plan = load_plan(cfg.state_dir, cfg.profile)
        state = store.load()
        if state.graph_version != plan.graph_version:
            raise DesktopLifecycleError("resume reconciliation found a graph version mismatch")
        for session in state.worker_sessions:
            if (
                session.get("status") == "RETRY_WAIT"
                and session.get("automatic_dispatch_state") == "RUNNING"
            ):
                dispatcher_pid = session.get("automatic_dispatch_pid")
                if _pid_alive(dispatcher_pid):
                    raise DesktopLifecycleError(
                        "retry-wait task still has a live automatic dispatcher"
                    )
                session["automatic_dispatch_state"] = "RETRY_WAIT"
                session["automatic_dispatch_pid"] = None
                session["automatic_dispatch_connection_pid"] = None
                _append_event(
                    state,
                    "stale_automatic_dispatcher_reconciled",
                    session,
                    at or utc_now(),
                    detail="dead dispatcher identity cleared during resume",
                )
        result = reconcile_running_work(
            plan,
            state,
            authoritative_states or {},
            now_epoch=epoch,
            retry_delay_seconds=cfg.retry.initial_seconds,
            at=at,
        )
        _prepare_state(plan, state, now_epoch=epoch)
        _finish_global_state(
            plan,
            state,
            (),
            paused=store.pause_requested(),
        )
        store.save(state)
    return result


def observe_worker_states(
    cfg: Config,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, str]:
    """Ask the server whether the turns of open sessions are alive.

    The observations ``reconcile_desktop_runtime`` lacked to work outside
    tests. The recovery function existed, was exported and was called only
    from tests - so a dead session never returned to work, and the task
    stayed in VERIFYING forever.

    A mapping: ownership token -> one of "active", "terminal", "absent",
    "unknown". Silence and a connection error yield "unknown", and such a
    session is retained rather than retried: "I do not know" must not read
    as "it ended".
    """

    from .appserver import AppServerClient

    store = StateStore(cfg.state_dir)
    state = store.load()
    pending = [
        item
        for item in state.worker_sessions
        if item.get("status") in PENDING_SESSION_STATUSES
    ]
    if not pending:
        return {}
    factory = client_factory or AppServerClient
    log_path = cfg.state_dir / "logs" / "observe-workers.jsonl"
    observations: dict[str, str] = {}
    try:
        with factory(cfg.desktop.binary, log_path) as client:
            for session in pending:
                token = str(
                    session.get("resource_ownership_token")
                    or session.get("reservation_token")
                    or ""
                )
                if not token:
                    continue
                observations[token] = _observe_one(client, session)
    except Exception:
        # No connection - no observations. An empty mapping retains everything.
        return {}
    return observations


def _observe_one(client: Any, session: Mapping[str, Any]) -> str:
    thread_id = str(session.get("thread_id") or "")
    if not thread_id:
        # There was no thread: creation never happened. This is not "gone",
        # it is "never appeared", and reconciliation must not touch it.
        return "unknown"
    try:
        thread = client.read_thread(thread_id)
    except Exception:
        return "unknown"
    if not thread:
        return "absent"
    status = thread.get("status")
    kind = str(status.get("type") or "") if isinstance(status, Mapping) else ""
    if kind in _LIVE_THREAD_STATUS:
        return "active"
    if kind in _FINISHED_THREAD_STATUS:
        return "terminal"
    return "unknown"


# What the server answers about a thread's turn. Measured on live run
# threads: a completed unloaded one returns "notLoaded", a completed loaded
# one "idle". An unfamiliar value stays "unknown": the list grows
# deliberately, not by guessing on the fly.
_LIVE_THREAD_STATUS = frozenset({"running", "busy", "streaming", "active"})
_FINISHED_THREAD_STATUS = frozenset({"idle", "notLoaded", "completed", "failed"})


def pending_descriptors(cfg: Config) -> tuple[LaunchDescriptor, ...]:
    state = StateStore(cfg.state_dir).load()
    return tuple(
        LaunchDescriptor.from_dict(dict(item["descriptor"]))
        for item in state.worker_sessions
        if item.get("status") in PENDING_SESSION_STATUSES
        and isinstance(item.get("descriptor"), dict)
    )


def creation_causality_coverage(state: RunState) -> tuple[int, int]:
    """How many creations the audit can assess, and how many there are.

    The run journal survives runtime restarts, and the relay_owner_thread_id
    field was not in the event schema from day one. Events recorded before
    it appeared are not assessed by the audit - they lack the data, their
    causality is not broken. The function makes that blind spot measurable,
    so "no violations" cannot be confused with "never checked".
    """

    journal = _causal_journal(state)
    schema_start = _causality_schema_start(journal)
    creations = [
        index
        for index, event in enumerate(journal)
        if str(event.get("event") or "") == "create_requested"
    ]
    return sum(1 for index in creations if index >= schema_start), len(creations)


def _causal_journal(state: RunState) -> list[dict[str, Any]]:
    return sorted(state.lifecycle_journal, key=lambda item: int(item.get("sequence") or 0))


def _causality_schema_start(journal: list[dict[str, Any]]) -> int:
    """The position of the first event carrying relay_owner_thread_id.

    _append_event always writes this key - as None when there is no owner.
    So a fully absent key means a record by an older runtime version, not
    an absent owner.
    """

    for index, event in enumerate(journal):
        if "relay_owner_thread_id" in event:
            return index
    return len(journal)


def _run_own_thread_ids(state: RunState) -> set[str]:
    return {
        str(item.get("thread_id") or "")
        for item in state.worker_sessions
        if item.get("thread_id")
    }


def audit_creation_causality(state: RunState) -> list[str]:
    """Rule R1: a task is created by the pipeline, not by a chat command.

    Checked on the journal after the fact: every create_requested but the
    very first of the run must have a preceding turn_completed of the relay
    owner. A creation without such a predecessor means something else
    produced the task - a direct user instruction in chat, say.

    Events recorded before relay_owner_thread_id entered the schema are
    skipped as unassessable - creation_causality_coverage reports their
    number. But a key that vanished AFTER it appeared is a violation:
    otherwise the rule is bypassed by simply no longer writing the field.

    Returns the list of violations; an empty list means the causality chain
    was never broken.
    """

    journal = _causal_journal(state)
    schema_start = _causality_schema_start(journal)
    # A user's turn gets no authoritative completion: nobody waits for it,
    # and no turn_completed is ever written for it. So an owner belonging
    # to no session of the run is the human's thread, i.e. the normal
    # arm/resume path, not a violation. Otherwise the audit would declare a
    # break on every resume.
    own_threads = _run_own_thread_ids(state)
    completed_owners: set[str] = set()
    violations: list[str] = []
    first_seen = False
    for index, event in enumerate(journal):
        name = str(event.get("event") or "")
        if name == "turn_completed":
            thread = str(event.get("thread_id") or "")
            if thread:
                completed_owners.add(thread)
            continue
        if name != "create_requested":
            continue
        if not first_seen:
            # The first task of a run has no predecessor by definition.
            first_seen = True
            continue
        if index < schema_start:
            # An old event schema: the record physically has no owner.
            continue
        if "relay_owner_thread_id" not in event:
            violations.append(
                f"R1: create_requested #{event.get('sequence')} for "
                f"{event.get('task_id')} dropped relay_owner_thread_id after "
                "the field was introduced"
            )
            continue
        owner = str(event.get("relay_owner_thread_id") or "")
        if not owner:
            violations.append(
                f"R1: create_requested #{event.get('sequence')} for "
                f"{event.get('task_id')} has no relay owner"
            )
        elif owner not in own_threads:
            # The human's thread: arm/resume creates a reservation from a turn
            # Autopilot does not watch and whose completion it does not
            # record. A documented path, not a bypass.
            continue
        elif owner not in completed_owners:
            violations.append(
                f"R1: create_requested #{event.get('sequence')} for "
                f"{event.get('task_id')} precedes turn_completed of its owner {owner}"
            )
    return violations
