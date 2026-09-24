from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from codex_autopilot.artifact_staging import (
    ArtifactClass,
    ArtifactStagingError,
    ArtifactStagingStore,
    CanonicalDriftError,
    StagedArtifactChangedError,
    StagingStatus,
    resolve_canonical_project_root,
    task_requires_staging,
)


class ArtifactStagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("OLD = True\n", encoding="utf-8")
        (self.root / "README.md").write_text("baseline\n", encoding="utf-8")
        (self.root / ".codex-autopilot" / "handoff").mkdir(parents=True)
        (self.root / ".codex-autopilot" / "handoff" / "M5.md").write_text(
            "before\n", encoding="utf-8"
        )
        self.store = ArtifactStagingStore(self.root)

    def prepare(self):
        return self.store.prepare(
            run_id="run-1",
            task_id="M5",
            reservation_token="reservation-1",
        )

    def test_worker_changes_are_isolated_until_verified_promotion(self) -> None:
        staged = self.prepare()
        (staged.workspace / "src" / "app.py").write_text(
            "OLD = False\n", encoding="utf-8"
        )
        (staged.workspace / "build" / "cover.png").parent.mkdir()
        (staged.workspace / "build" / "cover.png").write_bytes(b"png-v1")

        proposal = self.store.seal("M5")

        self.assertEqual((self.root / "src" / "app.py").read_text(), "OLD = True\n")
        self.assertFalse((self.root / "build" / "cover.png").exists())
        self.assertEqual(proposal.status, StagingStatus.PROPOSED)
        classes = {item.path: item.artifact_class for item in proposal.changes}
        self.assertEqual(classes["src/app.py"], ArtifactClass.CODE)
        self.assertEqual(classes["build/cover.png"], ArtifactClass.GENERATED_ASSET)

        with self.assertRaises(ArtifactStagingError):
            self.store.promote("M5")

        self.store.mark_verified("M5", verification_id="verification-7")
        promoted = self.store.promote(
            "M5", expected_verification_id="verification-7"
        )

        self.assertEqual((self.root / "src" / "app.py").read_text(), "OLD = False\n")
        self.assertEqual((self.root / "build" / "cover.png").read_bytes(), b"png-v1")
        self.assertTrue((promoted.snapshot_path / "snapshot.json").is_file())
        self.assertEqual(self.store.load("M5").status, StagingStatus.PROMOTED)
        repeated = self.store.promote(
            "M5", expected_verification_id="verification-7"
        )
        self.assertEqual(repeated, promoted)

    def test_revise_keeps_canonical_clean_and_reuses_workspace(self) -> None:
        staged = self.prepare()
        target = staged.workspace / "src" / "app.py"
        target.write_text("revision = 1\n", encoding="utf-8")
        self.store.seal("M5")

        revision = self.store.mark_revision_required("M5")
        self.assertEqual(revision.workspace, staged.workspace)
        self.assertEqual((self.root / "src" / "app.py").read_text(), "OLD = True\n")

        target.write_text("revision = 2\n", encoding="utf-8")
        proposal = self.store.seal("M5")
        self.assertEqual(proposal.status, StagingStatus.PROPOSED)
        self.assertEqual((self.root / "src" / "app.py").read_text(), "OLD = True\n")

    def test_canonical_drift_fails_closed_without_overwrite(self) -> None:
        staged = self.prepare()
        (staged.workspace / "src" / "app.py").write_text("staged\n", encoding="utf-8")
        self.store.seal("M5")
        self.store.mark_verified("M5", verification_id="verification-1")
        (self.root / "src" / "app.py").write_text("concurrent\n", encoding="utf-8")

        with self.assertRaisesRegex(CanonicalDriftError, "src/app.py"):
            self.store.promote("M5")

        self.assertEqual((self.root / "src" / "app.py").read_text(), "concurrent\n")
        self.assertEqual(self.store.load("M5").status, StagingStatus.VERIFIED)

    def test_mutation_after_verification_requires_a_new_proposal(self) -> None:
        staged = self.prepare()
        target = staged.workspace / "src" / "app.py"
        target.write_text("verifier saw this\n", encoding="utf-8")
        self.store.seal("M5")
        self.store.mark_verified("M5", verification_id="verification-1")
        target.write_text("changed after verdict\n", encoding="utf-8")

        with self.assertRaises(StagedArtifactChangedError):
            self.store.promote("M5")

        self.assertEqual((self.root / "src" / "app.py").read_text(), "OLD = True\n")

    def test_add_delete_and_symlink_are_promoted_from_one_snapshot(self) -> None:
        staged = self.prepare()
        (staged.workspace / "README.md").unlink()
        (staged.workspace / "new.txt").write_text("new\n", encoding="utf-8")
        (staged.workspace / "latest").symlink_to("new.txt")
        self.store.seal("M5")
        self.store.mark_verified("M5", verification_id="verification-2")

        self.store.promote("M5")

        self.assertFalse((self.root / "README.md").exists())
        self.assertEqual((self.root / "new.txt").read_text(), "new\n")
        self.assertTrue((self.root / "latest").is_symlink())
        self.assertEqual((self.root / "latest").readlink(), Path("new.txt"))

    def test_symlink_that_escapes_project_is_rolled_back(self) -> None:
        staged = self.prepare()
        (staged.workspace / "added" / "first.txt").parent.mkdir()
        (staged.workspace / "added" / "first.txt").write_text("partial\n")
        (staged.workspace / "z-escape").symlink_to("../../outside")
        self.store.seal("M5")
        self.store.mark_verified("M5", verification_id="verification-escape")

        with self.assertRaisesRegex(ArtifactStagingError, "symlink escapes"):
            self.store.promote("M5")

        self.assertFalse((self.root / "added").exists())
        self.assertFalse((self.root / "z-escape").exists())
        self.assertEqual(self.store.load("M5").status, StagingStatus.VERIFIED)

    def test_checkpoint_is_control_state_not_a_promoted_artifact(self) -> None:
        staged = self.prepare()
        checkpoint = staged.workspace / ".codex-autopilot" / "handoff" / "M5.md"
        checkpoint.write_text("after\n", encoding="utf-8")

        published = self.store.publish_checkpoint("M5")
        proposal = self.store.seal("M5")

        self.assertEqual(published.read_text(), "after\n")
        self.assertNotIn(".codex-autopilot/handoff/M5.md", {c.path for c in proposal.changes})

    def test_staged_cwd_resolves_project_memory_to_canonical_root(self) -> None:
        staged = self.prepare()
        nested = staged.workspace / "src" / "nested"
        nested.mkdir()
        self.assertEqual(resolve_canonical_project_root(nested), self.root)

    def test_filesystem_deliverable_with_write_authority_enters_staging(self) -> None:
        filesystem = SimpleNamespace(
            outputs=(SimpleNamespace(required=True, path="src/app.py"),),
            resources=(SimpleNamespace(kind="directory", access="write"),),
        )
        undeclared_output = SimpleNamespace(
            outputs=(),
            resources=(SimpleNamespace(kind="path", access="write"),),
        )
        optional_output = SimpleNamespace(
            outputs=(SimpleNamespace(required=False, path="src/app.py"),),
            resources=(SimpleNamespace(kind="glob", access="exclusive"),),
        )
        read_only = SimpleNamespace(
            outputs=(SimpleNamespace(required=True, path="src/app.py"),),
            resources=(SimpleNamespace(kind="directory", access="read"),),
        )
        logical = SimpleNamespace(
            outputs=(SimpleNamespace(required=True, path=None),),
            resources=(SimpleNamespace(kind="logical", access="write"),),
        )
        self.assertTrue(task_requires_staging(filesystem))
        self.assertFalse(task_requires_staging(undeclared_output))
        self.assertTrue(task_requires_staging(optional_output))
        self.assertFalse(task_requires_staging(read_only))
        self.assertFalse(task_requires_staging(filesystem, legacy_serial=True))
        self.assertFalse(task_requires_staging(logical))


class StagedArtifactLifecycleTests(unittest.TestCase):
    """T7: exercise reservation -> REVISE -> PASS on the production path."""

    def setUp(self) -> None:
        from _gates import patch_hook_trust_gates

        patch_hook_trust_gates(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".git").mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "result.txt").write_text("canonical\n", encoding="utf-8")
        self.skill = self.root / "SKILL.md"
        self.skill.write_text("# test skill\n", encoding="utf-8")

        from _plan_contract import (
            TEST_OUTCOME_ID,
            canonical_verification,
            canonicalize_plan,
            initialize_verified_project,
        )

        task = {
            "id": "A",
            "title": "Staged output",
            "objective": "Produce one isolated artifact.",
            "definition_of_done": ["The accepted content is promoted."],
            "execution_mode": "code",
            "execution_mode_reason": "Repository files and tests are sufficient.",
            "reasoning": "medium",
            "role": "builder",
            "depends_on": [],
            "priority": 0,
            "verification": canonical_verification(),
            "resources": [
                {
                    "id": "source",
                    "kind": "directory",
                    "target": "src",
                    "access": "write",
                }
            ],
            "required_capabilities": [],
            "context": {},
            "outputs": [
                {
                    "id": "result",
                    "description": "Verified staged result.",
                    "path": "src/result.txt",
                    "required": True,
                }
            ],
            "tags": [],
            "produces_outcomes": [TEST_OUTCOME_ID],
            "acceptance_class": "mixed",
        }
        payload = canonicalize_plan(
            {
                "schema_version": 3,
                "graph_version": 1,
                "goal": "Exercise the staged artifact lifecycle.",
                "user_request": "Canonical state changes only after independent PASS.",
                "model_strategy": "auto",
                "execution_strategy": "serial",
                "max_parallel_workers": 1,
                "computer_use_slots": 1,
                "roles": [
                    {
                        "id": "builder",
                        "name": "Builder",
                        "responsibilities": ["Produce and judge the fixture."],
                    }
                ],
                "tasks": [task],
            }
        )
        plan_file = self.root / "plan-input.json"
        plan_file.write_text(json.dumps(payload), encoding="utf-8")
        initialize_verified_project(
            self.root,
            plan_file,
            profile="adaptive",
            skill_path=self.skill,
            desktop_project_id="desktop-project",
        )

        from codex_autopilot.config import load_config
        from codex_autopilot.memory import ProjectMemory

        self.cfg = load_config(self.root)
        self.memory = ProjectMemory(self.root)

    def reserve(self):
        from _relay import reserve_ready_frontier

        return reserve_ready_frontier(self.cfg)[0]

    def activate(self, descriptor, thread_id: str) -> None:
        from _appserver_fakes import activate_via_app_server

        activate_via_app_server(self.cfg, self.root, descriptor, thread_id)

    def checkpoint_and_evidence(self, workspace: Path, label: str, role: str) -> None:
        checkpoint = workspace / ".codex-autopilot" / "handoff" / "A.md"
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        previous = checkpoint.read_text(encoding="utf-8") if checkpoint.is_file() else "# A\n"
        checkpoint.write_text(previous + f"\n{label}\n", encoding="utf-8")
        self.memory.record_evidence(
            kind="test",
            summary=label,
            milestone_id="A",
            role=role,
            command=f"check {label}",
            result="PASS",
            exit_code=0,
            created_by="staging-lifecycle-test",
        )

    def test_revise_leaves_no_canonical_trace_and_pass_promotes(self) -> None:
        from _plan_contract import attested_verdict
        from codex_autopilot.lifecycle import complete_desktop_worker

        implementation = self.reserve()
        workspace = Path(implementation.cwd)
        self.assertNotEqual(workspace, self.root)
        self.assertIn("STAGED_ARTIFACT_GATE", implementation.prompt)
        self.activate(implementation, "implementation-thread")
        (workspace / "src" / "result.txt").write_text("proposal-1\n", encoding="utf-8")
        self.checkpoint_and_evidence(workspace, "implementation", "implementation")
        first = complete_desktop_worker(
            self.cfg,
            thread_id="implementation-thread",
            turn_id="implementation-turn",
            final_message="AUTOPILOT_RULES: R24, R29\nAUTOPILOT_STATUS: ROTATE",
        )
        verifier = first.descriptors[0]
        self.assertEqual(Path(verifier.cwd), workspace)
        self.assertEqual((self.root / "src" / "result.txt").read_text(), "canonical\n")
        staged_evidence = [
            item
            for item in self.memory.milestone_evidence("A", limit=100)
            if item.get("role") == "staged-artifact"
        ]
        self.assertEqual(len(staged_evidence), 1)
        self.assertIn("staged-artifacts/A/workspace/src/result.txt", staged_evidence[0]["artifact_path"])
        self.assertIn(str(staged_evidence[0]["id"]), verifier.prompt)

        self.activate(verifier, "verifier-thread-1")
        self.checkpoint_and_evidence(
            workspace, "independent rejection", "independent_verification"
        )
        second = complete_desktop_worker(
            self.cfg,
            thread_id="verifier-thread-1",
            turn_id="verifier-turn-1",
            final_message=attested_verdict(self.cfg, "A", "REVISE", [
                {"code": "CONTENT", "summary": "Revise content", "details": "Use proposal 2.", "dod_refs": [1]}
            ], prefix="AUTOPILOT_RULES: R24, R29\n"),
        )
        revision = second.descriptors[0]
        self.assertEqual(Path(revision.cwd), workspace)
        self.assertEqual((self.root / "src" / "result.txt").read_text(), "canonical\n")

        self.activate(revision, "revision-thread")
        (workspace / "src" / "result.txt").write_text("proposal-2\n", encoding="utf-8")
        self.checkpoint_and_evidence(workspace, "revision", "implementation")
        third = complete_desktop_worker(
            self.cfg,
            thread_id="revision-thread",
            turn_id="revision-turn",
            final_message="AUTOPILOT_RULES: R24, R29\nAUTOPILOT_STATUS: ROTATE",
        )
        verifier_two = third.descriptors[0]
        self.assertEqual((self.root / "src" / "result.txt").read_text(), "canonical\n")

        self.activate(verifier_two, "verifier-thread-2")
        self.checkpoint_and_evidence(
            workspace, "independent pass", "independent_verification"
        )
        complete_desktop_worker(
            self.cfg,
            thread_id="verifier-thread-2",
            turn_id="verifier-turn-2",
            final_message=attested_verdict(self.cfg, "A", prefix="AUTOPILOT_RULES: R24, R29\n"),
        )

        self.assertEqual((self.root / "src" / "result.txt").read_text(), "proposal-2\n")
        staged = ArtifactStagingStore(self.root).load("A")
        self.assertEqual(staged.status, StagingStatus.PROMOTED)


if __name__ == "__main__":
    unittest.main()
