"""Departments, leads and rubrics, derived by the runtime (R30).

R30 was in the code whole and in force for nobody. It switched on only for a
task that declared two logical resources, department-binding and
rubric-binding; no planner wrote them, the skill's example named a faceless
"acceptance-reviewer", and the live plan of a real run (the art run, 18
tasks) carried neither. Without them the verifier was ``verifier_role or
task.role`` - the worker's own profession when the planner left the field
out - the rules block told it R30 was "NOT in force", and the first task of
a department could not be bound even in principle: its rubric pin had to come
from the evidence of a VERIFIED dependency, and M01 has none.

Now nothing of it is the model's to write or to forget:

- the department is a function of the WORKER's profession. Every task of one
  role names the same lead in ``verification.verifier_role``; that lead is
  the department's, and the verifier of every task of the role. A planner
  that gives one profession two leads, no lead, or itself as the lead is
  refused at admission, every such task in one list
  (``validate_department_leads``). An earlier design keyed the department by
  each task's own verifier_role, and the independent check named why that is
  R30 by name only: two tasks of one profession could reach two leads with
  two rubrics, and nothing would notice;
- the lead of a profession is the one that still has work of it to accept.
  A task already VERIFIED or CANCELLED keeps the lead it had and
  names nothing for the rest (``settled_task_ids``). The first cut read
  every task of the role, and the independent check reproduced where that
  goes: a plan from before R30 - admission let one profession name several
  verifier_role values then - with two VERIFIED tasks of one role and two
  leads refused every later plan change (a VERIFIED task cannot change), the
  role's other tasks could never get a lead, the on-call had nothing to fix,
  and the stop went to her;
- once a profession's work has been accepted, its lead does not change: a
  plan change names one of the leads that accepted it, or keeps the one the
  current plan names. The exemption above first let any change move the
  rest of a profession to a lead of the replanner's choosing the moment one
  task was VERIFIED - reproduced on the art-run shape: M01 VERIFIED by
  art-reviewer, M03 moved to a new "lax-lead" whose only expectation was
  "Anything goes.", admitted with no issue - and the new department started
  from a fresh version 1 built from a profile the replanner wrote: a new
  acceptance standard with no outcome evidence, the very thing
  ``ensure_department_rubric`` refuses to do for a changed profile. Only
  VERIFIED work under a lead that could lead today locks it: a CANCELLED
  task, or pre-R30 work verified with no lead, by its own profession or by
  legacy-worker, has no standard to keep (``_moved_leads``);
- the derivation is never written into plan.json. Doing so would change the
  plan digest and break the PLAN_VERIFIED receipt of every running run; a
  saved plan is never refused on load for it either - a task with no lead is
  stopped when its lead is needed (``department_gate``);
- version 1 of the rubric is written by the runtime from the lead's profile,
  before the first task (bootstrap), after a committed plan change, and
  under the coordinator lock when a verifier is reserved - the last one is
  what lands the rubric on a run already in progress. A later version comes
  only through outcome evidence, proposed by the lead or the on-call, never
  by a worker of the department (``authorize_rubric_proposal``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Collection, Iterable, Mapping, Sequence

from .department_acceptance import (
    RUNTIME_RUBRIC_AUTHOR,
    RUNTIME_RUBRIC_TOOL,
    DepartmentAcceptanceError,
    DepartmentContract,
    DepartmentDefinition,
    DepartmentRubric,
    LoadedDepartmentAcceptance,
    LoadedDepartmentRubric,
    RubricCriterion,
    RubricReference,
    _department_name,
    load_department_rubric,
    stored_rubric_versions,
    task_department_binding,
    write_runtime_rubric,
)
from .role_specification import role_profile_sha256, role_profile_snapshot


# The core every department's version 1 carries, stated without the text of
# any request: Project Memory outlives a run, and a criterion that embedded
# one run's request would judge the next run's work by it.
CORE_CRITERIA: tuple[RubricCriterion, ...] = (
    RubricCriterion(
        "request-fidelity",
        "The result does what the run's original user request and the task's objective "
        "ask for, as the prompt states them; a deviation is named as an issue, never "
        "accepted silently.",
    ),
    RubricCriterion(
        "dod-coverage",
        "Every Definition of Done item is closed by reproducible evidence recorded in "
        "Project Memory for this task; an item without such evidence is not accepted.",
    ),
    RubricCriterion(
        "independent-evidence",
        "The worker's own tests and statements are evidence to reproduce, not a verdict: "
        "acceptance rests on what the lead re-ran or inspected itself.",
    ),
)
DRIFT_SCOPE_PREFIX = "department-acceptance-drift:"


class SettledTasks(frozenset):
    """Settled task ids that also know which of them a lead actually accepted.

    Both VERIFIED and CANCELLED leave "one profession, one lead", but only a
    VERIFIED task was judged by anyone. The independent check found
    ``_moved_leads`` reading a CANCELLED task as accepted work: one task of a
    profession cancelled before any acceptance locked the lead of the rest,
    and the refusal told the replanner a lead had judged it. The set rides
    through every caller unchanged (plan_change_candidate, the replanner's
    admission) as the plain frozenset it was; ``accepted`` is read only where
    acceptance matters.
    """

    accepted: frozenset[str]

    def __new__(cls, ids: Iterable[str], accepted: Iterable[str] = ()):
        made = super().__new__(cls, ids)
        made.accepted = frozenset(accepted) & made
        return made


def settled_task_ids(task_states: Mapping[str, Any] | None) -> SettledTasks:
    """The tasks whose acceptance is over: VERIFIED or CANCELLED (absorbing)."""

    from .task_state import TERMINAL_TASK_STATES, TaskState

    terminal = {item.value for item in TERMINAL_TASK_STATES}
    states = {str(key): str(value) for key, value in (task_states or {}).items()}
    return SettledTasks(
        (key for key, value in states.items() if value in terminal),
        (key for key, value in states.items() if value == TaskState.VERIFIED.value),
    )


def _accepted_ids(settled: Collection[str]) -> frozenset[str]:
    """What a lead judged: VERIFIED only, when the caller knows (``SettledTasks``)."""

    return getattr(settled, "accepted", frozenset(settled))


def role_lead(plan: Any, role_id: str, *, settled: Collection[str] = ()) -> str:
    """The one lead of a profession: what its tasks still to be accepted name.

    Tasks in ``settled`` are asked only when none of the rest names a lead -
    a task of a plan from before R30 that names none takes the lead its
    profession's accepted work had, if that was one.
    """

    live: set[str] = set()
    done: set[str] = set()
    for task in plan.tasks:
        if task.role == role_id and task.verification.verifier_role:
            (done if task.id in settled else live).add(str(task.verification.verifier_role))
    leads = sorted(live or done)
    if not leads:
        raise DepartmentAcceptanceError(
            f"no Lead Role is defined for role {role_id!r}: none of its tasks names one in "
            "verification.verifier_role (R30)"
        )
    if len(leads) > 1 and live:
        raise DepartmentAcceptanceError(
            f"role {role_id!r} has several leads {leads}; one profession has exactly one "
            "lead (R30)"
        )
    if len(leads) > 1:
        raise DepartmentAcceptanceError(
            f"no Lead Role is defined for role {role_id!r}: its accepted tasks were judged by "
            f"several leads {leads} and none of its other tasks names one in "
            "verification.verifier_role (R30)"
        )
    return leads[0]


def derive_task_department(plan: Any, task: Any, *, settled: Collection[str] = ()) -> DepartmentDefinition:
    """The department of a task: the one its worker's profession belongs to.

    A settled task's is the department of the lead that judged it.
    """

    own = task.verification.verifier_role
    lead = own if own and task.id in settled else role_lead(plan, task.role, settled=settled)
    if own and own != lead:  # guarded by role_lead; kept for a hand-built plan
        raise DepartmentAcceptanceError(
            f"task {task.id} names lead {own!r}, its role's lead is {lead!r}"
        )
    if lead == task.role:
        raise DepartmentAcceptanceError(
            f"task {task.id}: the lead {lead!r} is the worker's own profession; a lead "
            "judges another profession's work (R30)"
        )
    roles = plan.role_map
    if lead not in roles:
        raise DepartmentAcceptanceError(
            f"task {task.id}: Lead Role {lead!r} is not a role of the plan"
        )
    department = _department_for_lead(plan, lead)
    binding = task_department_binding(task)
    if binding is not None and binding.department_id != department.id:
        raise DepartmentAcceptanceError(
            f"task {task.id} binds department {binding.department_id!r}, but its lead "
            f"{lead!r} heads department {department.id!r}"
        )
    return department


def _department_for_lead(plan: Any, lead: str) -> DepartmentDefinition:
    """Name and id: a declared entry wins; a 0.13 binding keeps its id; else the lead's."""

    declared = [item for item in plan.departments if item.lead_role_id == lead]
    if len(declared) > 1:
        raise DepartmentAcceptanceError(
            f"Lead Role {lead!r} heads several declared departments "
            f"{sorted(item.id for item in declared)}"
        )
    if declared:
        return DepartmentDefinition(declared[0].id, declared[0].name, lead)
    bound: set[str] = set()
    for task in plan.tasks:
        if task.verification.verifier_role != lead:
            continue
        try:
            binding = task_department_binding(task)
        except DepartmentAcceptanceError:
            continue  # reported for that task by its own derivation
        if binding is not None:
            bound.add(binding.department_id)
    if len(bound) > 1:
        raise DepartmentAcceptanceError(
            f"tasks judged by {lead!r} bind several departments {sorted(bound)}"
        )
    department_id = bound.pop() if bound else lead
    return DepartmentDefinition(
        department_id, _department_name(department_id, plan.role_map[lead].name), lead
    )


class _Graph:
    """What derivation reads of a plan, for a graph still being admitted."""

    def __init__(self, tasks: Sequence[Any], roles: Sequence[Any], departments: Sequence[Any]):
        self.tasks = tuple(tasks)
        self.roles = tuple(roles)
        self.departments = tuple(departments)
        self.role_map = {role.id: role for role in self.roles}


def validate_department_leads(
    tasks: Sequence[Any],
    roles: Sequence[Any],
    departments: Sequence[Any] = (),
    *,
    exempt: Iterable[str] = (),
    settled: Collection[str] = (),
    inherited: Any | None = None,
    report_unknown: bool = True,
) -> list[str]:
    """Every R30 lead violation of a graph, in one list, never the first alone.

    ``exempt`` are the tasks that keep what they had: the migrated v0.8
    tasks of a ``legacy_serial`` plan whose acceptance contract is untouched
    (R8/R29 provenance), and on a plan change every task it leaves as it
    was; the new and the changed tasks need a lead like any other.
    ``settled`` are the tasks whose acceptance is over (``settled_task_ids``):
    they do not take part in "one profession, one lead" - the lead of a
    profession is the one that still has work of it to accept - while an
    exempt task still to be accepted does. ``report_unknown`` is off where
    the graph stage already reports an unknown verifier role.

    ``inherited`` is the current plan of a plan change; ``settled`` means
    nothing without it. A settled task is history only as the current plan
    holds it - a split among accepted tasks is exempt because it is already
    there, never because a change wrote it - and a profession with accepted
    work keeps its lead (``_moved_leads``).
    """

    skip = frozenset(exempt)
    done = _history(tasks, settled, inherited)
    role_ids = {role.id for role in roles}
    missing: list[str] = []
    own: list[str] = []
    unknown: list[str] = []
    by_role: dict[str, dict[str, list[str]]] = {}
    for task in tasks:
        lead = task.verification.verifier_role
        if lead and task.id not in done:
            by_role.setdefault(task.role, {}).setdefault(lead, []).append(task.id)
        if task.id in skip:
            continue
        if not lead:
            missing.append(task.id)
        elif lead == task.role:
            own.append(f"{task.id} ({lead})")
        elif lead not in role_ids:
            unknown.append(f"{task.id} ({lead!r})")
    found: list[str] = []
    if missing:
        found.append(
            "R30: every task names its department's Lead Role in "
            f"verification.verifier_role; missing for: {', '.join(missing)}"
        )
    if own:
        found.append(
            "R30: a lead judges another profession's work, never its own; verifier_role "
            f"equals the task's role for: {', '.join(own)}"
        )
    if unknown and report_unknown:
        found.append(f"R30: verifier_role names no role of the plan for: {', '.join(unknown)}")
    for role, leads in sorted(by_role.items()):
        if len(leads) > 1:
            listed = "; ".join(f"{lead}: {', '.join(ids)}" for lead, ids in sorted(leads.items()))
            found.append(
                f"R30: one profession has exactly one lead; tasks of role {role!r} name "
                f"several - {listed}"
            )
    moved = _moved_leads(tasks, by_role, done, inherited, settled, {role.id: role for role in roles})
    found.extend(moved.values())
    # The rest - a declared department or a 0.13 binding that disagrees with
    # the lead - for every task the classes above did not already name.
    named = {item.split(" ")[0] for item in (*own, *unknown)} | set(missing)
    split = {role for role, leads in by_role.items() if len(leads) > 1} | set(moved)
    graph = _Graph(tasks, roles, departments)
    for task in tasks:
        if task.id in skip or task.id in named or task.role in split or task.role not in by_role:
            continue
        if any(lead == task.role or lead not in role_ids for lead in by_role[task.role]):
            continue
        try:
            derive_task_department(graph, task, settled=done)
        except DepartmentAcceptanceError as exc:
            found.append(f"R30: task {task.id}: {exc}")
    return found


def _history(tasks: Sequence[Any], settled: Collection[str], inherited: Any | None) -> frozenset[str]:
    """The settled tasks whose lead is history: as the current plan holds them."""

    if inherited is None:
        return frozenset()
    before = inherited.task_map
    return frozenset(
        task.id for task in tasks
        if task.id in settled and task.id in before
        and before[task.id].verification.verifier_role == task.verification.verifier_role
    )


def _moved_leads(
    tasks: Sequence[Any],
    by_role: Mapping[str, Mapping[str, list[str]]],
    history: frozenset[str],
    inherited: Any | None,
    settled: Collection[str],
    roles: Mapping[str, Any],
) -> dict[str, str]:
    """Professions a change moves, after acceptance began, to a lead they never had.

    The lead of a profession with accepted work is one of the leads that
    accepted it, or the one the current plan names for its tasks still to be
    accepted (a run from before R30 may hold both). Only when the accepted
    work had several leads is there a choice, and it is among them.

    Only a lead that could lead today counts, on either side. The
    independent check reproduced on the art-run shape, M01 VERIFIED:
    before R30 a character-artist task could name no verifier_role (the
    verifier was ``verifier_role or task.role``) or its own role, and both
    were admitted. Such a profession's "accepted lead" was 'None' or
    'character-artist'; the gate stopped M03 for want of a lead, the
    on-call's change naming art-reviewer was refused as a new lead, naming
    character-artist was refused as its own profession - no change could
    pass, and the stop went to her. The same for work accepted by the
    generic legacy-worker. A profession whose accepted work had no lead
    that could lead has no standard to keep: the planner names its lead as
    before any acceptance. And only VERIFIED work was accepted - a
    CANCELLED task was judged by no one (``SettledTasks.accepted``).
    """

    judged = _accepted_ids(settled)
    accepted: dict[str, set[str]] = {}
    for task in tasks:
        if task.id in history and task.id in judged and _can_lead(task.verification.verifier_role, task.role, roles):
            accepted.setdefault(task.role, set()).add(str(task.verification.verifier_role))
    found: dict[str, str] = {}
    for role, leads in sorted(by_role.items()):
        if role not in accepted or len(leads) != 1:
            continue
        kept = {
            str(task.verification.verifier_role) for task in inherited.tasks
            if task.role == role and task.id not in settled
            and _can_lead(task.verification.verifier_role, task.role, roles)
        }
        ((lead, ids),) = leads.items()
        if lead in accepted[role] | kept:
            continue
        found[role] = (
            f"R30: a profession keeps its lead once its work has been accepted - a new lead "
            f"would judge the rest by a new rubric with no outcome evidence; tasks of role "
            f"{role!r} name {lead!r} ({', '.join(ids)}), while its accepted work was judged by "
            f"{sorted(accepted[role])} and the current plan names "
            f"{sorted(kept) if kept else 'no lead'} for the rest: name one of those"
        )
    return found


def _can_lead(lead: Any, role_id: str, roles: Mapping[str, Any]) -> bool:
    """A lead R30 admits: named, another profession, a role of the plan, not legacy-worker."""

    role = roles.get(str(lead)) if lead else None
    return (
        role is not None and lead != role_id
        and role.id != "legacy-worker" and role.name.casefold() != "legacy serial worker"
    )


def lead_profile_digest(role: Any) -> str:
    return role_profile_sha256(role_profile_snapshot(role))


def derive_department_rubric(plan: Any, department: DepartmentDefinition) -> DepartmentRubric:
    """Version 1: the core, then the lead's own expectations of accepted work."""

    lead = plan.role_map[department.lead_role_id]
    expectations = tuple(lead.verification_expectations) or tuple(lead.responsibilities)
    criteria = CORE_CRITERIA + tuple(
        RubricCriterion(f"lead-expectation-{index}", text)
        for index, text in enumerate(expectations, 1)
    )
    standards = tuple(dict.fromkeys((*lead.responsibilities, *lead.domain_focus)))
    return DepartmentRubric(
        department_id=department.id,
        version=1,
        criteria=criteria,
        standards=standards,
    )


def ensure_department_rubric(
    memory: Any, plan: Any, department: DepartmentDefinition
) -> tuple[RubricReference, bool]:
    """The department's current rubric, writing version 1 when it has none.

    Returns the reference and whether the lead's profile changed since
    version 1 was derived. A changed profile does not rewrite the rubric:
    that would be a new standard without outcome evidence, and Project
    Memory outlives the run - a new run with a role of the same id would
    otherwise inherit, or silently replace, another run's standard. It is
    recorded once as a defect and shown to the lead, who may propose a
    version 2 through outcome evidence.
    """

    history = stored_rubric_versions(memory, department.id)
    if history:
        return history[-1].reference, _profile_drift(memory, plan, department, history[0])
    lead = plan.role_map[department.lead_role_id]
    reference = write_runtime_rubric(
        memory,
        derive_department_rubric(plan, department),
        evidence_result={
            "department_id": department.id,
            "lead_role_id": lead.id,
            "lead_role_version": lead.version,
            "lead_profile_sha256": lead_profile_digest(lead),
        },
    )
    return reference, False


def ensure_all_department_rubrics(memory: Any, plan: Any, *, settled: Collection[str] = ()) -> dict[str, str]:
    """Version 1 for every department of the plan; failures returned, never raised.

    Called where a failure must not stop what called it: the bootstrap and a
    committed plan change. A department that could not be written here is
    written - or stopped for the on-call - when its verifier is reserved.
    """

    failures: dict[str, str] = {}
    seen: set[str] = set()
    for task in plan.tasks:
        try:
            department = derive_task_department(plan, task, settled=settled)
        except DepartmentAcceptanceError:
            continue
        if department.id in seen:
            continue
        seen.add(department.id)
        try:
            ensure_department_rubric(memory, plan, department)
        except Exception as exc:  # noqa: BLE001 - tolerated; the reservation retries
            failures[department.id] = str(exc)
    return failures


def current_department_rubric(memory: Any, department_id: str) -> LoadedDepartmentRubric | None:
    history = stored_rubric_versions(memory, department_id)
    return history[-1] if history else None


def admit_department_rubric(
    memory: Any, plan: Any, department: DepartmentDefinition
) -> LoadedDepartmentAcceptance:
    """The department's rubric as its lead is admitted by it; the one check of it.

    Version 1 is written when there is none; a history that is not exactly
    1..n (two runtime v1s) is refused with its record ids
    (``stored_rubric_versions``); the current record is attested
    (``load_department_rubric``). The lead's reservation and prompt ask it
    here, and so does the roster (``staffing``), at its build and at every
    reservation pass: the roster used to call a department whole on a
    snapshot taken before a second v1 appeared, the worker started, and the
    task stopped only at its lead's reservation - after its work was done.
    """

    reference, drift = ensure_department_rubric(memory, plan, department)
    contract = DepartmentContract(
        department.id, department.name, department.lead_role_id, reference
    )
    return LoadedDepartmentAcceptance(
        department=contract,
        rubric=load_department_rubric(memory, contract),
        lead_profile_changed=drift,
    )


def load_task_department_acceptance(
    memory: Any, plan: Any, task: Any, *, ensure: bool, settled: Collection[str] = ()
) -> LoadedDepartmentAcceptance:
    """Derive the task's department and load its current rubric, exactly.

    ``ensure`` writes version 1 when the department has none - the verifier's
    reservation and prompt. Completion only reads: a verdict is judged
    against what exists, never against a rubric written after it.
    """

    department = derive_task_department(plan, task, settled=settled)
    if ensure:
        return admit_department_rubric(memory, plan, department)
    history = stored_rubric_versions(memory, department.id)
    if not history:
        raise DepartmentAcceptanceError(
            f"department {department.id!r} has no rubric in Project Memory"
        )
    reference = history[-1].reference
    drift = _profile_drift(memory, plan, department, history[0])
    contract = DepartmentContract(
        department.id, department.name, department.lead_role_id, reference
    )
    return LoadedDepartmentAcceptance(
        department=contract,
        rubric=load_department_rubric(memory, contract),
        lead_profile_changed=drift,
    )


def _profile_drift(
    memory: Any, plan: Any, department: DepartmentDefinition, first: LoadedDepartmentRubric
) -> bool:
    """Whether the lead's profile differs from the one version 1 was derived from."""

    lead = plan.role_map.get(department.lead_role_id)
    if lead is None:
        return False
    try:
        record = memory.get_record(first.reference.record_id)
    except Exception:  # noqa: BLE001 - loaded just above; a vanished record is load's to refuse
        return False
    recorded = ""
    for evidence in record.get("evidence") or ():
        if evidence.get("tool_name") != RUNTIME_RUBRIC_TOOL:
            continue
        try:
            recorded = str(json.loads(str(evidence.get("result") or "{}")).get("lead_profile_sha256") or "")
        except (TypeError, ValueError, AttributeError):
            recorded = ""
    if not recorded or recorded == lead_profile_digest(lead):
        return False
    _record_drift_once(memory, department, lead, recorded)
    return True


def _record_drift_once(memory: Any, department: DepartmentDefinition, lead: Any, recorded: str) -> None:
    scope = f"{DRIFT_SCOPE_PREFIX}{department.id}"
    current = lead_profile_digest(lead)
    statement = (
        f"R30 defect: the profile of Lead Role {lead.id!r} of department {department.id!r} "
        f"changed after rubric version 1 was derived from it ({recorded[:12]} -> "
        f"{current[:12]}). The rubric is not rewritten from the new profile; a version 2 "
        "goes through outcome evidence, proposed by the lead or the on-call."
    )
    try:
        page = memory.list_records(categories=["observation"], scope=scope, limit=20, full_statements=True)
        if any(str(item.get("statement") or "") == statement for item in page.records):
            return
        memory.add_observation(
            statement=statement, created_by=RUNTIME_RUBRIC_AUTHOR, confidence="high", scope=scope
        )
    except Exception:  # noqa: BLE001 - the defect is also in the verifier's prompt
        return


def rubric_for_workers(memory: Any, plan: Any, department: DepartmentDefinition) -> DepartmentRubric:
    """What the worker is told it will be judged by: the current version, or the v1 to come."""

    try:
        current = current_department_rubric(memory, department.id) if memory is not None else None
    except Exception:  # noqa: BLE001 - a broken history is the gate's to stop, not the worker's
        current = None
    return current.rubric if current is not None else derive_department_rubric(plan, department)


def r30_scope(plan: Any, task: Any, *, phase: str | None, memory: Any = None, settled: Collection[str] = ()) -> str:
    """R30 for this task and this reader, stated as facts it can act on.

    One text for every phase told a worker, a reviser and a screener that
    "the rubric is loaded into department_acceptance" - a block only the
    verifier's prompt has. They would look for it and not find it: the same
    class as the 23 minutes a verifier spent looking for a rubric that did
    not exist (rules.py). So the lead is told it is the lead and where its
    rubric is; everyone else whose work it will judge is told who judges,
    by which version, and gets the criteria as expectations of their work.
    """

    try:
        department = derive_task_department(plan, task, settled=settled)
    except DepartmentAcceptanceError as exc:
        return (
            f"In force. No Lead Role is defined for this task ({exc}): its acceptance "
            "will be refused, and the runtime stops the task for the on-call, who has "
            "the plan changed to name its lead. Not a defect of the work, and not a "
            "prerequisite this turn can supply."
        )
    lead = plan.role_map[department.lead_role_id].name
    if phase == "verification":
        return (
            f"In force: department {department.name!r} ({department.id}), Lead Role "
            f"{lead!r} - you. The department's versioned rubric is in "
            "department_acceptance: judge by it and attest it exactly."
        )
    rubric = rubric_for_workers(memory, plan, department)
    criteria = "; ".join(f"{item.id}: {item.requirement}" for item in rubric.criteria)
    return (
        f"In force: your work will be accepted by Lead Role {lead!r} against the rubric "
        f"of department {department.name!r} ({department.id}) v{rubric.version}. Its "
        f"criteria, as expectations of your work: {criteria}. The rubric is the "
        "runtime's and the lead's: do not look for one or write one."
    )


# The kinds of session that may propose a new version of a department's
# rubric: its lead (a verifier of one of its tasks) and the on-call.
PROPOSER_KINDS = frozenset({"verifier", "pipeline_engineer"})


def authorize_rubric_proposal(root: Path, department_id: str, caller_thread_id: str) -> str:
    """Refuse a proposal from anyone but the department's lead or the on-call.

    Returns the proposer's role name. The worker being judged used to be
    able to write the rubric it is judged by - a model's MCP call with any
    evidence was the whole door. The caller is known by its thread
    (CODEX_THREAD_ID, the same identity every ownership guard of the run
    reads), and it must be a pending session of this run: a lead of this
    department, or the on-call.
    """

    from .lifecycle_base import PENDING_SESSION_STATUSES, _session_kind
    from .config import load_config
    from .plan import load_plan
    from .run_state import StateStore

    thread = str(caller_thread_id or "").strip()
    if not thread:
        raise DepartmentAcceptanceError(
            "a rubric version is proposed from the lead's or the on-call's own thread: "
            "run `codex-autopilot department-rubric-propose` there (CODEX_THREAD_ID)"
        )
    cfg = load_config(Path(root))
    state = StateStore(cfg.state_dir).load()
    plan = load_plan(cfg.state_dir, cfg.profile)
    sessions = [
        item
        for item in state.worker_sessions
        if str(item.get("thread_id") or "") == thread
        and item.get("status") in PENDING_SESSION_STATUSES
    ]
    if not sessions:
        raise DepartmentAcceptanceError(
            "this thread is no pending session of the run; only the department's lead or "
            "the on-call proposes a rubric version"
        )
    session = sessions[-1]
    kind = _session_kind(session)
    if kind not in PROPOSER_KINDS:
        raise DepartmentAcceptanceError(
            f"a {kind} session never changes the standard it is judged by; the department's "
            "lead or the on-call proposes a rubric version"
        )
    if kind == "pipeline_engineer":
        return "Pipeline Engineer"
    task = plan.task_map.get(str(session.get("task_id") or ""))
    department = derive_task_department(plan, task, settled=settled_task_ids(state.task_states)) if task is not None else None
    if department is None or department.id != department_id:
        raise DepartmentAcceptanceError(
            f"this lead judges department {department.id if department else None!r}, not "
            f"{department_id!r}"
        )
    return plan.role_map[department.lead_role_id].name


def verdict_acceptance(
    memory: Any, plan: Any, task: Any, session: Mapping[str, Any], attested: RubricReference | None,
    *, settled: Collection[str] = (),
) -> tuple[LoadedDepartmentAcceptance | None, tuple[str, bool] | None]:
    """The department a verdict is judged against, and why it is refused, if it is.

    Completion only reads. A lead launched by a runtime from before R30 was
    never given a rubric - and on a run under way there may be none yet, or
    no lead for its task: that is not its mistake and not a runtime fault
    either; it is refused uncounted, and the fresh lead's reservation writes
    version 1 or stops the task for the on-call. Any other failure to load
    is the runtime's and raises.
    """

    prompt = str((session.get("descriptor") or {}).get("prompt") or "")
    try:
        loaded = load_task_department_acceptance(memory, plan, task, ensure=False, settled=settled)
    except DepartmentAcceptanceError as exc:
        if '"department_acceptance"' in prompt:
            raise
        return None, (
            f"this lead was launched without its department's rubric (a runtime from before "
            f"R30 derived a department for every task): {exc} - a fresh lead is raised",
            False,
        )
    return loaded, attestation_refusal(loaded.department.rubric, attested, session)


def attestation_refusal(
    expected: RubricReference, attested: RubricReference | None, session: Mapping[str, Any]
) -> tuple[str, bool] | None:
    """Why a verdict's `rubric` is refused, and whether it counts toward the limit.

    Three verifiers refused in a row stop the task (``MAX_VERIFICATION_REJECTIONS``),
    and the count is never reset. Two refusals here are not the model's
    mistake, and counting them would turn a runtime event into a stop: the
    rubric advanced to a new version while the lead was judging by the one it
    was given, or the lead was launched by a runtime from before every task
    had a department and was never given a rubric. Those are recorded and not
    counted; a fresh lead judges by the current version. A missing or wrong
    attestation of the rubric the lead WAS given counts, like any unreadable
    verdict.
    """

    from .department_acceptance import require_rubric_attestation

    try:
        require_rubric_attestation(expected, attested)
        return None
    except DepartmentAcceptanceError as exc:
        reason = str(exc)
    prompt = str((session.get("descriptor") or {}).get("prompt") or "")
    if '"department_acceptance"' not in prompt:
        return (
            f"{reason}; this lead was launched without its department's rubric (a runtime "
            "from before R30 derived a department for every task) - a fresh lead judges by "
            f"version {expected.version}",
            False,
        )
    if attested is not None and attested.sha256 in prompt and attested.record_id in prompt:
        return (
            f"{reason}; the department's rubric advanced to version {expected.version} during "
            "this acceptance - a fresh lead judges by it",
            False,
        )
    return (
        f"{reason}. Return AUTOPILOT_VERIFICATION with \"rubric\":"
        + json.dumps(expected.to_dict(), separators=(",", ":")),
        True,
    )
