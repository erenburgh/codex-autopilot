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

# How many identical successful resolutions of one signature it takes for
# the repair to stop requiring the engineer and become a deterministic
# runbook.
PROMOTION_THRESHOLD = 2
