"""M11-PRE-SIDE-EFFECT-FENCE: nothing is written into a retired task.

A retired session failed closed before too - but on turn completion,
that is, after the model had worked as a worker on a reservation that no
longer existed. Measured on the M11 run: while the same task's
replacement ran next door, the sources' mtimes changed.

The fence is placed on UserPromptSubmit, before a single model or tool
call, and is checked precisely by a repeat: the same thread, the same
input.
"""

from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest


def _project(tmp: Path, sessions: list[dict]) -> Path:
    root = tmp / "project"
    state_dir = root / ".codex-autopilot"
    state_dir.mkdir(parents=True)
    (state_dir / "config.toml").write_text("", encoding="utf-8")
    payload = {
        "schema_version": 5,
        "run_id": "r-1",
        "worker_sessions": sessions,
    }
    (state_dir / "run-state.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return root


def _session(**overrides) -> dict:
    base = {
        "task_id": "T2",
        "reservation_token": "token-1",
        "operation_id": "op-1",
        "client_user_message_id": "msg-1",
        "kind": "implementation",
        "attempt": 1,
        "status": "RETIRED_SUPERSEDED",
        "created_at": "2026-09-13T12:00:00+00:00",
        "thread_id": "thread-old",
        "worker_sequence": 4,
        "retired_reason": "replaced by a fresh attempt",
    }
    base.update(overrides)
    return base


class RetiredTaskFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _hook(self, root: Path, *, thread_id: str, prompt: str = "продолжай"):
        from codex_autopilot.control import handle_prompt_hook

        return handle_prompt_hook(
            {"prompt": prompt, "cwd": str(root), "session_id": thread_id}
        )

    def test_a_retired_thread_is_refused_before_anything_runs(self) -> None:
        root = _project(self.tmp, [_session()])
        result = self._hook(root, thread_id="thread-old")
        self.assertEqual(result["decision"], "block")
        self.assertIn("retired", result["reason"])
        self.assertIn("T2", result["reason"])
        self.assertIn("replaced by a fresh attempt", result["reason"])

    def test_the_same_refusal_repeats_on_replay(self) -> None:
        """The repeat is the check: the fence is not a one-shot."""

        root = _project(self.tmp, [_session()])
        first = self._hook(root, thread_id="thread-old")
        second = self._hook(root, thread_id="thread-old")
        self.assertEqual(first, second)
        self.assertEqual(second["decision"], "block")

    def test_every_kind_of_retirement_is_fenced(self) -> None:
        from codex_autopilot.lifecycle_base import RETIRED_SESSION_STATUSES

        for status in sorted(RETIRED_SESSION_STATUSES):
            with self.subTest(status=status):
                root = _project(
                    self.tmp / status, [_session(status=status, retired_reason="")]
                )
                self.assertEqual(
                    self._hook(root, thread_id="thread-old")["decision"], "block"
                )

    def test_a_live_thread_passes_through(self) -> None:
        root = _project(self.tmp, [_session(status="ACTIVE", retired_reason="")])
        self.assertEqual(self._hook(root, thread_id="thread-old"), {})

    def test_a_thread_taken_back_into_work_is_not_fenced(self) -> None:
        """The same thread can be retired and taken back: the live one wins."""

        root = _project(
            self.tmp,
            [
                _session(),
                _session(
                    status="ACTIVE",
                    reservation_token="token-2",
                    operation_id="op-2",
                    client_user_message_id="msg-2",
                    worker_sequence=5,
                    retired_reason="",
                ),
            ],
        )
        self.assertEqual(self._hook(root, thread_id="thread-old"), {})

    def test_an_unrelated_thread_is_untouched(self) -> None:
        root = _project(self.tmp, [_session()])
        self.assertEqual(self._hook(root, thread_id="thread-other"), {})

    def test_a_control_phrase_is_routed_past_the_fence(self) -> None:
        """A control phrase is about the run, not about the task.

        It never reaches the model, so the fence does not touch it: the
        fence sits on the branch "this is not a control phrase", that is,
        exactly where the input would have gone to the model.
        """

        from codex_autopilot.control import (
            PAUSE_PROMPTS,
            RESUME_PROMPTS,
            STATUS_PROMPTS,
            UNINSTALL_PROMPTS,
            _normalized_prompt,
        )

        control = PAUSE_PROMPTS | RESUME_PROMPTS | STATUS_PROMPTS | UNINSTALL_PROMPTS
        for phrase in ("статус", "статус Codex Autopilot", "приостанови Codex Autopilot"):
            with self.subTest(phrase=phrase):
                self.assertIn(_normalized_prompt(phrase), control)
        self.assertNotIn(_normalized_prompt("продолжай"), control)

    def test_the_skill_phrase_is_the_phrase_the_hook_knows(self) -> None:
        """The skill promises one word; the hook knew only the long forms.

        The promised visible path did not work exactly as written: the
        user says `статус`, the hook does not recognise the phrase, the
        input goes to the model, and no run report appears.
        """

        from codex_autopilot.control import STATUS_PROMPTS, _normalized_prompt

        skill = (
            Path(__file__).resolve().parents[1]
            / "plugins/codex-autopilot-adaptive/skills/codex-autopilot-adaptive/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("ask `status`", skill)
        self.assertIn(_normalized_prompt("статус"), STATUS_PROMPTS)

    def test_an_unreadable_state_does_not_gag_the_project(self) -> None:
        """The fence knows about one thread; without that it stays quiet."""

        root = self.tmp / "broken"
        (root / ".codex-autopilot").mkdir(parents=True)
        (root / ".codex-autopilot" / "config.toml").write_text("", encoding="utf-8")
        (root / ".codex-autopilot" / "run-state.json").write_text(
            "{ не json", encoding="utf-8"
        )
        self.assertEqual(self._hook(root, thread_id="thread-old"), {})


if __name__ == "__main__":
    unittest.main()


class EscalationReturnPathTests(unittest.TestCase):
    """Asking the user with no way back is a dead end, not an escalation.

    The engineer declared ESCALATE_TO_USER, the run went to BLOCKED, and
    resuming was refused for exactly the reason that the run was in
    BLOCKED. A person who had already fixed everything had no way to say
    so.

    The state produced by the previous version is checked separately: the
    run is marked escalated while the ticket stayed in PIPELINE_ENGINEER.
    A repair that cures only future cases and leaves what is already
    broken locked is half a repair.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / ".codex-autopilot"
        self.dir.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def _store(self):
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        return PipelineIncidentStore(self.dir)

    def _open(self, phase: str) -> str:
        from codex_autopilot.pipeline_engineer import (
            IncidentClass,
            IncidentSignal,
            SideEffectOutcome,
        )

        store = self._store()
        incident = store.open_incident(
            IncidentSignal(
                signal_id="sig-1",
                code="detached_dispatch_failed",
                surface=IncidentClass.PIPELINE,
                operation="create_thread",
                summary="вердикт не разобран",
                affected_task_ids=("M4",),
                system_state={},
                side_effect_outcome=SideEffectOutcome.KNOWN_FAILED,
            ),
            at="2026-09-13T17:00:00+00:00",
        )
        incident_id = str(incident["incident_id"])
        state = store.load()
        for item in state["incidents"]:
            if item["incident_id"] == incident_id:
                item["phase"] = phase
        from codex_autopilot.pipeline_engineer import _atomic_json

        _atomic_json(store.path, state)
        return incident_id

    def test_an_escalated_incident_is_closed_by_the_user(self) -> None:
        incident_id = self._open("ESCALATE_TO_USER")
        self.assertIn(incident_id, self._store().incident_ids_awaiting_the_user())
        phase = self._store().resolve_escalation_by_user(
            incident_id, at="2026-09-13T18:00:00+00:00", note="починено вручную"
        )
        self.assertEqual(phase.value, "RESOLVED")

    def test_an_incident_left_in_the_old_phase_is_closed_too(self) -> None:
        """Exactly the state the live run got stuck in."""

        incident_id = self._open("PIPELINE_ENGINEER")
        self.assertIn(incident_id, self._store().incident_ids_awaiting_the_user())
        phase = self._store().resolve_escalation_by_user(
            incident_id, at="2026-09-13T18:00:00+00:00"
        )
        self.assertEqual(phase.value, "RESOLVED")

    def test_a_resolved_incident_is_not_reopened_by_the_user(self) -> None:
        from codex_autopilot.pipeline_engineer import PipelineIncidentError

        incident_id = self._open("RESOLVED")
        self.assertNotIn(incident_id, self._store().incident_ids_awaiting_the_user())
        with self.assertRaises(PipelineIncidentError):
            self._store().resolve_escalation_by_user(
                incident_id, at="2026-09-13T18:00:00+00:00"
            )

    def test_closing_unpauses_the_affected_task(self) -> None:
        """Closing the ticket must lift the pause, or the task stays put."""

        incident_id = self._open("PIPELINE_ENGINEER")
        self.assertIn("M4", self._store().status_snapshot()["paused_task_ids"])
        self._store().resolve_escalation_by_user(
            incident_id, at="2026-09-13T18:00:00+00:00"
        )
        self.assertNotIn("M4", self._store().status_snapshot()["paused_task_ids"])
