"""A staged task's thread: filed at the root, writing only its workspace (R5, R6).

Every worker and verifier of a staged task used to get
``cwd = <root>/.codex-autopilot/staged-artifacts/<task>/workspace``; Desktop
files a thread only when its cwd EQUALS a project root, so all of them were
in no project, while the placement check said INSIDE by projectId alone.
Contract 2 (placement_contract) sends ``cwd = root`` and
``runtimeWorkspaceRoots = [workspace]`` on thread/start and on every
turn/start, under the task's own staged permission profile - once the
isolation probe proved that profile keeps the root read-only. Without that
proof the old placement stays, and each such thread is an R5 defect with a
ticket: never a silent fallback, never a stop.

The fake App Server below models what the real protocol gives and nothing
more (the second independent check found the first fake tying
``command/exec`` to a thread's roots, which codex 0.154.0 does not do):
``command/exec`` knows no thread and no runtime roots; it runs under a named
profile. ``:workspace`` writes its ``:workspace_roots``, which default to the
cwd; a staged profile, defined by the ``-c`` overrides the process was
launched with, resolves its filesystem table by the most specific entry, a
tie going to write. Both are what ``codex sandbox`` measured on 0.154.0
(isolation_probe).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock

import test_artifact_staging as _staging
from _appserver_fakes import FakeAppServerCreateClient
from _desktop_state import desktop_home
from codex_autopilot.appserver import AppServerRpcError, ApprovalRequired, TurnResult

OWNER, OWNER_TURN = "owner-thread", "owner-turn"


class SandboxedDispatcher(FakeAppServerCreateClient):
    """One App Server process, as launched: its ``-c`` overrides define profiles."""

    def __init__(self, root: Path, events: list[str], *, home: Path, thread_id: str = "worker-thread",
                 config_overrides=(), filesystem_honored: bool = True, widen: bool = False,
                 approval: bool = False, environments=None, on_complete=lambda: None) -> None:
        super().__init__(root, events, thread_id=thread_id, project_id=None)
        self.codex_home = str(home)
        self.launch(config_overrides)
        # False: a binary that accepts the profile and ignores its filesystem
        # table - the staged profile then acts as its base, ``:workspace``.
        self.filesystem_honored = filesystem_honored
        self.widen, self.approval, self.environments = widen, approval, environments
        self.on_complete = on_complete
        self.threads: list[dict] = []
        self.execs: list[dict] = []
        self.terminated: list[str] = []
        self.responded: list[object] = []
        self.turn_kwargs: dict = {}

    def launch(self, config_overrides=()):
        """The process's ``-c`` overrides: the profiles it defines."""

        self.config_overrides = tuple(config_overrides)
        self.profiles = tomllib.loads("\n".join(self.config_overrides)).get("permissions", {})
        return self

    def list_permission_profiles(self, cwd):
        return [{"id": ":workspace", "allowed": True}, *({"id": key, "allowed": True} for key in self.profiles)]

    def _known(self, profile):
        if profile != ":workspace" and profile not in self.profiles:
            raise AppServerRpcError("thread/start", {"message": f"unknown permissions profile {profile}"})

    def start_thread(self, **kwargs):
        self._known(kwargs["permission_profile"])
        self.threads.append(kwargs)
        roots = [Path(item) for item in kwargs.get("workspace_roots") or [kwargs["cwd"]]]
        answer_roots = [str(self.canonical_cwd)] if self.widen else [str(item) for item in roots]
        if kwargs.get("ephemeral"):
            return {
                "thread": {"id": "probe-thread", "cwd": str(kwargs["cwd"])},
                "runtimeWorkspaceRoots": answer_roots,
                "activePermissionProfile": {"id": kwargs["permission_profile"]},
                "sandbox": {"type": "workspaceWrite", "writableRoots": []},
            }
        started = super().start_thread(**kwargs)
        started["runtimeWorkspaceRoots"] = answer_roots
        started["activePermissionProfile"] = {"id": kwargs["permission_profile"]}
        return started

    def _writable(self, target: Path, cwd: Path, profile: str) -> bool:
        if profile == ":workspace" or not self.filesystem_honored:
            return _within(target, cwd)
        table = self.profiles[profile].get("filesystem") or {}
        matches = [] if ":workspace_roots" in table or not _within(target, cwd) else [(_depth(cwd), "write")]
        for key, mode in table.items():
            base = cwd if key == ":workspace_roots" else Path(key)
            if _within(target, base):
                matches.append((_depth(base), mode))
        return bool(matches) and max(matches)[1] == "write"

    def exec_command(self, command, *, cwd, process_id, permission_profile, timeout_ms=10_000):
        self.execs.append({"command": list(command), "cwd": Path(cwd), "process_id": process_id,
                           "permission_profile": permission_profile})
        # True: every command asks. "outside_roots": only a command writing
        # outside the thread's runtime roots asks - the workspace write runs.
        roots = [Path(item) for item in (self.threads[-1].get("workspace_roots") or ())] if self.threads else []
        if self.approval is True or (
            self.approval == "outside_roots" and not any(_within(Path(command[-1]), item) for item in roots)
        ):
            raise ApprovalRequired({"id": 7, "method": "item/commandExecution/requestApproval", "params": {}})
        self._known(permission_profile)
        target = Path(command[-1])
        if self._writable(target, Path(cwd), permission_profile):
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
        thread = super().read_thread(thread_id)
        if self.environments is not None:
            thread["environments"] = [{"environmentId": "local", "cwd": str(self.thread_cwd),
                                       "runtimeWorkspaceRoots": [str(item) for item in self.environments]}]
        return thread

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


def _depth(path: Path) -> int:
    return len(Path(path).resolve().parts)


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

    def factory(self, **kwargs):
        """A client_factory in production's call shape: (binary, log, config_overrides=...)."""

        made: list[SandboxedDispatcher] = []
        kwargs.setdefault("home", self.home)

        def build(*_args, **factory_kwargs):
            made.append(SandboxedDispatcher(
                self.root, [], config_overrides=factory_kwargs.get("config_overrides", ()), **kwargs
            ))
            return made[-1]

        build.made = made
        # isolation_probe's own call shape: open_client(overrides).
        build.open = lambda overrides: build("codex", Path(os.devnull), config_overrides=overrides)
        return build

    def launched(self, descriptor, *, probe=None, **kwargs):
        """The dispatcher's client, launched as the cli relay loop launches it."""

        from codex_autopilot.isolation_probe import dispatcher_overrides

        probe = probe or self.factory()
        overrides = dispatcher_overrides(self.cfg, descriptor.reservation_token, client_factory=probe)
        kwargs.setdefault("home", self.home)
        return SandboxedDispatcher(self.root, [], config_overrides=overrides, **kwargs)

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

    def prove_isolation(self, **kwargs):
        """A real measurement on the fake server, as the dispatcher makes it."""

        from codex_autopilot.isolation_probe import ensure_measured

        return ensure_measured(self.cfg, self.factory(**kwargs).open)


class ContractTwoTests(StagedRun):
    def test_a_proven_run_files_the_worker_at_the_root_under_its_staged_profile(self) -> None:
        """No record yet: the dispatcher measures on a server of its own, then uses contract 2.

        Mutations, each fails here: ``server_overrides`` returning nothing
        (the dispatcher's server has no staged profile - contract 1, cwd the
        workspace, Desktop OUTSIDE); the production turn naming the run's
        profile instead of the session's; ``thread_placement`` ignoring the
        proof; the turn's cwd back to the workspace or without
        ``workspace_roots``.
        """

        from codex_autopilot.isolation_probe import staged_profile_id

        descriptor = self.reserve()
        workspace = Path(descriptor.cwd)
        probe = self.factory()
        client = self.launched(descriptor, probe=probe)
        outcome = self.dispatch(client, descriptor)
        self.assertEqual(outcome.worker_status, "ROTATE")
        record = json.loads((self.cfg.state_dir / "isolation-probe.json").read_text())
        self.assertEqual(record["outcome"], "PASS")
        (probe_server,) = probe.made
        self.assertTrue(probe_server.threads[0]["ephemeral"])
        staged = staged_profile_id(workspace)
        (worker,) = client.threads
        self.assertEqual(
            (Path(worker["cwd"]), worker["workspace_roots"], worker["permission_profile"]),
            (self.root, (workspace,), staged),
        )
        self.assertEqual(
            (Path(client.turn_kwargs["cwd"]), client.turn_kwargs["workspace_roots"], client.turn_kwargs["permission_profile"]),
            (self.root, [workspace], staged),
        )
        session = self.session(descriptor.reservation_token)
        self.assertEqual((session["placement_contract"], session["permission_profile"]), (2, staged))
        self.assertEqual(Path(session["actual_cwd"]), self.root)
        self.assertEqual(Path(session["actual_workspace_root"]), workspace)
        self.assertEqual(session["app_server_creation_contract"]["params"]["permissions"], staged)
        self.assertEqual(session["desktop_placement"], "INSIDE")
        self.assertEqual(session["desktop_placement_after_first_turn"], "INSIDE")
        self.assertEqual(self.tickets(), [])
        self.assertEqual(client.responded, [])
        self.assertFalse(any(self.root.glob(".codex-autopilot-isolation-probe-*")))

    def test_a_root_the_staged_profile_does_not_hold_is_the_on_calls_not_her_choice(self) -> None:
        """Measured mid-run: the staged profile left the root writable. No stop, no fork for her.

        The thread keeps its workspace as cwd (contract 1), Desktop files it
        in no project, and that is an R5 defect with one ticket that holds
        nothing - the turn still runs. The ticket is a runtime defect: the
        first version told her to choose between isolated tasks outside the
        project and a deny profile (ARCHITECTURE_DECISION). Mutations:
        contract 2 regardless of the proof; ``record_placement_defect`` not
        called (no ticket); the old diagnosis back (her code, her choice).
        """

        descriptor = self.reserve()
        workspace = Path(descriptor.cwd)
        client = self.launched(descriptor, probe=self.factory(filesystem_honored=False))
        self.assertEqual(client.config_overrides, ())
        outcome = self.dispatch(client, descriptor)
        self.assertEqual(outcome.worker_status, "ROTATE")
        record = json.loads((self.cfg.state_dir / "isolation-probe.json").read_text())
        self.assertEqual(record["outcome"], "ROOT_WRITABLE")
        session = self.session(descriptor.reservation_token)
        self.assertEqual((session["placement_contract"], Path(session["actual_cwd"])), (1, workspace))
        self.assertEqual(session["permission_profile"], self.cfg.desktop.permission_profile)
        self.assertEqual(session["desktop_placement"], "OUTSIDE")
        self.assertEqual(session["r5_placement_defect"]["cause"], "isolation_not_proven")
        (ticket,) = self.tickets()
        system = ticket["system_state"]
        self.assertEqual(ticket["affected_task_ids"], [])
        self.assertEqual(system["reason_code"], "RECOVERY_EXHAUSTED")
        self.assertIn("not hers to choose", system["recommendation"])
        self.assertIn(str(session["thread_id"]), json.dumps(system["outside_threads"]))

    def test_a_server_that_widens_the_roots_is_refused_before_prepared(self) -> None:
        """Mutation: drop the ``roots_within`` check - the session becomes PREPARED."""

        from codex_autopilot.lifecycle import DesktopLifecycleError, create_desktop_thread_via_app_server

        self.prove_isolation()
        descriptor = self.reserve()
        factory = self.factory(widen=True)
        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            with self.assertRaisesRegex(DesktopLifecycleError, "widened"):
                create_desktop_thread_via_app_server(
                    self.cfg, descriptor.reservation_token, client_factory=factory,
                    relay_executor_thread_id=OWNER,
                )
        self.assertNotEqual(self.session(descriptor.reservation_token)["status"], "PREPARED")
        # The server this function launched itself defined the staged profile.
        self.assertTrue(factory.made[0].config_overrides)

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
        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            create_desktop_thread_via_app_server(
                self.cfg, descriptor.reservation_token, client_factory=self.factory(),
                relay_executor_thread_id=OWNER,
            )
        store = StateStore(self.cfg.state_dir)
        state = store.load()
        old = json.loads(json.dumps(self.session(descriptor.reservation_token, state)))
        self.assertEqual(old["placement_contract"], 2)
        claim_automatic_app_server_turn(self.cfg, descriptor.reservation_token, relay_executor_thread_id=OWNER)
        # The same session as the code before contract 2 left it.
        state = store.load()
        session = self.session(descriptor.reservation_token, state)
        session.update(old, status="PREPARED", actual_cwd=descriptor.cwd)
        session.pop("placement_contract")
        session.pop("permission_profile")
        store.save(state)
        claim_automatic_app_server_turn(self.cfg, descriptor.reservation_token, relay_executor_thread_id=OWNER)
        self.assertEqual(self.session(descriptor.reservation_token)["status"], "SEND_RELAYING")
        # Its turn names the profile it was created with - the run's.
        from codex_autopilot.placement_contract import session_profile

        self.assertEqual(session_profile(self.cfg, self.session(descriptor.reservation_token)), ":workspace")

    def test_roots_that_came_back_wider_are_signalled_and_the_turn_narrows_them(self) -> None:
        """Desktop rebuilds a thread's roots when she opens it (isolation_guard).

        The thread reports roots wider than its workspace before the turn:
        recorded on the session, one ticket that holds nothing, and the turn
        still goes out with the workspace and the staged profile. Mutation:
        ``check_thread_roots`` not called - no record, no ticket.
        """

        descriptor = self.reserve()
        workspace = Path(descriptor.cwd)
        client = self.launched(descriptor, environments=[self.root, workspace])
        self.assertEqual(self.dispatch(client, descriptor).worker_status, "ROTATE")
        session = self.session(descriptor.reservation_token)
        (widened,) = session["runtime_roots_widened"]
        self.assertEqual(widened["widened"], [str(self.root)])
        (ticket,) = self.tickets()
        self.assertEqual((ticket["system_state"]["cause"], ticket["affected_task_ids"]), ("runtime_roots_widened", []))
        self.assertEqual(client.turn_kwargs["workspace_roots"], [workspace])
        self.assertEqual(client.turn_kwargs["permission_profile"], session["permission_profile"])


class SessionsFromBeforeTheContractTests(StagedRun):
    """What a paused run brings with it, point by point (the verdict's amendment 7).

    The beyondness run paused on 2026-09-23 with the M01 verifier
    01a0cf05 ACTIVE: ``actual_cwd`` its staged workspace, no
    ``placement_contract``, its dispatcher gone. The two ways an old
    session meets the new code are exercised here with production's own
    call shapes: a PREPARED one is resumed and run by the dispatcher, an
    ACTIVE one is reconciled when she resumes the run.
    """

    def created_old(self, *, status_after=None):
        """A session as the code before contract 2 left it: cwd = its workspace, no contract."""

        from codex_autopilot.lifecycle import create_desktop_thread_via_app_server
        from codex_autopilot.run_state import StateStore

        self.prove_isolation()
        descriptor = self.reserve()
        workspace = Path(descriptor.cwd)
        creator = SandboxedDispatcher(self.root, [], home=self.home)
        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            create_desktop_thread_via_app_server(
                self.cfg, descriptor.reservation_token,
                client_factory=lambda *_a, config_overrides=(): creator.launch(config_overrides),
                relay_executor_thread_id=OWNER,
            )
        if status_after:
            status_after(descriptor)
        store = StateStore(self.cfg.state_dir)
        state = store.load()
        session = self.session(descriptor.reservation_token, state)
        session.pop("placement_contract")
        session.pop("permission_profile")
        session["actual_cwd"] = str(workspace)
        session["app_server_creation_contract"]["params"].update(
            cwd=str(workspace), permissions=self.cfg.desktop.permission_profile
        )
        store.save(state)
        return descriptor, workspace

    def test_an_old_prepared_session_is_resumed_and_turns_the_old_way(self) -> None:
        """Resume on a fresh dispatcher: its thread's cwd, its roots, the run's profile, no staged server.

        Isolation is proven for the run, yet the old thread keeps what it
        was created with to the end of its life. Mutations: ``session_cwd``
        always the root (the thread's cwd is refused before its turn);
        ``server_overrides`` giving a created thread the staged profile
        when isolation is proven (overrides for the old session);
        ``session_profile`` the staged profile (the turn names it).
        """

        from codex_autopilot.isolation_probe import dispatcher_overrides

        descriptor, workspace = self.created_old()
        probe = self.factory()
        self.assertEqual(dispatcher_overrides(self.cfg, descriptor.reservation_token, client_factory=probe), ())
        self.assertEqual(probe.made, [])
        client = SandboxedDispatcher(self.root, [], home=self.home)
        client.thread_cwd = workspace
        self.assertEqual(self.dispatch(client, descriptor).worker_status, "ROTATE")
        self.assertIn("thread-resumed", client.events)
        self.assertEqual(client.threads, [])
        self.assertEqual(
            (Path(client.turn_kwargs["cwd"]), client.turn_kwargs["workspace_roots"], client.turn_kwargs["permission_profile"]),
            (workspace, [workspace], self.cfg.desktop.permission_profile),
        )
        session = self.session(descriptor.reservation_token)
        self.assertIsNone(session.get("placement_contract"))
        self.assertNotIn("runtime_roots_widened", session)

    def test_the_paused_runs_active_session_is_retired_on_resume_and_its_successor_is_filed_at_the_root(self) -> None:
        """01a0cf05's road when she resumes: reconciled, never resumed; the next attempt is contract 2.

        Resume (``control._reconcile_before_resume``) asks the server about
        every pending session; a finished thread (``notLoaded``) retires the
        attempt to RETRY_WAIT - its thread is not resumed and no turn is
        started in it, so no placement or isolation check of the new
        contract ever meets it. The task's next attempt is a new thread,
        filed at the root under the staged profile. The old thread stays
        where it is and is named in the created-before ticket.
        Mutations: ``_observe_one`` reading notLoaded as unknown (the
        session stays ACTIVE); ``thread_placement`` ignoring the proof (the
        successor's cwd is its workspace).
        """

        import time

        from _relay import reserve_ready_frontier
        from codex_autopilot.control import _reconcile_before_resume
        from codex_autopilot.lifecycle import acknowledge_desktop_send, claim_automatic_app_server_turn
        from codex_autopilot.lifecycle_dispatch import app_server_creation_contract
        from codex_autopilot.run_state import StateStore

        def to_active(descriptor):
            claim_automatic_app_server_turn(self.cfg, descriptor.reservation_token, relay_executor_thread_id=OWNER)
            acknowledge_desktop_send(self.cfg, descriptor.reservation_token, thread_id="worker-thread",
                                     relay_executor_thread_id=OWNER)

        descriptor, workspace = self.created_old(status_after=to_active)
        self.assertEqual(self.session(descriptor.reservation_token)["status"], "ACTIVE")
        server = SandboxedDispatcher(self.root, [], home=self.home)
        server.read_thread = lambda thread_id: {
            "id": thread_id, "cwd": str(workspace), "status": {"type": "notLoaded"}, "turns": [],
        }
        with mock.patch("codex_autopilot.appserver.AppServerClient", lambda *_a, **_k: server):
            self.assertEqual(_reconcile_before_resume(self.cfg), ("A",))
        self.assertNotIn("thread-resumed", server.events)
        self.assertEqual(server.turn_kwargs, {})
        state = StateStore(self.cfg.state_dir).load()
        self.assertEqual(self.session(descriptor.reservation_token, state)["status"], "RETRY_WAIT")
        self.assertEqual(state.task_states["A"], "RETRY_WAIT")
        (successor,) = [
            item for item in reserve_ready_frontier(
                self.cfg, relay_owner_thread_id=OWNER, now_epoch=int(time.time()) + 86_400,
            ) if item.task_id == "A"
        ]
        params = app_server_creation_contract(self.cfg, successor)["params"]
        self.assertEqual((Path(params["cwd"]), params["runtimeWorkspaceRoots"]), (self.root, [successor.cwd]))


class IsolationProbeTests(StagedRun):
    def test_the_probe_measures_the_staged_profile_on_the_root_never_a_legacy_sandbox(self) -> None:
        """What the second independent check asked for, on the protocol as it is.

        ``command/exec`` runs with cwd = the root and the task's staged
        profile; nothing of the thread/start answer's legacy sandbox is sent;
        the ephemeral thread is started as a worker's; the workspace is under
        the state directory. Mutations: exec under the run's profile
        (``:workspace`` - its roots are the cwd, the root: ROOT_WRITABLE);
        exec with cwd = the workspace (cwd assertion); a temporary workspace
        (location assertion).
        """

        from codex_autopilot.isolation_probe import ensure_measured, probe_workspace, staged_profile_id

        probe = self.factory()
        record = ensure_measured(self.cfg, probe.open)
        self.assertEqual(record["outcome"], "PASS", record)
        (server,) = probe.made
        workspace = probe_workspace(self.cfg.state_dir).resolve()
        staged = staged_profile_id(workspace)
        self.assertEqual(Path(record["workspace"]), workspace)
        self.assertIn(staged, "\n".join(server.config_overrides))
        (thread,) = server.threads
        self.assertEqual((thread["ephemeral"], Path(thread["cwd"]), thread["workspace_roots"], thread["permission_profile"]),
                         (True, self.root, [workspace], staged))
        self.assertEqual({(item["cwd"], item["permission_profile"]) for item in server.execs}, {(self.root, staged)})
        self.assertNotIn("sandbox", json.dumps(server.execs, default=str))
        self.assertFalse(any(self.root.glob(".codex-autopilot-isolation-probe-*")))

    def test_a_permission_request_is_never_answered_and_proves_nothing(self) -> None:
        """Her boundary: the command is terminated, the request stays unanswered.

        Mutation: accept the request (``respond_*`` called) - ``responded``
        is not empty. Every command asks here, the workspace write too, so
        counting the request as "read-only" does not turn this outcome into
        PASS; the next test pins that mutation.
        """

        from codex_autopilot.isolation_probe import NOT_PROVEN, ensure_measured

        probe = self.factory(approval=True)
        record = ensure_measured(self.cfg, probe.open)
        self.assertEqual(record["outcome"], NOT_PROVEN)
        self.assertIn("never answered", record["reason"])
        (server,) = probe.made
        self.assertEqual(server.responded, [])
        self.assertEqual(len(server.terminated), 2)

    def test_a_request_on_the_root_write_alone_is_not_proven_never_pass(self) -> None:
        """The verdict's amendment: a permission request is "not proven", never "not writable".

        The test above has the fake ask on every command, the workspace
        write included, so the outcome is NOT_PROVEN through "the workspace
        was not writable" whatever the root's answer is; the fourth
        independent check turned the request into "did not write" in
        ``_wrote`` and all tests stayed green. Here the workspace write
        runs and only the write under the root asks. Mutation: ``_wrote``
        returning False on ApprovalRequired - the outcome is PASS, and
        contract 2 would be taken on a root nobody measured.
        """

        from codex_autopilot.isolation_probe import NOT_PROVEN, ensure_measured, isolation_proven

        probe = self.factory(approval="outside_roots")
        record = ensure_measured(self.cfg, probe.open)
        (server,) = probe.made
        self.assertEqual(record["outcome"], NOT_PROVEN)
        self.assertFalse(isolation_proven(self.cfg))
        self.assertEqual((record["workspace_write"], record["root_write"]), (True, None))
        self.assertIn("never answered", record["reason"])
        self.assertEqual(server.responded, [])
        self.assertEqual(len(server.terminated), 1)

    def test_a_record_of_another_shape_is_measured_again(self) -> None:
        """Only a record of the real shape counts; one of the first probe's does not.

        The first probe measured with a system temp directory as workspace
        and wrote version 1. Mutation: ``record_matches`` without the
        workspace-location check - the temp-directory record is taken, and
        nothing is measured.
        """

        from codex_autopilot.isolation_probe import (
            RECORD_VERSION, binary_identity, ensure_measured, runtime_code_identity, write_record,
        )

        base = {
            "root": str(self.cfg.root), "base_profile": self.cfg.desktop.permission_profile,
            "codex_binary": binary_identity(self.cfg.desktop.binary), "outcome": "PASS",
            "runtime_code": runtime_code_identity(),
        }
        for stale in ({**base, "version": RECORD_VERSION, "workspace": tempfile.gettempdir() + "/codex-autopilot-isolation-x"},
                      {**base, "version": 1, "workspace": str(self.cfg.state_dir / "isolation-probe" / "workspace")}):
            write_record(self.cfg.state_dir, stale)
            probe = self.factory(filesystem_honored=False)
            self.assertEqual(ensure_measured(self.cfg, probe.open)["outcome"], "ROOT_WRITABLE")
            self.assertEqual(len(probe.made), 1)
        probe = self.factory()
        self.assertEqual(ensure_measured(self.cfg, probe.open)["outcome"], "ROOT_WRITABLE")
        self.assertEqual(probe.made, [])

    def test_a_not_proven_record_is_measured_again_after_its_interval(self) -> None:
        """The third independent check: one transient failure used to keep the run on contract 1.

        A probe that could not prove anything (here: a permission request,
        never answered) writes NOT_PROVEN. Within its interval the
        dispatcher takes it as it is - no probe per task; after it, the
        dispatcher measures again on its own server and the task gets its
        staged profile. Mutations: ``record_stands`` ignoring the outcome
        (the record stands forever - no overrides after the interval); the
        interval never elapsing (the same).
        """

        from datetime import datetime, timedelta, timezone

        from codex_autopilot.isolation_probe import (
            NOT_PROVEN, NOT_PROVEN_RETRY_SECONDS, PASS, dispatcher_overrides, load_record, write_record,
        )

        descriptor = self.reserve()
        failing = self.factory(approval=True)
        self.assertEqual(dispatcher_overrides(self.cfg, descriptor.reservation_token, client_factory=failing), ())
        self.assertEqual(load_record(self.cfg.state_dir)["outcome"], NOT_PROVEN)
        healthy = self.factory()
        self.assertEqual(dispatcher_overrides(self.cfg, descriptor.reservation_token, client_factory=healthy), ())
        self.assertEqual(healthy.made, [])
        record = load_record(self.cfg.state_dir)
        record["measured_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=NOT_PROVEN_RETRY_SECONDS + 1)
        ).isoformat()
        write_record(self.cfg.state_dir, record)
        overrides = dispatcher_overrides(self.cfg, descriptor.reservation_token, client_factory=healthy)
        self.assertEqual(len(healthy.made), 1)
        self.assertEqual(load_record(self.cfg.state_dir)["outcome"], PASS)
        self.assertTrue(overrides)

    def test_an_installed_runtime_repair_is_measured_again_whatever_the_outcome(self) -> None:
        """The on-call's route for ROOT_WRITABLE: repair the runtime, and the next thread measures.

        A ROOT_WRITABLE on the same code and binary stands (a measurement,
        not a transient); the record names the runtime code that measured
        it, and code changed by an installed patch measures again.
        Mutation: ``record_matches`` without ``runtime_code`` - the old
        ROOT_WRITABLE stands after the repair and nothing is measured.
        """

        from codex_autopilot.isolation_probe import PASS, ROOT_WRITABLE, ensure_measured, runtime_code_identity

        record = ensure_measured(self.cfg, self.factory(filesystem_honored=False).open)
        self.assertEqual((record["outcome"], record["runtime_code"]), (ROOT_WRITABLE, runtime_code_identity()))
        again = self.factory()
        self.assertEqual(ensure_measured(self.cfg, again.open)["outcome"], ROOT_WRITABLE)
        self.assertEqual(again.made, [])
        with mock.patch("codex_autopilot.isolation_probe.runtime_code_identity", return_value="repaired0000000"):
            record = ensure_measured(self.cfg, again.open)
        self.assertEqual(len(again.made), 1)
        self.assertEqual((record["outcome"], record["runtime_code"]), (PASS, "repaired0000000"))

    def test_the_ticket_names_the_route_that_exists(self) -> None:
        """The recommendation no longer sends the on-call to wait for a record that never changes.

        Mutation: the earlier text ("it measures when the record does not
        match this root, profile and binary") - a route a runtime patch
        never takes.
        """

        from codex_autopilot.placement_defects import _diagnosis

        _, recommendation, code = _diagnosis("isolation_not_proven", {}, {})
        self.assertIn("measures again by itself", recommendation)
        self.assertIn("runtime code", recommendation)
        self.assertNotIn("does not match this root, profile and binary", recommendation)
        self.assertEqual(code, "RECOVERY_EXHAUSTED")

    def test_preflight_measures_the_runs_profile_in_the_state_dir_and_never_stops(self) -> None:
        """Preflight: the configured profile, a workspace under the state dir, a finding, no stop.

        Mutations: the profile hard-coded to ``:workspace`` (base_profile
        assertion); the workspace in a temporary directory (location
        assertion); a writable root raising PreflightError again - a stop
        no on-call would see.
        """

        from dataclasses import replace

        from codex_autopilot.preflight import _measure_isolation

        # Config admits only ``:workspace`` today (config.py); the probe must
        # still take the run's profile from the run's config, not a literal.
        configured = replace(self.cfg, desktop=replace(self.cfg.desktop, permission_profile="autopilot-run"))
        checks: list = []
        probe = self.factory()
        with mock.patch("codex_autopilot.config.load_config", return_value=configured):
            record = _measure_isolation(probe, self.root, "codex", {}, lambda *item: checks.append(item))
        self.assertEqual((record["outcome"], checks[-1][:2]), ("PASS", ("Isolation", "OK")))
        self.assertEqual(record["base_profile"], "autopilot-run")
        self.assertIn('extends="autopilot-run"', "\n".join(probe.made[0].config_overrides))
        self.assertTrue(Path(record["workspace"]).is_relative_to(self.cfg.state_dir))
        self.assertFalse((self.cfg.state_dir / "isolation-probe").exists())
        record = _measure_isolation(self.factory(filesystem_honored=False), self.root, "codex", {},
                                    lambda *item: checks.append(item))
        self.assertEqual((record["outcome"], checks[-1][:2]), ("ROOT_WRITABLE", ("Isolation", "FAIL")))
        self.assertIn("ISOLATION", checks[-1][2])
        self.assertFalse(any(self.root.glob(".codex-autopilot-isolation-probe-*")))


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
                self.cfg, descriptor.reservation_token,
                client_factory=lambda *_a, config_overrides=(): client.launch(config_overrides),
                relay_executor_thread_id=OWNER,
            )
        return descriptor

    def test_an_unobservable_placement_is_a_defect_the_on_call_gets_and_nothing_stops(self) -> None:
        """The loop the independent check found: the gate raised, the on-call's thread too.

        UNOBSERVABLE is recorded and signalled once as a ticket routed to
        the on-call that holds no task; the gate returns. The on-call's own
        thread then passes the same gate. Mutations: raise on a non-INSIDE
        placement (the old gate) - DesktopLifecycleError; no dedupe - a
        second ticket for the same cause while the first is open.
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
        # Once per cause while its ticket is open. This test used to pin
        # "once per run, even after the on-call closed it" (by writing
        # resolved_at into the journal by hand); the fourth independent
        # check showed that a repair that did not hold then reached nobody.
        # Closed by the on-call and seen again, the cause is a new ticket in
        # the on-call's lane (the bound on repeats is R23's, tested in
        # test_a_cause_that_comes_back_after_its_ticket_closed_is_signalled_again).
        self.close(ticket)
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        first, again = self.tickets()
        self.assertEqual((first["phase"], again["phase"]),
                         (IncidentPhase.RESOLVED.value, IncidentPhase.PIPELINE_ENGINEER.value))
        self.assertEqual(again["system_state"]["cause"], "unobservable:none")
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
        # The on-call's own thread, same cause, its ticket open: no third.
        self.assertEqual(len(self.tickets()), 2)

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

        Mutations: ``signal_earlier_outside_threads`` not called - no
        ticket; its ``again_after_close=False`` dropped - a second ticket
        naming the same threads after the first was closed.
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
        # A finished fact, not a cause that comes back: closed, it is not
        # filed again for the same threads on every later launch.
        self.close(ticket)
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        self.assertEqual(len(self.tickets()), 1)


    def test_an_unreadable_desktop_claims_no_thread_outside(self) -> None:
        """The third independent check: UNOBSERVABLE used to be listed as outside.

        With no Desktop state every thread measures UNOBSERVABLE; the
        ticket said "Desktop files them in no project" of old sessions at
        the root too. Now they are ``unobserved_threads`` and no
        created-before ticket is filed; once the state reads again, only
        the thread really outside is listed. Mutation: ``outside_threads``
        taking everything that is not INSIDE - the old root session is
        claimed outside and a created-before ticket is filed while Desktop
        cannot be read.
        """

        from codex_autopilot.lifecycle_dispatch import _require_thread_placement
        from codex_autopilot.run_state import StateStore

        missing = self.home.parent / "no-desktop-here"
        client = SandboxedDispatcher(self.root, [], home=missing)
        descriptor = self.created(client)
        store = StateStore(self.cfg.state_dir)
        state = store.load()
        template = json.loads(json.dumps(self.session(descriptor.reservation_token, state)))
        template.pop("placement_contract")
        for token, thread_id, title, cwd in (
            ("old-root", "01a0root", "Screening (old)", str(self.root)),
            ("old-workspace", "01a0ce87", "A · Implementation (old)", descriptor.cwd),
        ):
            old = dict(template, reservation_token=token, operation_id=f"{token}-op", thread_id=thread_id,
                       actual_thread_name=title, actual_cwd=cwd, desktop_placement="INSIDE", status="COMPLETED")
            state.worker_sessions.insert(0, old)
        store.save(state)
        self.assertEqual(
            _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None),
            "UNOBSERVABLE",
        )
        (ticket,) = self.tickets()
        system = ticket["system_state"]
        self.assertEqual(system["cause"], "unobservable:none")
        self.assertEqual(system["outside_threads"], [])
        self.assertEqual({item["thread_id"] for item in system["unobserved_threads"]},
                         {"01a0root", "01a0ce87", "worker-thread"})
        self.assertIn("not known and not claimed", system["diagnosis"])
        readable = SandboxedDispatcher(self.root, [], home=self.home)
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=readable, at=None)
        (earlier,) = [item for item in self.tickets() if item["system_state"]["cause"] == "created_before_the_honest_check"]
        self.assertEqual([item["thread_id"] for item in earlier["system_state"]["outside_threads"]], ["01a0ce87"])
        self.assertEqual(earlier["system_state"]["unobserved_threads"], [])

    def test_two_causes_of_one_run_are_two_tickets(self) -> None:
        """The second independent check's reproduction: a contract-1 thread and older outside threads.

        Isolation not proven (no record: contract 1) and a thread of this
        run created outside the project before the honest check are two
        causes; each gets its own ticket with its own diagnosis, although
        the first is still open. Mutation: the ticket's signal id without
        the cause (blocked_runs, ``signal_key``) - the second cause gets the
        first cause's ticket back, and the older threads are never named.
        """

        from codex_autopilot.lifecycle import create_desktop_thread_via_app_server
        from codex_autopilot.lifecycle_dispatch import _require_thread_placement
        from codex_autopilot.run_state import StateStore

        descriptor = self.reserve()
        client = SandboxedDispatcher(self.root, [], home=self.home)
        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            create_desktop_thread_via_app_server(
                self.cfg, descriptor.reservation_token,
                client_factory=lambda *_a, config_overrides=(): client.launch(config_overrides),
                relay_executor_thread_id=OWNER,
            )
        store = StateStore(self.cfg.state_dir)
        state = store.load()
        old = json.loads(json.dumps(self.session(descriptor.reservation_token, state)))
        old.update(
            reservation_token="old-token", operation_id="old-operation", thread_id="01a0ce87",
            actual_thread_name="A · Implementation (old)", desktop_placement="INSIDE", status="COMPLETED",
        )
        old.pop("placement_contract")
        state.worker_sessions.insert(0, old)
        store.save(state)
        self.assertEqual(self.session(descriptor.reservation_token)["placement_contract"], 1)
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        causes = sorted(item["system_state"]["cause"] for item in self.tickets())
        self.assertEqual(causes, ["created_before_the_honest_check", "isolation_not_proven"])
        (earlier,) = [item for item in self.tickets() if item["system_state"]["cause"] == "created_before_the_honest_check"]
        self.assertIn("01a0ce87", json.dumps(earlier["system_state"]["defect"]))


    def close(self, ticket) -> None:
        """The on-call closes the ticket as the store demands: a named repair and a healthcheck."""

        from codex_autopilot.pipeline_engineer import HealthcheckResult, IncidentPhase, PipelineIncidentStore

        self.assertEqual(ticket["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
        PipelineIncidentStore(self.cfg.state_dir).complete_pipeline_engineer(
            ticket["incident_id"], success=True, at="2026-09-24T00:00:00+00:00",
            actions=("repair_runtime_code",),
            healthcheck=HealthcheckResult(name="run_declared_healthcheck", passed=True,
                                          checks=("staged profile repaired",), observed_at="2026-09-24T00:00:00+00:00"),
        )

    def test_a_cause_that_comes_back_after_its_ticket_closed_is_signalled_again(self) -> None:
        """The fourth independent check: a closed ticket swallowed the cause for the rest of the run.

        The on-call repairs isolation and closes the ticket; the next staged
        thread still runs contract 1. That defect is a new ticket - and the
        third one, after two closures that did not hold, goes to her with
        the report (R23, stop_repeats), not to a third engineer. While a
        ticket is open no second one is filed. Another cause with the same
        reason code, after those closures, still goes to the on-call.
        Mutations: ``_file_once`` counting closed tickets again - one ticket
        after the closure; ``repeat_signature`` without ``signal_key`` - the
        first runtime_roots_widened ticket goes straight to her.
        """

        from codex_autopilot.isolation_guard import check_thread_roots
        from codex_autopilot.lifecycle import create_desktop_thread_via_app_server
        from codex_autopilot.lifecycle_dispatch import _require_thread_placement
        from codex_autopilot.pipeline_engineer import IncidentPhase

        descriptor = self.reserve()
        client = SandboxedDispatcher(self.root, [], home=self.home)
        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            create_desktop_thread_via_app_server(
                self.cfg, descriptor.reservation_token,
                client_factory=lambda *_a, config_overrides=(): client.launch(config_overrides),
                relay_executor_thread_id=OWNER,
            )
        self.assertEqual(self.session(descriptor.reservation_token)["placement_contract"], 1)

        def isolation_tickets():
            return [item for item in self.tickets() if item["system_state"]["cause"] == "isolation_not_proven"]

        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        (first,) = isolation_tickets()
        self.close(first)
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        self.assertEqual(len(isolation_tickets()), 2)
        self.assertEqual(isolation_tickets()[-1]["phase"], IncidentPhase.PIPELINE_ENGINEER.value)
        self.close(isolation_tickets()[-1])
        _require_thread_placement(self.cfg, descriptor.reservation_token, connected_client=client, at=None)
        third = isolation_tickets()[-1]
        self.assertEqual(len(isolation_tickets()), 3)
        self.assertEqual(third["phase"], IncidentPhase.ESCALATE_TO_USER.value)

        widened = check_thread_roots(
            self.cfg, descriptor.reservation_token,
            thread={"environments": [{"runtimeWorkspaceRoots": [str(self.root)]}]},
            response=None, workspace=Path(descriptor.cwd),
        )
        self.assertEqual(widened, [str(self.root)])
        (roots,) = [item for item in self.tickets() if item["system_state"]["cause"] == "runtime_roots_widened"]
        self.assertEqual(roots["system_state"]["reason_code"], third["system_state"]["reason_code"])
        self.assertEqual(roots["phase"], IncidentPhase.PIPELINE_ENGINEER.value)


class CanonicalAfterPromotionTests(StagedRun):
    def test_a_canonical_change_no_promotion_explains_goes_to_the_on_call(self) -> None:
        """After promotion, the root is compared with the manifest (isolation_guard).

        Task A was filed at the root. While it worked, task B was promoted
        (src/b.txt) and something else wrote src/leak.txt. Only the leak is
        unexplained: it is recorded on the session and goes to the on-call
        in a ticket that holds nothing. Mutations: the check not called in
        the promotion gate - no record, no ticket; other tasks' promotions
        not counted as explained - src/b.txt is reported too.
        """

        from codex_autopilot.artifact_staging import ArtifactStagingStore
        from codex_autopilot.artifact_staging_lifecycle import CompletionArtifactGate
        from codex_autopilot.run_state import StateStore, utc_now

        descriptor = self.reserve()
        store = ArtifactStagingStore(self.cfg.root, self.cfg.state_dir)
        state_store = StateStore(self.cfg.state_dir)
        state = state_store.load()
        other = store.prepare(run_id=state.run_id, task_id="B", reservation_token="token-b")
        (other.workspace / "src" / "b.txt").write_text("from B\n", encoding="utf-8")
        store.seal("B")
        store.mark_verified("B", verification_id="verify-b")
        store.promote("B", expected_verification_id="verify-b")
        (self.root / "src" / "leak.txt").write_text("written outside any manifest\n", encoding="utf-8")
        workspace = Path(descriptor.cwd)
        (workspace / "src" / "result.txt").write_text("staged A\n", encoding="utf-8")
        store.seal("A")
        session = self.session(descriptor.reservation_token, state)
        session["placement_contract"] = 2
        gate = CompletionArtifactGate(workspace=workspace, store=store, cfg=self.cfg)
        gate.promote("A", "verify-a", state=state, session=session, at=utc_now())
        state_store.save(state)
        self.assertEqual(session["canonical_outside_manifest"]["paths"], ["src/leak.txt"])
        (ticket,) = self.tickets()
        self.assertEqual(ticket["system_state"]["cause"], "canonical_changed_outside_manifest")
        self.assertEqual(ticket["affected_task_ids"], [])
        self.assertEqual((self.root / "src" / "result.txt").read_text(encoding="utf-8"), "staged A\n")


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
    """A permission request of a thread filed at the root is classified by the runtime.

    The request shapes are the App Server's (0.153.4 schema,
    CommandExecutionRequestApprovalParams); the sandbox's retry reason is
    the codex binary's own string (approval_stops.SANDBOX_RETRY_REASON).
    """

    def asking(self, payload, *, earlier=True):
        """Dispatch a contract-2 worker whose turn raises ``payload``; the ticket it files."""

        from codex_autopilot.blocked_runs import stop_run
        from codex_autopilot.lifecycle import DesktopLifecycleError
        from codex_autopilot.run_state import StateStore, utc_now

        self.prove_isolation()
        descriptor = self.reserve()
        if earlier:
            # An earlier request of this run - an on-call's, holding no task.
            store = StateStore(self.cfg.state_dir)
            state = store.load()
            stop_run(self.cfg, state, stop_kind="approval_required", phase="APPROVAL_REQUIRED",
                     reason="earlier", summary="earlier", at=utc_now(), task_ids=(), context_task_id="A")
            store.save(state)
        client = self.launched(descriptor)

        def asks(*_args, **_kwargs):
            raise ApprovalRequired(payload(Path(descriptor.cwd)))

        client.wait_for_turn = asks
        with self.assertRaises(DesktopLifecycleError):
            self.dispatch(client, descriptor)
        self.assertEqual(client.responded, [])
        (ticket,) = [item for item in self.tickets("approval_required") if item["affected_task_ids"] == ["A"]]
        return descriptor, ticket

    def refused_at_root(self, _workspace):
        from codex_autopilot.approval_stops import SANDBOX_RETRY_REASON

        return {"id": 3, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "worker-thread", "turnId": "turn-a", "itemId": "i1",
            "command": "blender -b scene.blend -o renders/frame.png", "cwd": str(self.root),
            "reason": SANDBOX_RETRY_REASON,
        }}

    def test_a_command_the_sandbox_refused_at_the_root_has_its_own_code(self) -> None:
        """The independent check's risk: a thread at the read-only root writes a relative path.

        Never answered; its own class ``root_write``, its own failure code
        (not counted towards the retry ceiling), RECOVERY_EXHAUSTED instead
        of DANGEROUS_PERMISSION, and its own count beside the run's total.
        Mutations: ``approval_class`` always ``task_permission`` (class,
        code and count fail); ``approvals_in_run`` without the class filter
        (the root-write count reads 2); ``approval_root_write`` counted
        towards the ceiling (the attempts appear).
        """

        from codex_autopilot.run_state import StateStore

        descriptor, ticket = self.asking(self.refused_at_root)
        system = ticket["system_state"]
        self.assertEqual(
            (system["approval_class"], system["reason_code"], system["placement_contract"]),
            ("root_write", "RECOVERY_EXHAUSTED", 2),
        )
        self.assertEqual((system["approvals_in_run"], system["root_write_approvals_in_run"]), (2, 1))
        state = StateStore(self.cfg.state_dir).load()
        self.assertNotIn("approval_root_write", state.failure_signature_attempts)
        details = [str(item.get("detail")) for item in state.lifecycle_journal
                   if item.get("reservation_token") == descriptor.reservation_token]
        self.assertTrue(any("(root_write)" in item for item in details), details)

    def test_what_the_request_proves_decides_the_class(self) -> None:
        """Read from the request itself; what it does not prove is a task permission.

        Mutations: network ignored (the curl at the root is root_write);
        explicit write paths not read (the render path under the root is a
        task permission); the workspace not excluded (a write there is
        root_write); the retry reason not required (a question at the root
        is root_write); the contract not required (a contract-1 thread is
        root_write); an entry that is not a mapping read as one (the
        classification raises instead of answering).
        """

        from codex_autopilot.approval_stops import ROOT_WRITE, SANDBOX_RETRY_REASON, TASK_PERMISSION, approval_class

        workspace = self.cfg.state_dir / "staged-artifacts" / "A" / "workspace"
        session = {"placement_contract": 2, "descriptor": {"cwd": str(workspace)}}
        command = "item/commandExecution/requestApproval"

        def ask(method=command, **params):
            return {"id": 1, "method": method, "params": {"threadId": "t", "itemId": "i", **params}}

        def write(path):
            return {"fileSystem": {"entries": [{"access": "write", "path": {"type": "path", "path": str(path)}}]}}

        cases = [
            (ask(cwd=str(self.root), reason=SANDBOX_RETRY_REASON, command="touch out.txt"), ROOT_WRITE),
            (ask(cwd=str(self.root), reason=SANDBOX_RETRY_REASON, command="curl https://example.com",
                 networkApprovalContext={"host": "example.com", "protocol": "https"}), TASK_PERMISSION),
            (ask(cwd=str(self.root), additionalPermissions=write(self.root / "renders")), ROOT_WRITE),
            (ask(cwd=str(self.root), additionalPermissions={"fileSystem": {"write": ["renders/frame.png"]}}), ROOT_WRITE),
            (ask(cwd=str(self.root), additionalPermissions=write(workspace / "renders")), TASK_PERMISSION),
            (ask(cwd=str(self.root), additionalPermissions=write(Path.home() / "Library" / "x")), TASK_PERMISSION),
            (ask(cwd=str(self.root), reason="needs the network", command="npm install"), TASK_PERMISSION),
            (ask(cwd=str(self.root), additionalPermissions={"fileSystem": {"entries": ["not an entry"]}}), TASK_PERMISSION),
            (ask("item/fileChange/requestApproval", grantRoot=str(self.root)), ROOT_WRITE),
            (ask("applyPatchApproval", fileChanges={str(self.root / "src" / "x.py"): {}}), ROOT_WRITE),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload["params"]):
                self.assertEqual(approval_class(self.cfg, session, payload), expected)
        old = {"descriptor": {"cwd": str(workspace)}}
        self.assertEqual(approval_class(self.cfg, old, cases[0][0]), TASK_PERMISSION)

    def test_her_permission_is_refused_for_a_write_the_runtime_caused(self) -> None:
        """Like a request the run's authorization covers: never sent to her as a permission.

        Mutation: the ``approval_class`` refusal removed from
        ``read_engineer_outcome`` - the escalation goes through.
        """

        from codex_autopilot.engineer_escalation import read_engineer_outcome

        _, ticket = self.asking(self.refused_at_root, earlier=False)
        escalation = (
            'AUTOPILOT_ESCALATION: {"diagnosis":"it wants to write the root","decision_needed":"allow?",'
            '"recommendation":"allow","options":[],"scope":"task"}\n'
        )
        status, _code, refused = read_engineer_outcome(
            self.cfg, ticket["incident_id"], escalation + "PIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER DANGEROUS_PERMISSION"
        )
        self.assertEqual(status, "PROTOCOL_ERROR")
        self.assertIn("root_write", refused)
        status, code, refused = read_engineer_outcome(
            self.cfg, ticket["incident_id"], escalation + "PIPELINE_ENGINEER_STATUS: ESCALATE_TO_USER RECOVERY_EXHAUSTED"
        )
        self.assertEqual((status, code, refused), ("ESCALATE_TO_USER", "RECOVERY_EXHAUSTED", ""))

    def test_a_task_permission_still_goes_up_as_hers(self) -> None:
        """A network wish from the same thread keeps DANGEROUS_PERMISSION and adds nothing to the root count.

        Mutation: ``reason_code`` RECOVERY_EXHAUSTED for every request.
        """

        _, ticket = self.asking(lambda _w: {"id": 4, "method": "item/commandExecution/requestApproval", "params": {
            "threadId": "worker-thread", "itemId": "i2", "command": "curl https://example.com", "cwd": str(self.root),
            "networkApprovalContext": {"host": "example.com", "protocol": "https"},
        }})
        system = ticket["system_state"]
        self.assertEqual((system["approval_class"], system["reason_code"]), ("task_permission", "DANGEROUS_PERMISSION"))
        self.assertEqual((system["approvals_in_run"], system["root_write_approvals_in_run"]), (2, 0))


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
