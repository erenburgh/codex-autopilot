"""Hook trust is checked on every path that starts production work.

The owner's boundary is absolute: hook trust and the ownership guard are
never bypassed. Three production entry points bypassed it anyway, and the
green suite said nothing because every one of them was reached only by
paths no test executed:

- ``devops-rearm-relay-owner`` and ``recreate-archived-retry`` are fresh
  CLI processes that reserve production work and spawn a NEW dispatcher.
  They called ``reserve_ready_frontier`` with the gate switched off, and
  control.py imported no gate at all - so hook trust was checked by nobody
  in any process on their path. The accepted exemption is a step inside an
  already-gated operation (lifecycle_completion, where ``gate(cfg)`` ran
  four lines earlier); neither of these is that.
- The Stop hook's continuation reaches ``_desktop_relay_continuation``
  exactly in the branch where ``complete_desktop_worker`` returns before
  its own ``gate(cfg)`` line, and spawned relays from there ungated.

A refusal here is also the honest answer: a relay is executed by the Stop
hook, so arming one while the hook is not trusted promises a launch that
can never happen.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from _gates import patch_hook_trust_gates
from _plan_contract import (
    TEST_OUTCOME_ID,
    canonical_verification,
    canonicalize_plan,
    initialize_verified_project,
)
from _relay import reserve_ready_frontier

from codex_autopilot import control
from codex_autopilot.config import load_config
from codex_autopilot.hook_trust import HookTrustApprovalRequired


def _task(task_id: str) -> dict[str, object]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "objective": f"Complete {task_id}.",
        "definition_of_done": [f"{task_id} is verified."],
        "execution_mode": "code",
        "execution_mode_reason": "Repository files and tests are sufficient.",
        "reasoning": "medium",
        "role": "builder",
        "depends_on": [],
        "priority": 0,
        "verification": canonical_verification(),
        "resources": [],
        "required_capabilities": [],
        "context": {},
        "outputs": [],
        "tags": [],
        "produces_outcomes": [TEST_OUTCOME_ID],
        "acceptance_class": "mixed",
    }


class HookTrustIsNeverBypassedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gates = patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# skill\n", encoding="utf-8")
        plan_file = self.root / "plan.json"
        plan_file.write_text(
            json.dumps(
                canonicalize_plan(
                    {
                        "schema_version": 3,
                        "graph_version": 1,
                        "goal": "Exercise the trust boundary.",
                        "user_request": "Exercise the trust boundary.",
                        "model_strategy": "auto",
                        "execution_strategy": "parallel",
                        "max_parallel_workers": 1,
                        "computer_use_slots": 1,
                        "roles": [
                            {
                                "id": "builder",
                                "name": "Builder",
                                "responsibilities": ["Build one task."],
                            }
                        ],
                        "tasks": [_task("A")],
                    }
                )
            ),
            encoding="utf-8",
        )
        initialize_verified_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)

    def _refuse_trust(self) -> None:
        self.gates["control"].side_effect = HookTrustApprovalRequired(
            plugin_id="codex-autopilot-adaptive",
            trust_status="untrusted",
            current_hash="deadbeef",
        )

    def test_the_devops_rearm_refuses_before_it_touches_anything(self) -> None:
        self._refuse_trust()
        with mock.patch.object(control, "spawn_automatic_app_server_relay") as spawn:
            with self.assertRaises(HookTrustApprovalRequired):
                control.reactivate_desktop_relay_owner(self.root)
        spawn.assert_not_called()

    def test_recreating_an_archived_retry_refuses_before_it_touches_anything(self) -> None:
        self._refuse_trust()
        with mock.patch.object(control, "spawn_automatic_app_server_relay") as spawn:
            with self.assertRaises(HookTrustApprovalRequired):
                control.recreate_archived_desktop_retry(
                    self.root,
                    reservation_token="some-token",
                    archived_thread_id="archived-thread",
                    predecessor_thread_id="owner-thread",
                )
        spawn.assert_not_called()

    def test_the_stop_hook_continuation_refuses_before_it_spawns(self) -> None:
        """The reservation exists and is launchable - and still nothing starts."""

        descriptor = reserve_ready_frontier(
            self.cfg, relay_owner_thread_id="owner-thread"
        )[0]
        self.assertEqual(descriptor.task_id, "A")
        self._refuse_trust()
        with mock.patch.object(control, "_spawn_automatic_descriptors") as spawn:
            with self.assertRaises(HookTrustApprovalRequired):
                control._desktop_relay_continuation(
                    self.cfg,
                    relay_owner_thread_id="owner-thread",
                    relay_owner_turn_id="owner-turn",
                )
        spawn.assert_not_called()

    def test_a_trusted_hook_still_lets_the_continuation_launch(self) -> None:
        """The gate must not become a wall: trusted hooks still start work."""

        reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-thread")
        with mock.patch.object(
            control, "_spawn_automatic_descriptors", return_value=(4242,)
        ) as spawn:
            with mock.patch.object(control, "_launch_report", return_value={"ok": True}):
                answer = control._desktop_relay_continuation(
                    self.cfg,
                    relay_owner_thread_id="owner-thread",
                    relay_owner_turn_id="owner-turn",
                )
        spawn.assert_called_once()
        self.assertEqual(answer, {"ok": True})


if __name__ == "__main__":
    unittest.main()
