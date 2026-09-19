"""The user documents promise what the code does.

The same rake as in the skill: text outlives code. The README carried a
"candidate not ready for release" warning with a list of defects removed
back in 0.8.1, GETTING_STARTED promised a 0.8.0-beta install directory
and warned about a start-skill default fixed today. Someone who
downloaded the build would first read that it is not ready.
"""

from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
GETTING_STARTED = ROOT / "GETTING_STARTED.md"
USER_DOCS = (README, GETTING_STARTED)


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


class RemovedConceptsTests(unittest.TestCase):
    """What was taken out of the code must not stay promised in the text."""

    REMOVED = (
        "headless_app_server",
        "not release-ready",
        "detached local dispatcher",
        "add-worker-slot",
    )

    def test_user_docs_do_not_promise_removed_mechanics(self) -> None:
        for path in USER_DOCS:
            text = _flat(path)
            for concept in self.REMOVED:
                with self.subTest(doc=path.name, concept=concept):
                    self.assertNotIn(concept, text)

    def test_no_user_doc_pins_a_stale_install_directory(self) -> None:
        """The install directory is named after the version, and the
        version changes."""

        for path in USER_DOCS:
            with self.subTest(doc=path.name):
                self.assertNotIn("CodexAutopilot/0.8.0-beta", _flat(path))


class ControlsAreRealTests(unittest.TestCase):
    def test_every_control_named_in_the_readme_is_recognised(self) -> None:
        from codex_autopilot.control import (
            PAUSE_PROMPTS,
            RESUME_PROMPTS,
            STATUS_PROMPTS,
            UNINSTALL_PROMPTS,
            _normalized_prompt,
        )

        known = PAUSE_PROMPTS | RESUME_PROMPTS | STATUS_PROMPTS | UNINSTALL_PROMPTS
        for phrase in (
            "status",
            "status detail",
            "Pause Codex Autopilot.",
            "Resume Codex Autopilot.",
            "Uninstall Codex Autopilot.",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(_normalized_prompt(phrase), known)

    def test_the_readme_explains_the_blocked_label(self) -> None:
        """Otherwise the normal answer of the hook reads as a breakdown."""

        self.assertIn("the hook replied instead of the model", _flat(README))


class MeasuredClaimsTests(unittest.TestCase):
    def test_the_readme_separates_live_evidence_from_open_items(self) -> None:
        text = _flat(README)
        self.assertIn("Verified live, not only by tests", text)
        self.assertIn("Not verified live and openly outstanding", text)

    def test_the_readme_states_the_sidebar_limit_with_its_cause(self) -> None:
        """A limit with no reason given will be called a defect in a month."""

        text = _flat(README)
        self.assertIn("separate process", text)
        self.assertIn("desktop_notifications", text)

    def test_getting_started_tells_how_to_watch_a_run(self) -> None:
        text = _flat(GETTING_STARTED)
        self.assertIn("status detail", text)
        self.assertIn("desktop_notifications = true", text)
        self.assertIn("brief", text)


if __name__ == "__main__":
    unittest.main()


class OneSentenceInstallTests(unittest.TestCase):
    """Installation is one phrase from the user, not a list of steps.

    A person opens their own project in Codex and says: download and
    install this skill, then start work on the project. Codex does
    everything else. The directory is not chosen: the target is the
    project the person is in, because every task created is placed in
    it.
    """

    def test_both_docs_lead_with_the_sentence(self) -> None:
        for path in USER_DOCS:
            with self.subTest(doc=path.name):
                text = _flat(path)
                self.assertIn("Download and install this skill", text)
                self.assertIn("start working on this project with it", text)

    def test_the_docs_do_not_ask_the_user_to_pick_a_directory(self) -> None:
        for path in USER_DOCS:
            with self.subTest(doc=path.name):
                text = _flat(path)
                self.assertIn("the project you are in", text)

    def test_the_trust_steps_are_named_as_codex_own(self) -> None:
        """They cannot be removed, but they can be named and asked about
        in advance."""

        text = _flat(README) + " " + _flat(GETTING_STARTED)
        self.assertIn("Autopilot never answers them for you", text)
        self.assertIn("never in the middle", text)

    def test_a_projectless_directory_is_refused_up_front(self) -> None:
        source = (ROOT / "src/codex_autopilot/preflight.py").read_text(encoding="utf-8")
        self.assertIn("belongs to no Codex project", source)
