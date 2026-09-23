"""Four holes the second independent check found in the on-call's powers.

- Resume looped on the same permission request: only ``unblock`` knew the
  one-retry-per-request rule, and a Resume recorded neither option nor
  signature, so every round sent the task back into the same request.
- The on-call's prompt told it never to patch a DANGEROUS_PERMISSION stop,
  and a permission request carries that code: the repair branch of the design
  (compare the request with R4 and the profile, repair a runtime defect) was
  unreachable through the prompt.
- Resume granted no fresh hire where ``unblock`` did: a task sent back to the
  top of its ladder under a runtime_patch_refused ticket went back spent.
- A staged runtime patch could drain the run forever: the quiet check asked
  every registered project, ignored SCHEDULED dispatchers, and a deferral
  filed nothing.

Every test drives the production entry points with fakes only: no live Codex,
no App Server.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from _appserver_fakes import activate_via_app_server
from _relay import reserve_ready_frontier
from codex_autopilot.lifecycle import complete_desktop_worker
from codex_autopilot.owner_answers import OwnerAnswerError, answer_task
from codex_autopilot.pipeline_engineer import IncidentPhase, PipelineIncidentStore
from codex_autopilot.runtime_install import pending_entries, stage_proven_patch
import test_devops_patch_grants as grants
import test_devops_powers as powers
import test_runtime_patch_install as installs
from test_runtime_patch_install import proven

AT = powers.AT


class _Permission(powers.ThePermissionRequestTests):
    """A worker's permission request; the parent's tests are not rerun here."""

    def _handed_to_her(self) -> str:
        from codex_autopilot.stop_holds import block_escalated_tasks

        incident_id = self._worker_asks()
        state = self.store.load()
        block_escalated_tasks(self.cfg, state, incident_id)
        self.store.save(state)
        PipelineIncidentStore(self.cfg.state_dir).escalate_incident_to_user(
            incident_id, reason_code="DANGEROUS_PERMISSION", at=AT
        )
        return incident_id

    def _asks_again(self) -> str:
        """The retried task ran and met the very same request."""

        state = self.store.load()
        for item in state.worker_sessions:
            if item["task_id"] == "A" and item["kind"] != "pipeline_engineer":
                item["status"] = "COMPLETED"
        state.task_states["A"] = "READY"
        state.task_retry_at.pop("A", None)
        self.store.save(state)
        return self._handed_to_her()

    def _engineer_for(self, incident_id: str) -> None:
        reserved = reserve_ready_frontier(self.cfg)
        engineer = next(item for item in reserved if item.kind == "pipeline_engineer")
        activate_via_app_server(self.cfg, self.root, engineer, "eng-1")
        state = self.store.load()
        session = next(i for i in state.worker_sessions if i["reservation_token"] == engineer.reservation_token)
        self.assertEqual(session["incident_id"], incident_id)


for _name in [name for name in vars(powers.ThePermissionRequestTests) if name.startswith("test_")]:
    setattr(_Permission, _name, None)


class ResumeRetriesAPermissionRequestOnceTests(_Permission):
    def test_the_second_resume_on_the_same_request_leaves_it_with_her(self) -> None:
        """Reproduces the check's probe: three Resumes, A back in READY each time."""

        from codex_autopilot.control import _answer_escalation

        first = self._handed_to_her()
        closed = _answer_escalation(self.cfg, self.store.load())
        self.assertEqual(closed, (first,))
        state = self.store.load()
        self.assertNotEqual(state.task_states["A"], "BLOCKED")
        answer = state.user_unblocks[-1]
        self.assertEqual(answer["option"], "retry")
        self.assertEqual(answer["approval_signature"], self.ticket(first)["system_state"]["approval_signature"])

        second = self._asks_again()
        again = _answer_escalation(self.cfg, self.store.load())

        self.assertEqual(again, ())
        self.assertIn("came back after your earlier retry", " ".join(again.held))
        self.assertIn("--option", " ".join(again.held))
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")
        self.assertEqual(self.ticket(second)["phase"], IncidentPhase.ESCALATE_TO_USER.value)
        self.assertIsNone(self.ticket(second).get("resolved_at"))
        # A third Resume changes nothing either: the loop is closed.
        self.assertEqual(_answer_escalation(self.cfg, self.store.load()), ())
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")

    def test_unblock_sees_a_retry_spent_by_resume(self) -> None:
        from codex_autopilot.control import _answer_escalation

        self._handed_to_her()
        _answer_escalation(self.cfg, self.store.load())
        self._asks_again()
        with self.assertRaisesRegex(OwnerAnswerError, "earlier retry"):
            answer_task(self.cfg, "A", "again", option="retry", raise_run=False)
        answer_task(self.cfg, "A", "do it without the network", option="replan", raise_run=False)
        self.assertTrue(self.store.load().plan_changes[-1]["requested_by_owner"])

    def test_the_on_call_reads_her_resume_as_an_earlier_answer(self) -> None:
        from codex_autopilot.control import _answer_escalation
        from codex_autopilot.lifecycle_reservations import pipeline_engineer_package

        self._handed_to_her()
        _answer_escalation(self.cfg, self.store.load())
        state = self.store.load()
        for item in state.worker_sessions:
            if item["task_id"] == "A" and item["kind"] != "pipeline_engineer":
                item["status"] = "COMPLETED"
        state.task_states["A"] = "READY"
        state.task_retry_at.pop("A", None)
        self.store.save(state)
        second = self._worker_asks()

        package = pipeline_engineer_package(self.cfg, self.store.load(), second)

        earlier = package["stop_context"]["approval"]["answered_before"]
        self.assertEqual([item["option"] for item in earlier], ["retry"])

    def test_the_resume_hook_says_what_answers_the_request(self) -> None:
        from codex_autopilot.control import _answer_escalation, handle_prompt_hook

        self._handed_to_her()
        _answer_escalation(self.cfg, self.store.load())
        self._asks_again()
        state = self.store.load()
        state.status = "BLOCKED"
        self.store.save(state)

        reply = handle_prompt_hook(
            {"hook_event_name": "UserPromptSubmit", "prompt": "продолжи кодекс автопайлот", "cwd": str(self.root)}
        )

        self.assertEqual(reply["decision"], "block")
        self.assertIn("came back after your earlier retry", reply["reason"])
        self.assertIn("unblock --project", reply["reason"])


class TheOnCallComparesAPermissionRequestTests(_Permission):
    def _prompt(self, incident_id: str) -> str:
        from codex_autopilot.ai_studio import AIStudioRuntime
        from codex_autopilot.lifecycle_reservations import pipeline_engineer_package
        from codex_autopilot.plan import load_plan

        package = pipeline_engineer_package(self.cfg, self.store.load(), incident_id)
        return AIStudioRuntime(
            load_plan(self.cfg.state_dir, self.cfg.profile),
            self.root,
            language="en",
            skill_path=self.cfg.skill_path,
        ).build_pipeline_engineer_prompt(package, reservation_token="t")

    def test_the_prompt_of_a_permission_request_names_what_to_compare_and_the_repair(self) -> None:
        from codex_autopilot.lifecycle_reservations import pipeline_engineer_package

        incident_id = self._worker_asks()
        context = pipeline_engineer_package(self.cfg, self.store.load(), incident_id)["stop_context"]
        self.assertEqual(context["means"], ["repair_runtime_code"])
        self.assertTrue(context["approval"]["run_authorization"])

        prompt = self._prompt(incident_id)

        for field in (
            "stop_context.approval.request",
            "stop_context.approval.run_authorization",
            "stop_context.approval.permission_profile",
            "stop_context.approval.answered_before",
        ):
            self.assertIn(field, prompt)
        self.assertIn("repair it with devops-repair-runtime", prompt)
        self.assertIn("does not make it hers before you look", prompt)
        # The sentence the check quoted no longer covers this stop.
        self.assertNotIn("A task stopped with PRODUCT_DECISION", prompt)

    def test_a_worker_stop_does_not_get_the_permission_paragraph(self) -> None:
        from codex_autopilot.blocked_runs import stop_run

        state = self.store.load()
        state.task_states["A"] = "BLOCKED"
        incident_id = stop_run(
            self.cfg, state, stop_kind="worker_blocked", phase="BLOCKED", reason="A: key", summary="s.",
            at=AT, task_ids=("A",), system_state={"reason_code": "DANGEROUS_PERMISSION"},
        )
        self.store.save(state)
        prompt = self._prompt(str(incident_id))
        self.assertNotIn("stop_context.approval.run_authorization", prompt)
        self.assertIn("RECOVERY_EXHAUSTED is never returned or patched by you", prompt)

    def test_it_closes_only_as_a_repaired_runtime_defect(self) -> None:
        from codex_autopilot.engineer_stop_actions import require_patch_holder

        incident_id = self._worker_asks()
        self._engineer_for(incident_id)
        require_patch_holder(self.cfg, incident_id, "eng-1")  # the repair door is open

        code, err = self.resolve(incident_id, "repair_runtime_code")
        self.assertEqual(code, 2)
        self.assertIn("closes only with a runtime patch", err)
        self.assertIsNone(self.ticket(incident_id).get("resolved_at"))

        powers.TheTopOfTheLadderTests._stage_patch(self, incident_id, "status.py", "p-profile")
        self.assertEqual(self.resolve(incident_id, "repair_runtime_code")[0], 0)
        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.RESOLVED.value)
        self.assertNotEqual(self.store.load().task_states["A"], "BLOCKED")


class ResumeGrantsTheSameFreshHireTests(grants._Ladder):
    def test_resume_on_a_revoked_grant_ticket_grants_a_fresh_hire(self) -> None:
        from codex_autopilot.control import _answer_escalation

        self._at_the_top()
        incident_id, _ = self.stopped(kind="runtime_patch_refused", reason_code="")
        complete_desktop_worker(
            self.cfg,
            thread_id="eng-1",
            turn_id="eng-1-turn",
            final_message=(
                'AUTOPILOT_ESCALATION: {"diagnosis":"the patch no longer fits","scope":"task"}\n'
                "PIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER RECOVERY_EXHAUSTED"
            ),
        )
        self.assertTrue(self._revise_again_blocks(), "the fixture must start at the top")

        closed = _answer_escalation(self.cfg, self.store.load())

        self.assertEqual(closed, (incident_id,))
        state = self.store.load()
        self.assertNotEqual(state.task_states["A"], "BLOCKED")
        self.assertEqual(state.task_rehires["A"], 4)
        self.assertFalse(self._revise_again_blocks(), "Resume sent the task back spent")


class TheDrainIsThisRunsAndBoundedTests(installs._Installation):
    def _live_dispatcher(self, state_name: str = "RUNNING") -> None:
        worker = reserve_ready_frontier(self.cfg)[0]
        state = self.store.load()
        session = next(i for i in state.worker_sessions if i["reservation_token"] == worker.reservation_token)
        session["automatic_dispatch_state"] = state_name
        session["automatic_dispatch_pid"] = os.getpid()
        self.store.save(state)

    def _staged_long_ago(self) -> None:
        stage_proven_patch(self.cfg.state_dir, proven())
        manifest_path = pending_entries(self.cfg.state_dir)[0] / "patch.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["staged_at"] = 0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_a_busy_neighbour_run_does_not_hold_this_one(self) -> None:
        from codex_autopilot.runtime_install import install_when_quiet
        from codex_autopilot.run_state import StateStore

        neighbour = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(neighbour, ignore_errors=True))
        shutil.copytree(self.cfg.state_dir, neighbour / ".codex-autopilot")
        other = StateStore(neighbour / ".codex-autopilot")
        busy = other.load()
        busy.dispatcher_pid = os.getpid()
        other.save(busy)
        stage_proven_patch(self.cfg.state_dir, proven())

        with mock.patch("codex_autopilot.wake.registered_projects", return_value=[str(neighbour)]):
            outcome = install_when_quiet(self.cfg, install_root=self.install)

        self.assertEqual(outcome["installed"][0]["entry"], "patch-demo")
        self.assertEqual(self.live_status(), "VALUE = 'new'\n")

    def test_a_scheduled_dispatcher_defers_the_install(self) -> None:
        from codex_autopilot.runtime_install import install_when_quiet

        self._live_dispatcher("SCHEDULED")
        stage_proven_patch(self.cfg.state_dir, proven())

        outcome = install_when_quiet(self.cfg, install_root=self.install)

        self.assertIn("dispatcher is alive in this run", outcome["deferred"])
        self.assertEqual(self.live_status(), "VALUE = 'old'\n")

    def test_a_drain_past_its_deadline_is_refused_not_waited_for(self) -> None:
        from codex_autopilot.runtime_install import install_when_quiet

        self._live_dispatcher()
        self._staged_long_ago()

        outcome = install_when_quiet(self.cfg, install_root=self.install)

        self.assertEqual(outcome["refused"][0]["entry"], "patch-demo")
        self.assertIn("drain deadline", outcome["refused"][0]["reason"])
        self.assertIn(str(os.getpid()), outcome["refused"][0]["reason"])
        self.assertEqual(pending_entries(self.cfg.state_dir), [])
        self.assertEqual(self.live_status(), "VALUE = 'old'\n")

    def test_the_sweep_raises_an_overdue_drain_and_the_on_call_gets_a_ticket(self) -> None:
        from codex_autopilot.wake import is_stranded

        self._live_dispatcher()
        self._staged_long_ago()
        self.assertTrue(is_stranded(self.cfg, self.store.load()), "nobody would ever refuse it")

        with mock.patch("codex_autopilot.runtime_install.install_root_from_env", return_value=self.install):
            self._wake("terminal")

        tickets = [
            item for item in self.incidents.load()["incidents"]
            if (item.get("system_state") or {}).get("stop_kind") == "runtime_patch_refused"
        ]
        self.assertEqual(len(tickets), 1)
        self.assertIn("drain deadline", tickets[0]["summary"] + json.dumps(tickets[0]))
        self.assertEqual(pending_entries(self.cfg.state_dir), [])
        engineers = [i for i in self.store.load().worker_sessions if i["kind"] == "pipeline_engineer"]
        self.assertEqual(engineers[-1]["incident_id"], tickets[0]["incident_id"])

    def test_a_fresh_drain_beside_a_live_dispatcher_still_waits(self) -> None:
        from codex_autopilot.wake import is_stranded

        self._live_dispatcher()
        stage_proven_patch(self.cfg.state_dir, proven())
        self.assertFalse(is_stranded(self.cfg, self.store.load()))


if __name__ == "__main__":
    import unittest

    unittest.main()
