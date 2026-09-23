"""Her answer reaches every ticket the card offers it for.

The independent check of the on-call's powers filed the on-call's own
permission request the way ``record_approval_required`` files it - no held
task, only its anchor (``context_task_id``) - escalated it, and ran the
command the status card printed: ``unblock --task A`` answered "A is not
stopped: it is now READY". A ticket with only an anchor, or with no task at
all (a run-level stop), could be answered by Resume alone.

Driven through the production entry points with fakes only.
"""

from __future__ import annotations

import contextlib
import io
import shlex

from _appserver_fakes import activate_via_app_server
from _relay import TEST_RELAY_OWNER
from codex_autopilot import cli
from codex_autopilot.blocked_runs import escalate_to_owner, stop_run
from codex_autopilot.engineer_reservation import tasks_paused_by_incidents
from codex_autopilot.owner_answers import answer_task
from codex_autopilot.pipeline_engineer import IncidentPhase
from codex_autopilot.plan import load_plan
from codex_autopilot.stop_diagnosis import owner_answer
import test_devops_powers as powers

AT = powers.AT
REQUEST = {
    "id": 9,
    "method": "item/commandExecution/requestApproval",
    "params": {"threadId": "eng-1", "itemId": "i9", "command": ["curl", "https://example.com"]},
}


class _Tickets(powers._Stopped):
    def _card_command(self, incident_id: str, option: str, reason: str) -> list[str]:
        """The card's command as she would run it, placeholders filled."""

        words = shlex.split(owner_answer(self.cfg, self.ticket(incident_id)))
        self.assertEqual(words[:2], ["codex-autopilot", "unblock"])
        words = [item for item in words[1:] if item not in {"[--option", "<code>]"}]
        at = words.index("--reason")
        words[at + 1] = reason
        return words + (["--option", option] if option else [])

    def _run(self, argv: list[str]) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(argv), 0)
        return out.getvalue()


class TheOnCallsOwnRequestTests(_Tickets):
    def _the_on_call_asks(self) -> str:
        from codex_autopilot.approval_stops import record_approval_required

        _, engineer = self.stopped()
        incident_id = str(
            record_approval_required(
                self.cfg,
                engineer.reservation_token,
                REQUEST,
                thread_id="eng-1",
                turn_id="eng-1-turn",
                owner=TEST_RELAY_OWNER,
            )
        )
        self.assertEqual(
            escalate_to_owner(
                self.cfg,
                incident_id,
                code="DANGEROUS_PERMISSION",
                detail="the on-call wants network access",
                at=AT,
                escalation={
                    "diagnosis": "network access",
                    "recommendation": "replan",
                    "options": [{"code": "replan", "means": "no network"}],
                    "scope": "task",
                },
            ),
            "escalated",
        )
        return incident_id

    def test_the_card_command_answers_it_and_the_plan_change_is_asked(self) -> None:
        incident_id = self._the_on_call_asks()
        ticket = self.ticket(incident_id)
        self.assertEqual(ticket["affected_task_ids"], [])
        self.assertEqual(ticket["context_task_id"], "A")
        self.assertIn(f"--incident-id {incident_id}", owner_answer(self.cfg, ticket))

        printed = self._run(
            self._card_command(incident_id, "replan", "work without the network")
            + ["--project", str(self.root)]
        )

        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.RESOLVED.value)
        state = self.store.load()
        answer = state.user_unblocks[-1]
        self.assertEqual((answer["task_id"], answer["option"]), ("A", "replan"))
        self.assertIn(incident_id, answer["incident_ids"])
        self.assertIsNotNone(state.active_plan_change_id)
        self.assertIn("A: your decision is recorded (option replan)", printed)

    def test_naming_the_anchored_task_answers_it_too(self) -> None:
        incident_id = self._the_on_call_asks()
        result = answer_task(self.cfg, "A", "no network", option="replan", raise_run=False)
        self.assertIn(incident_id, result["closed_tickets"])
        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.RESOLVED.value)


class ARunLevelTicketTests(_Tickets):
    def test_a_ticket_with_no_task_is_answered_by_its_id(self) -> None:
        state = self.store.load()
        incident_id = str(
            stop_run(
                self.cfg,
                state,
                stop_kind="no_successor",
                phase="NO_SUCCESSOR",
                reason="nothing is reservable",
                summary="s.",
                at=AT,
            )
        )
        self.store.save(state)
        escalate_to_owner(
            self.cfg,
            incident_id,
            code="RECOVERY_EXHAUSTED",
            detail="the run cannot go on",
            at=AT,
            escalation={"diagnosis": "d", "recommendation": "r", "options": [], "scope": "run"},
        )
        plan = load_plan(self.cfg.state_dir, self.cfg.profile)
        self.assertEqual(tasks_paused_by_incidents(self.cfg, plan), set(plan.task_map))

        printed = self._run(
            self._card_command(incident_id, "", "go on, the key is back")
            + ["--project", str(self.root)]
        )

        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.RESOLVED.value)
        self.assertEqual(tasks_paused_by_incidents(self.cfg, plan), set())
        answer = self.store.load().user_unblocks[-1]
        self.assertEqual(answer["incident_ids"], [incident_id])
        self.assertIn("the ticket named no task", printed)


if __name__ == "__main__":
    import unittest

    unittest.main()
