from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_clean_install_reinstall_and_legacy_replacement(self):
        base = Path(tempfile.mkdtemp(prefix="codex-autopilot-install-"))
        home = base / "home"
        install_root = base / "runtime"
        preserved_v06 = install_root / "0.6.0-beta" / "preserved-marker"
        preserved_v06.parent.mkdir(parents=True)
        preserved_v06.write_text("keep v0.6", encoding="utf-8")
        preserved_v07 = install_root / "0.7.0-beta" / "preserved-marker"
        preserved_v07.parent.mkdir(parents=True)
        preserved_v07.write_text("keep v0.7", encoding="utf-8")
        legacy = home / ".codex/skills/astra-autopilot-adaptive"
        legacy.mkdir(parents=True)
        (legacy / "SKILL.md").write_text("old preview", encoding="utf-8")
        fake_codex = base / "codex"
        calls = base / "codex-calls"
        fake_codex.write_text(f'''#!/bin/sh\necho "$*" >> "{calls}"\ncase "$1 $2" in\n  "app-server --help"|"login status") exit 0 ;;\n  *) exit 0 ;;\nesac\n''', encoding="utf-8")
        fake_codex.chmod(0o755)
        env = os.environ.copy()
        env.update({"HOME": str(home), "CODEX_AUTOPILOT_INSTALL_ROOT": str(install_root), "CODEX_AUTOPILOT_CODEX_BIN": str(fake_codex), "CODEX_AUTOPILOT_PYTHON": os.environ.get("PYTHON", "python3")})
        for _ in range(2):
            result = subprocess.run([str(ROOT / "install.sh"), "--profile", "adaptive"], cwd=ROOT, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((install_root / "current/bin/codex-autopilot").is_file())
        mcp = (install_root / "current/plugins/codex-autopilot-adaptive/.mcp.json").read_text(encoding="utf-8")
        self.assertNotIn("__CODEX_AUTOPILOT_RUNTIME__", mcp)
        self.assertIn(str((install_root / "0.8.0-beta/bin/codex-autopilot").resolve()), mcp)
        self.assertTrue((install_root / "legacy-backups/astra-autopilot-adaptive/SKILL.md").is_file())
        self.assertFalse(legacy.exists())
        command_text = calls.read_text()
        self.assertIn("plugin marketplace add", command_text)
        self.assertIn("plugin add codex-autopilot-adaptive@codex-autopilot-local", command_text)
        self.assertNotIn("config set", command_text)
        self.assertNotIn("danger", command_text)
        env["PATH"] = str(base) + os.pathsep + env.get("PATH", "")
        result = subprocess.run([str(install_root / "current/bin/codex-autopilot"), "uninstall", "--yes"], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((install_root / "0.8.0-beta").exists())
        self.assertFalse((install_root / "current").exists())
        self.assertEqual(preserved_v06.read_text(encoding="utf-8"), "keep v0.6")
        self.assertEqual(preserved_v07.read_text(encoding="utf-8"), "keep v0.7")
        self.assertTrue((install_root / "legacy-backups/astra-autopilot-adaptive/SKILL.md").is_file())


if __name__ == "__main__": unittest.main()
