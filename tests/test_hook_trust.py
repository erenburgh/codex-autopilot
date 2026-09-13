from __future__ import annotations

from pathlib import Path
import unittest
from unittest import mock

from codex_autopilot.hook_trust import (
    HookPreflightError,
    HookTrustApprovalRequired,
    installed_runtime_path,
    require_trusted_stop_hook,
    runtime_hook_command,
)


PLUGIN_ID = "codex-autopilot-adaptive@codex-autopilot-local"
ROOT = Path("/project").resolve()
COMMAND = runtime_hook_command(
    Path("/opt/CodexAutopilot/0.8.1-beta/bin/codex-autopilot")
)


class HookClient:
    def __init__(self, hooks=None, *, errors=None):
        self.hooks = hooks if hooks is not None else [self.hook()]
        self.errors = errors or []

    @staticmethod
    def hook(**updates):
        value = {
            "eventName": "stop",
            "handlerType": "command",
            "command": COMMAND,
            "pluginId": PLUGIN_ID,
            "enabled": True,
            "trustStatus": "trusted",
            "currentHash": "sha256:trusted",
        }
        value.update(updates)
        return value

    def list_hooks(self, cwd):
        return [{"cwd": str(cwd), "hooks": self.hooks, "warnings": [], "errors": self.errors}]


class HookTrustTests(unittest.TestCase):
    def gate(self, client):
        return require_trusted_stop_hook(
            client,
            ROOT,
            plugin_id=PLUGIN_ID,
            expected_command=COMMAND,
        )

    def test_trusted_exact_hook_passes(self):
        snapshot = self.gate(HookClient())
        self.assertEqual(snapshot.trust_status, "trusted")
        self.assertEqual(snapshot.command, COMMAND)

    def test_managed_exact_hook_passes(self):
        snapshot = self.gate(HookClient([HookClient.hook(trustStatus="managed")]))
        self.assertEqual(snapshot.trust_status, "managed")

    def test_modified_and_untrusted_require_explicit_approval(self):
        for status in ("modified", "untrusted"):
            with self.subTest(status=status):
                with self.assertRaises(HookTrustApprovalRequired) as caught:
                    self.gate(HookClient([HookClient.hook(trustStatus=status)]))
                self.assertIn("APPROVAL REQUIRED", str(caught.exception))

    def test_missing_hook_is_preflight_failure(self):
        with self.assertRaisesRegex(HookPreflightError, "is missing"):
            self.gate(HookClient([]))

    def test_disabled_hook_is_preflight_failure(self):
        with self.assertRaisesRegex(HookPreflightError, "is disabled"):
            self.gate(HookClient([HookClient.hook(enabled=False)]))

    def test_inventory_error_is_preflight_failure(self):
        with self.assertRaisesRegex(HookPreflightError, "reported errors"):
            self.gate(HookClient(errors=["bad hooks file"]))

    def test_cache_path_command_is_rejected_even_if_trusted(self):
        cache_command = '"/tmp/plugins/cache/version/hooks/codex-autopilot-hook"'
        with self.assertRaisesRegex(HookPreflightError, "stable installed runtime"):
            self.gate(HookClient([HookClient.hook(command=cache_command)]))

    def test_installed_runtime_keeps_current_symlink_unresolved(self):
        install_root = Path("/opt/CodexAutopilot")
        with mock.patch.dict(
            "os.environ",
            {
                "CODEX_AUTOPILOT_INSTALL_ROOT": str(install_root),
                "CODEX_AUTOPILOT_RUNTIME": "",
            },
        ):
            self.assertEqual(
                installed_runtime_path(),
                install_root / "current/bin/codex-autopilot",
            )


if __name__ == "__main__":
    unittest.main()
