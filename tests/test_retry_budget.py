"""R23: repeating the same failure is bounded.

The rule was declared ENFORCED, and there was no ceiling at all.
``maximum_attempts`` sat in the config at 96 and nobody read it: three
mentions per repository - template, default, parser - and not one
consumer. Only ``initial_seconds`` and ``maximum_seconds`` worked, that
is, the retry ran forever, not for a day.

The wall that bounded retries by accident was removed the same day. A
model protocol error used to crash the dispatcher, and the run stood -
needing a human. After the fix "an unreadable verdict goes back to the
verifier" the same refusal goes to RETRY_WAIT normally and repeats. If
the model errs consistently the same way - and that is exactly what we
saw, the same final-line format - the loop runs by itself, and nothing
was left to bound it.

The count is by SIGNATURE, not by task: one fault on two tasks is one
fault. The caller names the kind of failure (``failure_code``), because
in this project a signature answers "what broke", and the free-text
``reason`` does not - the very reason summary and affected_task_ids were
thrown out of ``incident_signature``.

At the ceiling the run does not stop: a ticket goes to the on-call
engineer, and the ticket's pause breaks the loop. The stop comes later,
when the engineer itself is exhausted - R3 requires that.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import unittest

from _gates import patch_hook_trust_gates
from _plan_contract import initialize_verified_project as initialize_project
from _relay import reserve_ready_frontier
from codex_autopilot.config import load_config
from codex_autopilot.lifecycle_base import PENDING_SESSION_STATUSES
from codex_autopilot.lifecycle_failures import record_desktop_failure
from codex_autopilot.pipeline_engineer import PipelineIncidentStore
from codex_autopilot.run_state import StateStore
from test_desktop_lifecycle import graph, task


SRC = Path(__file__).resolve().parents[1] / "src" / "codex_autopilot"


def production_failure_shapes() -> dict[str, frozenset[bool]]:
    """Which ``definitive`` production calls each failure code with.

    Taken by parsing ``src``, not from a constant here. The first
    edition of this file called everything with ``definitive=True`` and
    was green, missing that ``worker_protocol_rejected`` had no cap at
    all: production passes it with ``definitive=False``, and back then
    the count sat behind an early return. A test that picks the shape
    itself checks a combination that never happens in production, and
    stays silent exactly where it should shout.

    Where ``definitive`` is an expression, not a constant, both values
    are taken: the unknown has to be checked in the worst case, not in
    the convenient one.
    """

    shapes: dict[str, set[bool]] = {}
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", "") != "record_desktop_failure":
                continue
            keywords = {item.arg: item.value for item in node.keywords}
            code = keywords.get("failure_code")
            if not isinstance(code, ast.Constant) or not isinstance(code.value, str):
                # The code is assembled on the fly (the CLI path): it has no form.
                continue
            declared = keywords.get("definitive")
            if isinstance(declared, ast.Constant):
                values = {bool(declared.value)}
            else:
                values = {True, False}
            shapes.setdefault(code.value, set()).update(values)
    return {code: frozenset(values) for code, values in shapes.items()}


def production_definitive(code: str) -> bool:
    """The shape in which this code is hardest to count.

    If production calls the code both ways, we check the
    underdetermined path: it returns earlier, and that is where the
    count was once lost.
    """

    values = production_failure_shapes().get(code)
    assert values, f"{code} не зовётся в продакшене"
    return False if False in values else True


class RetryBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        skill = self.root / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        raw = graph(max_workers=2)
        raw["tasks"] = [task("A", path="src/a"), task("B", path="src/b")]
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

    # --- tools ---------------------------------------------------------

    def pending_token(self, task_id: str) -> str | None:
        """The token of the task's already open reservation, if any."""

        for item in self.store.load().worker_sessions:
            if item.get("task_id") != task_id:
                continue
            if item.get("status") in PENDING_SESSION_STATUSES:
                return str(item["reservation_token"])
        return None

    def fail_once(
        self,
        task_id: str,
        code: str,
        *,
        rate_limited: bool = False,
        definitive: bool | None = None,
    ) -> None:
        """One real failure of a task: take its slot and drop it.

        With two slots the frontier takes both tasks at once, so a
        reservation is only needed when there is no open one.
        """

        token = self.pending_token(task_id)
        if token is None:
            state = self.store.load()
            now = max([0, *state.task_retry_at.values()]) or None
            reserve_ready_frontier(self.cfg, now_epoch=now)
            token = self.pending_token(task_id)
        self.assertIsNotNone(token, f"{task_id} is not reserved")
        record_desktop_failure(
            self.cfg,
            token,
            reason=f"обстоятельства попытки для {task_id}",
            failure_code=code,
            definitive=production_definitive(code) if definitive is None else definitive,
            rate_limited=rate_limited,
            reserve_other_ready=False,
        )

    def attempts(self, code: str) -> int:
        return int(self.store.load().failure_signature_attempts.get(code, 0))

    def incidents_for(self, code: str) -> list[dict]:
        raw = PipelineIncidentStore(self.cfg.state_dir).load()
        return [item for item in raw["incidents"] if item.get("code") == code]

    # --- the checks themselves -----------------------------------------

    def test_the_same_signature_stops_looping_at_the_cap(self) -> None:
        cap = self.cfg.retry.maximum_attempts
        self.assertEqual(cap, 5, "the default cap changed silently")

        for _ in range(cap - 1):
            self.fail_once("A", "worker_protocol_rejected")
        self.assertEqual(self.attempts("worker_protocol_rejected"), cap - 1)
        self.assertEqual(
            self.incidents_for("worker_protocol_rejected"),
            [],
            "below the cap the engineer is not called",
        )

        self.fail_once("A", "worker_protocol_rejected")
        self.assertEqual(self.attempts("worker_protocol_rejected"), cap)

        opened = self.incidents_for("worker_protocol_rejected")
        self.assertEqual(len(opened), 1, "exactly one ticket opens at the cap")
        self.assertIn("A", opened[0]["affected_task_ids"])

        paused = PipelineIncidentStore(self.cfg.state_dir).status_snapshot()
        self.assertIn(
            "A",
            paused["paused_task_ids"],
            "it is the ticket's pause that breaks the loop",
        )

    def test_a_different_signature_does_not_share_the_cap(self) -> None:
        """Counted per signature: different faults do not share one cap."""

        for _ in range(3):
            self.fail_once("A", "worker_protocol_rejected")
        for _ in range(3):
            self.fail_once("A", "app_server_rpc_failed")

        self.assertEqual(self.attempts("worker_protocol_rejected"), 3)
        self.assertEqual(self.attempts("app_server_rpc_failed"), 3)
        self.assertEqual(self.incidents_for("worker_protocol_rejected"), [])
        self.assertEqual(self.incidents_for("app_server_rpc_failed"), [])

    def test_the_cap_counts_the_breakage_not_the_task(self) -> None:
        """One fault on two tasks is one fault.

        The signature deliberately leaves the task out: the same ground
        on which affected_task_ids is not part of incident_signature.
        """

        self.fail_once("A", "app_server_rpc_failed")
        self.fail_once("B", "app_server_rpc_failed")
        self.assertEqual(
            self.attempts("app_server_rpc_failed"),
            2,
            "counted per task instead of per signature",
        )

    def test_every_production_failure_shape_reaches_the_counter(self) -> None:
        """Every code production drops a worker with must be counted.

        This is the check that was missing. ``worker_protocol_rejected``
        is passed with ``definitive=False``, the early return stood
        before the counter - and the very loop R23 was written for had
        no cap at all. Five tests were green because the helper called
        everything with ``definitive=True``.

        The shape is not picked here: it is taken from ``src``. A new
        failure code, or a changed shape in an existing one, reaches
        this test by itself.
        """

        shapes = production_failure_shapes()
        self.assertIn(
            False,
            shapes.get("worker_protocol_rejected", frozenset()),
            "production no longer passes this code underdetermined - "
            "check that the motive behind R23 is still covered",
        )
        for code, values in sorted(shapes.items()):
            for definitive in sorted(values):
                with self.subTest(code=code, definitive=definitive):
                    before = self.attempts(code)
                    self.fail_once("A", code, definitive=definitive)
                    # Her own pause is the one code that is recorded and
                    # never counted: see test_her_pause_never_spends_the_cap.
                    self.assertEqual(
                        self.attempts(code),
                        before if code == "worker_paused" else before + 1,
                        f"{code} with definitive={definitive} was counted wrongly",
                    )

    def test_her_pause_never_spends_the_cap(self) -> None:
        """A pause is hers, not a fault of the pipeline.

        Production records a paused worker as ``worker_paused`` with
        ``definitive=True``, and it used to be counted like any failure:
        the fifth time she paused, the on-call got a ticket about her
        pause - a ticket about her own decision, which is not its to
        look at.
        """

        cap = self.cfg.retry.maximum_attempts
        for _ in range(cap + 1):
            self.fail_once("A", "worker_paused", definitive=True)
        self.assertEqual(self.attempts("worker_paused"), 0)
        self.assertEqual(self.incidents_for("worker_paused"), [])

    def test_a_repeat_after_a_closed_ticket_opens_a_new_one(self) -> None:
        """At the cap and beyond, not only exactly at it.

        The check was ``attempts == cap``: once the first ticket closed,
        the same signature never opened another, and the retries went on
        forever with nobody looking.
        """

        from codex_autopilot.pipeline_engineer import HealthcheckResult, _expected_healthcheck
        from codex_autopilot.run_state import utc_now

        cap = self.cfg.retry.maximum_attempts
        for _ in range(cap):
            self.fail_once("A", "worker_protocol_rejected")
        store = PipelineIncidentStore(self.cfg.state_dir)
        first = self.incidents_for("worker_protocol_rejected")[0]
        store.complete_pipeline_engineer(
            first["incident_id"],
            success=True,
            actions=("rearm_relay_owner",),
            at=utc_now(),
            healthcheck=HealthcheckResult(
                name=_expected_healthcheck(first) or "causal_predecessor_rearm_ready",
                passed=True,
                checks=("relay armed",),
                observed_at=utc_now(),
            ),
        )
        self.fail_once("A", "worker_protocol_rejected")
        opened = self.incidents_for("worker_protocol_rejected")
        self.assertEqual(len(opened), 2, "the repeat past the cap reached nobody")
        self.assertIsNone(opened[-1]["resolved_at"])

    def test_waiting_for_a_rate_limit_does_not_spend_the_cap(self) -> None:
        """A rate limit has its own barrier and its own reason.

        Spending the cap on it means stopping the run at someone else's
        expense: the task is not broken, it is waiting.
        """

        for _ in range(6):
            self.fail_once("A", "app_server_rpc_failed", rate_limited=True)

        self.assertEqual(self.attempts("app_server_rpc_failed"), 0)
        self.assertEqual(self.incidents_for("app_server_rpc_failed"), [])

    def test_a_failure_without_a_named_kind_is_refused_with_the_accepted_list(self) -> None:
        """R31: the refusal names what is accepted, so no sources are read."""

        from codex_autopilot.lifecycle_base import DesktopLifecycleError

        pending = reserve_ready_frontier(self.cfg)
        with self.assertRaises(DesktopLifecycleError) as caught:
            record_desktop_failure(
                self.cfg,
                pending[0].reservation_token,
                reason="что-то сломалось",
                failure_code="   ",
                definitive=True,
                reserve_other_ready=False,
            )
        message = str(caught.exception)
        self.assertIn("failure_code", message)
        self.assertIn("worker_protocol_rejected", message)


if __name__ == "__main__":
    unittest.main()
