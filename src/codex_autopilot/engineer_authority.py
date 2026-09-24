"""The on-call engineer's authority: what it may do and what is forbidden.

Moved out of pipeline_engineer.py into its own module not for tidiness.
Once the engineer gained the right to repair the runtime's code, everything
that shared a file with its authority would have had to be locked as a
whole - and that same file holds the ordinary incident bookkeeping, where
real defects actually happen. One such we repaired by hand on the v1.0
run: an escalation was not accepted after the engineer had already fixed
the fault.

So the boundary runs here. This module is out of reach for a repair and is
checked by hashes: the fault class, the action vocabulary, the forbidden
list and the threshold after which a repair starts working without a
human. Everything else in pipeline_engineer.py the engineer may repair -
like any other runtime module, through proof.
"""

from __future__ import annotations

from enum import Enum


class IncidentClass(str, Enum):
    PRODUCTION = "PRODUCTION"
    PIPELINE = "PIPELINE"
    RUNTIME = "RUNTIME"
    INTEGRATION = "INTEGRATION"
    TOOLING = "TOOLING"
    POLICY = "POLICY"
    AMBIGUOUS_SIDE_EFFECT = "AMBIGUOUS_SIDE_EFFECT"


class SideEffectOutcome(str, Enum):
    NONE = "NONE"
    KNOWN_FAILED = "KNOWN_FAILED"
    KNOWN_SUCCEEDED = "KNOWN_SUCCEEDED"
    UNKNOWN = "UNKNOWN"


# Operations that change state on the far side. A failure of such an
# operation with an unknown outcome is the one case where a blind retry is
# forbidden: that is exactly how extra threads appeared in a live run.
MUTATING_TRANSPORT_OPERATIONS = frozenset({"create_thread", "send_message_to_thread"})

# The classes the pipeline engineer deals with. Production is not among
# them: product quality is the workers' job, not the engineer's.
INFRASTRUCTURE_INCIDENT_CLASSES = frozenset(
    {
        IncidentClass.PIPELINE,
        IncidentClass.RUNTIME,
        IncidentClass.INTEGRATION,
        IncidentClass.TOOLING,
    }
)

# The classes that are not the engineer's to repair, but still pass through
# its lane. route_incident used to hand them to the owner directly, the one
# road to her that skipped DevOps - her requirement is that every stop is
# looked at first. So the on-call reads them, writes the diagnosis and the
# recommendation, and escalates: product quality is the workers' and hers,
# a policy is hers, and an ambiguous side effect may never be repeated. It
# cannot close them (RESOLVE_FORBIDDEN_CLASSES) and gets only diagnostics.
ADVISORY_INCIDENT_CLASSES = frozenset(
    {
        IncidentClass.PRODUCTION,
        IncidentClass.POLICY,
        IncidentClass.AMBIGUOUS_SIDE_EFFECT,
    }
)
ENGINEER_LANE_CLASSES = INFRASTRUCTURE_INCIDENT_CLASSES | ADVISORY_INCIDENT_CLASSES
# An ambiguous create has no thread the engineer could reconcile to, and a
# definitive failure may be recorded only by the reservation's own relay
# owner - a guard that is her boundary. So "resolved" would be a promise the
# engineer has no means to keep: every advisory ticket ends in a diagnosis.
RESOLVE_FORBIDDEN_CLASSES = ADVISORY_INCIDENT_CLASSES

READ_ONLY_DIAGNOSTIC_ACTIONS = (
    "inspect_bounded_system_state",
    "inspect_recent_events",
    "reconcile_durable_journal",
    "run_declared_healthcheck",
)

# Actions that change state rather than only read it. Each answers exactly
# one recovery command, and each of those has its own refusal when its
# preconditions do not hold. They may be named only as they are named: a
# prose retelling matches nothing.
REPAIR_ACTIONS = (
    "rearm_relay_owner",
    "rearm_run",
    "reconcile_thread_identity",
    "recreate_archived_retry",
    "record_definitive_transport_failure",
    "record_completed_worker_turn",
    "repair_runtime_code",
    # The two the on-call lacked for a stopped task. Without them a repair
    # closed its ticket and left the task BLOCKED for good: nothing but her
    # unblock could return it, and nothing could ask the replanner.
    "request_plan_change",
    "return_stopped_task",
)

# The whole vocabulary: what the engineer may report a repair with.
RECOVERY_ACTIONS = READ_ONLY_DIAGNOSTIC_ACTIONS + REPAIR_ACTIONS

# What level 1 may replay by itself, without a human. All of the
# diagnostics; of the repairing ones only the two commands that refuse by
# themselves when their preconditions do not hold, and are therefore safe
# to replay blindly. A code repair is never replayed: a patch that removed a
# fault here is, on another machine and in another state, a coincidence,
# not a cure.
AUTO_REPLAYABLE_ACTIONS = READ_ONLY_DIAGNOSTIC_ACTIONS + (
    "rearm_relay_owner",
    "rearm_run",
)

FORBIDDEN_ACTIONS = (
    "fix_production_quality_failures",
    "bypass_trust_or_permission_checks",
    "impersonate_or_speak_for_the_user",
    "change_global_codex_settings",
    "authorize_project_root_mutation_on_behalf_of_the_user",
    "delete_project_state",
    "perform_destructive_or_unbounded_repairs",
    "repeat_ambiguous_create_thread_or_send_message_to_thread",
    "create_or_message_codex_tasks_without_real_user_authority_or_an_official_platform_capability",
)

# What the on-call may do about each kind of stop. A return without a
# change of cause is the loop R23 forbids, and a decision that is hers
# (PRODUCT_DECISION, ARCHITECTURE_DECISION) is never lifted by it: those
# kinds list no return at all, and ``engineer_stop_actions`` refuses one.
#
# Keys are the stop kind, or ``worker_blocked:<reason code>`` for a worker's
# own stop. A kind not listed gets DEFAULT_STOP_MEANS.
_RETURN = ("repair_runtime_code", "rearm_relay_owner", "rearm_run", "return_stopped_task")
_REPLAN = ("request_plan_change", "repair_runtime_code")
_HERS = ()  # a diagnosis and ESCALATE with the same code
DEFAULT_STOP_MEANS = _RETURN + ("request_plan_change",)
STOP_MEANS: dict[str, tuple[str, ...]] = {
    "worker_blocked:MISSING_RESOURCE": _RETURN,
    "worker_blocked:ENVIRONMENT_FAILURE": _RETURN,
    "worker_blocked:UNSPECIFIED": _RETURN,
    "worker_blocked:": _RETURN,
    "worker_blocked:DEPENDENCY_DEFECT": _REPLAN,
    "worker_blocked:CONTRADICTORY_CONTRACT": _REPLAN,
    "worker_blocked:DANGEROUS_PERMISSION": _HERS,
    # The worker said recovery is spent. It used to get the full return
    # means, so the on-call could lift a stop whose own claim was that
    # nothing more could be tried - the spec's table puts it with hers.
    "worker_blocked:RECOVERY_EXHAUSTED": _HERS,
    "worker_blocked:PRODUCT_DECISION": _HERS,
    "worker_blocked:ARCHITECTURE_DECISION": _HERS,
    "verification_protocol": _RETURN,
    "verifier_routing": _RETURN,
    # No successor is a defect of the reservation itself, not of a task.
    "no_successor": ("repair_runtime_code",),
    "plan_change_rejected": _REPLAN,
    "plan_verification_rejected": _REPLAN,
    "plan_verification_protocol": _REPLAN,
    # A replanner or plan verifier whose prompt did not fit (R17). The change
    # is closed and its requester held; a new round would inherit the same
    # refusals and overflow again, so the requester goes back to its worker.
    "context_budget": _RETURN,
    # The top of the hiring ladder: a defect of the gate, rubric or verifier
    # is repaired in code (and only such a repair returns the task, see
    # LADDER_RESET_MODULES); a task too big for one hire is re-planned. Only
    # a judgement about the work itself is hers.
    "ladder_exhausted": ("repair_runtime_code", "request_plan_change", "return_stopped_task"),
    # A permission the run does not hold: a runtime that asked for more than
    # the run is authorized for is repaired; otherwise it is hers
    # (DANGEROUS_PERMISSION) and never answered by anyone else.
    "approval_required": ("repair_runtime_code",),
}
# Reason codes whose stop is hers to lift. The engineer diagnoses and hands
# it up with the same code; ``return_stopped_task`` refuses these - the same
# four the means table gives nothing but a diagnosis.
OWNER_STOP_REASONS = frozenset(
    {"PRODUCT_DECISION", "ARCHITECTURE_DECISION", "DANGEROUS_PERMISSION", "RECOVERY_EXHAUSTED"}
)

# The acceptance path (R29, R30), named explicitly: the gate, the rubric,
# the verifier and the verifier's prompt. A runtime patch returns a task from
# the top of its hiring ladder only when it changed one of these - R23 allows
# a reset "only after a change that touches the cause of the refusal", and a
# patch elsewhere must not buy a fresh budget past her judgement of the work.
#
# The first list was wider, and the independent check named why that is a
# hole: lifecycle_completion.py (the on-call's own completion, the stops,
# NO_SUCCESSOR), models.py (model routing) and rules.py are not where a
# refusal comes from, and a patch there bought a fresh hire all the same.
#
# Whole modules - every change in them is a change of acceptance:
LADDER_RESET_MODULES = frozenset(
    {
        "verification.py",
        "acceptance.py",
        "acceptance_floor.py",
        "department_acceptance.py",
    }
)
# The verifier's prompt shares its modules with the worker's and the
# on-call's, so only its own parts count: whole definitions that only the
# verifier reads, and in the shared builders only the body of an
# ``if phase == "verification"`` branch (the third field names that branch;
# None is the whole definition). Compared as syntax trees, so comments and
# layout change nothing.
LADDER_RESET_DEFINITIONS: tuple[tuple[str, str, str | None], ...] = (
    ("ai_studio.py", "_acceptance_gate", None),
    ("ai_studio.py", "_department_acceptance", None),
    ("ai_studio.py", "_verification_contract", None),
    ("ai_studio.py", "_verifier_definition_of_done", None),
    ("ai_studio.py", "build_prompt", "verification"),
    ("ai_studio.py", "_render_prompt", "verification"),
    ("lifecycle_prompts.py", "_verification_contract", None),
    ("lifecycle_prompts.py", "_worker_prompt", "verification"),
)

# R4: what starting the run authorized - the list of covered operations,
# fixed and versioned. Her rule: run-state holds the durable authorization
# with this list, and a confirmation request for a covered operation is
# refused, citing R4. It used to be three lines of prose in stop_diagnosis
# (a patchable module), and the independent check named why that is no
# basis: "a repeated request for a covered operation is a runtime defect"
# could not be decided by a machine. Here, out of the engineer's reach, each
# operation names the approval-request methods it answers for; which request
# falls under it is decided by ``run_authorization.covering_operation``
# (guarded). An operation with no methods is the runtime's own transport:
# it is never asked for. A change to this list is a new version - a run
# keeps the version it was armed with in run-state.
#
# Version 2 names the CLI subcommands the run is authorized for. Version 1
# covered every ``codex-autopilot`` subcommand, and so her own commands
# with them: a worker asking to run ``unblock`` (speaking for her) or
# ``authorize-project-root`` (her R6) was marked covered, recorded as an R4
# violation, and the on-call's escalation of it was refused as a protocol
# error - the one request that is hers reached her only as
# RECOVERY_EXHAUSTED, with the wrong class. Arming a run does not authorize
# an answer on her behalf, a mutation of her saved projects, her start or
# stop of a run, a skill revocation or an uninstall.
RUN_AUTHORIZATION_VERSION = 2
RUN_AUTHORIZED_CLI_SUBCOMMANDS: tuple[str, ...] = (
    # Read-only views of the run and the machine.
    "status",
    "logs",
    "timeline",
    "doctor",
    "preflight",
    "skills",
    # The runtime's own relay and recovery protocol.
    "relay-status",
    "relay-fail",
    "relay-complete",
    "reconcile-thread-identity",
    "recreate-archived-retry",
    # The on-call's actions - each one guarded by its own ticket and thread.
    "devops-rearm-relay-owner",
    "devops-repair-runtime",
    "devops-revert-runtime-patch",
    "devops-resolve-incident",
    "devops-return-task",
    "devops-request-plan-change",
)
RUN_AUTHORIZED_OPERATIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "task_transport",
        (),
        "App Server thread/start and turn/start for the scheduler-selected tasks of this run",
    ),
    (
        "file_change_in_project",
        ("item/fileChange/requestApproval", "applyPatchApproval"),
        "reads and writes inside the project working directory under the run's permission profile",
    ),
    (
        "autopilot_cli_in_project",
        ("item/commandExecution/requestApproval", "execCommandApproval"),
        "the plugin's own codex-autopilot commands, run inside the project",
    ),
)

# How many identical successful resolutions of one signature it takes for
# the repair to stop requiring the engineer and become a deterministic
# runbook.
PROMOTION_THRESHOLD = 2
