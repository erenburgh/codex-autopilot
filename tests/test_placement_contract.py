"""A staged task's thread: filed at the root, writing only its workspace (R5, R6).

Every worker and verifier of a staged task used to get
``cwd = <root>/.codex-autopilot/staged-artifacts/<task>/workspace``; Desktop
files a thread only when its cwd EQUALS a project root, so all of them were
in no project, while the placement check said INSIDE by projectId alone.
Contract 2 (placement_contract) sends ``cwd = root`` and
``runtimeWorkspaceRoots = [workspace]`` on thread/start and on every
turn/start - once the isolation probe proved the root stays read-only.
Without that proof the old placement stays, and each such thread is an R5
defect with a ticket: never a silent fallback, never a stop.

The run below goes through the production dispatcher
(``run_automatic_app_server_turn``) with one fake App Server connection
that models the sandbox on disk: a ``touch`` succeeds inside the runtime
roots, and inside the root only when the fake is told the root is writable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest import mock

import test_artifact_staging as _staging
from _appserver_fakes import FakeAppServerCreateClient
from _desktop_state import desktop_home
from codex_autopilot.appserver import ApprovalRequired, TurnResult

OWNER, OWNER_TURN = "owner-thread", "owner-turn"


class SandboxedDispatcher(FakeAppServerCreateClient):
    """One connection: create, isolation probe, placement, production turn."""

    def __init__(self, root: Path, events: list[str], *, home: Path, thread_id: str = "worker-thread",
                 root_writable: bool = False, widen: bool = False, approval: bool = False,
                 on_complete=lambda: None) -> None:
        super().__init__(root, events, thread_id=thread_id, project_id=None)
        self.codex_home = str(home)
        self.root_writable, self.widen, self.approval = root_writable, widen, approval
        self.on_complete = on_complete
        self.probe_roots: list[Path] = []
        self.threads: list[dict] = []
        self.execs: list[dict] = []
        self.terminated: list[str] = []
        self.responded: list[object] = []
        self.turn_kwargs: dict = {}

    def start_thread(self, **kwargs):
        self.threads.append(kwargs)
        roots = [Path(item) for item in kwargs.get("workspace_roots") or [kwargs["cwd"]]]
        if kwargs.get("ephemeral"):
            self.probe_roots = roots
            return {
                "thread": {"id": "probe-thread", "cwd": str(kwargs["cwd"])},
                "runtimeWorkspaceRoots": [str(item) for item in roots],
                "sandbox": {"type": "workspaceWrite", "writableRoots": []},
            }
        started = super().start_thread(**kwargs)
        started["runtimeWorkspaceRoots"] = [str(self.canonical_cwd)] if self.widen else [str(item) for item in roots]
        return started

    def exec_command(self, command, *, cwd, process_id, sandbox_policy=None, permission_profile=None, timeout_ms=10_000):
        self.execs.append({"command": list(command), "cwd": Path(cwd), "process_id": process_id, "sandbox": sandbox_policy})
        if self.approval:
            raise ApprovalRequired({"id": 7, "method": "item/commandExecution/requestApproval", "params": {}})
        target = Path(command[-1])
        allowed = any(_within(target, item) for item in self.probe_roots) or (
            self.root_writable and _within(target, self.canonical_cwd)
        )
        if allowed:
            target.touch()
            return {"exitCode": 0}
        return {"exitCode": 1, "stderr": "Operation not permitted"}

    def terminate_command(self, process_id):
        self.terminated.append(process_id)

    def respond_project_memory_approval(self, request, *, persist):  # must never be called here
        self.responded.append(request)

    def read_thread(self, thread_id):
        if thread_id == OWNER:
            return {"id": thread_id, "turns": [{"id": OWNER_TURN, "status": "completed", "items": []}]}
        return super().read_thread(thread_id)

    def start_turn(self, **kwargs):
        self.turn_kwargs = kwargs
        return {"turn": {"id": "turn-a"}}

    def wait_for_turn(self, thread_id, turn_id, **_kwargs):
        self.on_complete()
        return TurnResult(thread_id, {"id": turn_id, "status": "completed", "items": [
            {"type": "agentMessage", "phase": "final_answer", "text": "AUTOPILOT_RULES: R24, R29\nAUTOPILOT_STATUS: ROTATE"},
        ]}, [])


def _within(target: Path, root: Path) -> bool:
    try:
        Path(target).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


class StagedRun(unittest.TestCase):
    def setUp(self) -> None:
        _staging.StagedArtifactLifecycleTests.setUp(self)
        self.home = desktop_home(roots=[self.root])

    def reserve(self):
        from _relay import reserve_ready_frontier
        from codex_autopilot.run_state import StateStore

        descriptor = reserve_ready_frontier(self.cfg, relay_owner_thread_id=OWNER)[0]
        store = StateStore(self.cfg.state_dir)
        state = store.load()
        session = self.session(descriptor.reservation_token, state)
        session["automatic_dispatch_state"] = "RUNNING"
        session["automatic_dispatch_pid"] = os.getpid()
        store.save(state)
        return descriptor

    def session(self, token, state=None):
        from codex_autopilot.run_state import StateStore

        state = state or StateStore(self.cfg.state_dir).load()
        return next(item for item in state.worker_sessions if item["reservation_token"] == token)

    def dispatch(self, client, descriptor):
        from codex_autopilot.lifecycle import run_automatic_app_server_turn

        workspace = Path(descriptor.cwd)
        client.on_complete = lambda: _staging.StagedArtifactLifecycleTests.checkpoint_and_evidence(
            self, workspace, "implementation", "implementation"
        )
        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            return run_automatic_app_server_turn(
                self.cfg, descriptor.reservation_token, initiator_thread_id=OWNER,
                initiator_turn_id=OWNER_TURN, connected_client=client,
            )

    def tickets(self, kind="placement_defect"):
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        return [
            item for item in PipelineIncidentStore(self.cfg.state_dir).load()["incidents"]
            if (item.get("system_state") or {}).get("stop_kind") == kind
        ]

    def prove_isolation(self):
        from codex_autopilot.isolation_probe import binary_identity, write_record

        write_record(self.cfg.state_dir, {
            "root": str(self.cfg.root), "permission_profile": self.cfg.desktop.permission_profile,
            "codex_binary": binary_identity(self.cfg.desktop.binary), "outcome": "PASS",
        })


class ContractTwoTests(StagedRun):
    def test_a_proven_run_files_the_worker_at_the_root_and_it_writes_only_its_workspace(self) -> None:
        """No record yet: the dispatcher measures on its own connection, then uses contract 2.

        Mutations, each fails here: ``thread_placement`` ignoring the proof
        (always contract 1 - cwd is the workspace, Desktop OUTSIDE); the
        production turn's cwd back to the workspace; the turn without
        ``workspace_roots``; ``ensure_measured`` not called in the create
        path (no record, contract 1).
        """

        descriptor = self.reserve()
        workspace = Path(descriptor.cwd)
        client = SandboxedDispatcher(self.root, [], home=self.home)
        outcome = self.dispatch(client, descriptor)
        self.assertEqual(outcome.worker_status, "ROTATE")
        record = json.loads((self.cfg.state_dir / "isolation-probe.json").read_text())
        self.assertEqual(record["outcome"], "PASS")
        probe, worker = client.threads
        self.assertTrue(probe["ephemeral"])
        self.assertEqual(Path(probe["cwd"]), self.root)
        self.assertEqual((Path(worker["cwd"]), worker["workspace_roots"]), (self.root, (workspace,)))
        self.assertEqual((Path(client.turn_kwargs["cwd"]), client.turn_kwargs["workspace_roots"]), (self.root, [workspace]))
        session = self.session(descriptor.reservation_token)
        self.assertEqual(session["placement_contract"], 2)
        self.assertEqual(Path(session["actual_cwd"]), self.root)
        self.assertEqual(Path(session["actual_workspace_root"]), workspace)
        self.assertEqual(session["desktop_placement"], "INSIDE")
        self.assertEqual(session["desktop_placement_after_first_turn"], "INSIDE")
        self.assertEqual(self.tickets(), [])
        self.assertEqual(client.responded, [])
        self.assertFalse(any(self.root.glob(".codex-autopilot-isolation-probe-*")))

    def test_a_writable_root_keeps_the_old_placement_and_says_so(self) -> None:
        """Measured mid-run: the root is writable. No silent fallback, no stop.

        The thread keeps its workspace as cwd (contract 1), Desktop files it
        in no project, and that is an R5 defect with one ticket that holds
        nothing - the turn still runs. Mutations: contract 2 regardless of
        the proof (cwd = root here); ``record_placement_defect`` not called
        (no ticket).
        """

        descriptor = self.reserve()
        workspace = Path(descriptor.cwd)
        client = SandboxedDispatcher(self.root, [], home=self.home, root_writable=True)
        outcome = self.dispatch(client, descriptor)
        self.assertEqual(outcome.worker_status, "ROTATE")
        record = json.loads((self.cfg.state_dir / "isolation-probe.json").read_text())
        self.assertEqual(record["outcome"], "ROOT_WRITABLE")
        session = self.session(descriptor.reservation_token)
        self.assertEqual((session["placement_contract"], Path(session["actual_cwd"])), (1, workspace))
        self.assertEqual(session["desktop_placement"], "OUTSIDE")
        self.assertEqual(session["r5_placement_defect"]["cause"], "isolation_not_proven")
        (ticket,) = self.tickets()
        self.assertEqual(ticket["affected_task_ids"], [])
        self.assertEqual(ticket["system_state"]["reason_code"], "ARCHITECTURE_DECISION")
        self.assertIn(str(session["thread_id"]), json.dumps(ticket["system_state"]["outside_threads"]))

    def test_a_server_that_widens_the_roots_is_refused_before_prepared(self) -> None:
        """Mutation: drop the ``roots_within`` check - the session becomes PREPARED."""

        from codex_autopilot.lifecycle import DesktopLifecycleError, create_desktop_thread_via_app_server

        self.prove_isolation()
        descriptor = self.reserve()
        client = SandboxedDispatcher(self.root, [], home=self.home, widen=True)
        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            with self.assertRaisesRegex(DesktopLifecycleError, "widened"):
                create_desktop_thread_via_app_server(
                    self.cfg, descriptor.reservation_token, client_factory=lambda *_a: client,
                    relay_executor_thread_id=OWNER,
                )
        self.assertNotEqual(self.session(descriptor.reservation_token)["status"], "PREPARED")

    def test_a_session_from_before_the_contract_is_checked_the_old_way(self) -> None:
        """A paused run's PREPARED session: actual_cwd = workspace, no placement_contract.

        Both it and a contract-2 session reach their turn. Mutation:
        ``session_cwd`` always the root - the old session is refused at
        claim (and on its resume); always the workspace - the new one is.
        """

        from codex_autopilot.lifecycle import claim_automatic_app_server_turn, create_desktop_thread_via_app_server
        from codex_autopilot.run_state import StateStore

        self.prove_isolation()
        descriptor = self.reserve()
        client = SandboxedDispatcher(self.root, [], home=self.home)
        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            create_desktop_thread_via_app_server(
                self.cfg, descriptor.reservation_token, client_factory=lambda *_a: client,
                relay_executor_thread_id=OWNER,
            )
        store = StateStore(self.cfg.state_dir)
        state = store.load()
        old = json.loads(json.dumps(self.session(descriptor.reservation_token, state)))
        claim_automatic_app_server_turn(self.cfg, descriptor.reservation_token, relay_executor_thread_id=OWNER)
        # The same session as the code before contract 2 left it.
        state = store.load()
        session = self.session(descriptor.reservation_token, state)
        session.update(old, status="PREPARED", actual_cwd=descriptor.cwd)
        session.pop("placement_contract")
        store.save(state)
        claim_automatic_app_server_turn(self.cfg, descriptor.reservation_token, relay_executor_thread_id=OWNER)
        self.assertEqual(self.session(descriptor.reservation_token)["status"], "SEND_RELAYING")


class IsolationProbeTests(StagedRun):
    def test_a_permission_request_is_never_answered_and_proves_nothing(self) -> None:
        """Her boundary: the command is terminated, the request stays unanswered.

        Mutations: accept the request (``respond_*`` called) - ``responded``
        is not empty; count the request as "read-only" - the outcome is PASS.
        """

        from codex_autopilot.isolation_probe import NOT_PROVEN, ensure_measured

        client = SandboxedDispatcher(self.root, [], home=self.home, approval=True)
        record = ensure_measured(self.cfg, client)
        self.assertEqual(record["outcome"], NOT_PROVEN)
        self.assertIn("never answered", record["reason"])
        self.assertEqual(client.responded, [])
        self.assertEqual(len(client.terminated), 2)

    def test_a_writable_root_fails_preflight_with_an_isolation_finding(self) -> None:
        """Mutation: report a writable root as NOT PROVEN (a quiet fallback) - no FAIL."""

        from codex_autopilot.preflight import PreflightError, _measure_isolation

        checks: list = []
        client = SandboxedDispatcher(self.root, [], home=self.home, root_writable=True)
        with self.assertRaisesRegex(PreflightError, "ISOLATION"):
            _measure_isolation(client, self.root, "codex", {}, lambda *item: checks.append(item))
        self.assertEqual(checks[-1][:2], ("Isolation", "FAIL"))
        self.assertFalse(any(self.root.glob(".codex-autopilot-isolation-probe-*")))
        client = SandboxedDispatcher(self.root, [], home=self.home)
        record = _measure_isolation(client, self.root, "codex", {}, lambda *item: checks.append(item))
        self.assertEqual((record["outcome"], checks[-1][:2]), ("PASS", ("Isolation", "OK")))

    def test_a_record_of_this_root_profile_and_binary_is_not_measured_again(self) -> None:
        from codex_autopilot.isolation_probe import ensure_measured

        self.prove_isolation()
        client = SandboxedDispatcher(self.root, [], home=self.home)
        self.assertEqual(ensure_measured(self.cfg, client)["outcome"], "PASS")
        self.assertEqual(client.threads, [])


class PlacementDefectTests(StagedRun):
    def setUp(self) -> None:
        from dataclasses import replace

        super().setUp()
        # A run with both projects configured, as preflight leaves it.
        self.cfg = replace(self.cfg, desktop=replace(self.cfg.desktop, project_id="app-project"))

    def created(self, client):
        from codex_autopilot.lifecycle import create_desktop_thread_via_app_server

        self.prove_isolation()
        descriptor = self.reserve()
        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            create_desktop_thread_via_app_server(
                self.cfg, descriptor.reservation_token, client_factory=lambda *_a: client,
                relay_executor_thread_id=OWNER,
            )
        return descriptor

    def test_an_unobservable_placement_is_a_defect_the_on_call_gets_and_nothing_stops(self) -> None:
        """The loop the independent check found: the gate raised, the on-call's thread too.

        UNOBSERVABLE is recorded and signalled once as a ticket routed to
        the on-call that holds no task; the gate returns. The on-call's own
        thread then passes the same gate. Mutations: raise on a non-INSIDE
        placement (the old gate) - DesktopLifecycleError; no dedupe - a
        second ticket for the same cause.
        """

        from codex_autopilot.lifecycle_dispatch import _require_thread_placement
        from codex_autopilot.pipeline_engineer import IncidentPhase

        missing = self.home.parent / "no-desktop-here"
        client = SandboxedDispatcher(self.root, [], home=missing)
        descriptor = self.created(client)
        self.assertEqual(
            _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None),
            "UNOBSERVABLE",
        )
        (ticket,) = self.tickets()
        self.assertEqual(ticket["affected_task_ids"], [])
        self.assertEqual(ticket["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
        self.assertEqual(ticket["system_state"]["cause"], "unobservable:none")
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        self.assertEqual(len(self.tickets()), 1)
        # Once per cause per run, not once per open ticket: even after the
        # on-call closed it, the same cause does not raise another.
        from codex_autopilot.pipeline_engineer import PipelineIncidentStore

        path = PipelineIncidentStore(self.cfg.state_dir).path
        stored = json.loads(path.read_text(encoding="utf-8"))
        for item in stored["incidents"]:
            item["resolved_at"] = "2026-09-24T00:00:00+00:00"
        path.write_text(json.dumps(stored), encoding="utf-8")
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        self.assertEqual(len(self.tickets()), 1)
        for item in stored["incidents"]:
            item.pop("resolved_at")
        path.write_text(json.dumps(stored), encoding="utf-8")
        from _relay import reserve_ready_frontier

        engineers = [item for item in reserve_ready_frontier(self.cfg, relay_owner_thread_id=OWNER) if item.kind == "pipeline_engineer"]
        self.assertEqual(len(engineers), 1)
        engineer_client = SandboxedDispatcher(self.root, [], home=missing, thread_id="engineer-thread")
        from codex_autopilot.lifecycle import create_desktop_thread_via_app_server

        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            create_desktop_thread_via_app_server(
                self.cfg, engineers[0].reservation_token, client_factory=lambda *_a: engineer_client,
                relay_executor_thread_id=self.session(engineers[0].reservation_token)["relay_owner_thread_id"],
            )
        self.assertEqual(
            _require_thread_placement(self.cfg, engineers[0].reservation_token, connected_client=engineer_client, at=None),
            "UNOBSERVABLE",
        )
        self.assertEqual(len(self.tickets()), 1)

    def test_the_two_facts_are_recorded_apart(self) -> None:
        """R5: "projectId set" and "filed in the project" are two fields and two words.

        Mutation: write only ``desktop_placement`` - the fields are missing.
        """

        from codex_autopilot.lifecycle_dispatch import _require_thread_placement
        from codex_autopilot.run_state import StateStore

        client = SandboxedDispatcher(self.root, [], home=self.home)
        descriptor = self.created(client)
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        session = self.session(descriptor.reservation_token)
        self.assertEqual((session["app_server_project_id_ok"], session["desktop_rule"]), (True, "exact_root"))
        (event,) = [item for item in StateStore(self.cfg.state_dir).load().lifecycle_journal
                    if item.get("event") == "desktop_placement_verified" and "sidebar=" in str(item.get("detail"))][-1:]
        self.assertIn("projectId=ok; sidebar=exact_root", event["detail"])

    def test_threads_created_outside_before_the_honest_check_are_listed_once(self) -> None:
        """They were recorded INSIDE by projectId; nothing else would ever name them.

        Mutation: ``signal_earlier_outside_threads`` not called - no ticket.
        """

        from codex_autopilot.lifecycle_dispatch import _require_thread_placement
        from codex_autopilot.run_state import StateStore

        client = SandboxedDispatcher(self.root, [], home=self.home)
        descriptor = self.created(client)
        store = StateStore(self.cfg.state_dir)
        state = store.load()
        old = json.loads(json.dumps(self.session(descriptor.reservation_token, state)))
        old.update(
            reservation_token="old-token", operation_id="old-operation", thread_id="01a0ce87",
            actual_thread_name="A · Implementation (old)", actual_cwd=descriptor.cwd,
            desktop_placement="INSIDE", status="COMPLETED",
        )
        old.pop("placement_contract")
        state.worker_sessions.insert(0, old)
        store.save(state)
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        (ticket,) = self.tickets()
        self.assertEqual(ticket["system_state"]["cause"], "created_before_the_honest_check")
        listed = ticket["system_state"]["outside_threads"]
        self.assertEqual([(item["thread_id"], item["title"]) for item in listed], [("01a0ce87", "A · Implementation (old)")])


class WhatTheThreadAtTheRootSeesTests(StagedRun):
    def test_the_prompt_names_the_workdir_and_not_the_old_cwd_claim(self) -> None:
        """Mutation: the old text back - 'App Server cwd is the isolated workspace'."""

        descriptor = self.reserve()
        self.assertIn(f"workdir={descriptor.cwd}", descriptor.prompt)
        self.assertNotIn("App Server cwd is the isolated workspace", descriptor.prompt)

    def test_project_memory_finds_the_run_from_either_path_of_the_thread(self) -> None:
        """The MCP process runs in runtimeWorkspaceRoots[0] or in the thread's cwd.

        Both resolve to the canonical project under contract 2: the staged
        marker leads back from the workspace, the root is the root.
        Mutation: contract roots other than the staged workspace - the
        first path does not resolve to the project.
        """

        from codex_autopilot.artifact_staging import resolve_canonical_project_root
        from codex_autopilot.lifecycle_dispatch import app_server_creation_contract

        self.prove_isolation()
        descriptor = self.reserve()
        params = app_server_creation_contract(self.cfg, descriptor)["params"]
        self.assertEqual(Path(params["cwd"]), self.root)
        for start in (params["runtimeWorkspaceRoots"][0], params["cwd"]):
            self.assertEqual(resolve_canonical_project_root(Path(start)), self.root)

    def test_the_stop_hook_trusted_is_the_one_of_the_threads_cwd(self) -> None:
        """hooks/list is read for cfg.root; under contract 2 that is the worker's cwd.

        Before, a worker ran in its workspace and its Stop hook inventory
        was that directory's - never the one trust was checked for.
        Mutation: contract 2's cwd back to the workspace.
        """

        from codex_autopilot import hook_trust
        from codex_autopilot.lifecycle_dispatch import app_server_creation_contract

        asked: list = []

        class Hooks:
            def connect(self):
                return {}

            def close(self):
                pass

            def list_hooks(self, cwd):
                asked.append(Path(cwd))
                raise RuntimeError("stop here")

        self.prove_isolation()
        descriptor = self.reserve()
        # hook_trust itself is never substituted by _gates: this is the real gate.
        with self.assertRaises(hook_trust.HookPreflightError):
            hook_trust.require_trusted_stop_hook_for_config(self.cfg, client_factory=lambda *_a: Hooks())
        self.assertEqual(asked, [Path(app_server_creation_contract(self.cfg, descriptor)["params"]["cwd"])])


class ApprovalFrequencyTests(StagedRun):
    def test_a_permission_request_from_a_root_filed_thread_is_counted(self) -> None:
        """The independent check's risk: a thread at the read-only root writes a relative path.

        The request is never answered; its ticket carries how many such
        requests this run already had and the thread's placement contract,
        so a rising count is read as a runtime defect of placement.
        Mutation: ``approvals_in_run`` a constant (the earlier ticket not
        counted) - the count reads 1.
        """

        from codex_autopilot.blocked_runs import stop_run
        from codex_autopilot.lifecycle import DesktopLifecycleError
        from codex_autopilot.run_state import StateStore, utc_now

        self.prove_isolation()
        descriptor = self.reserve()
        # An earlier request of this run - an on-call's, holding no task.
        store = StateStore(self.cfg.state_dir)
        state = store.load()
        stop_run(self.cfg, state, stop_kind="approval_required", phase="APPROVAL_REQUIRED",
                 reason="earlier", summary="earlier", at=utc_now(), task_ids=(), context_task_id="A")
        store.save(state)
        client = SandboxedDispatcher(self.root, [], home=self.home)

        def asks(*_args, **_kwargs):
            raise ApprovalRequired({"id": 3, "method": "item/fileChange/requestApproval", "params": {"path": "out.txt"}})

        client.wait_for_turn = asks
        with self.assertRaises(DesktopLifecycleError):
            self.dispatch(client, descriptor)
        (latest,) = [item["system_state"] for item in self.tickets("approval_required") if item["affected_task_ids"] == ["A"]]
        self.assertEqual((latest["approvals_in_run"], latest["placement_contract"]), (2, 2))
        self.assertEqual(client.responded, [])


class CodexHomeTests(unittest.TestCase):
    def test_a_double_never_reads_this_machines_desktop(self) -> None:
        """Mutation: fall back to the default home for any client - the double reads ~/.codex."""

        from codex_autopilot.appserver import AppServerClient
        from codex_autopilot.desktop_sidebar import codex_home_of
        from codex_autopilot.preflight import default_codex_home

        self.assertIsNone(codex_home_of(object()))
        self.assertEqual(codex_home_of(type("C", (), {"codex_home": "/x"})()), Path("/x"))
        real = AppServerClient("codex", Path(os.devnull))
        self.assertEqual(codex_home_of(real), default_codex_home())


class LaunchChecklistTests(unittest.TestCase):
    def test_a_recorded_r5_defect_shows_and_does_not_open_a_second_ticket(self) -> None:
        """Mutation: the placement item stays decisive - the verdict is FAILED."""

        from codex_autopilot.launch_gate import LaunchCheck, LaunchVerdict, _desktop_visibility, launch_verdict

        ok = LaunchCheck("reserved", "A", True, "")
        session = {"desktop_placement": "OUTSIDE", "r5_placement_defect": {"cause": "outside:none"}}
        placed = _desktop_visibility("A", "t1", session)
        self.assertIs(placed.passed, False)
        self.assertIn("R5 defect recorded", placed.detail)
        self.assertIs(launch_verdict([ok, placed]), LaunchVerdict.CONFIRMED)
        bare = _desktop_visibility("A", "t1", {"desktop_placement": "OUTSIDE"})
        self.assertIs(launch_verdict([ok, bare]), LaunchVerdict.FAILED)


if __name__ == "__main__":
    unittest.main()
