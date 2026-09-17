"""An open incident holds its own tasks, not the whole run.

Any open ticket used to stop everything: while the on-call engineer
dealt with one task, no other moved - not even an independent one with
all slots free. One failed transport held twenty-three other tasks, and
the run stood for hours showing "running".

The incident names its own tasks, in the affected_task_ids field. The
pause covers exactly those.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from _gates import patch_hook_trust_gates
from _relay import reserve_ready_frontier
from _plan_contract import initialize_verified_project as initialize_project
from codex_autopilot.config import load_config
from codex_autopilot.lifecycle_failures import record_desktop_failure
from codex_autopilot.run_state import StateStore
from test_desktop_lifecycle import graph, task


class IncidentScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        raw = graph(max_workers=2)
        # Three independent tasks: two take the slots, the third waits its
        # turn. It is the one that must go when a slot frees up.
        raw["tasks"] = [
            task("A", path="src/a"),
            task("B", path="src/b"),
            task("D", path="src/d"),
        ]
        plan_file = self.root / "input-plan.json"
        plan_file.write_text(json.dumps(raw), encoding="utf-8")
        initialize_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=skill,
            desktop_project_id="desktop-project",
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)

    def test_the_run_keeps_going_while_one_task_waits_for_its_incident(self) -> None:
        first = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-1")
        busy = {item.task_id for item in first}
        self.assertEqual(len(busy), 2, "два слота заняты")
        stuck = sorted(busy)[0]
        token = next(item.reservation_token for item in first if item.task_id == stuck)

        # The real failure path: it also opens the ticket and releases the locks.
        record_desktop_failure(
            self.cfg,
            token,
            reason="Worker requested approval; the dispatcher never answers",
            failure_code="app_server_rpc_failed",
            definitive=True,
            reserve_other_ready=False,
        )
        from codex_autopilot.pipeline_engineer import (
            IncidentClass,
            IncidentSignal,
            PipelineIncidentStore,
            SideEffectOutcome,
        )
        from codex_autopilot.run_state import utc_now

        incidents = PipelineIncidentStore(self.cfg.state_dir)
        record = incidents.open_incident(
            IncidentSignal(
                signal_id=f"probe:{stuck}",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                summary="транспорт сорвался на одной задаче",
                affected_task_ids=(stuck,),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
                system_state={},
            ),
            at=utc_now(),
        )
        incident_id = str(record["incident_id"])
        incidents.route_incident(incident_id, at=utc_now())
        incidents.ensure_pipeline_engineer(incident_id, at=utc_now())

        # The engineer goes first - repair outranks any work.
        engineer = reserve_ready_frontier(
            self.cfg, relay_owner_thread_id="owner-2", now_epoch=2_000_000_000
        )
        self.assertEqual(
            [item.to_dict()["kind"] for item in engineer], ["pipeline_engineer"]
        )
        # And then the run must continue without waiting for the ticket to close.
        later = reserve_ready_frontier(
            self.cfg, relay_owner_thread_id="owner-3", now_epoch=2_000_000_000
        )
        started = {item.task_id for item in later if item.to_dict()["kind"] != "pipeline_engineer"}
        self.assertNotIn(stuck, started, "задача инцидента обязана ждать")
        self.assertTrue(
            started,
            "независимая задача обязана пойти, пока тикет висит незакрытым",
        )


    def test_the_pause_covers_exactly_the_tasks_the_incident_names(self) -> None:
        """Ни больше, ни меньше: список задач даёт сам тикет."""

        from codex_autopilot.lifecycle_reservations import tasks_paused_by_incidents
        from codex_autopilot.pipeline_engineer import (
            HealthcheckResult,
            IncidentClass,
            IncidentSignal,
            PipelineIncidentStore,
            SideEffectOutcome,
        )
        from codex_autopilot.plan import load_plan
        from codex_autopilot.run_state import utc_now

        plan = load_plan(self.cfg.state_dir, self.cfg.profile)
        self.assertEqual(tasks_paused_by_incidents(self.cfg, plan), set())

        incidents = PipelineIncidentStore(self.cfg.state_dir)
        record = incidents.open_incident(
            IncidentSignal(
                signal_id="probe:B",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                summary="транспорт сорвался",
                affected_task_ids=("B",),
                operation="create_thread",
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
                system_state={},
            ),
            at=utc_now(),
        )
        incident_id = str(record["incident_id"])
        self.assertEqual(tasks_paused_by_incidents(self.cfg, plan), {"B"})

        incidents.route_incident(incident_id, at=utc_now())
        incidents.ensure_pipeline_engineer(incident_id, at=utc_now())
        incidents.complete_pipeline_engineer(
            incident_id,
            success=True,
            actions=("inspect_bounded_system_state",),
            at=utc_now(),
            healthcheck=HealthcheckResult(
                name="probe",
                passed=True,
                checks=("проверено",),
                observed_at=utc_now(),
            ),
        )
        # A closed ticket holds nobody.
        self.assertEqual(tasks_paused_by_incidents(self.cfg, plan), set())


if __name__ == "__main__":
    unittest.main()


class RecoverySlotStatusTests(unittest.TestCase):
    """Статус обязан читаться и когда слот восстановления занят.

    Писатель клал в слот два ключа, читатель просил третий - и `status`
    падал KeyError ровно тогда, когда человек приходил разбираться.
    """

    def test_a_busy_recovery_slot_still_renders(self) -> None:
        from codex_autopilot.pipeline_engineer import render_pipeline_status

        base = {
            "phase": "PIPELINE_ENGINEER",
            "incident_count": 1,
            "paused_task_ids": ["A"],
            "pending_transport": [],
            "incidents": [],
        }
        full = render_pipeline_status(
            base | {"recovery_slot": {"incident_id": "inc-1", "token": "t", "owner_id": "own-1"}}
        )
        self.assertIn("inc-1", full)
        self.assertIn("own-1", full)
        # The old record's slot carries no owner. Status must still read
        # on it: otherwise the `status` command stops working forever.
        legacy = render_pipeline_status(
            base | {"recovery_slot": {"incident_id": "inc-1", "token": "t"}}
        )
        self.assertIn("inc-1", legacy)
