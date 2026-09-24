from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config
from .plan import Plan
from .lifecycle_base import audit_creation_causality, creation_causality_coverage
from .pipeline_engineer import PipelineIncidentStore, render_pipeline_status
from .config import _install_root
from .run_state import RunState
from .runtime_patch_log import render_applied_patches
from .scheduler import effective_worker_limit
from .task_state import TaskState, unmet_dependencies
from .thread_titles import task_phase_thread_title


_ACTIVE_SESSION_STATUSES = frozenset(
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


def project_status_snapshot(cfg: Config, state: RunState, plan: Plan) -> dict[str, Any]:
    sessions = _active_sessions(state)
    pipeline = PipelineIncidentStore(cfg.state_dir).status_snapshot()
    incident_paused = set(pipeline["paused_task_ids"])
    running: list[dict[str, str]] = []
    verifying: list[dict[str, str]] = []
    ready: list[dict[str, str]] = []
    waiting: list[dict[str, str]] = []

    for task in plan.tasks:
        task_state = TaskState(state.task_states[task.id])
        session = sessions.get(task.id)
        item = {"id": task.id, "title": task.title, "state": task_state.value}
        if task_state in {TaskState.RUNNING, TaskState.REVISING}:
            item["active_title"] = _active_title(plan, task.id, task_state, session)
            running.append(item)
        elif task_state is TaskState.VERIFYING:
            item["active_title"] = _active_title(plan, task.id, task_state, session, state.task_states)
            verifying.append(item)
        elif task_state is TaskState.READY and task.id in incident_paused:
            incident = next(
                item
                for item in pipeline["incidents"]
                if task.id in item["affected_task_ids"]
                and item["phase"] not in {"RECOVERED", "RESOLVED"}
            )
            item["reason"] = (
                f"paused by {incident['incident_id']} "
                f"({incident['classification']} / {incident['phase']})"
            )
            waiting.append(item)
        elif task_state is TaskState.READY:
            ready.append(item)
        elif task_state is not TaskState.VERIFIED:
            item["reason"] = _waiting_reason(plan, state, task.id, task_state, cfg.root)
            waiting.append(item)

    screening = _screening_snapshot(cfg, state, plan)
    verified = sum(
        value == TaskState.VERIFIED.value for value in state.task_states.values()
    )
    # The same calculation as the scheduler's: otherwise, on an unlimited
    # account, the card showed "3/2", computing the limit as min(plan, state).
    worker_limit = effective_worker_limit(plan, state)
    worker_used = len(state.active_task_ids)
    computer_use_limit = min(plan.computer_use_slots, state.computer_use_slots)
    computer_use_used = _computer_use_used(state, sessions)

    if state.status == "DONE" or verified == len(plan.tasks):
        semantic = "Done"
    elif state.status == "PAUSED":
        semantic = "Paused"
    elif state.status == "BLOCKED":
        semantic = "Blocked"
    elif state.active_plan_change_id:
        semantic = "Replanning" if running else "Waiting"
    elif running:
        semantic = "Running"
    elif verifying:
        semantic = "Verifying"
    elif ready:
        semantic = "Ready"
    else:
        semantic = "Waiting"

    association = _association_status(cfg, sessions)
    return {
        "status": semantic,
        "progress": {"verified": verified, "total": len(plan.tasks)},
        "worker_slots": {
            "used": worker_used,
            "total": worker_limit,
            "available": max(0, worker_limit - worker_used),
        },
        "computer_use_slots": {
            "used": computer_use_used,
            "total": computer_use_limit,
            "available": max(0, computer_use_limit - computer_use_used),
        },
        "running": running,
        "verifying": verifying,
        "waiting": waiting,
        "ready": ready,
        "placement": {
            "canonical_cwd": str(cfg.root),
            "desktop_project_id": cfg.desktop.desktop_project_id,
            "app_server_project_id": cfg.desktop.project_id,
            "association": association,
        },
        "pause": {
            "requested": state.status == "PAUSED",
            "semantics": "drain",
            "phase": state.phase,
        },
        "plan_change": _plan_change_status(state),
        "rate_limit_until": state.rate_limit_until,
        "pipeline_engineer": pipeline,
        "screening": screening,
        "creation_causality": _creation_causality(state),
    }


# The statuses under which the worker actually received the capability.
_FILLED_STATUSES = frozenset({"hired", "installed"})


def _screening_snapshot(cfg: Config, state: RunState, plan: Plan) -> dict[str, Any]:
    """What hiring has cost this run, and what it bought.

    Screening spends one Codex thread per task out of the user's limits.
    A number she can see before and while it is spent is the difference
    between a setting and a surprise.
    """

    from .lifecycle_screening import screening_applies

    mode = getattr(cfg.runtime, "skill_screening", "never")
    threads = sum(
        1 for item in state.worker_sessions if item.get("kind") == "screening"
    )
    screened = 0
    unscreened = 0
    hired = 0
    unfilled = 0
    for record in (state.task_hiring or {}).values():
        if not isinstance(record, dict):
            continue
        if record.get("unscreened"):
            unscreened += 1
        else:
            screened += 1
        for outcome in (record.get("decision") or {}).get("outcomes") or ():
            # HiringDecision.unfilled excludes "installed" for a reason: the
            # worker did get that bundle, as a skill to read rather than as a
            # governed pack. Counting it as an unfilled need reported a
            # successful hire as a capability the run failed to find.
            if outcome.get("status") in _FILLED_STATUSES:
                hired += 1
            else:
                unfilled += 1
    from .hired_skills import hired_skill_records

    return {
        "mode": mode,
        "enabled": screening_applies(cfg, plan),
        "installed_bundles": len(hired_skill_records(cfg.state_dir)),
        "threads_spent": threads,
        "tasks_screened": screened,
        "tasks_unscreened": unscreened,
        "skills_hired": hired,
        "needs_unfilled": unfilled,
    }


def _render_screening(screening: dict[str, Any]) -> str:
    head = (
        f"Screening: on ({screening['mode']})"
        if screening["enabled"]
        else f"Screening: off (runtime.skill_screening={screening['mode']})"
    )
    parts = [_count(screening["threads_spent"], "thread", "threads") + " spent"]
    if screening["tasks_screened"]:
        parts.append(_count(screening["tasks_screened"], "task", "tasks") + " screened")
    if screening["tasks_unscreened"]:
        parts.append(f"{screening['tasks_unscreened']} unscreened")
    if screening["skills_hired"]:
        parts.append(_count(screening["skills_hired"], "skill", "skills") + " hired")
    if screening["needs_unfilled"]:
        parts.append(_count(screening["needs_unfilled"], "need", "needs") + " unfilled")
    if screening["installed_bundles"]:
        parts.append(
            _count(screening["installed_bundles"], "bundle", "bundles")
            + " installed (codex-autopilot skills)"
        )
    return f"{head} — " + ", ".join(parts)


def _count(value: int, singular: str, plural: str) -> str:
    return f"{value} {singular if value == 1 else plural}"


def _creation_causality(state: RunState) -> dict[str, Any]:
    """Rule R1, checked against the journal rather than promised.

    M11-R1-REACHABILITY: the audit existed and was called only from tests,
    so the claim "the causality chain is checked" was backed by nothing in
    production. This is a report, not a ban: the causality barrier stands at
    creation time, and here it is re-checked after the fact over the whole
    run journal.

    The blind spot is named as a number: events recorded before
    relay_owner_thread_id entered the schema cannot be assessed, and "no
    violations" must not read as "everything checked".
    """

    assessed, total = creation_causality_coverage(state)
    violations = audit_creation_causality(state)
    return {"assessed": assessed, "total": total, "violations": violations}



_CARD_WORDS = {
    "en": {
        "verified": "verified",
        "running": "Running",
        "verifying": "Verifying",
        "ticket": "Ticket",
        "paused": "Pause requested: no new tasks are launched.",
        "idle": "Nobody is working: the dispatcher is not running.",
        "causality": "Creation causality breaks",
        "screening": "Screening",
        "screening_threads": "threads spent",
        "screening_hired": "hired",
        "more": "Details: say «detailed status».",
        "diagnosis": "Diagnosis",
        "decision": "Your decision",
        "recommendation": "Recommendation",
        "options": "Options",
        "answer": "Answer (the run continues by itself)",
        "hook": "Hook trust",
    },
    "ru": {
        "verified": "проверено",
        "running": "Идёт",
        "verifying": "Проверяется",
        "ticket": "Тикет",
        "paused": "Пауза запрошена: новых задач не запускается.",
        "idle": "Никто не работает: диспетчер не запущен.",
        "causality": "Разрывов причинности создания",
        "screening": "Скрининг",
        "screening_threads": "веток потрачено",
        "screening_hired": "нанято",
        "more": "Подробно: скажи «подробный статус».",
        "diagnosis": "Диагноз",
        "decision": "Ваше решение",
        "recommendation": "Рекомендация",
        "options": "Варианты",
        "answer": "Ответ (прогон продолжит сам)",
        "hook": "Доверие хуку",
    },
}


def _card_words(language: str) -> dict[str, str]:
    from .language import is_russian

    return _CARD_WORDS["ru" if is_russian(language) else "en"]


def render_short_status(
    cfg: Config,
    state: RunState,
    plan: Plan,
    *,
    dispatcher_running: bool,
) -> str:
    """A chat reply: a few lines, not a state dump.

    The hook answers «status» by blocking the input, and its text reaches
    the user whole, in one piece. The full report is twenty-five lines with
    paths and metadata: fine in a terminal, but in a conversation it reads
    like a wall and hides the one thing worth knowing now.

    Here is exactly what answers "so what next": how much is done, what runs
    right now, what is in the way. The full report stays behind a separate
    phrase.
    """

    snapshot = project_status_snapshot(cfg, state, plan)
    progress = snapshot["progress"]
    # The card is the one thing the user reads in chat, so it speaks the run
    # language. Everything else the runtime prints is harness output in
    # English; the model relays it in the user's language.
    words = _card_words(cfg.language)
    lines = [
        f"Codex Autopilot — {snapshot['status']}: "
        f"{progress['verified']}/{progress['total']} {words['verified']}"
    ]
    for heading, key in ((words["running"], "running"), (words["verifying"], "verifying")):
        for item in snapshot[key]:
            lines.append(f"{heading}: {item['id']} — {item['title']}")
    blocked = [
        item
        for item in snapshot["pipeline_engineer"].get("incidents") or []
        if item["phase"] not in {"RECOVERED", "RESOLVED"}
    ]
    for incident in blocked:
        lines.append(
            f"{words['ticket']} {incident['incident_id']}: {incident['phase']} — "
            f"{_clip(incident['summary'], 120)}"
        )
        if incident["phase"] == "ESCALATE_TO_USER":
            lines.extend(_owner_decision_lines(cfg, incident, words))
    hook_signal = _hook_trust_signal(state)
    if hook_signal:
        lines.append(f"{words['hook']}: {_clip(hook_signal, 240)}")
    if snapshot["pause"]["requested"]:
        lines.append(words["paused"])
    # Hiring is on by default and spends a Codex thread per task out of the
    # user's limits. The detailed report carried that number and this card
    # did not - and this card is the one a user actually types «status» for,
    # so the cost lived where nobody looked. Only while it is on: a line
    # reading "0" on every run is noise, not disclosure.
    screening = snapshot["screening"]
    if screening["enabled"]:
        spent = (
            f"{words['screening']}: {screening['threads_spent']} "
            f"{words['screening_threads']}"
        )
        if screening["skills_hired"]:
            spent += f", {screening['skills_hired']} {words['screening_hired']}"
        lines.append(spent)
    # The dispatcher is a short-lived process: it rises on the transition
    # between tasks and goes away while a worker or verifier holds the turn.
    # The condition once looked only at "running" and forgot "verifying", so
    # the card declared the dispatcher dead in the middle of an acceptance,
    # contradicting its own "Verifying" line two rows above. The one place
    # the user looks for the truth was lying to them.
    if (
        not dispatcher_running
        and not snapshot["running"]
        and not snapshot["verifying"]
        and not blocked
    ):
        lines.append(words["idle"])
    audit = snapshot["creation_causality"]
    if audit["violations"]:
        lines.append(
            f"{words['causality']} (R1): {len(audit['violations'])}."
        )
    lines.append(words["more"])
    return "\n".join(lines)


def _owner_decision_lines(cfg: Config, incident: dict, words: dict[str, str]) -> list[str]:
    """What she decides with: the on-call's diagnosis, its advice, and her command.

    A ticket handed to her used to show a phase and a summary - the code
    and nothing else. The engineer's diagnosis and recommendation were kept
    in its thread, which nobody opens, and the card offered no way to answer
    but "Resume", which answered every ticket at once.
    """

    from .stop_diagnosis import owner_answer

    escalation = incident.get("escalation") or {}
    lines = []
    for key, value in (
        ("diagnosis", escalation.get("diagnosis") or incident.get("escalation_detail")),
        ("decision", escalation.get("decision_needed")),
        ("recommendation", escalation.get("recommendation")),
    ):
        if value:
            lines.append(f"  {words[key]}: {_clip(value, 240)}")
    options = [
        f"{item.get('code')} — {item.get('means') or ''}".strip(" —")
        for item in escalation.get("options") or ()
        if isinstance(item, dict) and item.get("code")
    ]
    if options:
        lines.append(f"  {words['options']}: " + "; ".join(_clip(item, 120) for item in options[:4]))
    lines.append(f"  {words['answer']}: {owner_answer(cfg, incident)}")
    return lines


def _hook_trust_signal(state: RunState) -> str:
    """The one signal that goes to her without the on-call: revoked hook trust.

    Raising the engineer passes the same trust gate, and going around it is
    her boundary - so the wake-up records the refusal with its diagnosis and
    recommendation (``wake.run_wake``), and the card shows it until a later
    wake-up gets through.
    """

    for item in reversed(list(state.resilience_journal or ())):
        event = str(item.get("event") or "")
        detail = item.get("detail") or {}
        if event == "wake_dispatched":
            return ""
        if event == "wake_skipped" and detail.get("why") == "hook trust is not in place":
            return f"{detail.get('diagnosis')}; {detail.get('recommendation')}"
    return ""


def _clip(text: str, limit: int) -> str:
    value = str(text)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def render_project_status(
    cfg: Config,
    state: RunState,
    plan: Plan,
    *,
    dispatcher_running: bool,
) -> str:
    snapshot = project_status_snapshot(cfg, state, plan)
    progress = snapshot["progress"]
    workers = snapshot["worker_slots"]
    computer = snapshot["computer_use_slots"]
    lines = [
        f"Codex Autopilot — {snapshot['status']}",
        f"Verified progress: {progress['verified']}/{progress['total']}",
        f"Worker slots: {workers['used']}/{workers['total']} used, {workers['available']} available",
        f"Computer Use slots: {computer['used']}/{computer['total']} used, {computer['available']} available",
        (
            f"Pause: drain ({snapshot['pause']['phase']})"
            if snapshot["pause"]["requested"]
            else "Pause: not requested"
        ),
        (
            f"Plan change: {snapshot['plan_change']['id']} / {snapshot['plan_change']['status']}"
            + (
                f" — rejected by the runtime ({snapshot['plan_change']['rejection_count']}): "
                f"{snapshot['plan_change']['rejection']}"
                if snapshot["plan_change"].get("rejection")
                else ""
            )
            if snapshot["plan_change"]
            else "Plan change: none"
        ),
        (
            f"Rate-limit barrier: epoch {snapshot['rate_limit_until']}"
            if snapshot["rate_limit_until"] is not None
            else "Rate-limit barrier: none"
        ),
        render_pipeline_status(snapshot["pipeline_engineer"]),
        _render_screening(snapshot["screening"]),
        _render_creation_causality(snapshot["creation_causality"]),
    ]
    for heading, key in (
        ("Running", "running"),
        ("Verifying", "verifying"),
        ("Waiting", "waiting"),
        ("Ready", "ready"),
    ):
        lines.append(f"{heading}:")
        items = snapshot[key]
        if not items:
            lines.append("- none")
            continue
        for item in items:
            suffix = ""
            if item.get("active_title"):
                suffix = f" — active title: {item['active_title']}"
            elif item.get("reason"):
                suffix = f" — {item['reason']}"
            lines.append(f"- {item['id']}: {item['title']}{suffix}")
    placement = snapshot["placement"]
    lines.extend(
        [
            f"Canonical cwd: {placement['canonical_cwd']}",
            f"Project association: {placement['association']}",
            (
                # The model and reasoning level are removed from here: no
                # production path writes them, and the substituted "Host
                # default" was a claim without a measurement - R26. Measured:
                # zero writes to selected_model_display and
                # selected_reasoning outside run_state.py; the line printed
                # the same on any run. The real choice belongs to the task
                # and lives in AIStudioRuntime routing, not in run state.
                f"Runtime: execution_mode={state.execution_mode or plan.tasks[state.milestone_index].execution_mode}, "
                f"strategy={plan.model_strategy}, surface={cfg.runtime.worker_surface}, "
                f"dispatcher={'running' if dispatcher_running else 'not running'}, "
                f"phase={state.phase}, last_error={state.last_error or 'none'}"
            ),
            # A runtime code repair silently changes how the installation
            # behaves: the user is entitled to see that it happened.
            render_applied_patches(_install_root() / "runtime"),
        ]
    )
    return "\n".join(lines)


def _active_sessions(state: RunState) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    active = set(state.active_task_ids)
    for session in state.worker_sessions:
        task_id = str(session.get("task_id") or "")
        if task_id in active and session.get("status") in _ACTIVE_SESSION_STATUSES:
            result[task_id] = session
    return result


def _plan_change_status(state: RunState) -> dict[str, Any] | None:
    selected = state.active_plan_change_id
    if selected is None:
        # An exhausted plan change stops being active, but its task stands
        # on it. Not showing it here leaves the person facing a stop with no
        # reason - exactly why the run went silent. The run's phase no
        # longer names the stop (it is derived, and the on-call may be at
        # work), so the task still stopped is what says it matters.
        rejected = [
            item
            for item in state.plan_changes
            if item.get("status") == "REJECTED"
            and state.task_states.get(str(item.get("requester_task_id") or "")) == "BLOCKED"
        ]
        if not rejected:
            return None
        selected = str(rejected[-1]["id"])
    record = next(
        (item for item in state.plan_changes if item.get("id") == selected),
        None,
    )
    if record is None:
        return {"id": state.active_plan_change_id, "status": "UNKNOWN"}
    rejections = list(record.get("rejections") or [])
    return {
        "id": str(record["id"]),
        "status": str(record["status"]),
        "requester_task_id": str(record["requester_task_id"]),
        "summary": str((record.get("request") or {}).get("summary") or ""),
        "rejection": str(rejections[-1].get("reason") or "") if rejections else "",
        "rejection_count": len(rejections),
    }


def _active_title(
    plan: Plan,
    task_id: str,
    task_state: TaskState,
    session: dict[str, Any] | None,
    task_states: dict[str, Any] | None = None,
) -> str:
    if session:
        descriptor = session.get("descriptor")
        if isinstance(descriptor, dict) and isinstance(descriptor.get("title"), str):
            return str(descriptor["title"])
    kind = "verifier" if task_state is TaskState.VERIFYING else "revision" if task_state is TaskState.REVISING else "implementation"
    task = plan.task_map[task_id]
    role_name = plan.role_map[task.role].name
    if kind == "verifier":
        # The judge is the lead of the department of the worker's profession
        # (R30), as the reservation names it. This fallback read
        # ``verifier_role or task.role`` after the runtime stopped doing so,
        # and named the worker's own profession as the verifier of a task
        # from before R30 whose lead comes from its profession.
        from .department_runtime import settled_task_ids
        from .verification import VerificationProtocolError, verifier_route

        try:
            role_name = plan.role_map[verifier_route(plan, task, settled=settled_task_ids(task_states)).role_id].name
        except VerificationProtocolError:
            role_name = "No Lead Role"
    revision_number = int((session or {}).get("revision_number") or 1)
    return task_phase_thread_title(
        task_id=task_id,
        task_title=task.title,
        kind=kind,
        role_name=role_name,
        revision_number=revision_number,
        departmental_verifier=(kind == "verifier"),
    )


def _waiting_reason(
    plan: Plan,
    state: RunState,
    task_id: str,
    task_state: TaskState,
    project_root: Path,
) -> str:
    if task_state is TaskState.WAITING:
        dependencies = unmet_dependencies(plan, task_id, state.task_states)
        if dependencies:
            return f"waiting for verified dependencies: {', '.join(dependencies)}"
        # Section 33 of the specification requires naming the wait reason:
        # "T18 · resource locked by T14". Without it a task whose
        # dependencies are done stands unexplained.
        return _resource_reason(plan, state, task_id, project_root) or (
            "waiting for scheduler eligibility"
        )
    if task_state is TaskState.RETRY_WAIT:
        retry_at = state.task_retry_at.get(task_id)
        return f"retry scheduled at epoch {retry_at}" if retry_at is not None else "retry is pending"
    if task_state is TaskState.IMPLEMENTED:
        return "implementation complete; verification not yet started"
    if task_state is TaskState.REVISION_REQUIRED:
        return "verification requires a revision" + _hiring_suffix(state, task_id)
    if task_state is TaskState.BLOCKED:
        # The acceptance complaints lay in the state and were shown to
        # nobody. Without them "blocked" does not say what the owner must
        # decide.
        return (
            # last_error already names the hire number and the step - only
            # the acceptance complaints themselves are added here.
            f"blocked: {state.last_error or 'no reason recorded'}"
            + _issue_suffix(state, task_id)
        )
    if task_state is TaskState.FAILED:
        return f"failed: {state.last_error or 'no reason recorded'}"
    if task_state is TaskState.CANCELLED:
        return "cancelled"
    return f"state={task_state.value}"


def _hiring_suffix(state: RunState, task_id: str) -> str:
    """Which executor in order drives the task, and at which step."""

    hires = int(state.task_rehires.get(task_id, 0))
    if not hires:
        return ""
    effort = state.task_effort.get(task_id)
    tail = f", effort {effort}" if effort else ""
    return f" (hire {hires + 1}{tail})"


def _issue_suffix(state: RunState, task_id: str) -> str:
    issues = _latest_issues(state, task_id)
    if not issues:
        return ""
    lines = []
    for item in issues:
        refs = ", ".join(f"DoD {ref}" for ref in item.get("dod_refs") or [])
        head = str(item.get("code") or "issue")
        summary = str(item.get("summary") or "").strip()
        lines.append(f"  - {head}: {summary}" + (f" [{refs}]" if refs else ""))
    return "\n" + "\n".join(lines)


def _latest_issues(state: RunState, task_id: str) -> list[dict[str, object]]:
    for item in reversed(state.worker_sessions):
        if item.get("task_id") == task_id and item.get("verification_issues"):
            return list(item["verification_issues"])
    return []


def _resource_reason(
    plan: Plan, state: RunState, task_id: str, project_root: Path
) -> str | None:
    """Why the task waits on a resource, if it does.

    An unreadable lock record is not passed off as an absent owner: "could
    not read" and "nobody holds it" are different things, and the second
    reassures where there is nothing reassuring.
    """

    task = plan.task_map.get(task_id)
    if task is None or not task.resources:
        return None
    from .resources import DurableResourceLock, claims_conflict, normalize_task_claims

    try:
        wanted = normalize_task_claims(task, project_root)
    except (ValueError, TypeError) as error:
        return f"resource claims are unreadable: {error}"
    unreadable = 0
    for raw in state.resource_locks:
        try:
            lock = DurableResourceLock.from_dict(raw)
        except (ValueError, KeyError, TypeError):
            unreadable += 1
            continue
        if lock.owner.task_id == task_id:
            continue
        if any(claims_conflict(left, right) for left in wanted for right in lock.claims):
            return f"resource locked by {lock.owner.task_id}"
    if unreadable:
        return f"resource lock state is unreadable ({unreadable} of {len(state.resource_locks)})"
    return None


def _computer_use_used(
    state: RunState,
    sessions: dict[str, dict[str, Any]],
) -> int:
    slots = {
        lock.get("computer_use_slot")
        for lock in state.resource_locks
        if lock.get("computer_use_slot") is not None
    }
    session_count = 0
    for session in sessions.values():
        descriptor = session.get("descriptor")
        if isinstance(descriptor, dict) and descriptor.get("execution_mode") == "computer_use":
            session_count += 1
    return max(len(slots), session_count)


def _association_status(
    cfg: Config,
    sessions: dict[str, dict[str, Any]],
) -> str:
    details = [
        str(session.get("project_association_verification"))
        for session in sessions.values()
        if session.get("project_association_verification")
    ]
    if details:
        return "; ".join(dict.fromkeys(details))
    if cfg.desktop.project_id:
        return f"App Server saved project {cfg.desktop.project_id} configured; no active metadata snapshot"
    if cfg.desktop.desktop_project_id:
        return (
            f"Desktop project {cfg.desktop.desktop_project_id} is Codex App-authoritative; "
            "App Server projectId is a separate namespace and is unavailable"
        )
    return "no saved-project association; task appears in Tasks/Recents"


def _render_creation_causality(audit: dict[str, Any]) -> str:
    assessed = audit["assessed"]
    total = audit["total"]
    violations = audit["violations"]
    head = f"Creation causality (R1): {assessed}/{total} creations audited"
    if not violations:
        return f"{head}, no break found"
    lines = [f"{head}, {len(violations)} break(s):"]
    lines.extend(f"- {item}" for item in violations)
    return "\n".join(lines)
