"""Ёмкость прогона считается по реальным лимитам аккаунта.

Заявленное пользователем число - его решение и потолок. Понижать его
можно только когда лимит действительно рядом, и только с названной
причиной. Человеку с автосписанием урезать нечего: он платит по факту,
и «тебе положено десять» было бы наглостью, а не заботой.

Данные настоящие: App Server шлёт account/rateLimits/updated с
usedPercent, длиной окна, признаком безлимита, кредитами и отметкой о
достигнутом пределе расходов.
"""

from __future__ import annotations

import unittest

from codex_autopilot.usage import worker_budget


class TheUserNumberIsTheCeilingTests(unittest.TestCase):
    def test_no_data_never_lowers_anything(self) -> None:
        """Молчаливое понижение по незнанию - худший вариант."""

        budget = worker_budget(10, None)
        self.assertEqual(budget.workers, 10)
        self.assertFalse(budget.limited)

    def test_unlimited_is_never_throttled(self) -> None:
        budget = worker_budget(
            10, {"credits": {"unlimited": True}, "primary": {"usedPercent": 99}}
        )
        self.assertEqual(budget.workers, 10)
        self.assertFalse(budget.limited)
        self.assertIn("безлимит", budget.reason)

    def test_the_budget_never_exceeds_what_was_asked(self) -> None:
        budget = worker_budget(3, {"primary": {"usedPercent": 0}, "credits": {}})
        self.assertEqual(budget.workers, 3)


class ItNarrowsOnlyWhenTheLimitIsNearTests(unittest.TestCase):
    def budget(self, used: int, declared: int = 10, **credits):
        return worker_budget(
            declared, {"primary": {"usedPercent": used}, "credits": credits}
        )

    def test_plenty_of_headroom_keeps_everything(self) -> None:
        self.assertEqual(self.budget(28).workers, 10)

    def test_half_spent_halves_the_workers(self) -> None:
        self.assertEqual(self.budget(70).workers, 5)

    def test_nearly_spent_leaves_a_pair(self) -> None:
        self.assertEqual(self.budget(85).workers, 2)

    def test_almost_gone_leaves_one(self) -> None:
        self.assertEqual(self.budget(95).workers, 1)

    def test_credits_soften_the_narrowing(self) -> None:
        """Списание продолжится за окном - запас считается щедрее."""

        self.assertEqual(self.budget(85, hasCredits=True).workers, 5)

    def test_every_narrowing_names_its_reason(self) -> None:
        budget = self.budget(85)
        self.assertTrue(budget.limited)
        self.assertIn("85", budget.reason)


class AStatedStopIsObeyedTests(unittest.TestCase):
    def test_a_reached_spend_control_drops_to_one(self) -> None:
        """Предел поставил сам пользователь - спорить не с чем."""

        budget = worker_budget(10, {"spendControlReached": True})
        self.assertEqual(budget.workers, 1)
        self.assertIn("предел расходов", budget.reason)

    def test_a_reached_rate_limit_drops_to_one(self) -> None:
        budget = worker_budget(10, {"rateLimitReachedType": "primary"})
        self.assertEqual(budget.workers, 1)

    def test_the_app_server_envelope_is_accepted_as_is(self) -> None:
        """Событие приходит завёрнутым в rateLimits - разворачивать его
        на вызывающей стороне значило бы разложить формат по всему коду."""

        budget = worker_budget(
            10, {"rateLimits": {"primary": {"usedPercent": 95}, "credits": {}}}
        )
        self.assertEqual(budget.workers, 1)


class TheSchedulerUsesTheBudgetTests(unittest.TestCase):
    def test_the_scheduler_asks_for_a_budget(self) -> None:
        """Иначе правило живёт в тестах, а не в прогоне."""

        import inspect

        from codex_autopilot import scheduler

        self.assertIn("worker_budget(", inspect.getsource(scheduler))


if __name__ == "__main__":
    unittest.main()
