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
