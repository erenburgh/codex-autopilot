"""A test helper for frontier reservation.

Rule R21: acceptance runs in a clean environment. The suite may not
depend on variables specific to the author's session.

reserve_ready_frontier requires the relay owner's identity and, if none
is passed, reads CODEX_THREAD_ID from the environment. Inside a Codex
session the variable is always there, so 31 tests passed for the author
and failed in the declared CI. Here identity is passed explicitly and
deterministically.
"""

from __future__ import annotations

from codex_autopilot.lifecycle import reserve_ready_frontier as _reserve_ready_frontier

TEST_RELAY_OWNER = "test-relay-owner-thread"


def reserve_ready_frontier(cfg, **kwargs):
    kwargs.setdefault("relay_owner_thread_id", TEST_RELAY_OWNER)
    return _reserve_ready_frontier(cfg, **kwargs)
