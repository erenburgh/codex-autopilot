"""Model routing - directly, without the dead orchestrator.

These properties were checked through HeadlessAppServerOrchestrator,
removed in 0.8.1 as unreachable. resolve_selection itself is alive:
preflight calls it (preflight.py). The same is checked, but on the
function itself.

The key rule the suite exists for: there is no model substitution. An
unavailable model is a refusal, not a silent switch to another.
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
        """The registry wants the exact id; a foreign one is a refusal."""

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
    """Two Astras at once are not allowed under any strategy.

    They share one Computer Use surface, take control away from each
    other and burn through limits. Under the `auto` strategy Astra is
    chosen exactly for execution_mode="computer_use", and the slot held
    that on its own. Under `astra-only` ALL tasks go to Astra, code
    included - and the slot did not hold those.
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
        """This is the hole that left two Astras side by side."""

        self.assertIn("computer_use", self.capabilities("astra-only", "code"))

    def test_sol_only_never_takes_the_surface(self) -> None:
        self.assertNotIn("computer_use", self.capabilities("sol-only", "code"))


class TheWorkerCeilingIsNotTheTemplateDefaultTests(unittest.TestCase):
    """The worker ceiling is the user's decision, not a template number.

    Two was the default and it got into the plan template, from where
    the planner copied it without looking: a graph of 24 tasks with four
    independent branches ran two at a time.
    """

    def test_the_default_allows_real_parallelism(self) -> None:
        from codex_autopilot.plan import DEFAULT_MAX_PARALLEL_WORKERS

        self.assertGreaterEqual(DEFAULT_MAX_PARALLEL_WORKERS, 10)

    def test_the_skill_template_matches_the_default(self) -> None:
        """Otherwise the planner writes the old number back in."""

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
        """A raised ceiling must not let a second Astra through."""

        from codex_autopilot.plan import DEFAULT_COMPUTER_USE_SLOTS

        self.assertEqual(DEFAULT_COMPUTER_USE_SLOTS, 1)
