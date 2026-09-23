"""A fresh hire bought by a runtime patch, the patch commands' binding, the drain.

The independent check of the on-call's powers found six holes, each measured
on fakes; every test here reproduces one through the production entry points
(the frontier, the dispatcher's completion, the CLI, the wake-up's install)
and no live Codex or App Server.
"""

from __future__ import annotations

import contextlib
import io
import os
from unittest import mock

from _relay import reserve_ready_frontier
from codex_autopilot.engineer_stop_actions import (
    EngineerStopActionError,
    request_plan_change,
    return_stopped_task,
    take_back_patch,
)
from codex_autopilot.lifecycle import complete_desktop_worker
from codex_autopilot.pipeline_engineer import IncidentPhase, PipelineIncidentStore
from codex_autopilot.stop_diagnosis import means_for
from test_devops_powers import AT, TheTopOfTheLadderTests, _Stopped


class _Ladder(TheTopOfTheLadderTests):
    """A task at the top of its ladder; the parent's tests are not rerun here."""

    def _grant_on_a_staged_patch(self, patch_id: str = "p-rubric") -> str:
        self._at_the_top()
        incident_id, _ = self.stopped(kind="ladder_exhausted", reason_code="")
        self._stage_patch(incident_id, "department_acceptance.py", patch_id)
        result = return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self.assertIsNotNone(result["grant"])
        self.assertEqual(self.store.load().task_rehires["A"], 4)
        return incident_id

    def _open_tickets(self) -> list[dict]:
        return [
            item
            for item in PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
            if not item.get("resolved_at")
        ]


for _name in [name for name in vars(TheTopOfTheLadderTests) if name.startswith("test_")]:
    setattr(_Ladder, _name, None)


class OnlyTheAcceptancePathBuysAHireTests(_Ladder):
    def test_the_verifiers_prompt_counts_and_the_workers_does_not(self) -> None:
        """The prompt builders are shared; only the verifier's branch is on the path."""

        self._at_the_top()
        incident_id, _ = self.stopped(kind="ladder_exhausted", reason_code="")
        # The revision worker's identity: the `elif` of the same `if` whose
        # body is the verifier's - another phase's prompt.
        self._stage_patch(
            incident_id,
            "ai_studio.py",
            "p-worker-prompt",
            old='f"fresh revision worker R{revision_number}"',
            new='f"fresh revision worker, round R{revision_number}"',
        )
        # A comment on the verifier's module changes no syntax.
        self._stage_patch(
            incident_id,
            "verification.py",
            "p-comment",
            new="from __future__ import annotations\n# the on-call was here\n",
        )
        with self.assertRaisesRegex(EngineerStopActionError, "acceptance path"):
            return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self._stage_patch(
            incident_id,
            "ai_studio.py",
            "p-verifier-prompt",
            old='else f"fresh independent verifier V{verification_round}"',
            new='else f"fresh, independent verifier V{verification_round}"',
        )
        result = return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self.assertEqual(result["grant"]["grounds"]["runtime_patch_ids"], ["p-verifier-prompt"])


class APatchThatNeverLandsBuysNothingTests(_Ladder):
    def test_a_withdrawn_patch_buys_no_hire(self) -> None:
        """Reproduces the check: staged on verification.py, withdrawn, still granted."""

        self._at_the_top()
        incident_id, _ = self.stopped(kind="ladder_exhausted", reason_code="")
        self._stage_patch(incident_id, "verification.py", "p-w")
        take_back_patch(self.cfg, incident_id=incident_id, patch_id="p-w", thread_id="eng-1")
        with self.assertRaisesRegex(EngineerStopActionError, "acceptance path"):
            return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self.assertEqual(self.store.load().task_rehires["A"], 3)

    def test_a_reverted_patch_buys_no_hire(self) -> None:
        """Installed, then taken back: the revert is staged and the patch no longer counts."""

        from codex_autopilot.runtime_install import INSTALLED, _file, patch_status

        self._at_the_top()
        incident_id, _ = self.stopped(kind="ladder_exhausted", reason_code="")
        self._stage_patch(incident_id, "verification.py", "p-r")
        # What install_pending does with an entry it installed.
        _file(self.cfg.state_dir, "p-r", INSTALLED, {"tree": "t"})
        take_back_patch(self.cfg, incident_id=incident_id, patch_id="p-r", thread_id="eng-1")
        self.assertEqual(patch_status(self.cfg.state_dir, "p-r"), "reverted")
        with self.assertRaisesRegex(EngineerStopActionError, "acceptance path"):
            return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self.assertEqual(self.store.load().task_rehires["A"], 3)

    def test_withdrawing_after_the_return_revokes_the_hire(self) -> None:
        incident_id = self._grant_on_a_staged_patch()

        result = take_back_patch(self.cfg, incident_id=incident_id, patch_id="p-rubric", thread_id="eng-1")

        self.assertEqual(result["blocked_again"], ["A"])
        state = self.store.load()
        self.assertEqual(state.task_states["A"], "BLOCKED")
        self.assertEqual(state.task_rehires["A"], 3, "the fresh hire outlived its patch")
        self.assertEqual(self.ticket(incident_id)["revoked_grants"][0]["runtime_patch_ids"], ["p-rubric"])
        # Its own open stop ticket holds it: no second ticket, and it cannot
        # be closed over a BLOCKED task.
        self.assertIsNone(result["ticket"])
        self.assertEqual(self.resolve(incident_id, "return_stopped_task")[0], 2)

    def test_a_patch_refused_at_install_revokes_the_hire_and_a_ticket_holds_the_task(self) -> None:
        """The wake-up's install: not an installation, so the staged set is refused."""

        from codex_autopilot import wake

        self._grant_on_a_staged_patch()
        # Returned, and done work goes back to acceptance: IMPLEMENTED.
        self.assertEqual(self.store.load().task_states["A"], "IMPLEMENTED")
        environment = {k: v for k, v in os.environ.items() if k != "CODEX_AUTOPILOT_INSTALL_ROOT"}
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertIsNone(wake._install_staged_patch(self.cfg, self.store))

        state = self.store.load()
        self.assertEqual(state.task_states["A"], "BLOCKED")
        self.assertEqual(state.task_rehires["A"], 3)
        refused = [
            item
            for item in self._open_tickets()
            if (item.get("system_state") or {}).get("stop_kind") == "runtime_patch_refused"
        ]
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["affected_task_ids"], ["A"])

    def test_the_ladder_gate_follows_the_task_not_the_ticket(self) -> None:
        """Back under a runtime_patch_refused ticket, the task is still at the top."""

        self._at_the_top()
        incident_id, _ = self.stopped(kind="runtime_patch_refused", reason_code="")
        with self.assertRaisesRegex(EngineerStopActionError, "top of its hiring ladder"):
            return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")


class RecoveryExhaustedIsHersTests(_Stopped):
    def test_a_worker_that_declared_recovery_exhausted_is_not_returned_or_patched(self) -> None:
        incident_id, _ = self.stopped(reason_code="RECOVERY_EXHAUSTED")
        ticket = self.ticket(incident_id)
        self.assertEqual(means_for(ticket), ())
        with self.assertRaisesRegex(EngineerStopActionError, "owner's"):
            return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        with self.assertRaisesRegex(EngineerStopActionError, "not re-planned"):
            request_plan_change(self.cfg, incident_id=incident_id, task_id="A", reason="r", thread_id="eng-1")
        from codex_autopilot.engineer_stop_actions import require_patch_holder

        with self.assertRaisesRegex(EngineerStopActionError, "not repaired in code"):
            require_patch_holder(self.cfg, incident_id, "eng-1")
        self.assertEqual(self.store.load().task_states["A"], "BLOCKED")


class ThePatchCommandsAreBoundTests(_Ladder):
    def _revert(self, incident_id: str, patch_id: str, thread: str) -> tuple[int, str]:
        from codex_autopilot import cli

        err = io.StringIO()
        with mock.patch.object(cli, "_relay_executor_thread_id", return_value=thread), \
             contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(
                [
                    "devops-revert-runtime-patch", "--project", str(self.root),
                    "--incident-id", incident_id, "--patch-id", patch_id,
                ]
            )
        return code, err.getvalue()

    def test_only_this_tickets_engineer_takes_a_patch_back(self) -> None:
        from codex_autopilot.runtime_install import patch_status

        incident_id = self._grant_on_a_staged_patch()
        code, err = self._revert(incident_id, "p-rubric", "worker-A")
        self.assertEqual(code, 2)
        self.assertIn("own thread", err)
        self.assertEqual(patch_status(self.cfg.state_dir, "p-rubric"), "pending")
        self.assertEqual(self.store.load().task_rehires["A"], 4)

        self.assertEqual(self._revert(incident_id, "p-rubric", "eng-1")[0], 0)
        self.assertEqual(patch_status(self.cfg.state_dir, "p-rubric"), "withdrawn")
        self.assertEqual(self.store.load().task_rehires["A"], 3)

    def test_a_staged_patch_of_another_ticket_is_not_withdrawn(self) -> None:
        from codex_autopilot.runtime_install import patch_status

        self._at_the_top()
        incident_id, _ = self.stopped(kind="ladder_exhausted", reason_code="")
        self._stage_patch(incident_id, "verification.py", "p-mine")
        # Another ticket's patch, staged earlier and still waiting.
        other = PipelineIncidentStore(self.cfg.state_dir)
        tickets = other.load()["incidents"]
        self.assertEqual(len(tickets), 1)
        from codex_autopilot.runtime_install import patch_root, PENDING

        foreign = patch_root(self.cfg.state_dir) / PENDING / "p-foreign"
        (foreign).mkdir(parents=True)
        (foreign / "patch.json").write_text('{"kind": "patch"}', encoding="utf-8")
        with self.assertRaisesRegex(EngineerStopActionError, "not this ticket's"):
            take_back_patch(self.cfg, incident_id=incident_id, patch_id="p-foreign", thread_id="eng-1")
        self.assertEqual(patch_status(self.cfg.state_dir, "p-foreign"), "pending")


class TheDrainIsNotAStopTests(_Ladder):
    def test_a_staged_patch_then_a_return_files_no_no_successor(self) -> None:
        """Reproduces the check: stage, return, close, complete RESOLVED."""

        incident_id, _ = self.stopped()  # worker_blocked MISSING_RESOURCE
        self._stage_patch(incident_id, "status.py", "p-fix")
        return_stopped_task(self.cfg, incident_id=incident_id, task_id="A", thread_id="eng-1")
        self.assertEqual(self.store.load().task_states["A"], "READY")
        self.assertEqual(self.resolve(incident_id, "repair_runtime_code", "return_stopped_task")[0], 0)
        state = self.store.load()
        for item in state.worker_sessions:
            if item["kind"] != "pipeline_engineer":
                item["status"] = "COMPLETED"
        state.active_task_ids = []
        self.store.save(state)

        complete_desktop_worker(
            self.cfg,
            thread_id="eng-1",
            turn_id="eng-1-turn",
            final_message="Patched the rubric and returned A.\nPIPELINE_ENGINEER_STATUS: RESOLVED",
        )

        kinds = [(item.get("system_state") or {}).get("stop_kind") for item in self._open_tickets()]
        self.assertNotIn("no_successor", kinds)
        self.assertEqual(self.ticket(incident_id)["phase"], IncidentPhase.RESOLVED.value)
        self.assertEqual(self.store.load().phase, "RUNTIME_PATCH_PENDING")
        self.assertEqual([item.kind for item in reserve_ready_frontier(self.cfg)], [])


class ThePromptSaysHowARepairLandsTests(_Stopped):
    def test_the_on_call_is_told_the_patch_is_staged_and_the_run_drains(self) -> None:
        from codex_autopilot.ai_studio import AIStudioRuntime
        from codex_autopilot.lifecycle_reservations import pipeline_engineer_package
        from codex_autopilot.plan import load_plan

        incident_id, _ = self.stopped()
        package = pipeline_engineer_package(self.cfg, self.store.load(), incident_id)
        prompt = AIStudioRuntime(
            load_plan(self.cfg.state_dir, self.cfg.profile),
            self.root,
            language="en",
            skill_path=self.cfg.skill_path,
        ).build_pipeline_engineer_prompt(package, reservation_token="t")
        self.assertNotIn("takes effect on the next dispatched turn", prompt)
        self.assertIn("proven and staged, not installed", prompt)
        self.assertIn("drains", prompt)
        self.assertIn("devops-revert-runtime-patch --project <root> --incident-id <id>", prompt)
        self.assertIn("RECOVERY_EXHAUSTED is never returned or patched by you", prompt)
