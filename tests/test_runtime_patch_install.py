"""A proven runtime patch is staged in the project and installed atomically.

The on-call's command used to write the proven patch straight into the
installed runtime: outside the project (beyond its ``:workspace`` sandbox)
and file by file under every live dispatcher. Now it stages the patch in the
project, the run drains, and the wake-up installs it into a fresh version
directory and switches ``current`` with one rename - only when no
dispatcher is alive.

Only fakes: a temporary install root, no live Codex, no App Server.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import mock

from _relay import reserve_ready_frontier
from codex_autopilot.runtime_install import (
    install_pending,
    install_when_quiet,
    pending_entries,
    stage_proven_patch,
    stage_revert,
    withdraw_staged,
)
from codex_autopilot.runtime_repair import ModuleChange, PatchRecord, ProvenPatch, _sha256
from test_a_dead_dispatcher_strands_nothing import _Base

OLD = "VALUE = 'old'\n"
NEW = "VALUE = 'new'\n"


def proven(before: str = OLD, patch_id: str = "patch-demo") -> ProvenPatch:
    return ProvenPatch(
        record=PatchRecord(
            patch_id=patch_id,
            changes=(ModuleChange(module="status.py", sha256_before=_sha256(before), sha256_after=_sha256(NEW)),),
            test_name="test_demo_repro",
            at="2026-09-24T10:00:00+00:00",
        ),
        sources={"status.py": NEW},
        originals={"status.py": before},
        test_name="test_demo_repro",
        test_source="import unittest\n",
    )


class _Installation(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.install = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.install, ignore_errors=True))
        version = self.install / "some-version"
        package = version / "runtime" / "src" / "codex_autopilot"
        package.mkdir(parents=True)
        (version / "runtime" / "tests").mkdir()
        (package / "status.py").write_text(OLD, encoding="utf-8")
        (self.install / "current").symlink_to(version)
        self.version = version

    def live_status(self) -> str:
        return (self.install / "current" / "runtime" / "src" / "codex_autopilot" / "status.py").read_text(
            encoding="utf-8"
        )


class AStagedPatchDrainsTheRunTests(_Installation):
    def test_nothing_new_starts_while_a_patch_waits(self) -> None:
        from codex_autopilot.engineer_reservation import stranded_reason
        from codex_autopilot.run_status import _finish_global_state
        from codex_autopilot.plan import load_plan

        stage_proven_patch(self.cfg.state_dir, proven())

        self.assertEqual(reserve_ready_frontier(self.cfg), ())
        state = self.store.load()
        _finish_global_state(load_plan(self.cfg.state_dir, self.cfg.profile), state, (), cfg=self.cfg)
        self.assertEqual((state.status, state.phase), ("WAITING", "RUNTIME_PATCH_PENDING"))
        self.assertEqual(stranded_reason(self.cfg, state), "a proven runtime patch waits to be installed")


class InstallingAtomicallyTests(_Installation):
    def test_a_new_version_directory_and_one_switch_of_current(self) -> None:
        stage_proven_patch(self.cfg.state_dir, proven())

        outcome = install_pending(self.install, [self.cfg.state_dir], now=lambda: 0)

        self.assertEqual([item["entry"] for item in outcome["installed"]], ["patch-demo"])
        target = (self.install / "current").resolve()
        self.assertNotEqual(target, self.version.resolve())
        self.assertTrue(target.name.startswith("some-version.repaired-"))
        self.assertEqual(self.live_status(), NEW)
        # The version every running process started from is untouched.
        self.assertEqual(
            (self.version / "runtime" / "src" / "codex_autopilot" / "status.py").read_text(encoding="utf-8"),
            OLD,
        )
        backup = target / "runtime" / "patches" / "patch-demo"
        self.assertEqual((backup / "status.py.orig").read_text(encoding="utf-8"), OLD)
        self.assertTrue((target / "runtime" / "tests" / "test_demo_repro.py").is_file())
        self.assertEqual(pending_entries(self.cfg.state_dir), [])
        self.assertTrue((self.cfg.state_dir / "runtime-patches" / "installed" / "patch-demo").is_dir())

    def test_a_patch_proven_against_other_text_is_refused_not_forced(self) -> None:
        stage_proven_patch(self.cfg.state_dir, proven(before="VALUE = 'something else'\n"))

        outcome = install_pending(self.install, [self.cfg.state_dir], now=lambda: 0)

        self.assertEqual(outcome["installed"], [])
        self.assertIn("changed since the patch was proven", outcome["refused"][0]["reason"])
        self.assertEqual((self.install / "current").resolve(), self.version.resolve())
        self.assertEqual(self.live_status(), OLD)
        self.assertEqual(
            [item.name for item in self.install.iterdir() if ".repaired-" in item.name], []
        )

    def test_an_installed_patch_is_taken_back_the_same_way(self) -> None:
        stage_proven_patch(self.cfg.state_dir, proven())
        install_pending(self.install, [self.cfg.state_dir], now=lambda: 0)
        self.assertFalse(withdraw_staged(self.cfg.state_dir, "patch-demo"))
        stage_revert(self.cfg.state_dir, "patch-demo", at="2026-09-24T11:00:00+00:00")

        outcome = install_pending(self.install, [self.cfg.state_dir], now=lambda: 3600)

        self.assertEqual(outcome["installed"][0]["kind"], "revert")
        self.assertEqual(self.live_status(), OLD)

    def test_a_staged_patch_is_withdrawn_not_deleted(self) -> None:
        stage_proven_patch(self.cfg.state_dir, proven())
        self.assertTrue(withdraw_staged(self.cfg.state_dir, "patch-demo"))
        self.assertEqual(pending_entries(self.cfg.state_dir), [])
        self.assertTrue((self.cfg.state_dir / "runtime-patches" / "withdrawn" / "patch-demo").is_dir())


class OnlyWhenNoDispatcherIsAliveTests(_Installation):
    def test_a_live_dispatcher_defers_the_install(self) -> None:
        worker = reserve_ready_frontier(self.cfg)[0]
        state = self.store.load()
        session = next(i for i in state.worker_sessions if i["reservation_token"] == worker.reservation_token)
        session["automatic_dispatch_state"] = "RUNNING"
        session["automatic_dispatch_pid"] = os.getpid()
        self.store.save(state)
        stage_proven_patch(self.cfg.state_dir, proven())

        outcome = install_when_quiet(self.cfg, install_root=self.install)

        self.assertIn("dispatcher is alive", outcome["deferred"])
        self.assertEqual(self.live_status(), OLD)
        self.assertEqual(len(pending_entries(self.cfg.state_dir)), 1)

    def test_a_quiet_run_is_patched(self) -> None:
        stage_proven_patch(self.cfg.state_dir, proven())
        outcome = install_when_quiet(self.cfg, install_root=self.install)
        self.assertEqual(outcome["installed"][0]["entry"], "patch-demo")
        self.assertEqual(self.live_status(), NEW)


class TheWakeUpInstallsOrFilesTests(_Installation):
    def test_the_wake_up_installs_and_leaves_the_run_to_the_next_sweep(self) -> None:
        stage_proven_patch(self.cfg.state_dir, proven())
        with mock.patch("codex_autopilot.runtime_install.install_root_from_env", return_value=self.install), \
             mock.patch("codex_autopilot.wake.registered_projects", return_value=[]):
            self._wake("terminal")
        self.assertEqual(self.live_status(), NEW)
        events = [item["event"] for item in self.store.load().resilience_journal]
        self.assertEqual(events[-1], "runtime_patch_installed")
        self.assertEqual(self.spawned, [], "this process imported the old tree; it raises nothing")

    def test_outside_an_installation_the_patch_is_refused_and_ticketed(self) -> None:
        """Waiting would drain the run forever: a silent stop."""

        stage_proven_patch(self.cfg.state_dir, proven())
        with mock.patch("codex_autopilot.runtime_install.install_root_from_env", return_value=None):
            self._wake("terminal")
        self.assertEqual(pending_entries(self.cfg.state_dir), [])
        ticket = self.incidents.load()["incidents"][-1]
        self.assertEqual(ticket["system_state"]["stop_kind"], "runtime_patch_refused")
        # The run is not drained any more: the on-call comes for the ticket.
        engineers = [item for item in self.store.load().worker_sessions if item["kind"] == "pipeline_engineer"]
        self.assertEqual(engineers[-1]["incident_id"], ticket["incident_id"])


if __name__ == "__main__":
    import unittest

    unittest.main()
