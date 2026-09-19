from __future__ import annotations

from pathlib import Path
import unittest

from codex_autopilot.models import MODEL_IDS


ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    # The only exemption from the AppleScript ban. The ban protects against
    # the old architecture - driving Codex itself through Accessibility
    # and staged clicks. The system banner has nothing to do with it: it
    # automates nothing and knocks on no application, and there is no
    # other way to show a notification on macOS without an external
    # dependency. A separate test below checks the boundary, so the
    # exemption is by name, not a hole in the list.
    NOTIFICATION_EXEMPT = ROOT / "src/codex_autopilot/notify.py"

    def test_production_has_no_preview_architecture(self):
        production = [ROOT / "src", ROOT / "plugins", ROOT / "README.md", ROOT / "GETTING_STARTED.md", ROOT / "docs"]
        allowed = {".py", ".md", ".json", ".toml", ""}
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for base in production for path in ([base] if base.is_file() else base.rglob("*")) if path.is_file() and path.suffix in allowed and path != self.NOTIFICATION_EXEMPT)
        banned = ["Beyond" + "ness", "ASTRA ROTATION " + "TEST", "NEXT_" + "REASONING", "codex " + "exec", "Apple" + "Script", "Access" + "ibility automation", "self-" + "rotation", "CONTINUE " + "status", "p." + "erenburg", "extra_" + "args", "dangerously-" + "bypass"]
        for token in banned:
            self.assertNotIn(token, text, token)

    def test_the_notification_exemption_automates_nothing(self):
        """The exemption is by name: the banner yes, driving an app no.

        The old architecture drove Codex through Accessibility and staged
        clicks, and the AppleScript ban stands against exactly that. The
        notification automates nothing, so it is taken out from under the
        ban - but only within these bounds, and the bounds are checked
        here, not taken on trust.
        """

        text = self.NOTIFICATION_EXEMPT.read_text(encoding="utf-8")
        self.assertIn("display notification", text)
        for forbidden in (
            "tell application",
            "System Events",
            "keystroke",
            "key code",
            "click at",
            "UI element",
            "accessibility",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_the_notifier_is_off_unless_the_user_turns_it_on(self):
        """A side effect on a person's machine is never turned on silently."""

        from codex_autopilot.config import RuntimeConfig

        self.assertFalse(RuntimeConfig().desktop_notifications)

    def test_the_notifier_passes_text_as_arguments(self):
        """Gluing strings into AppleScript is injection, only when is open."""

        text = self.NOTIFICATION_EXEMPT.read_text(encoding="utf-8")
        self.assertIn("item 1 of argv", text)
        self.assertNotIn('f"display notification', text)

    def test_a_failing_notifier_never_breaks_the_pipeline(self):
        from codex_autopilot.config import RuntimeConfig
        from codex_autopilot.notify import notify

        class Cfg:
            runtime = RuntimeConfig(desktop_notifications=True)

        import codex_autopilot.notify as module
        original = module.subprocess.run
        module.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("нет"))
        try:
            self.assertFalse(notify(Cfg(), "t", "s", "m"))
        finally:
            module.subprocess.run = original

    def test_host_skill_has_no_escalation_contract(self):
        text = (ROOT / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md").read_text()
        self.assertNotIn("ESCALATE", text)
        self.assertNotIn("REQUIRE_COMPUTER_USE", text)

    def test_no_low_reasoning_value_in_production(self):
        files = [ROOT / "src/codex_autopilot/reasoning.py", ROOT / "src/codex_autopilot/models.py", ROOT / "src/codex_autopilot/plan.py"] + list((ROOT / "plugins").rglob("SKILL.md"))
        for path in files:
            self.assertNotIn('"low"', path.read_text(encoding="utf-8"), str(path))

    def test_model_registry_is_exactly_sol_and_astra(self):
        self.assertEqual(MODEL_IDS, {"sol": "gpt-5.6-sol", "astra": "gpt-6-astra"})
        for path in (ROOT / "src/codex_autopilot").glob("*.py"):
            if path.name != "models.py":
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("gpt-5.6-sol", text, str(path))
                self.assertNotIn("gpt-6-astra", text, str(path))


    def test_internal_docs_do_not_ship_to_users(self):
        """The target spec of the next version is a working plan, not docs.

        It lives in the repository for the sake of the run's workers and
        contains commercial positioning. It must not go into the user
        archive, and the release guard caught this once already - by the
        name of a private project inside it.
        """

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_release", ROOT / "scripts/build_release.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn("docs/V1_TARGET.md", module.INTERNAL_DOCS)
        self.assertIn("docs/V1_RUN.md", module.INTERNAL_DOCS)
        # The user's decision of 14 September 2026: the internal documents of
        # the v1.0 line do not live in the public repository. They used to be
        # in git for the workers, to whom every run prompt names them - the
        # price of the decision is that a fresh clone does not get them.
        # Exclusion from the archive and absence from git are different mechanisms, and both are needed.
        import subprocess

        # Only the repository can be asked about git. The installed copy of
        # the runtime is not it: there the archive contents are checked, not history.
        repository = (ROOT / ".git").exists() and (ROOT / ".gitignore").is_file()
        for internal in ("docs/V1_TARGET.md", "docs/V1_RUN.md") if repository else ():
            tracked = subprocess.run(
                ["git", "ls-files", internal],
                cwd=ROOT, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(tracked, "", f"{internal} is under git again")
            ignored = subprocess.run(
                ["git", "check-ignore", internal],
                cwd=ROOT, capture_output=True, text=True,
            ).returncode
            self.assertEqual(
                ignored, 0, f"{internal} is not protected by .gitignore"
            )
        # Records of developing the skill itself: audits of our runs and
        # reports on repairing milestones. A thousand lines of internal history
        # the user downloaded together with the skill.
        for record in (
            "docs/RELEASE_VERIFICATION_0.9.0-beta.md",
            "docs/M11_COMPLETION.md",
            "docs/M11_CONTRACT_CHECKPOINT.md",
            "docs/RELEASE_REPORT_0.8.0-beta.md",
        ):
            with self.subTest(record=record):
                self.assertIn(record, module.INTERNAL_DOCS)
        for internal in module.INTERNAL_DOCS:
            with self.subTest(internal=internal):
                self.assertNotIn(internal.split("/")[-1], module.USER_ITEMS)
        # This used to require every internal document to be in the tree:
        # the specification was in git for the run's workers. That decision
        # is reversed - it does not go to the public repository at all. The
        # test remained and failed in a clean clone: a user who cloned the tag
        # got a red test suite out of nowhere. What is checked now is what
        # was decided: the target specification is not in the public tree.
        # Tracking is checked, not presence: on the developer's machine the
        # file is on disk under .gitignore and does not go to the public
        # repository. The first version of this check looked at the disk and
        # so failed for the very person working with it.
        for secret in ("docs/V1_TARGET.md", "docs/V1_RUN.md"):
            with self.subTest(secret=secret):
                self.assertIn(secret, module.INTERNAL_DOCS)
                tracked = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", secret],
                    cwd=ROOT, capture_output=True, text=True,
                ).returncode
                self.assertNotEqual(
                    tracked,
                    0,
                    f"{secret} must not be tracked by the public repository",
                )

    def test_the_user_archive_ships_what_the_installer_and_the_engineer_need(self):
        """The installer lays the runtime out as a repository-shaped tree.

        The test suite proves behaviour only on such a tree: it needs
        plugins, scripts, pyproject and the documentation, not src alone.
        A user archive without them did not install and was no use to the
        engineer for proving a repair. The patches directory - the state
        of the machine where the repair happened - does not travel in the
        source archive.
        """

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_release_shape", ROOT / "scripts/build_release.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for item in ("tests", "scripts", "build_backend", "pyproject.toml", "plugins"):
            self.assertIn(item, module.USER_ITEMS, item)
        self.assertIn("patches", module.SOURCE_EXCLUDES)
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        for item in ("tests", "scripts", "plugins", "pyproject.toml"):
            self.assertRegex(installer, r"for item in [^\n]*\b" + item.replace(".", r"\.") + r"\b")

    def test_the_approval_answering_harness_stays_out_of_the_user_archive(self):
        """docs/SECURITY.md says it is excluded. It has to be true.

        The live-acceptance harness carries the only flags in this
        repository that can answer an approval. The security document -
        the section whose whole job is to assure a reader there is no
        approval bypass in what they installed - stated that the harness
        is excluded from the macOS user ZIP. It was not: scripts/ ships
        whole, so the file was there, and the reassurance rested on an
        exclusion that never happened.
        """

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_release_dev_only", ROOT / "scripts/build_release.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn("scripts/live_acceptance.py", module.DEV_ONLY)
        security = (ROOT / "docs/SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("excluded from the macOS user ZIP", security)

    def test_rule_provenance_carries_no_private_language(self):
        """A rule says where it came from, not what was said in private.

        The provenance fields held twelve verbatim quotations of the
        owner's own messages, profanity and a dated personal account of a
        bad night among them. Nothing in production reads the field; it is
        read by people. Provenance survives in English and in the third
        person - what was required, and what had happened - and the
        private words do not travel.
        """

        import re

        from codex_autopilot.rules import RULES

        cyrillic = re.compile(r"[\u0400-\u04FF]")
        carried = [item.id for item in RULES if item.source and cyrillic.search(item.source)]
        self.assertEqual(carried, [], "rule provenance must not quote private messages")
        # Depersonalising is not deleting: every rule that said where it
        # came from still says it.
        self.assertEqual(len([item for item in RULES if item.source]), 12)

    def test_run_state_never_reaches_the_source_archive(self):
        """Run state belongs to whoever worked here.

        The release guard used to catch it by absolute paths inside
        plan.json - that is, by the symptom. The cause is what has to be
        caught: the state directory is excluded from the source archive
        whole.
        """

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_release", ROOT / "scripts/build_release.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn(".codex-autopilot", module.SOURCE_EXCLUDES)
        # The source ZIP hangs in the same public release as the
        # user one: internal material is excluded from both.
        source = (ROOT / "scripts/build_release.py").read_text(encoding="utf-8")
        self.assertIn("INTERNAL_DOCS | GENERATED_FILES", source)
        self.assertIn("ROADMAP.md", module.GENERATED_FILES)

    def test_the_repository_ships_no_generated_roadmap(self):
        """The autopilot generates ROADMAP.md in every project itself.

        In the skill's own repository it is a leftover of the run that
        built it: eleven milestones of someone else's work with all their
        DoD.
        """

        import subprocess

        tracked = subprocess.run(
            ["git", "ls-files", "ROADMAP.md"],
            cwd=ROOT, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(tracked, "", "ROADMAP.md is under git again")

    def test_no_separate_model_quota_or_silent_fallback_claim(self):
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for base in (ROOT / "src", ROOT / "plugins", ROOT / "docs", ROOT / "README.md") for path in ([base] if base.is_file() else base.rglob("*")) if path.is_file())
        for phrase in ("Astra quota", "Sol quota", "fallback to Sol", "fallback to Astra"):
            self.assertNotIn(phrase, text)


if __name__ == "__main__": unittest.main()
