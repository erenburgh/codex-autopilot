"""A skill may be installed. A plugin may never be.

The boundary is the owner's, and it is what protects her absolute rule that
hook trust is never touched: a plugin registers hooks, MCP servers and
commands and lives in the Codex plugin cache, so installing one changes the
host's trust surface. A skill is a SKILL.md bundle and registers nothing.

Measured on this machine against codex-cli 0.154.0: Codex's own system skill
`skill-installer` installs into $CODEX_HOME/skills/<name> from a GitHub repo
path and touches no plugin; and TurnStartParams.input accepts a
SkillUserInput carrying an arbitrary path, which is how this runtime already
hands a worker its own skill.

These tests drive the admission path at the places it must not be able to
reach, and require a refusal from each.
"""

from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_autopilot.hired_skills import (
    HIRED_SKILLS_DIRNAME,
    STAGED_SKILLS_DIRNAME,
    HiredSkillError,
    admit_skill_bundle,
    hired_skill_records,
    installed_skill_bundles,
    revoke_hired_skill,
)


SKILL_MD = """---
name: taste
description: Opinionated front-end procedure.
---

# Taste

Write less code. Use the project's own design tokens.
"""


class AdmissionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_dir = self.root / ".codex-autopilot"
        self.state_dir.mkdir(parents=True)

    def stage(self, name: str = "taste", *, body: str = SKILL_MD) -> Path:
        staged = self.state_dir / STAGED_SKILLS_DIRNAME / name
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "SKILL.md").write_text(body, encoding="utf-8")
        return staged

    def admit(self, staged: Path, **overrides):
        payload = {
            "name": "taste",
            "provider": "github.com/example/skills",
            "locator": "skills/taste",
        }
        payload.update(overrides)
        return admit_skill_bundle(self.state_dir, staged_path=staged, **payload)


class TheInstallerCannotReachTheHostTests(AdmissionCase):
    """The rule is structural: the path cannot be named, not merely refused."""

    def test_it_refuses_every_place_a_plugin_or_hook_would_live(self) -> None:
        staged = self.stage()
        home = Path.home()
        for destination in (
            home / ".codex" / "plugins",
            home / ".codex" / "skills",
            home / ".codex" / "hooks",
            home / ".codex" / "config.toml",
            Path("/tmp"),
            self.root,
            self.state_dir,
        ):
            with self.subTest(destination=str(destination)):
                with self.assertRaises(HiredSkillError) as caught:
                    admit_skill_bundle(
                        self.state_dir,
                        staged_path=staged,
                        name="taste",
                        provider="github.com/example/skills",
                        locator="skills/taste",
                        destination_root=destination,
                    )
                self.assertIn("hired-skills", str(caught.exception))

    def test_a_name_cannot_climb_out_of_the_directory(self) -> None:
        staged = self.stage()
        for name in ("../plugins/evil", "..", "a/b", "/etc/passwd", ".hidden"):
            with self.subTest(name=name):
                with self.assertRaises(HiredSkillError):
                    self.admit(staged, name=name)

    def test_a_staged_bundle_outside_the_project_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            staged = Path(elsewhere) / "taste"
            staged.mkdir()
            (staged / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

            with self.assertRaisesRegex(HiredSkillError, "inside the project"):
                self.admit(staged)

    def test_a_symlink_that_escapes_the_project_is_refused(self) -> None:
        staged = self.stage()
        (staged / "outside").symlink_to(Path.home() / ".codex")

        with self.assertRaisesRegex(HiredSkillError, "symbolic link"):
            self.admit(staged)


class AdmissionTests(AdmissionCase):
    def test_a_bundle_is_admitted_under_its_content_digest(self) -> None:
        record = self.admit(self.stage())

        installed = Path(record["path"])
        self.assertTrue(installed.is_dir())
        self.assertEqual(
            installed.parent.name, HIRED_SKILLS_DIRNAME, "bundles live in one place"
        )
        self.assertTrue(installed.name.startswith("taste@"))
        self.assertEqual(
            (installed / "SKILL.md").read_text(encoding="utf-8"), SKILL_MD
        )
        self.assertEqual(record["provider"], "github.com/example/skills")

    def test_the_same_bundle_twice_is_the_same_directory(self) -> None:
        first = self.admit(self.stage())
        second = self.admit(self.stage())

        self.assertEqual(first["path"], second["path"])
        self.assertEqual(first["digest"], second["digest"])
        self.assertEqual(len(hired_skill_records(self.state_dir)), 1)

    def test_a_changed_bundle_is_a_different_revision_beside_it(self) -> None:
        """The first-party installer aborts when the destination exists. A
        project must be able to hold a second revision instead."""

        first = self.admit(self.stage())
        second = self.admit(
            self.stage(body=SKILL_MD + "\nAlso: prefer CSS grid.\n")
        )

        self.assertNotEqual(first["digest"], second["digest"])
        self.assertTrue(Path(first["path"]).is_dir())
        self.assertTrue(Path(second["path"]).is_dir())
        self.assertEqual(len(hired_skill_records(self.state_dir)), 2)

    def test_a_bundle_without_a_skill_file_is_not_a_skill(self) -> None:
        staged = self.state_dir / STAGED_SKILLS_DIRNAME / "taste"
        staged.mkdir(parents=True)
        (staged / "README.md").write_text("not a skill", encoding="utf-8")

        with self.assertRaisesRegex(HiredSkillError, "SKILL.md"):
            self.admit(staged)

    def test_a_bundle_that_ships_a_plugin_manifest_is_refused(self) -> None:
        """A skill bundle carrying plugin registration is a plugin wearing a
        skill's clothes, and the boundary is the whole point."""

        staged = self.stage()
        (staged / ".codex-plugin").mkdir()
        (staged / ".codex-plugin" / "plugin.json").write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(HiredSkillError, "plugin"):
            self.admit(staged)

    def test_a_bundle_that_registers_hooks_or_mcp_is_refused(self) -> None:
        for name in ("hooks", ".mcp.json"):
            with self.subTest(entry=name):
                staged = self.stage(name=f"taste-{name.strip('.')}")
                if name.endswith(".json"):
                    (staged / name).write_text("{}", encoding="utf-8")
                else:
                    (staged / name).mkdir()
                with self.assertRaises(HiredSkillError) as caught:
                    self.admit(staged, name=f"taste-{name.strip('.')}")
                self.assertIn(name, str(caught.exception))

    def test_revoking_removes_the_bundle_and_says_what_it_removed(self) -> None:
        record = self.admit(self.stage())

        removed = revoke_hired_skill(self.state_dir, record["id"])

        self.assertEqual(removed["id"], record["id"])
        self.assertFalse(Path(record["path"]).exists())
        self.assertEqual(hired_skill_records(self.state_dir), ())

    def test_revoking_something_absent_names_what_is_there(self) -> None:
        self.admit(self.stage())

        with self.assertRaises(HiredSkillError) as caught:
            revoke_hired_skill(self.state_dir, "no-such-skill")

        self.assertIn("taste@", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class InterruptedAndRepeatedAdmissionTests(AdmissionCase):
    """What the coordinating session said it would aim at.

    Already present, present at another revision, a half-finished copy, and
    a run interrupted between installing and hiring.
    """

    def test_a_half_finished_copy_is_never_mistaken_for_an_admitted_bundle(self) -> None:
        """A bundle is copied under a dotted name and renamed into place, so
        an interrupted admission leaves nothing that looks installed."""

        staged = self.stage()
        record = self.admit(staged)
        pending = Path(record["path"]).parent / f".{Path(record['path']).name}.pending"
        pending.mkdir()
        (pending / "SKILL.md").write_text("half a file", encoding="utf-8")

        self.assertEqual(
            [item["id"] for item in hired_skill_records(self.state_dir)],
            [record["id"]],
        )

    def test_a_leftover_pending_copy_does_not_block_a_later_admission(self) -> None:
        staged = self.stage()
        root = self.state_dir / HIRED_SKILLS_DIRNAME
        root.mkdir(parents=True)
        digest_dir = root / ".taste@000000000000.pending"
        digest_dir.mkdir()
        (digest_dir / "junk").write_text("x", encoding="utf-8")

        record = self.admit(staged)

        self.assertTrue(Path(record["path"]).is_dir())

    def test_readmitting_the_same_bundle_does_not_disturb_what_is_installed(self) -> None:
        """The run may be interrupted between installing and hiring, and the
        next screening staging the same bundle must be harmless."""

        first = self.admit(self.stage())
        marker = Path(first["path"]) / "SKILL.md"
        before = marker.stat().st_mtime_ns

        second = self.admit(self.stage())

        self.assertEqual(second["path"], first["path"])
        self.assertEqual(marker.stat().st_mtime_ns, before, "it was not recopied")

    def test_revoking_one_revision_leaves_the_other(self) -> None:
        first = self.admit(self.stage())
        second = self.admit(self.stage(body=SKILL_MD + "\nAlso: prefer grid.\n"))

        revoke_hired_skill(self.state_dir, first["id"])

        self.assertFalse(Path(first["path"]).exists())
        self.assertTrue(Path(second["path"]).is_dir())
        self.assertEqual(
            [item["id"] for item in hired_skill_records(self.state_dir)], [second["id"]]
        )

    def test_an_unreadable_file_is_refused_before_anything_is_copied(self) -> None:
        """The digest reads every file, so a bundle that cannot be read in
        full never reaches the copy at all."""

        import os

        staged = self.stage()
        unreadable = staged / "reference.md"
        unreadable.write_text("half of a procedure", encoding="utf-8")
        os.chmod(unreadable, 0o000)
        self.addCleanup(os.chmod, unreadable, 0o600)

        with self.assertRaises(PermissionError):
            self.admit(staged)

        self.assertEqual(hired_skill_records(self.state_dir), ())

    def test_a_copy_that_dies_partway_leaves_nothing_that_looks_admitted(self) -> None:
        """Measured on this machine: shutil.copytree creates its destination
        first and copies afterwards, so a failure partway leaves a partial
        directory behind - `destination exists after failure: True`. Copying
        under a dotted name and renaming into place is what keeps that
        wreckage from being listed, and handed to a worker, as the skill.

        The failure is injected because the real one is a disk filling up or
        a process dying mid-copy; the shape injected here is the shape that
        was measured.
        """

        from unittest import mock

        staged = self.stage()

        def half_a_copy(src, dst, **_kwargs):
            Path(dst).mkdir(parents=True, exist_ok=True)
            (Path(dst) / "SKILL.md").write_text("half a", encoding="utf-8")
            raise OSError("no space left on device")

        with mock.patch("codex_autopilot.hired_skills.shutil.copytree", half_a_copy):
            with self.assertRaises(OSError):
                self.admit(staged)

        self.assertEqual(
            hired_skill_records(self.state_dir),
            (),
            "a half-copied bundle must not be listed as installed",
        )


class InstalledSkillsAreReadNeverWrittenTests(unittest.TestCase):
    """The skills she installed herself are part of what is available.

    Measured on this machine: nothing in src reads her Codex skills
    directory except preflight, for unrelated reasons, so a screener looking
    for a capability she already has would record it unmet or go to the
    market for a second copy of something on her disk.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.skills = self.home / "skills"
        self.skills.mkdir(parents=True)

    def install(self, name: str, *, body: str = SKILL_MD) -> Path:
        bundle = self.skills / name
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "SKILL.md").write_text(body, encoding="utf-8")
        return bundle

    def test_a_missing_codex_home_is_simply_an_empty_library(self) -> None:
        bundles, refused = installed_skill_bundles(self.home / "nowhere")

        self.assertEqual(bundles, ())
        self.assertEqual(refused, ())

    def test_it_lists_what_she_installed(self) -> None:
        self.install("taste")
        self.install("another-skill")

        bundles, refused = installed_skill_bundles(self.home)

        self.assertEqual([item["name"] for item in bundles], ["another-skill", "taste"])
        self.assertEqual(refused, ())
        self.assertTrue(bundles[1]["path"].endswith("/skills/taste"))
        self.assertTrue(bundles[1]["digest"])

    def test_codex_own_system_skills_are_not_offered(self) -> None:
        """They are preinstalled for every session by Codex itself, so
        hiring one adds nothing and only crowds the brief. They are excluded
        by the dotted-entry rule, `.system` being dotted - a separate check
        for the name was written and a mutation proved it dead."""

        self.install("taste")
        system = self.skills / ".system" / "skill-creator"
        system.mkdir(parents=True)
        (system / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

        bundles, refused = installed_skill_bundles(self.home)

        self.assertEqual([item["name"] for item in bundles], ["taste"])
        self.assertEqual(refused, (), "and not named as a refusal either")

    def test_a_directory_that_is_not_a_skill_is_refused_by_name(self) -> None:
        """Her directory is not Autopilot's configuration, so one unusable
        entry is named and skipped rather than stopping the run."""

        self.install("taste")
        (self.skills / "leftovers").mkdir()

        bundles, refused = installed_skill_bundles(self.home)

        self.assertEqual([item["name"] for item in bundles], ["taste"])
        self.assertEqual(len(refused), 1)
        self.assertIn("leftovers", refused[0])
        self.assertIn("SKILL.md", refused[0])

    def test_reading_never_writes_to_her_codex_home(self) -> None:
        """Driven, not promised: the whole tree is made unwritable and the
        read still succeeds. Autopilot owns nothing in her Codex home."""

        import os

        self.install("taste")
        os.chmod(self.skills / "taste", 0o500)
        os.chmod(self.skills, 0o500)
        self.addCleanup(os.chmod, self.skills, 0o700)
        self.addCleanup(os.chmod, self.skills / "taste", 0o700)

        bundles, refused = installed_skill_bundles(self.home)

        self.assertEqual([item["name"] for item in bundles], ["taste"])
        self.assertEqual(refused, ())

    def test_an_installed_bundle_is_never_an_admission_destination(self) -> None:
        """Reading hers must not become the exception that lets the
        installer write there."""

        staged_root = self.home / ".codex-autopilot"
        staged = staged_root / STAGED_SKILLS_DIRNAME / "taste"
        staged.mkdir(parents=True)
        (staged / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

        with self.assertRaisesRegex(HiredSkillError, "hired-skills"):
            admit_skill_bundle(
                staged_root,
                staged_path=staged,
                name="taste",
                provider="github.com/example/skills",
                destination_root=self.skills,
            )

    def test_a_symlinked_entry_is_refused_by_name_not_followed(self) -> None:
        """Her directory is hers, and a link in it could point anywhere.
        The read names it and moves on rather than walking out of the tree."""

        self.install("taste")
        linked = self.skills / "linked"
        linked.mkdir()
        (linked / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        (linked / "elsewhere").symlink_to(self.home)

        bundles, refused = installed_skill_bundles(self.home)

        self.assertEqual([item["name"] for item in bundles], ["taste"])
        self.assertEqual(len(refused), 1)
        self.assertIn("linked", refused[0])
        self.assertIn("symbolic link", refused[0])

    def test_an_unreadable_entry_is_refused_by_name_and_the_rest_still_read(self) -> None:
        import os

        self.install("taste")
        broken = self.install("broken")
        secret = broken / "reference.md"
        secret.write_text("x", encoding="utf-8")
        os.chmod(secret, 0o000)
        self.addCleanup(os.chmod, secret, 0o600)

        bundles, refused = installed_skill_bundles(self.home)

        self.assertEqual([item["name"] for item in bundles], ["taste"])
        self.assertEqual(len(refused), 1)
        self.assertIn("broken", refused[0])

    def test_a_bundle_that_registers_hooks_is_not_offered_from_her_directory(self) -> None:
        """The plugin boundary holds for what she installed too: a bundle
        that registers things is not a skill wherever it sits."""

        self.install("taste")
        sneaky = self.install("sneaky")
        (sneaky / "hooks").mkdir()

        bundles, refused = installed_skill_bundles(self.home)

        self.assertEqual([item["name"] for item in bundles], ["taste"])
        self.assertIn("hooks", refused[0])
