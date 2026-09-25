"""Desktop's own filing rule, repeated - and the placement check that reads it (R5).

The check this replaces asked App Server for projectId and called a thread
INSIDE when it matched. Desktop files threads by its own rule (assignment,
then projectless, then a cwd EQUAL to a project root); every staged worker
of the art run had the right projectId, a cwd below the root, and was
in no project. The independent check then corrected the first copy of the
rule: Desktop folds case and does not strip a trailing '/', keys a project
by its aliases too, and lets an assignment to an unknown project fall
through to the cwd.
"""

from __future__ import annotations

from pathlib import Path
import plistlib
import tempfile
import unittest

from _desktop_state import desktop_home
from codex_autopilot.desktop_sidebar import (
    INSIDE,
    OUTSIDE,
    UNOBSERVABLE,
    desktop_version,
    observe,
)

ROOT = "/Users/owner/Developer/game"
STAGED = ROOT + "/.codex-autopilot/staged-artifacts/M01/workspace"


class DesktopRuleTests(unittest.TestCase):
    def test_a_root_is_in_the_project_and_its_staged_subfolder_is_not(self) -> None:
        """Mutation: compare by prefix (``startswith``) - the staged cwd reads INSIDE."""

        home = desktop_home(roots=["/Users/owner/Documents/Game", ROOT])
        self.assertEqual(observe("t1", ROOT, "desktop-project", home).placement, INSIDE)
        staged = observe("t1", STAGED, "desktop-project", home)
        self.assertEqual((staged.placement, staged.rule), (OUTSIDE, "none"))

    def test_case_is_folded_as_desktop_folds_it(self) -> None:
        """A real pair on her machine: cwd .../game, root .../Game.

        Mutation: ``normalize`` without ``.lower()`` - a false OUTSIDE.
        """

        home = desktop_home(roots=["/Users/owner/Documents/Game"])
        placed = observe("t1", "/Users/owner/Documents/game", "desktop-project", home)
        self.assertEqual((placed.placement, placed.rule), (INSIDE, "exact_root"))

    def test_a_trailing_slash_is_not_stripped(self) -> None:
        """Desktop keys '/a/b/' and '/a/b' apart; the first copy pinned the opposite.

        Mutation: ``normalize`` with ``.rstrip('/')`` - INSIDE here.
        """

        home = desktop_home(roots=[ROOT + "/"])
        self.assertEqual(observe("t1", ROOT, "desktop-project", home).placement, OUTSIDE)

    def test_an_assignment_decides_before_the_cwd(self) -> None:
        """Mutation: consult the cwd before the assignment - the second case reads INSIDE."""

        home = desktop_home(
            roots=[ROOT],
            others={"other": {"id": "other", "rootPaths": ["/elsewhere"]}},
            assignments={
                "mine": {"projectKind": "local", "projectId": "desktop-project"},
                "theirs": {"projectKind": "local", "projectId": "other"},
            },
        )
        mine = observe("mine", "/anywhere/at/all", "desktop-project", home)
        self.assertEqual((mine.placement, mine.rule), (INSIDE, "assignment"))
        theirs = observe("theirs", ROOT, "desktop-project", home)
        self.assertEqual((theirs.placement, theirs.project), (OUTSIDE, "other"))

    def test_an_assignment_to_a_project_desktop_does_not_know_falls_through(self) -> None:
        """Mutation: an unknown assigned project decides OUTSIDE - the root reads OUTSIDE."""

        home = desktop_home(roots=[ROOT], assignments={"t1": {"projectKind": "local", "projectId": "gone"}})
        placed = observe("t1", ROOT, "desktop-project", home)
        self.assertEqual((placed.placement, placed.rule), (INSIDE, "exact_root"))

    def test_chatgpt_and_projectless_assignments_are_outside(self) -> None:
        home = desktop_home(
            roots=[ROOT],
            assignments={
                "chat": {"projectOrigin": "chatgpt", "projectId": "desktop-project"},
                "loose": {"workspaceKind": "projectless", "projectId": "desktop-project"},
            },
        )
        self.assertEqual(observe("chat", ROOT, "desktop-project", home).placement, OUTSIDE)
        self.assertEqual(observe("loose", ROOT, "desktop-project", home).placement, OUTSIDE)

    def test_a_projectless_thread_at_the_root_is_outside(self) -> None:
        """Mutation: drop the projectless branch - INSIDE by the cwd."""

        home = desktop_home(roots=[ROOT], projectless=["t1"])
        placed = observe("t1", ROOT, "desktop-project", home)
        self.assertEqual((placed.placement, placed.rule), (OUTSIDE, "projectless"))

    def test_aliases_are_keys_of_the_project(self) -> None:
        """Mutation: key a project by ``rootPaths`` alone - both read OUTSIDE."""

        home = desktop_home(roots=["/real"], project={"rootPathAliases": ["/alias"], "pathAlias": "/other-alias"})
        self.assertEqual(observe("t1", "/alias", "desktop-project", home).placement, INSIDE)
        self.assertEqual(observe("t1", "/other-alias", "desktop-project", home).placement, INSIDE)

    def test_no_state_or_no_project_is_unobservable_never_inside(self) -> None:
        """Mutation: an unreadable state defaults to INSIDE."""

        missing = Path(tempfile.mkdtemp(prefix="codex-autopilot-no-desktop-"))
        self.assertEqual(observe("t1", ROOT, "desktop-project", missing).placement, UNOBSERVABLE)
        home = desktop_home(roots=[ROOT])
        self.assertEqual(observe("t1", ROOT, "someone-else", home).placement, UNOBSERVABLE)
        self.assertEqual(observe("t1", ROOT, None, home).placement, UNOBSERVABLE)

    def test_the_desktop_version_is_read_from_the_bundle(self) -> None:
        app = Path(tempfile.mkdtemp(prefix="codex-autopilot-app-")) / "ChatGPT.app"
        (app / "Contents").mkdir(parents=True)
        with (app / "Contents" / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleShortVersionString": "26.917.62051"}, handle)
        self.assertEqual(desktop_version(app), "26.917.62051")
        self.assertIsNone(desktop_version(app.parent / "Missing.app"))


class HonestPlacementTests(unittest.TestCase):
    """launch_gate.measure_placement: one thread/read, two facts, kept apart."""

    class Client:
        def __init__(self, thread, home):
            self.thread, self.codex_home = thread, str(home)

        def read_thread(self, thread_id):
            return dict(self.thread)

    def test_project_id_set_with_a_staged_cwd_is_outside(self) -> None:
        """The bug itself: projectId matches, cwd a subfolder, no assignment.

        Mutation: ``measure_placement`` returning INSIDE on projectId alone.
        """

        from codex_autopilot.launch_gate import OUTSIDE as GATE_OUTSIDE, measure_placement

        home = desktop_home(roots=[ROOT])
        client = self.Client({"id": "t1", "projectId": "p1", "cwd": STAGED}, home)
        placement, observation = measure_placement(client, "t1", "p1", "desktop-project")
        self.assertEqual(placement, GATE_OUTSIDE)
        self.assertTrue(observation["app_server_project_id_ok"])
        self.assertEqual(observation["desktop_rule"], "none")
        self.assertEqual(observation["cwd"], STAGED)
        self.assertIn("desktop_version", observation)

    def test_inside_needs_both_facts(self) -> None:
        from codex_autopilot.launch_gate import INSIDE as GATE_INSIDE, OUTSIDE as GATE_OUTSIDE, measure_placement

        home = desktop_home(roots=[ROOT])
        both = self.Client({"id": "t1", "projectId": "p1", "cwd": ROOT}, home)
        self.assertEqual(measure_placement(both, "t1", "p1", "desktop-project")[0], GATE_INSIDE)
        wrong = self.Client({"id": "t1", "projectId": "p2", "cwd": ROOT}, home)
        placement, observation = measure_placement(wrong, "t1", "p1", "desktop-project")
        self.assertEqual(placement, GATE_OUTSIDE)
        self.assertFalse(observation["app_server_project_id_ok"])
        self.assertEqual(observation["desktop_placement"], INSIDE)

    def test_an_unreadable_desktop_is_unobservable(self) -> None:
        from codex_autopilot.launch_gate import UNOBSERVABLE as GATE_UNOBSERVABLE, measure_placement

        missing = Path(tempfile.mkdtemp(prefix="codex-autopilot-no-desktop-"))
        client = self.Client({"id": "t1", "projectId": "p1", "cwd": ROOT}, missing)
        self.assertEqual(measure_placement(client, "t1", "p1", "desktop-project")[0], GATE_UNOBSERVABLE)


if __name__ == "__main__":
    unittest.main()
