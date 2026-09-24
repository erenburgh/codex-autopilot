"""The run's roster, staffed before the first task: who works, who accepts, by what.

Her idea (23 Sep 2026, "огонь, делаем"): staff everything up front and see
every branch. What she wanted to see in the sidebar was a thread per task
created in advance. That is not how it is done here, and it was settled by
measurement, not taste:

- App Server does not keep a thread with no turn: four empty probe threads
  vanished from thread/list (launch_gate), so the dispatcher starts a thread
  10-60 ms before its turn;
- pre-created threads already existed ("slots", worker_thread_ids) and were
  removed: there is no primitive to hand a thread's ownership over ("already
  has an active writer"), and an idle thread was gone ("thread not found")
  two hours later (DESKTOP_RUNTIME.md, git 11a3112, e2138c7);
- R30 makes the lead a fresh session for every acceptance, and REHIRING a
  fresh worker for every revision: a thread made in advance breaks both;
- and handing thread creation back to the model (R2) brought back approvals
  in the middle of a run, guardrail refusals and blind duplicates.

So threads are still started by the dispatcher right before their turn, and
the roster is data: after the plan is checked and before the first
reservation, the runtime - no model - derives for every task its department
and lead (R30, ``department_runtime``), the department's rubric (record,
version, sha256; version 1 is the runtime's), the skills known at start, the
acceptance rules (class, policy, deterministic checks, the R29 clean suite),
the hiring ladder, the on-call and the escalation route, the thread names
(docs/THREAD_NAMING.md) and the dependencies; for the run, the isolation
measurement (isolation_probe), the cwd scheme it implies (placement_contract)
and the last roots audit (project_roots_audit, R6).

The roster is checked whole, every violation in one list (``IssueCollector``
of the validator line, ``validate_department_leads`` of the R30 line). One
that does not assemble does not start the run: before any task is reserved
the stop goes through the one door (``blocked_runs.stop_run``) to the on-call
with the full list, holding every task not yet settled, and the board
says so before the first task. The on-call's plan change asked from such a
ticket must leave the roster whole (``requires_roster``), not just its
requester - one list, one round.

The roster is never written into plan.json: that would change the plan
digest and break the PLAN_VERIFIED receipt. It is a snapshot the runtime
alone writes (``roster.json`` next to run-state), stamped with the
``plan_sha256`` it was built from, rebuilt at the bootstrap, after a
committed plan change, and by the gate whenever the stamp is not the current
plan's or the last build was incomplete. The replanner edits the graph only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Collection, Mapping

from .plan_issues import IssueCollector, PlanIssue, render_issues

ROSTER_FILE = "roster.json"
ROSTER_SCHEMA_VERSION = 1
STAFFING_STOP_KIND = "staffing"
ON_CALL_ROLE = "Pipeline Engineer"
# The route every stop takes, as the door enforces it (blocked_runs): the
# on-call first, always; the owner only with a ticket the on-call hands up
# with a reason from her closed list (R13), or one the on-call closed twice
# and saw come back (R23).
ESCALATION_ROUTE = (
    "Pipeline Engineer (on-call): every stop, first",
    "owner: only a ticket the on-call escalates with an R13 reason code, "
    "or a stop it closed twice that came back (R23)",
)


def roster_path(state_dir: Path) -> Path:
    return Path(state_dir) / ROSTER_FILE


def load_roster(state_dir: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(roster_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def escalation_route(state_dir: Path) -> list[str]:
    """The route a stop takes, read from the roster; the rule's own when there is none.

    Routing reads the on-call and the route from here, and the rule does
    not change with it: every stop goes to the on-call first, no exceptions.
    """

    roster = load_roster(state_dir) or {}
    route = (roster.get("run") or {}).get("escalation_route")
    if isinstance(route, list) and route and ON_CALL_ROLE in str(route[0]):
        return [str(item) for item in route]
    return list(ESCALATION_ROUTE)


def _exempt(plan: Any, settled: Collection[str]) -> frozenset[str]:
    """Tasks with no acceptance ahead of them in this roster.

    A settled task (VERIFIED or CANCELLED) was judged or will never be; a
    migrated v0.8 task keeps its historical self-acceptance (R8/R29) - the
    same exemption plan admission gives it.
    """

    from .plan_admission import legacy_exempt_task_ids

    return legacy_exempt_task_ids(plan.tasks, legacy_serial=plan.legacy_serial) | frozenset(settled)


def collect_graph_issues(c: Any, plan: Any, settled: Collection[str]) -> None:
    """What the graph alone decides: a known role and one lead per profession.

    ``inherited`` is the plan itself: the roster judges the plan as it
    stands, its settled tasks as history - a lead they had is not a
    violation, and a profession with accepted work keeps its lead.
    """

    from .department_runtime import validate_department_leads

    exempt = _exempt(plan, settled)
    roles = plan.role_map
    for task in plan.tasks:
        if task.id not in exempt and task.role not in roles:
            c.add(
                "roles", f"task {task.id}.role",
                f"staffing: task {task.id} is staffed by role {task.role!r}, which is not a "
                "role of the plan",
            )
    for message in validate_department_leads(
        plan.tasks, plan.roles, plan.departments, exempt=exempt,
        settled=settled, inherited=plan, report_unknown=True,
    ):
        c.add("leads", "plan.tasks", message)


def graph_issues(plan: Any, settled: Collection[str] = ()) -> list[str]:
    """The roster's graph violations of a plan, as plain messages (plan admission)."""

    c = IssueCollector()
    collect_graph_issues(c, plan, settled)
    exempt = _exempt(plan, settled)
    for task in plan.tasks:
        if task.id not in exempt and task.role in plan.role_map:
            c.check("threads", f"task {task.id}", _thread_names, plan, task, None)
            c.check("ladder", f"task {task.id}", _ladder, task)
    return [item.message for item in c.issues]


def build_roster(
    plan: Any,
    state: Any,
    *,
    state_dir: Path,
    memory: Any | None,
    screening: bool,
    occasion: str,
    at: str | None = None,
) -> dict[str, Any]:
    """The whole roster and every violation of it; never raises on the plan's account."""

    from .department_runtime import settled_task_ids
    from .plan_verification import plan_sha256
    from .run_state import utc_now

    settled = settled_task_ids(getattr(state, "task_states", None))
    exempt = _exempt(plan, settled)
    c = IssueCollector()
    collect_graph_issues(c, plan, settled)
    run = _run_section(plan, state, state_dir)
    rubrics: dict[str, Any] = {}
    tasks = [
        _task_entry(c, plan, task, state=state, settled=settled, exempt=exempt,
                    memory=memory, rubrics=rubrics, screening=screening,
                    isolation_proven=run["isolation"]["outcome"] == "PASS"
                    and run["isolation"]["of_this_root"])
        for task in plan.tasks
    ]
    return {
        "schema_version": ROSTER_SCHEMA_VERSION,
        "plan_sha256": plan_sha256(plan),
        "graph_version": plan.graph_version,
        "built_at": at or utc_now(),
        "occasion": occasion,
        "complete": not c.issues,
        "issues": [item.to_dict() for item in c.issues],
        "run": run,
        "tasks": tasks,
    }


def _run_section(plan: Any, state: Any, state_dir: Path) -> dict[str, Any]:
    from .isolation_probe import load_record

    record = load_record(Path(state_dir)) or {}
    root = str(Path(state_dir).parent)
    outcome = str(record.get("outcome") or "NOT_MEASURED")
    audit = getattr(state, "roots_audit", None)
    findings = [
        {"code": str(item.get("code") or ""), "status": str(item.get("status") or "")}
        for item in ((audit or {}).get("findings") or ())
        if isinstance(item, Mapping)
    ]
    return {
        "model_strategy": plan.model_strategy,
        "on_call": ON_CALL_ROLE,
        "escalation_route": list(ESCALATION_ROUTE),
        "isolation": {
            "outcome": outcome,
            "measured_at": record.get("measured_at"),
            "of_this_root": record.get("root") == root,
            # Contract 2 files a staged task's thread at the root under its
            # staged profile; it is used only after the measurement proved
            # the profile keeps the root read-only (placement_contract).
            "staged_cwd": "root with the task's staged profile (contract 2)"
            if outcome == "PASS" and record.get("root") == root
            else "the task's staged workspace, outside the project in Desktop (contract 1)",
        },
        "roots_audit": {"recorded": isinstance(audit, Mapping), "findings": findings},
    }


def _task_entry(
    c: Any, plan: Any, task: Any, *, state: Any, settled: Collection[str], exempt: Collection[str],
    memory: Any | None, rubrics: dict[str, Any], screening: bool, isolation_proven: bool,
) -> dict[str, Any]:
    from .artifact_staging import task_requires_staging
    from .department_acceptance import DepartmentAcceptanceError
    from .department_runtime import derive_task_department

    role = plan.role_map.get(task.role)
    entry: dict[str, Any] = {
        "id": task.id,
        "title": task.title,
        "role": {"id": task.role, "name": getattr(role, "name", None)},
        "acceptance_ahead": task.id not in exempt,
        "department": None,
        "lead": None,
        "rubric": None,
    }
    if task.id in settled:
        entry["note"] = "settled: its acceptance is over"
    elif task.id in exempt:
        entry["note"] = "migrated v0.8 task: historical self-acceptance (R8)"
    try:
        department = derive_task_department(plan, task, settled=settled)
    except (DepartmentAcceptanceError, KeyError) as exc:
        department = None
        if task.id not in exempt:
            # The derivation says more than the lead check's class ("missing
            # for: M03"): which leads the profession's accepted work had.
            # Kept with the task and shown to the on-call, not counted twice.
            entry["department_error"] = str(exc)
        # Every such refusal of a task still to be accepted is named by the
        # lead check above (it derives every task its classes did not name);
        # this only keeps one that slipped past it from vanishing.
        if task.id not in exempt and not any(task.id in item.message for item in c.issues):
            c.add("leads", f"task {task.id}", f"R30: task {task.id}: {exc}")
    if department is not None:
        lead = plan.role_map.get(department.lead_role_id)
        entry["department"] = {"id": department.id, "name": department.name}
        entry["lead"] = {"id": department.lead_role_id, "name": getattr(lead, "name", None)}
        if task.id not in exempt:
            entry["rubric"] = _rubric(c, plan, department, memory, rubrics)
    if role is not None and task.id not in exempt:
        entry["threads"] = c.check("threads", f"task {task.id}", _thread_names, plan, task, department) or None
    elif role is not None:
        try:
            entry["threads"] = _thread_names(plan, task, department)
        except ValueError:
            entry["threads"] = None
    entry["skills"] = _skills(state, task, screening)
    entry["acceptance"] = _acceptance(task)
    # A failed check returns FAILED, which is not JSON: None in the snapshot.
    entry["ladder"] = (c.check("ladder", f"task {task.id}", _ladder, task) or None) if task.id not in exempt else _ladder_or_none(task)
    entry["model"] = _model(plan, task)
    entry["on_call"] = ON_CALL_ROLE
    entry["escalation_route"] = list(ESCALATION_ROUTE)
    staged = task_requires_staging(task, legacy_serial=plan.legacy_serial)
    entry["placement"] = {
        "workspace": "staged" if staged else "root",
        "cwd": ("root (staged profile)" if isolation_proven else "staged workspace") if staged else "root",
    }
    entry["depends_on"] = list(task.depends_on)
    entry["dependency_outputs"] = list(task.context.dependency_outputs)
    entry["outputs"] = [item.id for item in task.outputs]
    return entry


def _rubric(c: Any, plan: Any, department: Any, memory: Any | None, cache: dict[str, Any]) -> dict[str, Any] | None:
    """The department's current rubric; version 1 is written when it has none (R30).

    Written here only where the runtime writes it anyway - the bootstrap, a
    committed plan change, the reservation under the coordinator lock.
    """

    from .department_runtime import ensure_department_rubric

    if department.id in cache:
        return cache[department.id]
    if memory is None:
        cache[department.id] = None
        return None
    try:
        reference, drift = ensure_department_rubric(memory, plan, department)
        value: dict[str, Any] | None = {**reference.to_dict(), "lead_profile_changed": bool(drift)}
    except Exception as exc:  # noqa: BLE001 - every failure is a named violation of the roster
        value = None
        c.add(
            "rubric", f"department {department.id}",
            f"staffing: the rubric of department {department.id!r} (lead "
            f"{department.lead_role_id!r}) could not be read or written: {exc}",
        )
    cache[department.id] = value
    return value


def _thread_names(plan: Any, task: Any, department: Any | None) -> dict[str, Any]:
    """The names its threads will carry (docs/THREAD_NAMING.md), made now, not threads."""

    from .thread_titles import task_phase_thread_title

    names: dict[str, Any] = {
        "worker": task_phase_thread_title(
            task_id=task.id, task_title=task.title, kind="implementation",
            role_name=plan.role_map[task.role].name,
        ),
        "lead": None,
    }
    if department is not None and department.lead_role_id in plan.role_map:
        names["lead"] = task_phase_thread_title(
            task_id=task.id, task_title=task.title, kind="verifier",
            role_name=plan.role_map[department.lead_role_id].name, departmental_verifier=True,
        )
    return names


def _ladder(task: Any) -> dict[str, Any]:
    from .models import PUBLIC_REASONING

    start = task.reasoning or "medium"
    if start not in PUBLIC_REASONING:
        raise ValueError(
            f"staffing: task {task.id} starts at effort {start!r}, which is not a step of the "
            f"hiring ladder {list(PUBLIC_REASONING)}"
        )
    return {
        "start_effort": start,
        "max_revision_attempts": task.verification.max_revision_attempts,
        "steps": list(PUBLIC_REASONING[PUBLIC_REASONING.index(start):]),
    }


def _ladder_or_none(task: Any) -> dict[str, Any] | None:
    try:
        return _ladder(task)
    except ValueError:
        return None


def _skills(state: Any, task: Any, screening: bool) -> dict[str, Any]:
    record = (getattr(state, "task_hiring", None) or {}).get(task.id)
    hired: list[str] = []
    if isinstance(record, Mapping):
        for outcome in (record.get("decision") or {}).get("outcomes") or ():
            if isinstance(outcome, Mapping) and outcome.get("status") in {"hired", "installed"}:
                skill = outcome.get("skill") or {}
                hired.append(str(skill.get("id") or outcome.get("capability") or "?"))
    if isinstance(record, Mapping):
        status = "unscreened" if record.get("unscreened") else "screened"
    else:
        status = "at task start" if screening else "off"
    return {
        "planned": [f"{item.id}@{item.version}" for item in task.loaded_skills],
        "hired": hired,
        "screening": status,
    }


def _acceptance(task: Any) -> dict[str, Any]:
    from .acceptance_floor import is_clean_suite_command

    policy = task.verification
    return {
        "acceptance_class": getattr(task.acceptance_class, "value", str(task.acceptance_class)),
        "policy": policy.policy,
        "required": policy.required,
        "deterministic_checks": [item.id for item in policy.deterministic_checks],
        "clean_suite": any(is_clean_suite_command(item) for item in policy.deterministic_checks),
        "max_revision_attempts": policy.max_revision_attempts,
    }


def _model(plan: Any, task: Any) -> dict[str, Any]:
    from .models import MODEL_IDS, ModelRoutingError, logical_model

    try:
        model = MODEL_IDS[logical_model(plan.model_strategy, task.execution_mode)]
    except (ModelRoutingError, KeyError):
        model = "host settings"
    return {"model": model, "effort": task.reasoning or "medium", "execution_mode": task.execution_mode}


def write_roster(state_dir: Path, roster: Mapping[str, Any]) -> None:
    from .plan import atomic_json

    atomic_json(roster_path(state_dir), dict(roster))


def refresh_roster(
    state_dir: Path, plan: Any, state: Any, *, occasion: str, memory: Any | None = None
) -> dict[str, Any]:
    """Build and write the roster; the runtime is its only writer."""

    state_dir = Path(state_dir)
    if memory is None:
        from .memory import ProjectMemory

        memory = ProjectMemory(state_dir.parent)
    roster = build_roster(
        plan, state, state_dir=state_dir, memory=memory,
        screening=_screening_applies(state_dir, plan), occasion=occasion,
    )
    write_roster(state_dir, roster)
    return roster


def _screening_applies(state_dir: Path, plan: Any) -> bool:
    try:
        from .config import load_config
        from .lifecycle_screening import screening_applies

        return bool(screening_applies(load_config(state_dir.parent), plan))
    except Exception:  # noqa: BLE001 - only a label of the roster: "at task start" or "off"
        return False


def roster_issues(roster: Mapping[str, Any] | None) -> tuple[PlanIssue, ...]:
    return tuple(
        PlanIssue(str(item.get("stage") or ""), str(item.get("path") or ""), str(item.get("message") or ""))
        for item in (roster or {}).get("issues") or ()
        if isinstance(item, Mapping)
    )


def staffing_gate(cfg: Any, plan: Any, state: Any) -> dict[str, Any]:
    """Before any reservation: the current plan's roster, and a stop when it is not whole.

    Called under the coordinator lock by every reservation pass, after the
    plan gate and before the frontier is read. A roster whose stamp is the
    current plan's and that assembled is taken as it is; otherwise it is
    rebuilt now. One that does not assemble is a stop through the one door:
    a ticket for the on-call with the full list, holding every task not yet
    settled - the frontier read next is then empty, and the pass reserves
    the on-call next to nothing. A ticket already open for it is not filed
    again, only widened to a task the graph gained since; the on-call reads
    the current list from the roster in its package (``stop_diagnosis``).
    """

    from .plan_verification import plan_sha256

    roster = load_roster(cfg.state_dir)
    if not roster or roster.get("plan_sha256") != plan_sha256(plan) or not roster.get("complete"):
        try:
            roster = refresh_roster(cfg.state_dir, plan, state, occasion="reservation")
        except Exception as exc:  # noqa: BLE001 - a roster that cannot be built is a stop, never a raise
            roster = {
                "plan_sha256": plan_sha256(plan), "complete": False,
                "issues": [{"stage": "runtime", "path": "roster",
                            "message": f"staffing: the roster could not be built: {exc}"}],
            }
    if roster.get("complete"):
        return roster
    _stop(cfg, plan, state, roster)
    return roster


def _stop(cfg: Any, plan: Any, state: Any, roster: Mapping[str, Any]) -> str | None:
    from .blocked_runs import stop_run
    from .department_runtime import settled_task_ids
    from .pipeline_engineer import PipelineIncidentStore
    from .run_state import utc_now

    # The run does not start: every task not settled is held - a migrated
    # v0.8 task too, though no acceptance is ahead of it. The docs once said
    # "every task still to be accepted"; the code held them all, and the
    # independent check (25 Sep 2026) asked which. All: the roster is the
    # run's, and a run that did not assemble starts nothing.
    settled = settled_task_ids(state.task_states)
    held = [task.id for task in plan.tasks if task.id not in settled]
    store = PipelineIncidentStore(cfg.state_dir)
    open_tickets = [
        item for item in store.load().get("incidents") or ()
        if not item.get("resolved_at") and (item.get("system_state") or {}).get("stop_kind") == STAFFING_STOP_KIND
    ]
    if open_tickets:
        # One ticket per stop, but it holds the graph as it is now: a task a
        # plan change added while the roster stayed incomplete (a rubric that
        # cannot be read passes plan admission) was named by no ticket and
        # could be reserved. It is added to the open ticket's hold.
        named = {str(task) for item in open_tickets for task in item.get("affected_task_ids") or ()}
        missing = [task_id for task_id in held if task_id not in named]
        ticket = str(open_tickets[-1].get("incident_id"))
        if not missing:
            return ticket
        try:
            store.hold_more_tasks(ticket, missing, at=utc_now())
            return ticket
        except Exception:  # noqa: BLE001 - never unheld: the door files them a ticket of their own
            held = missing
    issues = roster_issues(roster)
    listed = render_issues(issues) if issues else "the roster did not assemble"
    details = [
        f"{item.get('id')}: {item.get('department_error')}"
        for item in roster.get("tasks") or () if isinstance(item, Mapping) and item.get("department_error")
    ]
    if details:
        listed += "\nPer task: " + "; ".join(details)
    return stop_run(
        cfg, state, stop_kind=STAFFING_STOP_KIND, phase="STAFFING_BLOCKED",
        reason=f"staffing: the run's roster did not assemble - {listed}",
        summary=(
            f"The roster of plan v{plan.graph_version} did not assemble "
            f"({len(issues)} violation{'s' if len(issues) != 1 else ''}); no task starts until it does."
        ),
        at=utc_now(), task_ids=held,
        system_state={
            "diagnosis": listed,
            "roster_issues": [item.to_dict() for item in issues],
            "plan_sha256": str(roster.get("plan_sha256") or ""),
            "recommendation": (
                "ask the replanner (devops-request-plan-change) for a graph whose roster is "
                "whole - every violation in the list at once: each task still to be accepted "
                "names its profession's one Lead Role in verification.verifier_role (another "
                "profession, a role of the plan), and a profession with accepted work keeps a "
                "lead that accepted it; a rubric that cannot be read is a runtime defect to "
                "repair, or a stray record to supersede (devops-supersede-rubric)"
            ),
        },
        # One cause for the whole run: R23 counts its closures across plan
        # changes, so a roster the replanner cannot make whole reaches her
        # with a report instead of cycling through the on-call.
        signal_key="roster",
    )
