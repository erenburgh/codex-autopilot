from __future__ import annotations

from pathlib import Path
import unittest

from codex_autopilot.models import MODEL_IDS


ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_production_has_no_preview_architecture(self):
        production = [ROOT / "src", ROOT / "plugins", ROOT / "README.md", ROOT / "GETTING_STARTED.md", ROOT / "docs"]
        allowed = {".py", ".md", ".json", ".toml", ""}
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for base in production for path in ([base] if base.is_file() else base.rglob("*")) if path.is_file() and path.suffix in allowed)
        banned = ["Beyond" + "ness", "ASTRA ROTATION " + "TEST", "NEXT_" + "REASONING", "codex " + "exec", "Apple" + "Script", "Access" + "ibility automation", "self-" + "rotation", "CONTINUE " + "status", "p." + "erenburg", "extra_" + "args", "dangerously-" + "bypass"]
        for token in banned:
            self.assertNotIn(token, text, token)

    def test_host_skill_has_no_escalation_contract(self):
        text = (ROOT / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md").read_text()
        self.assertNotIn("ESCALATE", text)
        self.assertNotIn("REQUIRE_COMPUTER_USE", text)

    def test_no_low_reasoning_value_in_production(self):
        files = list((ROOT / "src").rglob("*.py")) + list((ROOT / "plugins").rglob("SKILL.md"))
        for path in files:
            self.assertNotIn('"low"', path.read_text(encoding="utf-8"), str(path))

    def test_model_registry_is_exactly_sol_and_astra(self):
        self.assertEqual(MODEL_IDS, {"sol": "gpt-5.6-sol", "astra": "gpt-6-astra"})
        for path in (ROOT / "src/codex_autopilot").glob("*.py"):
            if path.name != "models.py":
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("gpt-5.6-sol", text, str(path))
                self.assertNotIn("gpt-6-astra", text, str(path))

    def test_no_separate_model_quota_or_silent_fallback_claim(self):
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for base in (ROOT / "src", ROOT / "plugins", ROOT / "docs", ROOT / "README.md") for path in ([base] if base.is_file() else base.rglob("*")) if path.is_file())
        for phrase in ("Astra quota", "Sol quota", "fallback to Sol", "fallback to Astra"):
            self.assertNotIn(phrase, text)


if __name__ == "__main__": unittest.main()
