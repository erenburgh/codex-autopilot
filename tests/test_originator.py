"""The thread is created by the same originator the application itself runs as.

A thread created under a foreign originator belongs to "another
application": it is visible in the sidebar but requires a manual
takeover by button. The pipeline cannot press that button, so the work
stops.

The value is taken from Codex Desktop itself: there
CODEX_INTERNAL_ORIGINATOR_OVERRIDE defaults to "Codex Desktop".
"""

from __future__ import annotations

import unittest
from unittest import mock

from codex_autopilot.appserver import DESKTOP_ORIGINATOR, AppServerClient


class OriginatorTests(unittest.TestCase):
    def env_of_spawned_client(self, **kwargs) -> dict:
        captured: dict = {}

        def popen(command, **popen_kwargs):
            captured.update(popen_kwargs.get("env") or {})
            raise RuntimeError("дальше запускать нечего: нужен только env")

        client = AppServerClient(
            "codex", mock.Mock(), popen_factory=popen, **kwargs
        )
        with self.assertRaises(RuntimeError):
            client.connect()
        return captured

    def test_the_default_originator_is_the_desktop_one(self) -> None:
        self.assertEqual(DESKTOP_ORIGINATOR, "Codex Desktop")

    def test_a_client_created_without_arguments_uses_it(self) -> None:
        """Раньше по умолчанию originator не ставился вовсе, и ветка
        оказывалась чужой."""

        env = self.env_of_spawned_client()
        self.assertEqual(
            env.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE"), DESKTOP_ORIGINATOR
        )

    def test_an_explicit_originator_still_wins(self) -> None:
        env = self.env_of_spawned_client(originator="Something Else")
        self.assertEqual(
            env.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE"), "Something Else"
        )

    def test_no_call_site_uses_the_old_private_originator(self) -> None:
        """codex_work_desktop не совпадал с originator приложения."""

        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "src" / "codex_autopilot"
        offenders = [
            path.name
            for path in source.glob("*.py")
            if "codex_work_desktop" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
