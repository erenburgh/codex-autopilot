"""The memory tool does not ask permission on every turn.

The manifest declared `approval_mode: "auto"`. No such value exists in
the Codex enumeration - the official codex-app-tools plugin writes
`approve` where no asking is needed and `prompt` where it is. An
unrecognized value fell back to prompting, and permission was required
anew: after every update, and inside a run on every fresh worker,
because a worker is fresh by construction.

The price was measured on a live run: the replanner's turn hit the
prompt, the dispatcher does not answer approvals, the turn stayed
interrupted forever, the on-call engineer could not start because of the
dead predecessor, and a 24-task run stood with zero done.
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest


PLUGINS = Path(__file__).resolve().parent.parent / "plugins"
# The values Codex understands for a plugin MCP server tool.
ACCEPTED = {"approve", "prompt", "writes"}


class MemoryToolApprovalTests(unittest.TestCase):
    def manifests(self):
        found = sorted(PLUGINS.glob("*/.mcp.json"))
        self.assertTrue(found, "no MCP manifests were found")
        for path in found:
            yield path, json.loads(path.read_text(encoding="utf-8"))

    def test_every_declared_mode_is_one_codex_understands(self) -> None:
        for path, data in self.manifests():
            for name, server in data["mcpServers"].items():
                default = server.get("default_tools_approval_mode")
                if default is not None:
                    self.assertIn(default, ACCEPTED, f"{path.parent.name}/{name}")
                for tool, config in (server.get("tools") or {}).items():
                    mode = config.get("approval_mode")
                    if mode is not None:
                        self.assertIn(
                            mode, ACCEPTED, f"{path.parent.name}/{name}/{tool}"
                        )

    def test_the_memory_tool_is_approved_without_asking(self) -> None:
        """Otherwise the run walks into a human on every fresh worker."""

        for path, data in self.manifests():
            server = data["mcpServers"]["codex_autopilot_memory"]
            self.assertEqual(
                server["tools"]["memory"]["approval_mode"],
                "approve",
                path.parent.name,
            )
            self.assertEqual(
                server["default_tools_approval_mode"], "approve", path.parent.name
            )


if __name__ == "__main__":
    unittest.main()


class PluginCacheConsistencyTests(unittest.TestCase):
    """Codex has to load the same copy of the plugin that is installed.

    It reads the plugin from its own cache, and the runtime reads it from
    the install directory. While the previous copy stayed in the cache, it
    was the one loaded: the user had 0.9.7 installed and 0.9.0 running -
    with Interrupt declared at 30 seconds. Codex clamps it to 3, rewrites
    the file, the hash changes, and Stop-hook trust drops on every load.
    From the outside this is indistinguishable from "the hooks drop by
    themselves", and trusting them again does not cure it.
    """

    def setUp(self) -> None:
        import tempfile
        from codex_autopilot import preflight

        self.preflight = preflight
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)

    def _plugin(self, root: Path, name: str, version: str) -> Path:
        target = root / ".codex-plugin"
        target.mkdir(parents=True, exist_ok=True)
        (target / "plugin.json").write_text(
            json.dumps({"name": name, "version": version}), encoding="utf-8"
        )
        return root

    def _cache(self, name: str, *versions: str) -> None:
        for version in versions:
            self._plugin(
                self.home / "plugins/cache/codex-autopilot-local" / name / version,
                name,
                version,
            )

    def state(self, installed_version: str):
        import os
        from unittest import mock

        plugin_root = self._plugin(
            self.home / "install" / "plugin", "codex-autopilot-adaptive", installed_version
        )
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)}):
            return self.preflight.plugin_cache_state(plugin_root)

    # The versions here are synthetic and deliberately differ from the real one:
    # the rule is checked, not the release number.
    INSTALLED = "7.7.7-beta.local.20260101.000000"

    def test_one_matching_copy_passes(self) -> None:
        self._cache("codex-autopilot-adaptive", self.INSTALLED)
        status, detail = self.state(self.INSTALLED)
        self.assertEqual(status, "OK", detail)

    def test_an_older_copy_in_the_cache_fails(self) -> None:
        self._cache("codex-autopilot-adaptive", "1.0.0-beta")
        status, detail = self.state(self.INSTALLED)
        self.assertEqual(status, "FAIL", detail)
        self.assertIn("1.0.0-beta", detail)

    def test_two_copies_fail_even_when_the_first_one_matches(self) -> None:
        """A spare copy is dangerous in itself: Codex chooses it, our code
        does not.
        """

        self._cache("codex-autopilot-adaptive", self.INSTALLED, "9.9.9-beta")
        status, detail = self.state(self.INSTALLED)
        self.assertEqual(status, "FAIL", detail)

    def test_a_source_tree_is_not_compared_with_a_machine_cache(self) -> None:
        """Without the installer mark this is a source tree, not an install."""

        self._cache("codex-autopilot-adaptive", "1.0.0-beta")
        status, _ = self.state("7.7.7-beta")
        self.assertEqual(status, "WARN")
