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


class OnlyOneAstraAtATimeTests(unittest.TestCase):
    """Две Астры одновременно недопустимы при любой стратегии.

    Они делят одну поверхность Computer Use, перехватывают управление
    друг у друга и жгут лимиты. При стратегии `auto` Астра выбирается
    ровно для execution_mode="computer_use", и слот держал это сам. При
    `astra-only` на Астру уходят ВСЕ задачи, включая code, - и слот их
    не удерживал.
    """

    def capabilities(self, strategy: str, mode: str) -> tuple[str, ...]:
        from types import SimpleNamespace

        from codex_autopilot.scheduler import _task_capabilities

        task = SimpleNamespace(
            required_capabilities=(), execution_mode=mode, id="T1"
        )
        plan = SimpleNamespace(model_strategy=strategy)
        return _task_capabilities(task, plan)

    def test_computer_use_always_takes_the_surface(self) -> None:
        self.assertIn("computer_use", self.capabilities("auto", "computer_use"))

    def test_a_code_task_on_auto_does_not(self) -> None:
        self.assertNotIn("computer_use", self.capabilities("auto", "code"))

    def test_a_code_task_on_astra_only_takes_the_surface(self) -> None:
        """Именно эта дыра и оставляла две Астры рядом."""

        self.assertIn("computer_use", self.capabilities("astra-only", "code"))

    def test_sol_only_never_takes_the_surface(self) -> None:
        self.assertNotIn("computer_use", self.capabilities("sol-only", "code"))


class TheWorkerCeilingIsNotTheTemplateDefaultTests(unittest.TestCase):
    """Потолок воркеров - решение пользователя, а не число из шаблона.

    Двойка стояла умолчанием и попадала в шаблон плана, откуда
    планировщик копировал её не глядя: граф из 24 задач с четырьмя
    независимыми ветками исполнялся по две.
    """

    def test_the_default_allows_real_parallelism(self) -> None:
        from codex_autopilot.plan import DEFAULT_MAX_PARALLEL_WORKERS

        self.assertGreaterEqual(DEFAULT_MAX_PARALLEL_WORKERS, 10)

    def test_the_skill_template_matches_the_default(self) -> None:
        """Иначе планировщик снова впишет старое число."""

        import json
        from pathlib import Path

        from codex_autopilot.plan import DEFAULT_MAX_PARALLEL_WORKERS

        root = Path(__file__).resolve().parent.parent
        for skill in root.glob("plugins/*/skills/*/SKILL.md"):
            text = skill.read_text(encoding="utf-8")
            if '"max_parallel_workers"' not in text:
                continue
            with self.subTest(skill=skill.parts[-3]):
                self.assertIn(
                    f'"max_parallel_workers":{DEFAULT_MAX_PARALLEL_WORKERS}', text
                )

    def test_the_computer_use_slot_stays_at_one(self) -> None:
        """Поднятый потолок не должен пускать вторую Астру."""

        from codex_autopilot.plan import DEFAULT_COMPUTER_USE_SLOTS

        self.assertEqual(DEFAULT_COMPUTER_USE_SLOTS, 1)
