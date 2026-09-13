"""Маршрутизация моделей — напрямую, без мёртвого оркестратора.

Эти свойства проверялись через HeadlessAppServerOrchestrator, снятый в
0.8.1 как недостижимый. Сама resolve_selection живая: её зовёт префлайт
(preflight.py). Проверяется то же самое, но на самой функции.

Ключевое правило, ради которого набор существует: подмены модели нет.
Недоступная модель — это отказ, а не тихий переход на другую.
"""

from __future__ import annotations

import unittest

from codex_autopilot.models import MODEL_IDS, ModelRoutingError, resolve_selection


def catalog(*keys: str) -> list[dict]:
    return [
        {
            "id": MODEL_IDS[key],
            "model": MODEL_IDS[key],
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort} for effort in ("medium", "high", "max")
            ],
        }
        for key in keys
    ]


def choose(strategy: str, mode: str, *available: str):
    return resolve_selection(
        catalog(*(available or ("sol", "astra"))),
        strategy=strategy,
        execution_mode=mode,
        requested_reasoning="medium",
        execution_reason="routing contract test",
    )


class RoutingTableTests(unittest.TestCase):
    def test_auto_sends_code_to_sol(self) -> None:
        self.assertEqual(choose("auto", "code").model_id, MODEL_IDS["sol"])

    def test_auto_sends_computer_use_to_astra(self) -> None:
        self.assertEqual(choose("auto", "computer_use").model_id, MODEL_IDS["astra"])

    def test_astra_only_sends_code_to_astra(self) -> None:
        self.assertEqual(choose("astra-only", "code").model_id, MODEL_IDS["astra"])

    def test_sol_only_sends_code_to_sol(self) -> None:
        self.assertEqual(choose("sol-only", "code").model_id, MODEL_IDS["sol"])


class NoSilentFallbackTests(unittest.TestCase):
    def test_sol_only_refuses_computer_use(self) -> None:
        with self.assertRaises(ModelRoutingError) as caught:
            choose("sol-only", "computer_use")
        self.assertIn("Sol-only", str(caught.exception))

    def test_a_missing_model_is_a_refusal_not_a_substitution(self) -> None:
        with self.assertRaises(ModelRoutingError) as caught:
            choose("auto", "code", "astra")
        text = str(caught.exception)
        self.assertIn(MODEL_IDS["sol"], text)
        self.assertIn("no fallback", text.lower())

    def test_a_renamed_model_is_a_refusal(self) -> None:
        """Реестр ждёт точный идентификатор; чужой — отказ, не подмена."""

        wrong = [{"id": MODEL_IDS["sol"], "model": "gpt-somethingelse"}]
        with self.assertRaises(ModelRoutingError):
            resolve_selection(
                wrong,
                strategy="auto",
                execution_mode="code",
                requested_reasoning="medium",
                execution_reason="routing contract test",
            )


if __name__ == "__main__":
    unittest.main()
