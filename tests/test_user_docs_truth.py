"""Документы пользователя обещают то, что код делает.

Это те же грабли, что и в скилле: текст переживает код. README нёс
предупреждение «кандидат не готов к выпуску» с перечнем дефектов,
снятых ещё в 0.8.1, GETTING_STARTED обещал каталог установки
0.8.0-beta и предупреждал об умолчании start-skill, исправленном
сегодня. Человек, скачавший сборку, первым делом читал бы, что она
не готова.
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
    """Снятое из кода не должно оставаться обещанным в тексте."""

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
        """Каталог установки называется версией, а версия меняется."""

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
        """Иначе штатный ответ хука читается как поломка."""

        self.assertIn("the hook replied instead of the model", _flat(README))


class MeasuredClaimsTests(unittest.TestCase):
    def test_the_readme_separates_live_evidence_from_open_items(self) -> None:
        text = _flat(README)
        self.assertIn("Verified live, not only by tests", text)
        self.assertIn("Not verified live and openly outstanding", text)

    def test_the_readme_states_the_sidebar_limit_with_its_cause(self) -> None:
        """Ограничение без причины через месяц объявят дефектом."""

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
