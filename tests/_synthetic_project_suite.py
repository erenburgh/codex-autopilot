"""Real one-test suite for lifecycle fixtures with synthetic project roots."""

from __future__ import annotations

import unittest


class SyntheticProjectSuite(unittest.TestCase):
    def test_fixture_suite_runs(self) -> None:
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
