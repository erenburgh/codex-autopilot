"""Rule R21: acceptance runs in a clean environment.

A result reproducible only in the author's environment is not a
confirmation. These tests fail if the suite again starts depending on
variables absent from the declared CI.

History: 260 tests passed with CODEX_THREAD_ID set and gave 31 errors
without it. No test set the variable, and CI runs in a clean
environment. Eight self-accepted tasks did not notice, because each ran
the tests locally.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src" / "codex_autopilot"

# Variables that exist only inside a live Codex session.
SESSION_SCOPED_ENV = ("CODEX_THREAD_ID", "CODEX_TURN_ID", "CODEX_SESSION_ID")

# tests/_relay.py is the sanctioned helper: it exists precisely so
# that identity is passed explicitly, and reads nothing itself.
SANCTIONED_HELPERS = {"_relay.py"}

# The points where production may legitimately read identity from the environment.
# The list is deliberately exact: growth in the number of points must be visible.
# lifecycle.py -> lifecycle_reservations.py: the read moved together
# with reserve_ready_frontier when lifecycle was split into modules.
# The number of places did not change, one file name did.
DECLARED_PRODUCTION_READS = {
    ("cli.py", 'os.environ.get("CODEX_THREAD_ID")'),
    ("lifecycle_reservations.py", 'os.environ.get("CODEX_THREAD_ID")'),
}

# Only the live session's identity. The product's own variables
# (CODEX_AUTOPILOT_*) are unrelated and legitimate.
_ENV_READ = re.compile(
    r"""os\.environ(?:\.get\(|\[)\s*["'](""" + "|".join(SESSION_SCOPED_ENV) + r""")["']"""
)


def _test_sources() -> list[Path]:
    skip = {Path(__file__).name} | SANCTIONED_HELPERS
    return [p for p in sorted(TESTS_DIR.glob("*.py")) if p.name not in skip]


class CleanEnvironmentTests(unittest.TestCase):
    def test_no_test_module_reads_session_scoped_environment(self) -> None:
        offenders = [
            f"{path.name}: {name}"
            for path in _test_sources()
            for name in _ENV_READ.findall(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            offenders,
            [],
            "a test must not read a variable of a live Codex session; "
            "pass identity explicitly, the way tests/_relay.py does",
        )

    def test_frontier_reservation_is_imported_through_the_explicit_helper(self) -> None:
        block_import = re.compile(
            r"from\s+codex_autopilot\.lifecycle\s+import\s+\(([^)]*)\)", re.S
        )
        flat_import = re.compile(
            r"from\s+codex_autopilot\.lifecycle\s+import\s+[^\n(]*reserve_ready_frontier"
        )
        direct: set[str] = set()
        for path in _test_sources():
            text = path.read_text(encoding="utf-8")
            if any("reserve_ready_frontier" in b for b in block_import.findall(text)):
                direct.add(path.name)
            if flat_import.search(text):
                direct.add(path.name)
        self.assertEqual(
            sorted(direct),
            [],
            "reserve_ready_frontier is imported from _relay in tests, "
            "otherwise the owner's identity leaks into os.environ again",
        )

    def test_production_environment_reads_stay_declared(self) -> None:
        found = {
            (path.name, f'os.environ.get("{name}")')
            for path in sorted(SRC_DIR.glob("*.py"))
            for name in _ENV_READ.findall(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(
            found,
            DECLARED_PRODUCTION_READS,
            "the set of places where production reads identity from the "
            "environment has changed; if this is deliberate, update "
            "DECLARED_PRODUCTION_READS and explain the growth",
        )


if __name__ == "__main__":
    unittest.main()
