"""The saved Codex project's roots: audited, recorded, and changed only on her word (R6).

Measured on the beyondness run: the Codex project had two roots - an old
iCloud copy of the game at position 0 and the run's root at position 1 -
and the initiating agent had created a second App Server project on the
run's root. Preflight printed "rootPaths OK" and said nothing; every later
start without --app-server-project-id failed on "multiple saved Codex
Projects match this path"; her own chats opened in the broken copy.

Every test below builds Desktop's state in a temporary Codex home - never
the developer's own ~/.codex - and drives the production entry points:
``audit_project_roots``, ``match_saved_project``, ``run_preflight`` ->
``initialize_project``, ``status_text``, ``create_desktop_thread_via_app_server``,
``run_wake``, ``AppServerClient`` and the CLI's ``main``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import unittest
from unittest import mock

from _gates import patch_hook_trust_gates
from _plan_contract import initialize_verified_project as initialize_project
from codex_autopilot.appserver import AppServerClient, ProjectMutationRefused
from codex_autopilot.config import load_config
from codex_autopilot.memory import ProjectMemory
from codex_autopilot.project_association import (
    match_saved_project,
    project_root_authorization_statement,
    resolve_preflight_project,
)
from codex_autopilot import project_roots_audit as audit_module
from codex_autopilot.project_roots_audit import (
    ACTIVE_ROOT_MISMATCH,
    DUPLICATE_APP_SERVER_PROJECTS,
    ID_PAIR_MISMATCH,
    ROOTS_DIVERGED,
    SIBLING_ROOTS,
    UNVERIFIED,
    audit_project_roots,
    copy_signals,
    roots_decisions,
    sync_roots_decisions,
)
from codex_autopilot.project_roots_change import (
    DELETE_PROJECT,
    REMOVE_ROOT,
    change_authorized,
    change_project_roots,
    change_statement,
    retire_duplicate_project,
    terminal_confirmation,
)
from codex_autopilot.run_state import StateStore
from test_desktop_lifecycle import graph
import test_wake

DESKTOP = "local-desktop-project"
RUN_PROJECT = "01a049a3-run-project"
DUPLICATE = "01a0ce52-agent-duplicate"
MAPPING = "app-server-project-id-by-legacy-project-id-by-host"


def codex_home(
    roots,
    *,
    linked: str | None = RUN_PROJECT,
    active=None,
    selected=None,
    mapping: bool = True,
    extra_projects=None,
) -> Path:
    home = Path(tempfile.mkdtemp(prefix="codex-autopilot-roots-home-")).resolve()
    state: dict = {
        "local-projects": {
            DESKTOP: {"id": DESKTOP, "rootPaths": [str(item) for item in roots]},
            **(extra_projects or {}),
        },
    }
    if mapping:
        state[MAPPING] = {f"local:{home}": ({DESKTOP: linked} if linked else {})}
    if active is not None:
        state["active-workspace-roots"] = [str(item) for item in active]
    if selected is not None:
        state["selected-project"] = selected
    (home / ".codex-global-state.json").write_text(json.dumps(state), encoding="utf-8")
    return home


def game_tree(parent: Path, name: str, *, remote: str | None, markers=True) -> Path:
    root = parent / name
    root.mkdir(parents=True)
    if markers:
        (root / "Game.uproject").write_text("{}", encoding="utf-8")
        (root / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
        (root / ".git").mkdir()
        config = "[core]\n\tbare = false\n"
        if remote:
            config += f'[remote "origin"]\n\turl = {remote}\n'
        (root / ".git" / "config").write_text(config, encoding="utf-8")
    return root


def app_project(project_id: str, *roots: Path, name: str = "") -> dict:
    return {"id": project_id, "name": name or project_id, "roots": [{"path": str(item)} for item in roots]}


class Machine:
    """Two game trees side by side, as on her machine: a copy and the run's root."""

    def __init__(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="codex-autopilot-roots-")).resolve()
        # The copy has another folder name on purpose: then it is recognized
        # by its markers and its remote alone - the name cannot carry it.
        self.copy = game_tree(self.parent / "Documents", "Game-old", remote="git@github.com:owner/game.git")
        self.target = game_tree(self.parent / "Developer", "game", remote="https://gitlab.com/owner/game.git")
        self.assets = self.parent / "Assets"
        self.assets.mkdir()
        (self.assets / "texture.png").write_bytes(b"png")


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = Machine()

    def test_a_copy_among_the_roots_is_found_by_its_signs(self) -> None:
        """[copy, target, assets]: the copy is a finding, the assets folder is not.

        Mutation 1: ``same_markers`` always False - the copy (another name)
        has only its remote left, one sign, and is not found. Mutation 2:
        drop ``if not signals["is_copy"]: continue`` - any second root is a
        finding and the assets folder appears too.
        """

        home = codex_home([self.m.copy, self.m.target, self.m.assets])
        audit = audit_project_roots(home, self.m.target, DESKTOP, None, RUN_PROJECT)
        siblings = [item for item in audit.findings if item.code == SIBLING_ROOTS]
        self.assertEqual([item.path for item in siblings], [str(self.m.copy)])
        self.assertEqual(siblings[0].status, "WARN")
        self.assertTrue(siblings[0].evidence["same_markers"])
        self.assertIn("Edit project", siblings[0].command)

    def test_a_copy_at_position_zero_is_where_new_chats_open(self) -> None:
        """The target is a root, but not the first: ACTIVE_ROOT_MISMATCH names both.

        Mutation: judge by membership in rootPaths instead of position 0 -
        the target IS a member, and nothing is found.
        """

        home = codex_home([self.m.copy, self.m.target])
        audit = audit_project_roots(home, self.m.target, DESKTOP, None, RUN_PROJECT)
        (active,) = [item for item in audit.findings if item.code == ACTIVE_ROOT_MISMATCH]
        self.assertIn(f"open in {self.m.copy}", active.detail)
        self.assertIn(f"the run lives in {self.m.target}", active.detail)

    def test_desktops_global_active_roots_count_only_for_this_project(self) -> None:
        """active-workspace-roots is Desktop's global UI state, not the project's.

        Open in another project, it must not raise a finding; open in this
        one and not naming the target, it does. Mutation: ignore
        ``selected-project`` - the first half fails.
        """

        elsewhere = self.m.parent / "Elsewhere"
        home = codex_home([self.m.target, self.m.assets], active=[elsewhere], selected="another-project")
        audit = audit_project_roots(home, self.m.target, DESKTOP, None, RUN_PROJECT)
        self.assertNotIn(ACTIVE_ROOT_MISMATCH, [item.code for item in audit.findings])
        home = codex_home([self.m.target, self.m.assets], active=[elsewhere], selected=DESKTOP)
        audit = audit_project_roots(home, self.m.target, DESKTOP, None, RUN_PROJECT)
        (active,) = [item for item in audit.findings if item.code == ACTIVE_ROOT_MISMATCH]
        self.assertIn(str(elsewhere), active.detail)

    def test_missing_desktop_keys_are_a_warn_could_not_check_never_a_fail(self) -> None:
        """Desktop's format is undocumented: a missing key is WARN "could not check".

        Mutation: drop the UNVERIFIED record for the id pair - the missing
        map passes silently and this test fails.
        """

        home = codex_home([self.m.target], mapping=False)
        audit = audit_project_roots(home, self.m.target, "not-in-desktop", [app_project(RUN_PROJECT, self.m.target)], RUN_PROJECT)
        unverified = [item for item in audit.findings if item.code == UNVERIFIED]
        self.assertEqual(len(unverified), 2, [item.detail for item in unverified])
        self.assertTrue(all(item.status == "WARN" and item.detail.startswith("Could not check") for item in unverified))
        self.assertFalse([item for item in audit.findings if item.status == "FAIL"])

    def test_an_unanswering_git_is_bounded_and_marks_the_root_unavailable(self) -> None:
        """A .git pointer makes git itself run; an evicted store made it hang.

        Mutation: drop ``timeout=`` from the git call - the fake git sleeps
        10 s and the audit takes that long.
        """

        (self.m.copy / ".git" / "config").unlink()
        (self.m.copy / ".git").rmdir()
        (self.m.copy / ".git").write_text("gitdir: /nowhere\n", encoding="utf-8")
        bin_dir = self.m.parent / "bin"
        bin_dir.mkdir()
        fake_git = bin_dir / "git"
        fake_git.write_text("#!/bin/sh\nsleep 10\n", encoding="utf-8")
        fake_git.chmod(fake_git.stat().st_mode | stat.S_IEXEC)
        started = time.monotonic()
        with mock.patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}):
            signals = copy_signals(self.m.copy, self.m.target, timeout=0.5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(any("did not answer" in item for item in signals["unavailable"]), signals)

    def test_an_evicted_git_config_is_not_opened(self) -> None:
        """SF_DATALESS on .git/config: reported, and the file is never read.

        Mutation: drop the dataless check in ``_remote_names`` - the config
        is read (the guard below raises) and the reason is not reported.
        """

        config = self.m.copy / ".git" / "config"
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if Path(path) == config:
                return mock.Mock(st_flags=audit_module.SF_DATALESS, st_mode=result.st_mode)
            return result

        real_read = Path.read_text

        def read_text(self, *args, **kwargs):
            if self == config:
                raise AssertionError("an evicted file was opened")
            return real_read(self, *args, **kwargs)

        with mock.patch.object(audit_module.os, "lstat", lstat), mock.patch.object(Path, "read_text", read_text):
            signals = copy_signals(self.m.copy, self.m.target)
        self.assertTrue(any("dataless" in item for item in signals["unavailable"]), signals)


class IdPairAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = Machine()

    def test_id_pair_mismatch_fails_only_for_a_target_in_neither_space(self) -> None:
        """The audit's one FAIL: no project of the pair holds the root, and Desktop does not list it.

        A creation-time audit after the roots moved: Desktop links a ghost
        project on the assets folder, the run's configured project holds
        only the copy. Desktop lists the target - WARN, the target is in
        one space; Desktop does not - FAIL, still a record with its fix and
        never a stop (preflight prints it, creation records it). Mutations:
        drop ``in_desktop`` from the WARN condition - the first case turns
        FAIL, fails; make the last return WARN - the second case, fails.
        """

        projects = [app_project("ghost-project", self.m.assets), app_project(RUN_PROJECT, self.m.copy)]
        listed = codex_home([self.m.target], linked="ghost-project")
        audit = audit_project_roots(listed, self.m.target, DESKTOP, projects, RUN_PROJECT)
        (pair,) = [item for item in audit.findings if item.code == ID_PAIR_MISMATCH]
        self.assertEqual(pair.status, "WARN")
        self.assertIn("Edit project", pair.command)
        absent = codex_home([self.m.assets], linked="ghost-project")
        audit = audit_project_roots(absent, self.m.target, DESKTOP, projects, RUN_PROJECT)
        (pair,) = [item for item in audit.findings if item.code == ID_PAIR_MISMATCH]
        self.assertEqual(pair.status, "FAIL")
        self.assertIn("in neither space", pair.detail)
        self.assertIn(f"add {self.m.target}", pair.command)


class MatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = Path(tempfile.mkdtemp(prefix="codex-autopilot-roots-match-")).resolve()

    def test_the_project_desktop_links_wins_a_tie_and_the_other_is_written(self) -> None:
        """Two App Server projects on one root: Desktop's link decides, not a stop.

        Mutation: restore the raise on ``len(best) != 1`` before the link is
        consulted - ProjectAssociationError, this test fails.
        """

        projects = [app_project(DUPLICATE, self.target), app_project(RUN_PROJECT, self.target)]
        findings: list = []
        chosen = match_saved_project(self.target, projects, desktop_linked_project_id=DUPLICATE, findings=findings)
        self.assertEqual(chosen["id"], DUPLICATE)
        self.assertEqual([(item.code, item.project_id) for item in findings], [(DUPLICATE_APP_SERVER_PROJECTS, RUN_PROJECT)])

    def test_a_tie_without_a_link_is_broken_deterministically_not_a_stop(self) -> None:
        """No link: the project Desktop shows, else the lowest (oldest UUIDv7) id.

        This case used to raise "multiple saved Codex Projects match this
        path". Mutations: raise again - fails; take the first candidate in
        list order - the duplicate is listed first and is chosen, fails.
        """

        projects = [app_project(DUPLICATE, self.target), app_project(RUN_PROJECT, self.target)]
        findings: list = []
        chosen = match_saved_project(self.target, projects, findings=findings)
        self.assertEqual(chosen["id"], RUN_PROJECT)
        self.assertEqual([item.project_id for item in findings], [DUPLICATE])
        findings = []
        chosen = match_saved_project(
            self.target, projects, desktop_visible_project_ids={DUPLICATE}, findings=findings
        )
        self.assertEqual(chosen["id"], DUPLICATE)
        self.assertEqual([item.project_id for item in findings], [RUN_PROJECT])
        self.assertIn("App Server only", findings[0].detail)

    def test_an_explicit_id_other_than_the_linked_one_is_a_warn_and_the_linked_is_used(self) -> None:
        """--app-server-project-id names another holder of the root: Desktop's pair wins.

        Mutation: return the explicit project regardless - fails.
        """

        projects = [app_project(DUPLICATE, self.target), app_project(RUN_PROJECT, self.target)]
        findings: list = []
        chosen = match_saved_project(
            self.target, projects, explicit_project_id=DUPLICATE, desktop_linked_project_id=RUN_PROJECT, findings=findings
        )
        self.assertEqual(chosen["id"], RUN_PROJECT)
        self.assertEqual([(item.code, item.status) for item in findings], [(ID_PAIR_MISMATCH, "WARN")])


    def test_a_link_without_the_root_takes_the_holder_explicit_or_not(self) -> None:
        """The link lacks the root: the holder is used, never the linked ghost, never a stop.

        Mutations: in the explicit branch return the linked project whenever
        the pair is off - the ghost is chosen, fails; drop ``mismatch(chosen)``
        in the automatic branch - nothing is written, fails.
        """

        other = self.target.parent / "elsewhere"
        projects = [app_project("ghost-project", other), app_project(RUN_PROJECT, self.target)]
        for explicit in (RUN_PROJECT, None):
            findings: list = []
            chosen = match_saved_project(
                self.target, projects, explicit_project_id=explicit,
                desktop_linked_project_id="ghost-project", findings=findings,
            )
            self.assertEqual(chosen["id"], RUN_PROJECT, explicit)
            self.assertEqual([(item.code, item.status) for item in findings], [(ID_PAIR_MISMATCH, "WARN")], explicit)
            self.assertIn("Edit project", findings[0].command)

    def test_an_explicit_id_without_the_root_yields_to_the_linked_holder(self) -> None:
        """--app-server-project-id names a project without the root; Desktop links the holder.

        The verdict: when Desktop's map links a holder, it is taken and the
        rest is a WARN - an explicit flag included - written as the pair's
        finding, naming the linked project and its id as the fix. Mutation:
        drop the "Desktop's pair wins" return in the explicit branch - the
        holder is still reached, but only as the flag's own finding (on the
        flag's id), fails.
        """

        other = self.target.parent / "elsewhere"
        projects = [app_project(DUPLICATE, other), app_project(RUN_PROJECT, self.target)]
        findings: list = []
        chosen = match_saved_project(
            self.target, projects, explicit_project_id=DUPLICATE,
            desktop_linked_project_id=RUN_PROJECT, findings=findings,
        )
        self.assertEqual(chosen["id"], RUN_PROJECT)
        self.assertEqual([(item.code, item.status, item.project_id) for item in findings],
                         [(ID_PAIR_MISMATCH, "WARN", RUN_PROJECT)])

    def test_an_explicit_id_without_the_root_and_no_link_takes_the_holder(self) -> None:
        """The flag names a project without the root, nothing links: the holder, with a WARN.

        This refused before ("does not contain the target root") although
        RUN_PROJECT held the root - a stop on a target that is there. With
        no holder at all it refused too; now the run goes on as without the
        flag (no App Server project) and the flag is written with the
        Desktop fix - preflight reaches this only after finding the root in
        Desktop's rootPaths, so the target is in one space, not in neither.
        Mutations: raise whenever the explicit project lacks the root -
        ProjectAssociationError, fails; raise again when no project holds
        the root - fails on the last case.
        """

        projects = [app_project(DUPLICATE, self.target.parent / "elsewhere"), app_project(RUN_PROJECT, self.target)]
        for requested in (DUPLICATE, "absent-project"):
            findings: list = []
            chosen = match_saved_project(self.target, projects, explicit_project_id=requested, findings=findings)
            self.assertEqual(chosen["id"], RUN_PROJECT, requested)
            self.assertEqual([(item.code, item.status, item.project_id) for item in findings],
                             [(ID_PAIR_MISMATCH, "WARN", requested)])
            self.assertEqual(findings[0].command, f"--app-server-project-id {RUN_PROJECT}")
        # The flag's project was not the one used: its source is not "explicit".
        self.assertEqual(resolve_preflight_project(self.target, projects, explicit_project_id=DUPLICATE)[1], "target")
        findings = []
        self.assertIsNone(match_saved_project(self.target, projects[:1], explicit_project_id=DUPLICATE, findings=findings))
        self.assertEqual([(item.code, item.status) for item in findings], [(ID_PAIR_MISMATCH, "WARN")])
        self.assertIn("Edit project", findings[0].command)


class _Initialized(unittest.TestCase):
    language = "en"

    def setUp(self) -> None:
        patch_hook_trust_gates(self)
        self.m = Machine()
        self.root = self.m.target
        skill = self.m.parent / "SKILL.md"
        skill.write_text("# test skill\n", encoding="utf-8")
        plan_file = self.m.parent / "input-plan.json"
        plan_file.write_text(json.dumps(graph()), encoding="utf-8")
        self.plan_file = plan_file
        self.skill = skill
        initialize_project(
            self.root, plan_file, profile="adaptive", skill_path=skill,
            project_id=RUN_PROJECT, desktop_project_id=DESKTOP, language=self.language,
        )
        self.cfg = load_config(self.root)
        self.store = StateStore(self.cfg.state_dir)
        self.memory = ProjectMemory(self.root)


class PreflightRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        from test_preflight import PreflightClient

        PreflightClient.instances.clear()
        PreflightClient.probe_instances.clear()
        self.m = Machine()

    def _client(self, home: Path, projects: list[dict]):
        from test_preflight import PreflightClient

        class Client(PreflightClient):
            def connect(self):
                return {"userAgent": "fake-app-server", "codexHome": str(home)}

            def list_projects(self):
                return [dict(item) for item in projects]

        return Client

    def _preflight(self, home: Path, projects: list[dict], **kwargs):
        from codex_autopilot.preflight import run_preflight
        from test_preflight import SKILL, plan

        return run_preflight(
            self.m.target, plan=plan(), profile="adaptive", skill_path=SKILL, binary="/bin/echo",
            client_factory=self._client(home, projects), desktop_project_id=DESKTOP, emit=None, **kwargs,
        )

    def test_a_link_to_a_project_without_the_root_takes_the_holder_and_goes_on(self) -> None:
        """Desktop links its project to an App Server project that lacks the root.

        This test used to pin a FAIL here (preflight refused). The
        independent check: Desktop's rootPaths hold the target (preflight
        has just checked them) and RUN_PROJECT holds it in App Server - the
        target is in both spaces, only the pair is off, and the verdict
        allows a FAIL only for a target in neither. Now the holder is taken
        and ID_PAIR_MISMATCH is a WARN whose fix is one save in Desktop;
        ROOTS_DIVERGED says the linked project's roots differ.

        Mutations: raise ProjectAssociationError again when the link lacks
        the root (match_saved_project) - preflight refuses, this test fails;
        make id_pair_finding FAIL whenever the linked project lacks the root
        - the printed line is FAIL, this test fails.
        """

        home = codex_home([self.m.target], linked="ghost-project")
        projects = [app_project("ghost-project", self.m.assets), app_project(RUN_PROJECT, self.m.target)]
        result = self._preflight(home, projects)
        self.assertEqual(result.project_id, RUN_PROJECT)
        roots = {name: (status, detail) for name, status, detail in result.checks if name.startswith("Project roots")}
        status, detail = roots[f"Project roots {ID_PAIR_MISMATCH}"]
        self.assertEqual(status, "WARN")
        self.assertIn(f"{RUN_PROJECT} holds it, and the run uses {RUN_PROJECT}", detail)
        self.assertIn("Edit project", detail)
        self.assertEqual(roots[f"Project roots {ROOTS_DIVERGED}"][0], "WARN")
        self.assertNotIn("FAIL", [status for status, _detail in roots.values()])
        recorded = [item["code"] for item in result.roots_audit["findings"]]
        self.assertIn(ID_PAIR_MISMATCH, recorded)

    def test_a_flag_naming_a_project_without_the_root_is_written_and_the_run_goes_on(self) -> None:
        """--app-server-project-id names a project without the root, and no App Server project holds it.

        Preflight refused here ("does not contain the target root") though
        Desktop's rootPaths - checked a moment before - hold the target.
        Now the run goes on as it does without the flag: no App Server
        project, Desktop files the thread by its cwd; the flag is a WARN with
        the Desktop fix, kept for bootstrap to record. Mutation: raise
        ProjectAssociationError again when no project holds the root -
        preflight refuses, this test fails.
        """

        home = codex_home([self.m.target], linked=DUPLICATE)
        result = self._preflight(home, [app_project(DUPLICATE, self.m.assets)], app_server_project_id=DUPLICATE)
        self.assertIsNone(result.project_id)
        checks = {name: status for name, status, _detail in result.checks}
        self.assertEqual(checks["Codex project metadata"], "NONE")
        self.assertEqual(checks[f"Project roots {ID_PAIR_MISMATCH}"], "WARN")
        self.assertNotIn("FAIL", checks.values())
        (pair,) = [item for item in result.roots_audit["findings"] if item["code"] == ID_PAIR_MISMATCH]
        self.assertEqual(pair["project_id"], DUPLICATE)

    def test_a_copy_root_is_reported_recorded_and_proposed_without_stopping(self) -> None:
        """Preflight goes on; bootstrap records the audit and her proposed decision.

        Mutation: preflight does not keep the audit in its result - the run
        state has no roots_audit and memory has no proposal.
        """

        (self.m.target / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        home = codex_home([self.m.copy, self.m.target])
        result = self._preflight(home, [app_project(RUN_PROJECT, self.m.copy, self.m.target)])
        codes = [name for name, _status, _detail in result.checks if name.startswith("Project roots")]
        self.assertIn(f"Project roots {SIBLING_ROOTS}", codes)
        patch_hook_trust_gates(self)
        plan_file = self.m.parent / "plan.json"
        plan_file.write_text(json.dumps(graph()), encoding="utf-8")
        skill = self.m.parent / "SKILL.md"
        skill.write_text("# skill\n", encoding="utf-8")
        initialize_project(
            self.m.target, plan_file, profile="adaptive", skill_path=skill,
            project_id=result.project_id, desktop_project_id=DESKTOP, roots_audit=result.roots_audit,
        )
        state = StateStore(self.m.target / ".codex-autopilot").load()
        self.assertEqual(state.roots_audit["occasion"], "preflight")
        self.assertIn(SIBLING_ROOTS, [item["code"] for item in state.roots_audit["findings"]])
        self.assertIn("roots_audit", [item["event"] for item in state.resilience_journal])
        proposals = roots_decisions(ProjectMemory(self.m.target))
        sibling = [item for item in proposals if f" {SIBLING_ROOTS} " in item["statement"].split("\n")[0]]
        self.assertEqual(len(sibling), 1)
        self.assertEqual((sibling[0]["origin"], sibling[0]["status"]), ("environment", "proposed"))
        self.assertIn("Recommendation: remove the old copy", sibling[0]["statement"])
        self.assertIn("To do it:", sibling[0]["statement"])


class DecisionRecordTests(_Initialized):
    def test_one_proposal_per_finding_closed_by_the_runtime_when_it_is_gone(self) -> None:
        """Audits repeat at every creation: a proposal is not repeated with them.

        Mutations: drop the "already proposed" reuse - two proposals; drop
        the closing loop - the fixed finding stays proposed forever.
        """

        home = codex_home([self.m.copy, self.root])
        audit = audit_project_roots(home, self.root, DESKTOP, None, RUN_PROJECT)
        first = sync_roots_decisions(self.memory, audit)
        again = sync_roots_decisions(self.memory, audit_project_roots(home, self.root, DESKTOP, None, RUN_PROJECT))
        self.assertEqual(first, again)
        self.assertEqual(len([item for item in roots_decisions(self.memory) if item["status"] == "proposed"]), 2)
        # She removed the copy in Desktop: the next audit closes both.
        fixed = codex_home([self.root])
        sync_roots_decisions(self.memory, audit_project_roots(fixed, self.root, DESKTOP, None, RUN_PROJECT))
        statuses = sorted(item["status"] for item in roots_decisions(self.memory))
        self.assertEqual(statuses, ["superseded", "superseded"])

    def test_a_rejected_proposal_is_not_proposed_again(self) -> None:
        home = codex_home([self.m.copy, self.root])
        opened = sync_roots_decisions(self.memory, audit_project_roots(home, self.root, DESKTOP, None, RUN_PROJECT))
        for decision_id in opened.values():
            self.memory.set_decision_status(decision_id, "rejected", actor="user", reason="no")
        self.assertEqual(sync_roots_decisions(self.memory, audit_project_roots(home, self.root, DESKTOP, None, RUN_PROJECT)), {})


class StatusLineTests(_Initialized):
    language = "ru"

    def test_the_card_carries_one_roots_line_while_the_decision_is_open(self) -> None:
        """Shown while proposed; gone once accepted or superseded.

        Mutation: ignore the decision's status - the line stays after she
        accepted it, and this test fails.
        """

        from codex_autopilot.control import status_text

        home = codex_home([self.m.copy, self.root])
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            audit = audit_project_roots(home, self.root, DESKTOP, None, RUN_PROJECT)
            opened = sync_roots_decisions(self.memory, audit)
            card = status_text(self.root, detailed=False)
            (line,) = [item for item in card.splitlines() if item.startswith("Проект Codex")]
            self.assertIn("2 корня", line)
            self.assertIn(f"активный — копия {self.m.copy}", line)
            for decision_id in opened.values():
                self.assertIn(decision_id, line)
            first, second = opened.values()
            self.memory.set_decision_status(first, "accepted", actor="user", reason="yes")
            self.memory.set_decision_status(second, "superseded", actor="runtime", reason="gone")
            card = status_text(self.root, detailed=False)
            self.assertNotIn("Проект Codex", card)


class ProjectsServer(AppServerClient):
    """App Server's project methods over an in-memory store; every request is kept."""

    def __init__(self, projects: list[dict], home: Path) -> None:
        super().__init__("codex", Path(tempfile.mktemp()))
        self.projects = {item["id"]: json.loads(json.dumps(item)) for item in projects}
        self.calls: list[tuple[str, dict]] = []
        self.codex_home = str(home)

    def request(self, method, params, timeout=60):
        self.calls.append((method, params))
        if method == "project/list":
            return {"data": [json.loads(json.dumps(item)) for item in self.projects.values()]}
        if method == "project/read":
            return {"project": json.loads(json.dumps(self.projects[params["projectId"]]))}
        if method == "project/update":
            self.projects[params["projectId"]]["roots"] = list(params["roots"])
            return {"project": json.loads(json.dumps(self.projects[params["projectId"]]))}
        if method == "project/delete":
            self.projects.pop(params["projectId"])
            return {}
        raise AssertionError(method)

    def __enter__(self):  # no process: the store above is the server
        return self

    def __exit__(self, *_args):
        return None

    def mutations(self) -> list[str]:
        return [method for method, _ in self.calls if method in {"project/update", "project/delete"}]


class MutationTests(_Initialized):
    def test_replace_project_roots_sends_nothing_without_her_decision(self) -> None:
        """Mutation: ``authorized=True`` by default - project/update is sent."""

        server = ProjectsServer([app_project(RUN_PROJECT, self.m.copy, self.root)], codex_home([self.root]))
        with self.assertRaises(ProjectMutationRefused):
            server.replace_project_roots(RUN_PROJECT, [self.root], keep=self.root)
        with self.assertRaises(ProjectMutationRefused):
            server.replace_project_roots(RUN_PROJECT, [self.m.copy], keep=self.root, authorized=True)
        self.assertEqual(server.mutations(), [])

    def test_a_decision_for_another_project_or_path_does_not_authorize(self) -> None:
        """Her permission names one action, one project, one path.

        Mutation: match the decision by its action prefix only - the other
        path and the other project authorize, this test fails.
        """

        self.memory.propose_decision(
            statement=change_statement(REMOVE_ROOT, RUN_PROJECT, self.m.copy),
            origin="user", created_by="user", status="accepted",
        )
        self.assertTrue(change_authorized(self.memory, REMOVE_ROOT, RUN_PROJECT, self.m.copy))
        self.assertFalse(change_authorized(self.memory, REMOVE_ROOT, RUN_PROJECT, self.m.assets))
        self.assertFalse(change_authorized(self.memory, REMOVE_ROOT, DUPLICATE, self.m.copy))
        self.assertFalse(change_authorized(self.memory, DELETE_PROJECT, RUN_PROJECT, self.m.copy))

    def test_app_server_roots_only_follow_desktop_and_are_checked_again(self) -> None:
        """Desktop never reads App Server's roots back: App Server is not changed ahead of it.

        Desktop still lists the copy: refused, nothing sent, the Desktop
        instruction given. Desktop already dropped it (App Server lags):
        her typed confirmation, project/update with Desktop's list, the
        ROOTS_DIVERGED proposal closed, the audit re-run. Mutation: drop the
        Desktop comparison - the first call sends project/update.
        """

        server = ProjectsServer([app_project(RUN_PROJECT, self.m.copy, self.root)], codex_home([self.root]))
        leads = codex_home([self.m.copy, self.root])
        outcome = change_project_roots(
            self.cfg, server, self.memory, action=REMOVE_ROOT, root=self.m.copy, codex_home=leads, confirm=lambda _s: True
        )
        self.assertFalse(outcome.done)
        self.assertIn("Make the change in Codex Desktop first", outcome.message)
        self.assertEqual(server.mutations(), [])
        follows = codex_home([self.root])
        sync_roots_decisions(self.memory, audit_project_roots(follows, self.root, DESKTOP, server.list_projects(), RUN_PROJECT))
        asked: list[str] = []
        outcome = change_project_roots(
            self.cfg, server, self.memory, action=REMOVE_ROOT, root=self.m.copy, codex_home=follows,
            confirm=lambda statement: asked.append(statement) or True,
        )
        self.assertTrue(outcome.done, outcome.message)
        self.assertEqual(asked, [change_statement(REMOVE_ROOT, RUN_PROJECT, self.m.copy)])
        self.assertEqual(server.projects[RUN_PROJECT]["roots"], [{"path": str(self.root)}])
        statuses = {item["status"] for item in roots_decisions(self.memory)}
        self.assertEqual(statuses, {"superseded"})
        self.assertEqual(self.store.load().roots_audit["occasion"], "owner decision")

    def _threads_db(self, home: Path, count: int) -> None:
        with contextlib.closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id TEXT, project_id TEXT)")
            db.executemany("INSERT INTO threads VALUES (?, ?)", [(f"t{n}", DUPLICATE) for n in range(count)])

    def test_the_duplicate_is_deleted_only_when_invisible_empty_and_confirmed(self) -> None:
        """project/delete: App Server only, holds the run's root, no threads, her word.

        Mutations: drop the thread count - a project with a thread is
        deleted; drop the Desktop visibility check - a project Desktop shows
        is deleted.
        """

        projects = [app_project(RUN_PROJECT, self.root), app_project(DUPLICATE, self.root, name="Game - Developer")]
        # Desktop shows it: hers to delete in Desktop.
        shown = codex_home([self.root], extra_projects={"other": {"id": "other", "rootPaths": [str(self.root)]}})
        state = json.loads((shown / ".codex-global-state.json").read_text())
        state[MAPPING][f"local:{shown}"]["other"] = DUPLICATE
        (shown / ".codex-global-state.json").write_text(json.dumps(state))
        self._threads_db(shown, 0)
        server = ProjectsServer(projects, shown)
        outcome = retire_duplicate_project(self.cfg, server, self.memory, project_id=DUPLICATE, codex_home=shown, confirm=lambda _s: True)
        self.assertFalse(outcome.done)
        self.assertIn("Desktop shows", outcome.message)
        # Threads are filed in it.
        busy = codex_home([self.root])
        self._threads_db(busy, 1)
        outcome = retire_duplicate_project(self.cfg, server, self.memory, project_id=DUPLICATE, codex_home=busy, confirm=lambda _s: True)
        self.assertIn("1 thread(s)", outcome.message)
        # Not confirmed.
        empty = codex_home([self.root])
        self._threads_db(empty, 0)
        outcome = retire_duplicate_project(self.cfg, server, self.memory, project_id=DUPLICATE, codex_home=empty, confirm=lambda _s: False)
        self.assertFalse(outcome.changed)
        self.assertEqual(server.mutations(), [])
        # The proposal first, then her word: deleted, snapshot kept, proposal closed.
        sync_roots_decisions(self.memory, audit_project_roots(empty, self.root, DESKTOP, server.list_projects(), RUN_PROJECT))
        (proposal,) = [item for item in roots_decisions(self.memory) if item["status"] == "proposed"]
        self.assertIn(change_statement(DELETE_PROJECT, DUPLICATE, self.root), proposal["statement"])
        outcome = retire_duplicate_project(self.cfg, server, self.memory, project_id=DUPLICATE, codex_home=empty, confirm=lambda _s: True)
        self.assertTrue(outcome.done, outcome.message)
        self.assertEqual(server.mutations(), ["project/delete"])
        self.assertNotIn(DUPLICATE, server.projects)
        self.assertTrue(Path(outcome.snapshot).is_file())
        self.assertEqual(self.memory.get_record(proposal["id"])["status"], "superseded")
        self.assertIn("codex_project_deleted", [item["event"] for item in self.store.load().resilience_journal])

    def test_the_confirmation_is_typed_at_her_terminal_never_inside_a_task(self) -> None:
        """Mutation: drop the CODEX_THREAD_ID refusal - an agent's TTY confirms."""

        tty = mock.Mock(isatty=lambda: True)
        out = io.StringIO()
        out.isatty = lambda: True
        inside = terminal_confirmation("p1", environ={"CODEX_THREAD_ID": "t"}, stdin=tty, stdout=out, ask=lambda _q: "p1")
        self.assertFalse(inside("statement"))
        piped = terminal_confirmation("p1", environ={}, stdin=mock.Mock(isatty=lambda: False), stdout=out, ask=lambda _q: "p1")
        self.assertFalse(piped("statement"))
        hers = terminal_confirmation("p1", environ={}, stdin=tty, stdout=out, ask=lambda _q: "p1")
        self.assertTrue(hers("statement"))
        wrong = terminal_confirmation("p1", environ={}, stdin=tty, stdout=out, ask=lambda _q: "yes")
        self.assertFalse(wrong("statement"))


class OwnerCommandTests(_Initialized):
    def test_the_cli_retires_a_duplicate_only_on_a_confirmation_typed_by_her(self) -> None:
        """``authorize-project-root --retire-duplicate`` from inside a Codex task: refused.

        The production command, end to end, with App Server replaced by the
        in-memory store. Mutation: drop the routing to the owner's decision
        in cli.main - the old add-root flow answers instead, and nothing
        says "Not confirmed".
        """

        from codex_autopilot import cli

        home = codex_home([self.root])
        MutationTests._threads_db(self, home, 0)
        server = ProjectsServer([app_project(RUN_PROJECT, self.root), app_project(DUPLICATE, self.root)], home)
        out = io.StringIO()
        with mock.patch.object(cli, "AppServerClient", lambda *_args, **_kwargs: server), \
                mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "an-agent-task"}), contextlib.redirect_stdout(out):
            code = cli.main(["authorize-project-root", "--project", str(self.root), "--retire-duplicate", DUPLICATE])
        self.assertEqual(code, 3)
        self.assertIn("runs inside a Codex task", out.getvalue())
        self.assertIn("Not confirmed", out.getvalue())
        self.assertEqual(server.mutations(), [])
        self.assertIn(DUPLICATE, server.projects)

    def test_a_permission_an_agent_wrote_itself_opens_nothing(self) -> None:
        """An accepted "user" record with her exact text is not her keyboard.

        The independent check's probe: a task wrote
        ``AUTOPILOT_PROJECT_DELETE project_id=<dup> root=<root>`` as
        origin=user, status=accepted - the text is printed in the proposal -
        then ran ``--retire-duplicate`` inside its Codex task: exit 0 and
        project/delete, nobody typed anything. ``--remove-root`` took the
        same path. The record is forged here straight into Project Memory
        (the memory tool refuses it now, see MemoryToolTests), and each
        command runs inside a task and then outside one without a terminal.
        Mutation: take an existing accepted record as hers in
        ``_record_decision`` again - project/delete and project/update are
        sent, this test fails.
        """

        from codex_autopilot import cli

        home = codex_home([self.root])
        MutationTests._threads_db(self, home, 0)
        server = ProjectsServer(
            [app_project(RUN_PROJECT, self.m.copy, self.root), app_project(DUPLICATE, self.root)], home
        )
        for statement in (
            change_statement(DELETE_PROJECT, DUPLICATE, self.root),
            change_statement(REMOVE_ROOT, RUN_PROJECT, self.m.copy),
        ):
            self.memory.propose_decision(statement=statement, origin="user", created_by="worker-agent", status="accepted")
        commands = (["--retire-duplicate", DUPLICATE], ["--remove-root", str(self.m.copy)])
        for flag in commands:
            for thread in ("an-agent-task", ""):
                out = io.StringIO()
                with mock.patch.object(cli, "AppServerClient", lambda *_args, **_kwargs: server), \
                        mock.patch.dict(os.environ, {"CODEX_THREAD_ID": thread}), \
                        mock.patch.object(cli.sys, "stdin", io.StringIO()), contextlib.redirect_stdout(out):
                    code = cli.main(["authorize-project-root", "--project", str(self.root), *flag])
                self.assertEqual(code, 3, (flag, thread, out.getvalue()))
                self.assertIn("Not confirmed", out.getvalue())
        self.assertEqual(server.mutations(), [])
        self.assertIn(DUPLICATE, server.projects)

    def test_the_add_root_permission_is_not_granted_from_inside_a_task(self) -> None:
        """``authorize-project-root --yes`` inside a Codex task: an agent's --yes, refused.

        It wrote origin=user for any caller (the verdict's "whose word"
        gap). Outside a task the existing flow stands. Mutation: drop the
        CODEX_THREAD_ID refusal in cli.main - the permission is recorded
        from inside the task, this test fails.
        """

        from codex_autopilot import cli

        statement = project_root_authorization_statement(RUN_PROJECT, self.root)
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "an-agent-task"}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["authorize-project-root", "--project", str(self.root), "--yes"])
        self.assertEqual(code, 3)
        self.assertIn("inside a Codex task", err.getvalue())
        self.assertIsNone(self.memory.accepted_user_decision(statement))
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": ""}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["authorize-project-root", "--project", str(self.root), "--yes"]), 0)
        self.assertIsNotNone(self.memory.accepted_user_decision(statement))


class MemoryToolTests(_Initialized):
    def test_the_memory_tool_neither_writes_nor_answers_her_project_decisions(self) -> None:
        """The agent's memory tool cannot speak for her on her saved projects (R6).

        The probe's first step was ``memory_propose_decision`` with origin
        "user" and status "accepted" - the tool refused only agent+accepted.
        Now her permissions (by their exact prefixes) and the roots
        proposals (by their scope) are refused on every writing operation;
        other decisions pass as before. Mutations: drop the check in
        ``_propose_decision`` - the forged permission is written; in
        ``_set_decision_status`` - the proposal is rejected by an agent; in
        ``_user_correction`` - the permission is written as a correction.
        Each fails this test.
        """

        from codex_autopilot.memory import MemoryValidationError
        from codex_autopilot.memory_mcp import MemoryMcpServer
        from codex_autopilot.project_roots_change import PRIMARY_ROOT

        server = MemoryMcpServer(self.root)
        permissions = [
            change_statement(DELETE_PROJECT, DUPLICATE, self.root),
            change_statement(REMOVE_ROOT, RUN_PROJECT, self.m.copy),
            change_statement(PRIMARY_ROOT, RUN_PROJECT, self.root),
            project_root_authorization_statement(RUN_PROJECT, self.root),
        ]
        for statement in permissions:
            with self.assertRaisesRegex(MemoryValidationError, "authorize-project-root"):
                server.call("memory", {
                    "operation": "propose_decision", "statement": statement, "origin": "user",
                    "created_by": "worker-agent", "status": "accepted",
                })
            with self.assertRaisesRegex(MemoryValidationError, "authorize-project-root"):
                server.call("memory", {"operation": "user_correction", "statement": statement, "related_ids": []})
            self.assertIsNone(self.memory.accepted_user_decision(statement))
        home = codex_home([self.m.copy, self.root])
        sync_roots_decisions(
            self.memory,
            audit_project_roots(home, self.root, DESKTOP, [app_project(RUN_PROJECT, self.m.copy, self.root)], RUN_PROJECT),
        )
        proposal = next(item for item in roots_decisions(self.memory) if item["status"] == "proposed")
        with self.assertRaisesRegex(MemoryValidationError, "authorize-project-root"):
            server.call("memory", {
                "operation": "set_decision_status", "decision_id": proposal["id"], "status": "rejected", "actor": "worker",
            })
        self.assertEqual(self.memory.get_record(proposal["id"])["status"], "proposed")
        ordinary = server.call("memory", {
            "operation": "propose_decision", "statement": "Use SQLite for the cache", "origin": "agent", "created_by": "worker",
        })
        self.assertEqual(ordinary["status"], "proposed")


class EveryCreationAndWakeTests(_Initialized):
    def test_a_creation_audits_the_roots_and_records_them(self) -> None:
        """R6: "preflight and every creation". Mutation: drop the call in lifecycle_dispatch."""

        from _appserver_fakes import FakeAppServerCreateClient
        from _relay import reserve_ready_frontier
        from codex_autopilot.lifecycle import create_desktop_thread_via_app_server

        descriptor = reserve_ready_frontier(self.cfg, relay_owner_thread_id="owner-thread")[0]
        client = FakeAppServerCreateClient(self.root, [], thread_id="created-thread", project_id=RUN_PROJECT)
        client.codex_home = str(codex_home([self.m.copy, self.root]))
        with mock.patch("codex_autopilot.lifecycle_dispatch.installed_plugin_root", return_value=self.root):
            create_desktop_thread_via_app_server(
                self.cfg, descriptor.reservation_token,
                client_factory=lambda *_args, **_kwargs: client, relay_executor_thread_id="owner-thread",
            )
        record = self.store.load().roots_audit
        self.assertEqual(record["occasion"], "creation")
        self.assertIn(SIBLING_ROOTS, [item["code"] for item in record["findings"]])

    def test_a_wake_audits_a_run_that_has_no_record_yet(self) -> None:
        """A run paused before this audit: the wake-up reads her own Codex home.

        Mutation: drop the call in run_wake - no record.
        """

        WakeTests, _Clock = test_wake.WakeTests, test_wake._Clock
        home = codex_home([self.m.copy, self.root])
        self.spawned = []
        due = WakeTests.hit_the_limit(self)
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(home)}):
            WakeTests.wake(self, at_epoch=due, clock=_Clock(due - 5))
        record = self.store.load().roots_audit
        self.assertEqual(record["occasion"], "wake")
        self.assertEqual(Path(record["codex_home"]), home)

    # The wake tests' own tools, the same functions (not a copy).
    pending_token = test_wake.WakeTests.pending_token
    fake_spawn_relay = test_wake.WakeTests.fake_spawn_relay


class CliProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.m = Machine()
        (self.m.target / ".codex-autopilot").mkdir()
        (self.m.target / ".codex-autopilot" / "config.toml").write_text("", encoding="utf-8")
        self.home = codex_home([self.m.copy, self.m.target])
        self.previous = Path.cwd()
        os.chdir(self.m.copy)
        self.addCleanup(os.chdir, self.previous)

    def test_a_command_that_would_start_a_run_in_the_copy_names_the_run_and_refuses(self) -> None:
        """Mutation: take the cwd silently - no refusal naming the run."""

        from codex_autopilot.cli import main

        err = io.StringIO()
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}), contextlib.redirect_stderr(err):
            code = main(["start-skill", "--plan-file", str(self.m.parent / "absent.json")])
        self.assertNotEqual(code, 0)
        self.assertIn(f"has one at {self.m.target}", err.getvalue())

    def test_a_reading_command_uses_the_sibling_run_and_says_so(self) -> None:
        from codex_autopilot.project_roots_audit import resolve_cli_project

        project, note = resolve_cli_project("status", None, self.m.copy, self.home)
        self.assertEqual(project, self.m.target)
        self.assertIn(str(self.m.target), note)
        explicit, note = resolve_cli_project("start-skill", self.m.copy, self.m.copy, self.home)
        self.assertEqual((explicit, note), (self.m.copy, ""))


class SkillTests(unittest.TestCase):
    def test_both_skills_explain_the_copy_and_close_the_agents_side_doors(self) -> None:
        """The initiating agent created a project and edited Desktop's state itself.

        Both plugins' skills now forbid it, name the one door, recommend
        the Desktop edit for a copy root, and pass Desktop's linked App
        Server id. Mutation: drop the paragraph from either skill.
        """

        root = Path(__file__).resolve().parents[1]
        for name in ("codex-autopilot-adaptive", "codex-autopilot-host-settings"):
            text = (root / f"plugins/{name}/skills/{name}/SKILL.md").read_text(encoding="utf-8")
            start = text[text.index("## Start a run"):]
            for needle in (
                "`project/create`", "`project/update`", ".codex-global-state.json",
                "--app-server-project-id", "SIBLING_ROOTS", "Edit project",
                "--retire-duplicate", "## Saved-project root drift",
            ):
                self.assertIn(needle, start, (name, needle))


if __name__ == "__main__":
    unittest.main()
