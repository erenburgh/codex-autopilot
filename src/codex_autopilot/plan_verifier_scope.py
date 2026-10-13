"""The rules the plan verifier is handed, scoped to what it is shown.

On a fresh 0.14 install a tester's run stopped at preflight with "Plan
verification: FAIL", and his agent named the cause: the verifier is held to
structured fields that the plugin itself keeps out of the plan it is given.
The prompt bore that out. ``build_plan_verification_prompt`` handed the
fresh verifier the whole rules block with no scope at all - R9's structured
RoleProfile, R30's leads and rubrics, the acceptance floor of R8, R21 and
R29, the declared scope of R7 and R24 - while ``verifier_payload`` keeps
roles, departments, resources and the verification policy out of its view
by design (R25). A verifier that reads the rules as a contract meets a
rule, looks for its fields, does not find them, and can answer only REVISE,
on any plan. Six stand-in verifiers on the same prompt read past it; his
did not.

Telling this reader those rules do not apply here would be the exception
from her rule that ``rules_for_prompt`` refuses to write. So every rule
stays in the block whole (R17), and each rule that asks for a field the
view leaves out gains a scope, as R30 has one per task: which fields are not
in the view, what the runtime admitted of them before this verification,
and who judges the rest, and when. What admission let through is computed
here, on the graph being judged, with admission's own functions; a task it
let through without a part (one kept from a plan older than the part, or
migrated from a v0.8 run) is named, never covered by "every task". The six
dimensions stay the verifier's. No scope names a value of a withheld field:
the roles, the leads and the resource targets stay out of the judgment
prompt, as ``verifier_payload`` keeps them.
"""

from __future__ import annotations

from typing import Sequence

from .plan_admission import _is_legacy_role, acceptance_issues


# The last sentence of every scope that withholds a field: it is withheld on
# purpose, and what is still the reader's.
_NOT_YOURS = (
    "What your view leaves out is left out by R25's design, not missing from the plan: it "
    "is not yours to judge; the six dimensions this prompt lists are."
)


def plan_verifier_scopes(plan) -> dict[str, str]:
    """Rule id -> scope, for the fresh verifier of ``plan`` (``build_plan_verification_prompt``)."""

    roles = plan.role_map
    generic = [task.id for task in plan.tasks if task.role in roles and _is_legacy_role(roles[task.role])]
    unled = [
        task.id
        for task in plan.tasks
        if not task.verification.verifier_role or task.verification.verifier_role == task.role
    ]
    # The floor as admission states it, read with no migration exemption:
    # the tasks it names are the ones admission let through without it.
    below = list(dict.fromkeys(task_id for task_id, _ in acceptance_issues(plan.tasks, legacy_serial=False)))
    floor_tasks = _every_task(below)
    floor_gap = _gap(
        below,
        "the floor does not hold. Admission lets only a task migrated from a v0.8 serial run, its "
        "acceptance contract unchanged, keep the contract recorded for it "
        "(plan_admission.legacy_exempt_task_ids)",
    )
    independent = (
        'is judged by an independent verifier (verification.policy "independent", never "self"; '
        "verification.required true)"
    )
    suite = (
        "declares a full-suite deterministic check run directly through env with CODEX_THREAD_ID, "
        "CODEX_TURN_ID and CODEX_SESSION_ID reset"
    )
    floor_source = "(plan_admission.acceptance_issues, acceptance_floor.is_clean_suite_command)"
    return {
        "R7": (
            "In force. Not in your view: each task's declared scope (task.resources) and its "
            "attempt budget (verification.max_revision_attempts); the plan has no field for a time "
            "or token budget. Before this verification admission checked only the shape of each "
            "resource claim (plan_parse.resource_from_raw); it does not require a write claim. "
            "Staying inside the scope is checked by the runtime when a task completes: a changed "
            "path that no write claim of the task covers is a defect citing R7 "
            "(artifact_staging_lifecycle, scope.audit_declared_scope); the runtime counts revision "
            "attempts against the budget (revision_budget). " + _NOT_YOURS
        ),
        "R8": (
            "In force. Not in your view: verification.policy and verification.required of each "
            "task, and the user's original request. Admitted deterministically before this "
            f"verification: {floor_tasks} {independent} (plan_admission.acceptance_issues)."
            f"{floor_gap} Each task's result is compared with the user's "
            "original request when the task is accepted, by its department's lead; plan "
            "verification judges the graph against the Goal Contract (R25). " + _NOT_YOURS
        ),
        "R9": (
            "In force. Not in your view: plan.roles and each task's role. Admitted "
            "deterministically before this verification: every role is a structured RoleProfile "
            "with an id, a name and responsibilities (plan_parse.role_from_raw); every task's role "
            "is one of them" + ("" if generic else ", and none is the generic legacy-worker")
            + " (plan_admission._graph)."
            + _gap(
                generic,
                "the role is the generic legacy-worker. Admission lets only a plan migrated from a "
                "v0.8 serial run keep it",
            )
            + " The task.title you see carries no role name by design: the runtime builds each "
            "thread's title from the task's RoleProfile when it creates the thread and reads it "
            "back from the App Server (thread_titles, lifecycle_dispatch). " + _NOT_YOURS
        ),
        "R17": (
            "In force. This prompt is assembled by the runtime: the whole rules block first, never "
            "truncated - a prompt over the budget is refused, not cut. In place of the user's "
            "original request you are given the Goal Contract: R25 limits the plan verifier to the "
            "Goal Contract, the constraints, the graph and the DoD, so the request text is withheld "
            "from you by design, not missing from the plan; each task's result is compared with it "
            "when the task is accepted (R8). The order of this prompt is the runtime's, not a "
            "property of the plan."
        ),
        "R21": (
            "In force. Not in your view: each task's verification.deterministic_checks. Admitted "
            f"deterministically before this verification: {floor_tasks} {suite} {floor_source}."
            f"{floor_gap} The acceptance run "
            "itself happens when each task is accepted, not in plan verification. " + _NOT_YOURS
        ),
        "R24": (
            "In force. Not in your view: each task's write scope (task.resources). Admission "
            "checked the shape of each claim (plan_parse.resource_from_raw), not that a write claim "
            "exists. The worker writes into a staged copy of the project; the canonical project is "
            "changed only when the runtime promotes that copy after a verifier's attestation "
            "(artifact_staging), and the paths a task changed are compared with its declared write "
            "claims when it completes (artifact_staging_lifecycle). " + _NOT_YOURS
        ),
        "R27": (
            "In force. Not in your view: any rubric - a rubric is not a part of the plan. The "
            "runtime derives each department's rubric from its lead's RoleProfile and stores it, "
            "versioned, in Project Memory (department_runtime.ensure_department_rubric): version 1 "
            "before the first task and after a committed plan change. Each lead judges by the "
            "stored version and attests it. " + _NOT_YOURS
        ),
        "R29": (
            "In force. Not in your view: verification.policy, verification.required, "
            "verification.deterministic_checks and verification.max_revision_attempts. Admitted "
            f"deterministically before this verification: {floor_tasks} {independent}, allows at "
            f"least 2 revision attempts and {suite} {floor_source}.{floor_gap} The full-suite check "
            "admits work to "
            "judgement and never replaces it: the runtime refuses the transition to VERIFIED "
            "without a recorded verdict when each task is accepted. Of R29 you judge one part, the "
            "acceptance_class dimension, from acceptance_class and semantic_judgment_required, "
            "which you see. " + _NOT_YOURS
        ),
        "R30": (
            "In force. Not in your view: each task's lead (verification.verifier_role), the lead's "
            "RoleProfile with its verification_expectations, and plan.departments. Admitted "
            "deterministically before this verification "
            f"(department_runtime.validate_department_leads): {_every_task(unled)} names its "
            "department's lead, a role of the plan and never the task's own role; the work of one "
            "profession still to be accepted has one lead; a profession whose work was accepted "
            "keeps its lead."
            + _gap(
                unled,
                "no lead of another profession is named. Admission lets a task a plan change leaves as "
                "it was, or one migrated from a v0.8 run, keep what it had; the runtime stops such "
                "a task for the on-call when its lead is needed (department_gate)",
            )
            + " The runtime derives the department from the lead "
            "(department_runtime.derive_task_department) and writes its rubric, by which the lead "
            "judges each task when it is accepted. " + _NOT_YOURS
        ),
    }


def _every_task(exceptions: Sequence[str]) -> str:
    """"every task of this graph", or every task but the ones admission let through without it."""

    if not exceptions:
        return "every task of this graph"
    return f"every task of this graph but {', '.join(exceptions)}"


def _gap(task_ids: Sequence[str], what: str) -> str:
    """The sentence naming the tasks a claim leaves out, and why admission let them through."""

    if not task_ids:
        return ""
    return f" Left out of that: {', '.join(task_ids)} - {what}."
