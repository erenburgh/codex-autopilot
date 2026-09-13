from __future__ import annotations

from pathlib import Path
import unittest

from codex_autopilot.models import MODEL_IDS


ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    # Единственное исключение из запрета на AppleScript. Запрет защищает
    # от прежней архитектуры - управления самим Codex через Accessibility
    # и подставные клики. Системный банер к ней отношения не имеет: он
    # ничего не автоматизирует и ни в какое приложение не стучится, а
    # другого способа показать уведомление на macOS без внешней
    # зависимости нет. Границу проверяет отдельный тест ниже, поэтому
    # исключение именное, а не дыра в списке.
    NOTIFICATION_EXEMPT = ROOT / "src/codex_autopilot/notify.py"

    def test_production_has_no_preview_architecture(self):
        production = [ROOT / "src", ROOT / "plugins", ROOT / "README.md", ROOT / "GETTING_STARTED.md", ROOT / "docs"]
        allowed = {".py", ".md", ".json", ".toml", ""}
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for base in production for path in ([base] if base.is_file() else base.rglob("*")) if path.is_file() and path.suffix in allowed and path != self.NOTIFICATION_EXEMPT)
        banned = ["Beyond" + "ness", "ASTRA ROTATION " + "TEST", "NEXT_" + "REASONING", "codex " + "exec", "Apple" + "Script", "Access" + "ibility automation", "self-" + "rotation", "CONTINUE " + "status", "p." + "erenburg", "extra_" + "args", "dangerously-" + "bypass"]
        for token in banned:
            self.assertNotIn(token, text, token)

    def test_the_notification_exemption_automates_nothing(self):
        """Исключение именное: банер разрешён, управление приложением - нет.

        Прежняя архитектура водила Codex через Accessibility и подставные
        клики, и запрет на AppleScript стоит именно против неё. Уведомление
        ничего не автоматизирует, поэтому оно из-под запрета выведено - но
        ровно в этих границах, и границы проверяются здесь, а не на слово.
        """

        text = self.NOTIFICATION_EXEMPT.read_text(encoding="utf-8")
        self.assertIn("display notification", text)
        for forbidden in (
            "tell application",
            "System Events",
            "keystroke",
            "key code",
            "click at",
            "UI element",
            "accessibility",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_the_notifier_is_off_unless_the_user_turns_it_on(self):
        """Побочный эффект на машине человека молча не включают."""

        from codex_autopilot.config import RuntimeConfig

        self.assertFalse(RuntimeConfig().desktop_notifications)

    def test_the_notifier_passes_text_as_arguments(self):
        """Склейка строк в AppleScript - инъекция, вопрос только когда."""

        text = self.NOTIFICATION_EXEMPT.read_text(encoding="utf-8")
        self.assertIn("item 1 of argv", text)
        self.assertNotIn('f"display notification', text)

    def test_a_failing_notifier_never_breaks_the_pipeline(self):
        from codex_autopilot.config import RuntimeConfig
        from codex_autopilot.notify import notify

        class Cfg:
            runtime = RuntimeConfig(desktop_notifications=True)

        import codex_autopilot.notify as module
        original = module.subprocess.run
        module.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(OSError("нет"))
        try:
            self.assertFalse(notify(Cfg(), "t", "s", "m"))
        finally:
            module.subprocess.run = original

    def test_host_skill_has_no_escalation_contract(self):
        text = (ROOT / "plugins/codex-autopilot-host-settings/skills/codex-autopilot-host-settings/SKILL.md").read_text()
        self.assertNotIn("ESCALATE", text)
        self.assertNotIn("REQUIRE_COMPUTER_USE", text)

    def test_no_low_reasoning_value_in_production(self):
        files = [ROOT / "src/codex_autopilot/reasoning.py", ROOT / "src/codex_autopilot/models.py", ROOT / "src/codex_autopilot/plan.py"] + list((ROOT / "plugins").rglob("SKILL.md"))
        for path in files:
            self.assertNotIn('"low"', path.read_text(encoding="utf-8"), str(path))

    def test_model_registry_is_exactly_sol_and_astra(self):
        self.assertEqual(MODEL_IDS, {"sol": "gpt-5.6-sol", "astra": "gpt-6-astra"})
        for path in (ROOT / "src/codex_autopilot").glob("*.py"):
            if path.name != "models.py":
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("gpt-5.6-sol", text, str(path))
                self.assertNotIn("gpt-6-astra", text, str(path))


    def test_internal_docs_do_not_ship_to_users(self):
        """Целевая спецификация следующей версии - рабочий план, не документация.

        Она живёт в репозитории ради воркеров прогона и содержит
        коммерческое позиционирование. В пользовательский архив ей
        нельзя, и защита релиза это уже поймала однажды - по имени
        частного проекта внутри.
        """

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "build_release", ROOT / "scripts/build_release.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn("docs/V1_TARGET.md", module.INTERNAL_DOCS)
        for internal in module.INTERNAL_DOCS:
            with self.subTest(internal=internal):
                self.assertTrue((ROOT / internal).is_file(), internal)
                self.assertNotIn(internal.split("/")[-1], module.USER_ITEMS)
    def test_no_separate_model_quota_or_silent_fallback_claim(self):
        text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for base in (ROOT / "src", ROOT / "plugins", ROOT / "docs", ROOT / "README.md") for path in ([base] if base.is_file() else base.rglob("*")) if path.is_file())
        for phrase in ("Astra quota", "Sol quota", "fallback to Sol", "fallback to Astra"):
            self.assertNotIn(phrase, text)


if __name__ == "__main__": unittest.main()
