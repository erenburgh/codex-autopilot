"""What the on-call reads about a stopped task, and what the owner can answer.

The engineer's package used to carry the ticket and nothing of the stop: the
summary said "M11 exhausted the hiring ladder", and to learn why it had to
go digging through a journal it was told not to read beyond its package.
The owner, in turn, received a bare code with no command to answer it.

So a stop ticket's package now carries its own diagnosis material, bounded:

- what kind of stop it is, and the worker's own reason code;
- the verifier's last issue codes and summaries (the ladder, a revise loop);
- how far the ladder went - hires and effort;
- the replanner's refusals, for a plan change that did not take;
- the bounded end of the stopped session's final message;
- the means for this kind of stop - from the table in ``engineer_authority``,
  which the engineer cannot edit;
- for a permission request, the request itself next to the durable
  authorization run-state holds for the run (R4, ``run_authorization``), the
  operation of it that covers the request if one does, and the permission
  profile the run uses;
- and the command that is the owner's answer, ready to run.

Read-only: nothing here writes state.
"""

from __future__ import annotations

import shlex
from typing import Any, Mapping

MAX_TAIL_CHARS = 1_200
MAX_ISSUES = 8
MAX_REJECTIONS = 3

# The answers the runtime itself acts on, by stop kind. Any other option code
# returns the task to work with her choice recorded where its next worker
# reads it (user_unblocks). ``replan`` asks the replanner instead.
OWNER_OPTIONS: dict[str, tuple[dict[str, str], ...]] = {
    "approval_required": (
        {
            "code": "replan",
            "means": "change the plan so the task does not need the operation",
        },
        {
            "code": "retry",
            "means": (
                "you granted the permission yourself (Codex settings or the project's "
                "permission profile); the task runs once more - the same request after "
                "that is not retried again"
            ),
        },
    ),
    "ladder_exhausted": (
        {"code": "retry", "means": "a fresh hire at the top effort, with your note to the worker"},
        {"code": "replan", "means": "change the plan (split the task, change its contract)"},
    ),
}


def stop_kind_of(incident: Mapping[str, Any]) -> str:
    return str((incident.get("system_state") or {}).get("stop_kind") or "")


def means_key(incident: Mapping[str, Any]) -> str:
    """The key of the means table for this stop."""

    system = incident.get("system_state") or {}
    kind = stop_kind_of(incident)
    if kind == "worker_blocked":
        return f"worker_blocked:{system.get('reason_code') or ''}"
    return kind


def means_for(incident: Mapping[str, Any]) -> tuple[str, ...]:
    """What the on-call may do about this stop - the unpatchable table's word."""

    from .engineer_authority import DEFAULT_STOP_MEANS, STOP_MEANS

    return tuple(STOP_MEANS.get(means_key(incident), DEFAULT_STOP_MEANS))


def owner_answer(cfg: Any, incident: Mapping[str, Any]) -> str:
    """The command that is her answer: ready to run, no Resume needed.

    A ticket that holds no task is named by its id. The card used to offer
    ``--task <context task>`` for it, or the literal ``<task>`` for a
    run-level stop - and ``unblock`` refused both: the task was not stopped.
    """

    tasks = [str(item) for item in incident.get("affected_task_ids") or ()]
    root = shlex.quote(str(getattr(cfg, "root", "<project>")))
    if tasks:
        target = f"--task {shlex.quote(tasks[0])}"
    else:
        target = f"--incident-id {shlex.quote(str(incident.get('incident_id') or '<ticket>'))}"
    return (
        f"codex-autopilot unblock --project {root} {target} "
        "[--option <code>] --reason '<your decision>'"
    )


def stop_context(cfg: Any, plan: Any, state: Any, incident: Mapping[str, Any]) -> dict[str, Any]:
    """The bounded diagnosis material of one stop ticket."""

    system = dict(incident.get("system_state") or {})
    kind = stop_kind_of(incident)
    tasks = [str(item) for item in incident.get("affected_task_ids") or ()]
    context: dict[str, Any] = {
        "stop_kind": kind,
        "reason_code": str(system.get("reason_code") or ""),
        "held": bool(system.get("held")),
        "tasks": {task: _task_view(plan, state, task) for task in tasks if task in plan.task_map},
        "means": list(means_for(incident)),
        "owner_options": [dict(item) for item in OWNER_OPTIONS.get(kind, ())],
    }
    if kind == "ladder_exhausted":
        context["ladder"] = {
            key: system.get(key) for key in ("hires", "effort", "revision_attempts") if key in system
        }
    if kind in {"plan_change_rejected", "plan_verification_rejected", "plan_verification_protocol"}:
        context["replanner_refusals"] = _plan_change_refusals(state, system)
    if kind == "approval_required":
        context["approval"] = {
            "request": system.get("approval") or {},
            "signature": system.get("approval_signature") or "",
            # R4: the durable authorization run-state holds, versioned - the
            # on-call compares with it, not with prose. A module constant of
            # three sentences stood here; the run carried no list of its own.
            "run_authorization": _run_authorization(cfg, state),
            "covered_by": system.get("covered_by"),
            "approvals_in_run": system.get("approvals_in_run"),
            "placement_contract": system.get("placement_contract"),
            "permission_profile": str(
                getattr(getattr(cfg, "desktop", None), "permission_profile", "") or ""
            ),
            "answered_before": [
                dict(item)
                for item in getattr(state, "user_unblocks", None) or ()
                if isinstance(item, dict)
                and item.get("approval_signature")
                and item.get("approval_signature") == system.get("approval_signature")
            ][-3:],
        }
    if kind == "placement_defect":
        context["placement"] = {
            key: system.get(key)
            for key in ("cause", "diagnosis", "recommendation", "defect", "outside_threads")
        }
    return context


def _run_authorization(cfg: Any, state: Any) -> dict[str, Any]:
    recorded = getattr(state, "durable_authorization", None)
    if isinstance(recorded, Mapping):
        return dict(recorded)
    from .run_authorization import authorization_record

    # Read-only here: shown as what arming would record, marked unrecorded.
    return authorization_record(cfg, at="", granted_by="unrecorded")


def _task_view(plan: Any, state: Any, task_id: str) -> dict[str, Any]:
    sessions = [
        item
        for item in getattr(state, "worker_sessions", None) or ()
        if str(item.get("task_id") or "") == task_id and item.get("kind") != "pipeline_engineer"
    ]
    last = sessions[-1] if sessions else {}
    verifier = next(
        (
            item
            for item in reversed(sessions)
            if item.get("verification_issues") or item.get("verification_result")
        ),
        {},
    )
    issues = list(verifier.get("verification_issues") or [])
    if not issues:
        issues = list((verifier.get("verification_result") or {}).get("issues") or [])
    return {
        "state": state.task_states.get(task_id),
        "title": getattr(plan.task_map.get(task_id), "title", ""),
        "revisions": int((getattr(state, "task_revisions", None) or {}).get(task_id, 0)),
        "hires": int((getattr(state, "task_rehires", None) or {}).get(task_id, 0)),
        "effort": (getattr(state, "task_effort", None) or {}).get(task_id),
        "last_session": {
            "kind": last.get("kind"),
            "status": last.get("status"),
            "final_status": last.get("final_status"),
            "reason_code": last.get("reason_code"),
            "final_message_tail": str(last.get("final_message_tail") or "")[-MAX_TAIL_CHARS:],
        },
        "verifier_issues": [
            {"code": item.get("code"), "summary": str(item.get("summary") or "")[:300]}
            for item in issues[:MAX_ISSUES]
            if isinstance(item, Mapping)
        ],
        "verification_rejections": [
            str(item.get("reason") or "")[:300]
            for item in ((getattr(state, "verification_rejections", None) or {}).get(task_id) or [])
            if isinstance(item, Mapping)
        ][-MAX_REJECTIONS:],
    }


def _plan_change_refusals(state: Any, system: Mapping[str, Any]) -> list[str]:
    change_id = str(system.get("plan_change_id") or "")
    for item in reversed(getattr(state, "plan_changes", None) or []):
        if not change_id or str(item.get("id")) == change_id:
            return [
                str(entry.get("reason") or "")[:400]
                for entry in item.get("rejections") or ()
                if isinstance(entry, Mapping)
            ][-MAX_REJECTIONS:]
    return []
