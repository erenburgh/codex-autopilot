"""The Desktop-owned lifecycle: a facade over the implementation modules.

The module used to be one 4844-line file. It was split by cohesion; the
dependency graph is one-directional:

    lifecycle_base          types, the session journal, checkpoints, small helpers
      <- lifecycle_reservations   frontier reservation and descriptor building
      <- lifecycle_failures       failures, incidents, identity reconciliation
      <- lifecycle_dispatch       task creation and the production turn via App Server
      <- lifecycle_completion     consuming the authoritative completion
         lifecycle_prompts        phase prompt assembly

Two back edges are broken by late imports inside functions:
lifecycle_failures -> app_server_creation_contract and
lifecycle_dispatch -> complete_desktop_worker.

This file implements nothing. It exists so the public API stays the same:
both the CLI and the tests still import from codex_autopilot.lifecycle.

Exactly what is really imported through the facade is re-exported, and
the list is pinned in __all__. The mechanical split of the monolith
dragged 91 names in here, 47 of them private: a private helper was never
public API, and its presence here made the boundary indistinguishable
from the contents.
"""

from __future__ import annotations

from .lifecycle_base import (  # noqa: F401
    DESKTOP_SLOT_READY,
    DesktopLifecycleError,
    LaunchDescriptor,
    WORKSPACE_HANDOFF_OK,
    acknowledge_desktop_send,
    audit_creation_causality,
    creation_causality_coverage,
    parse_applied_rules,
    parse_desktop_worker_status,
    retired_session_for_thread,
    pause_desktop_run,
    pending_descriptors,
    observe_worker_states,
    reconcile_desktop_runtime,
    relay_session_status,
    task_checkpoint_path,
)

from .lifecycle_reservations import (  # noqa: F401
    recover_desktop_frontier_from_predecessor_stop,
    relayable_descriptors,
    reserve_ready_frontier,
)

from .lifecycle_failures import (  # noqa: F401
    reconcile_desktop_thread_identity,
    record_desktop_failure,
    record_desktop_interrupt,
    record_policy_rejected_create_transport,
)

from .lifecycle_dispatch import (  # noqa: F401
    adopt_automatic_dispatcher_successor,
    claim_automatic_app_server_turn,
    create_desktop_thread_via_app_server,
    record_automatic_app_server_exit,
    run_automatic_app_server_turn,
)

from .lifecycle_completion import (  # noqa: F401
    complete_desktop_worker,
)

__all__ = [
    "DESKTOP_SLOT_READY",
    "DesktopLifecycleError",
    "LaunchDescriptor",
    "WORKSPACE_HANDOFF_OK",
    "acknowledge_desktop_send",
    "adopt_automatic_dispatcher_successor",
    "audit_creation_causality",
    "claim_automatic_app_server_turn",
    "complete_desktop_worker",
    "create_desktop_thread_via_app_server",
    "creation_causality_coverage",
    "parse_applied_rules",
    "parse_desktop_worker_status",
    "retired_session_for_thread",
    "pause_desktop_run",
    "pending_descriptors",
    "observe_worker_states",
    "reconcile_desktop_runtime",
    "reconcile_desktop_thread_identity",
    "record_automatic_app_server_exit",
    "record_desktop_failure",
    "record_desktop_interrupt",
    "record_policy_rejected_create_transport",
    "recover_desktop_frontier_from_predecessor_stop",
    "relay_session_status",
    "relayable_descriptors",
    "reserve_ready_frontier",
    "run_automatic_app_server_turn",
    "task_checkpoint_path",
]
