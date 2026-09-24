"""R30's two checks that watch the leads themselves.

A lead that outlived its acceptance. R30 wants the lead to be a fresh
session: it materializes for one acceptance, loads the rubric, gives its
verdict and ends; a lead that lives on is a defect. Nothing looked: "outlived"
appeared only in the rules text. The check at completion would be pointless -
at that moment the thread has exactly the one turn the runtime started. So the
thread is read later, by the server's own answer (thread/read with turns, R2
allows it), once per lead session, from the wake-up and the periodic sweep
(``audit_lead_sessions``) - the sweep reads them for a finished or paused run
too, or the leads of a run's last minutes would never be read. A turn after the runtime's is recorded as the
defect it is - an R30 violation and a verification result on the task - and
never as an incident that would take the on-call's lane from the work: a
harmless extra turn (her own message in the lead's thread) would have stopped
the run. Only a turn still running there, not started by the runtime, is a
ticket for the on-call, and it holds no task.

A second lead. R30 names its known risk: one lead with an incomplete rubric
errs systematically across the department, and the mitigation is to judge
the same work again, every N-th acceptance, by a second lead, and to record
the disagreement - its rate measures the rubric, not chance. The second lead
is an ordinary fresh verifier of the same task, on the same proposal and the
same rubric (``second_lead_gate``): the first verdict is recorded at once and
applied when the second is in, whatever the second says - the acceptance is
the first lead's, the second one only measures it. N is
``runtime.second_lead_every`` (default 5), counted per department over the
first verdicts of the run, whatever they were.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Callable, Mapping

from .department_acceptance import RUNTIME_RUBRIC_AUTHOR, DepartmentAcceptanceError
from .department_runtime import derive_task_department

LEAD_AUDIT_TOOL = "codex-autopilot/lead-audit"
LEAD_AUDIT_CHECK = "r30-lead-session"
SECOND_LEAD_CHECK = "r30-second-lead"
# How long after its completion a lead's thread is read: an extra turn needs
# time to appear, and one read per lead is the budget.
LEAD_AUDIT_DELAY_SECONDS = 600
LEAD_AUDIT_BATCH = 20
_FINISHED_TURN = frozenset({"completed", "interrupted", "failed"})


def audit_lead_sessions(
    cfg: Any,
    *,
    client_factory: Callable[..., Any] | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Read the threads of finished leads once; record the ones that lived on.

    Returns the findings. Housekeeping: no connection, no audit, and a lead
    is read again by the next sweep. Never raises into its caller.
    """

    from .resources import ResourceLockCoordinator
    from .run_state import StateStore

    try:
        store = StateStore(cfg.state_dir)
        due = _due(store.load(), now)
        if not due:
            return []
        threads = _read_threads(cfg, due, client_factory)
        if not threads:
            return []
        with ResourceLockCoordinator(store, cfg.root).transaction():
            state = store.load()
            findings = _record(cfg, state, threads)
            store.save(state)
        return findings
    except Exception:  # noqa: BLE001 - an audit may never break the wake-up or the sweep
        return []


def _due(state: Any, now: float | None) -> list[dict[str, Any]]:
    moment = datetime.now(timezone.utc).timestamp() if now is None else now
    found = []
    for session in state.worker_sessions:
        if session.get("kind") != "verifier" or session.get("status") != "COMPLETED":
            continue
        if session.get("lead_audit") or not session.get("thread_id") or not session.get("turn_id"):
            continue
        try:
            completed = datetime.fromisoformat(str(session.get("completed_at"))).timestamp()
        except (TypeError, ValueError):
            continue
        if moment - completed >= LEAD_AUDIT_DELAY_SECONDS:
            found.append(session)
    return found[:LEAD_AUDIT_BATCH]


def _read_threads(cfg: Any, due: list[dict[str, Any]], client_factory: Callable[..., Any] | None) -> dict[str, Any]:
    from .appserver import AppServerClient

    factory = client_factory or AppServerClient
    threads: dict[str, Any] = {}
    try:
        with factory(cfg.desktop.binary, cfg.state_dir / "logs" / "lead-audit.jsonl") as client:
            for session in due:
                try:
                    threads[str(session["reservation_token"])] = client.read_thread(str(session["thread_id"]))
                except Exception:  # noqa: BLE001 - unknown is not "clean"; read again later
                    continue
    except Exception:  # noqa: BLE001 - no connection, no observation
        return {}
    return threads


def _record(cfg: Any, state: Any, threads: Mapping[str, Any]) -> list[dict[str, Any]]:
    from .lifecycle_base import _append_event
    from .run_state import utc_now

    at = utc_now()
    findings: list[dict[str, Any]] = []
    for session in state.worker_sessions:
        thread = threads.get(str(session.get("reservation_token")))
        if not isinstance(thread, Mapping) or session.get("lead_audit"):
            continue
        thread_id = str(session["thread_id"])
        own = {
            str(item.get("turn_id"))
            for item in state.worker_sessions
            if str(item.get("thread_id") or "") == thread_id and item.get("turn_id")
        }
        turns = [item for item in thread.get("turns") or () if isinstance(item, Mapping)]
        ids = [str(item.get("id") or "") for item in turns]
        positions = [index for index, turn_id in enumerate(ids) if turn_id in own]
        if not positions:
            continue  # the server does not show the runtime's own turn: read again later
        after = [
            {"id": ids[index], "status": str(turns[index].get("status") or "")}
            for index in range(positions[-1] + 1, len(turns))
            if ids[index] not in own
        ]
        live = [item for item in after if item["status"] not in _FINISHED_TURN]
        session["lead_audit"] = {"at": at, "outlived": bool(after), "turns_after": after}
        if not after:
            continue
        finding = {"task_id": str(session.get("task_id")), "thread_id": thread_id, "turns_after": after}
        findings.append(finding)
        _append_event(state, "lead_outlived_acceptance", session, at, detail=json.dumps(after))
        _record_defect(cfg, session, finding)
        if live:
            _ticket_live_turn(cfg, state, session, live, at)
    return findings


def _record_defect(cfg: Any, session: Mapping[str, Any], finding: Mapping[str, Any]) -> None:
    from .memory import ProjectMemory
    from .rules import record_violation

    task_id = str(finding["task_id"])
    detail = (
        f"the lead of {task_id} (thread {finding['thread_id']}) had "
        f"{len(finding['turns_after'])} turn(s) after its acceptance"
    )
    record_violation(cfg.state_dir, "R30", detail=detail)
    try:
        memory = ProjectMemory(cfg.root)
        evidence = memory.record_evidence(
            kind="tool",
            summary=f"R30 defect: {detail}.",
            tool_name=LEAD_AUDIT_TOOL,
            result=json.dumps(dict(finding), sort_keys=True),
            exit_code=1,
            created_by=RUNTIME_RUBRIC_AUTHOR,
        )
        memory._record_runtime_verification_result(
            task_id=task_id,
            check_id=LEAD_AUDIT_CHECK,
            policy="deterministic",
            verdict="REVISE",
            summary=f"R30: the department lead session outlived its acceptance ({detail}).",
            evidence_ids=[str(evidence["id"])],
            created_by=RUNTIME_RUBRIC_AUTHOR,
            provider="codex-desktop",
            provider_thread_id=str(finding["thread_id"]),
            provider_turn_id=str(finding["turns_after"][0]["id"]),
            details={"defect": "lead_outlived_acceptance", **dict(finding)},
        )
    except Exception:  # noqa: BLE001 - the violation and the journal already hold it
        return


def _ticket_live_turn(cfg: Any, state: Any, session: Mapping[str, Any], live: list, at: str) -> None:
    from .blocked_runs import stop_run

    task_id = str(session.get("task_id") or "")
    stop_run(
        cfg,
        state,
        stop_kind="lead_outlived",
        phase="LEAD_OUTLIVED_ACCEPTANCE",
        reason=(
            f"R30: a turn not started by the runtime is running in the lead thread of "
            f"{task_id} after its acceptance: {', '.join(item['id'] for item in live)}"
        ),
        summary=f"The department lead of {task_id} lives on after its acceptance.",
        at=at,
        context_task_id=task_id,
        system_state={"thread_id": str(session.get("thread_id")), "turns": list(live)},
    )


def awaiting_second_lead(state: Any, task_id: str) -> dict[str, Any] | None:
    """The first lead's session whose verdict waits for a second lead, if any."""

    for session in reversed(state.worker_sessions):
        if session.get("task_id") == task_id and (session.get("second_lead") or {}).get("status") == "AWAITING":
            return session
    return None


def second_lead_details(state: Any, task_id: str, verdict: Any) -> dict[str, Any]:
    """What the second lead's verification result records beside its verdict."""

    first_session = awaiting_second_lead(state, task_id)
    if first_session is None:
        return {}
    first = first_session["second_lead"]
    agrees = verdict.verdict == first["verdict"]["verdict"]
    rate = disagreement_rate(state, first["department_id"], pending_agrees=agrees)
    return {
        "second_lead": {
            "primary_verification_id": first["verification_id"],
            "primary_verdict": first["verdict"]["verdict"],
            "second_verdict": verdict.verdict,
            "agrees": agrees,
            "department_disagreement": rate,
        }
    }


def disagreement_rate(state: Any, department_id: str, *, pending_agrees: bool | None = None) -> dict[str, Any]:
    compared = [
        item["second_lead"]
        for item in state.worker_sessions
        if (item.get("second_lead") or {}).get("status") == "COMPARED"
        and item["second_lead"].get("department_id") == department_id
    ]
    agreements = [bool(item.get("agrees")) for item in compared]
    if pending_agrees is not None:
        agreements.append(pending_agrees)
    disagreements = sum(1 for item in agreements if not item)
    return {
        "department_id": department_id,
        "second_leads": len(agreements),
        "disagreements": disagreements,
        "rate": round(disagreements / len(agreements), 4) if agreements else 0.0,
    }


def second_lead_gate(
    cfg: Any, plan: Any, state: Any, current: dict[str, Any], verdict: Any, verification_id: Any, at: str
) -> tuple[bool, Any, Any]:
    """(deferred, the verdict to apply, its verification id).

    The first lead of a selected acceptance: its verdict is kept and the
    task goes back to IMPLEMENTED, so the ordinary path raises a fresh lead.
    The second lead: its verdict was recorded beside the first
    (``second_lead_details``); the first verdict is the one applied.
    """

    from .lifecycle_base import _append_event
    from .task_state import TaskState, transition_task
    from .verification import VerificationIssue, VerificationVerdict

    task_id = str(current["task_id"])
    first_session = awaiting_second_lead(state, task_id)
    if first_session is not None and first_session is not current:
        first = first_session["second_lead"]
        agrees = verdict.verdict == first["verdict"]["verdict"]
        current["second_lead_of"] = first_session["reservation_token"]
        first.update(
            status="COMPARED", agrees=agrees, second_verdict=verdict.verdict,
            second_verification_id=verification_id, compared_at=at,
        )
        rate = disagreement_rate(state, first["department_id"])
        _append_event(state, "second_lead_compared", current, at, detail=json.dumps(rate, sort_keys=True))
        primary = VerificationVerdict(
            verdict=first["verdict"]["verdict"],
            issues=tuple(VerificationIssue.from_dict(item) for item in first["verdict"]["issues"]),
        )
        return False, primary, first["verification_id"]
    try:
        department = derive_task_department(plan, plan.task_map[task_id])
    except DepartmentAcceptanceError:
        return False, verdict, verification_id
    ordinal = 1 + sum(
        1
        for item in state.worker_sessions
        if item is not current and _first_verdict_of(plan, item, department.id)
    )
    every = int(getattr(getattr(cfg, "runtime", None), "second_lead_every", 0) or 0)
    if every <= 0 or ordinal % every:
        return False, verdict, verification_id
    current["second_lead"] = {
        "status": "AWAITING", "department_id": department.id, "ordinal": ordinal, "every": every,
        "verdict": verdict.to_dict(), "verification_id": verification_id, "requested_at": at,
    }
    state.task_states = transition_task(plan, state.task_states, task_id, TaskState.IMPLEMENTED)
    _append_event(
        state, "second_lead_requested", current, at,
        detail=f"{department.id}: acceptance {ordinal}, every {every}; first verdict {verdict.verdict}",
    )
    return True, verdict, verification_id


def _first_verdict_of(plan: Any, session: Mapping[str, Any], department_id: str) -> bool:
    if session.get("kind") != "verifier" or session.get("second_lead_of"):
        return False
    if session.get("final_status") not in {"PASS", "REVISE"}:
        return False
    task = plan.task_map.get(str(session.get("task_id") or ""))
    try:
        return task is not None and derive_task_department(plan, task).id == department_id
    except DepartmentAcceptanceError:
        return False
