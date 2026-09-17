"""Control phrases: Russian dictation must not break a command."""

from __future__ import annotations

import unittest

from codex_autopilot.control import (
    PAUSE_PROMPTS,
    RESUME_PROMPTS,
    STATUS_PROMPTS,
    UNINSTALL_PROMPTS,
    _normalized_prompt,
)


class ProductSpellingTests(unittest.TestCase):
    """Название продукта произносят и латиницей, и кириллицей."""

    def resumes(self, text: str) -> bool:
        return _normalized_prompt(text) in RESUME_PROMPTS

    def test_latin_product_name_resumes(self) -> None:
        self.assertTrue(self.resumes("продолжи codex autopilot"))

    def test_cyrillic_product_name_resumes(self) -> None:
        """Голосовой ввод по-русски даёт кириллицу, и это не ошибка."""

        self.assertTrue(self.resumes("продолжи кодекс автопайлот"))
        self.assertTrue(self.resumes("продолжи кодекс автопилот"))

    def test_english_verbs_still_resume(self) -> None:
        self.assertTrue(self.resumes("resume codex autopilot"))
        self.assertTrue(self.resumes("continue codex autopilot"))

    def test_punctuation_from_dictation_is_ignored(self) -> None:
        self.assertTrue(self.resumes("продолжить кодекс, автопайлот"))
        self.assertTrue(self.resumes("продолжи кодекс автопилот."))

    def test_a_leading_filler_word_is_ignored(self) -> None:
        self.assertTrue(self.resumes("Просто продолжи кодекс автопайлот"))
        self.assertTrue(self.resumes("Давай, продолжи кодекс автопилот!"))

    def test_every_command_family_accepts_both_spellings(self) -> None:
        for prompts, latin, cyrillic in (
            (PAUSE_PROMPTS, "приостанови codex autopilot", "приостанови кодекс автопайлот"),
            (STATUS_PROMPTS, "статус codex autopilot", "статус кодекс автопайлот"),
            (UNINSTALL_PROMPTS, "удали codex autopilot", "удали кодекс автопайлот"),
        ):
            with self.subTest(latin=latin):
                self.assertIn(_normalized_prompt(latin), prompts)
                self.assertIn(_normalized_prompt(cyrillic), prompts)


class StrictnessTests(unittest.TestCase):
    """Сопоставление остаётся точным: хук не перехватывает обычные просьбы."""

    def test_an_ordinary_request_is_not_a_control_phrase(self) -> None:
        for text in (
            "продолжи работу над задачей",
            "продолжи codex autopilot и покажи диф",
            "расскажи про codex autopilot",
            "codex autopilot",
        ):
            with self.subTest(text=text):
                self.assertNotIn(_normalized_prompt(text), RESUME_PROMPTS)

    def test_a_filler_in_the_middle_does_not_match(self) -> None:
        self.assertNotIn(
            _normalized_prompt("продолжи просто кодекс автопайлот"), RESUME_PROMPTS
        )

    def test_command_families_do_not_overlap(self) -> None:
        families = [RESUME_PROMPTS, PAUSE_PROMPTS, STATUS_PROMPTS, UNINSTALL_PROMPTS]
        for index, left in enumerate(families):
            for right in families[index + 1 :]:
                self.assertEqual(left & right, set())


if __name__ == "__main__":
    unittest.main()
