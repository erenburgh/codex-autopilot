"""Run capacity is computed from the account's real limits.

The number the user declared is their decision and the ceiling. It may
be lowered only when the limit is really near, and only with a named
reason. Someone on auto-billing has nothing to cut: they pay as they go,
and "you get ten" would be impertinence, not care.

The data is real: App Server sends account/rateLimits/updated with
usedPercent, the window length, the unlimited flag, credits and a mark
that the spending limit was reached.
"""

from __future__ import annotations

import unittest

from codex_autopilot.usage import worker_budget


class TheUserNumberIsTheCeilingTests(unittest.TestCase):
    def test_no_data_never_lowers_anything(self) -> None:
        """Lowering the number silently, out of ignorance, is the worst
        of the options."""

        budget = worker_budget(10, None)
        self.assertEqual(budget.workers, 10)
        self.assertFalse(budget.limited)

    def test_unlimited_has_no_ceiling_at_all(self) -> None:
        """There is nothing to cap for someone who pays as they go."""

        budget = worker_budget(
            10, {"credits": {"unlimited": True}, "primary": {"usedPercent": 99}}
        )
        self.assertIsNone(budget.workers)
        self.assertFalse(budget.limited)
        self.assertIn("no ceiling", budget.reason)

    def test_a_number_the_user_named_is_kept_even_on_unlimited(self) -> None:
        """Asked for three means three; unlimited does not cancel that."""

        budget = worker_budget(
            3, {"credits": {"unlimited": True}}, declared_by_user=True
        )
        self.assertEqual(budget.workers, 3)

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

    def test_every_narrowing_names_its_reason(self) -> None:
        budget = self.budget(85)
        self.assertTrue(budget.limited)
        self.assertIn("85", budget.reason)


class AStatedStopIsObeyedTests(unittest.TestCase):
    def test_a_reached_spend_control_drops_to_one(self) -> None:
        """The user set the cap themselves - there is nothing to argue
        with."""

        budget = worker_budget(10, {"spendControlReached": True})
        self.assertEqual(budget.workers, 1)
        self.assertIn("spending cap", budget.reason)

    def test_a_reached_rate_limit_drops_to_one(self) -> None:
        budget = worker_budget(10, {"rateLimitReachedType": "primary"})
        self.assertEqual(budget.workers, 1)

    def test_the_app_server_envelope_is_accepted_as_is(self) -> None:
        """The event arrives wrapped in rateLimits - unwrapping it on the
        calling side would spread the format through the whole code."""

        budget = worker_budget(
            10, {"rateLimits": {"primary": {"usedPercent": 95}, "credits": {}}}
        )
        self.assertEqual(budget.workers, 1)


class TheSchedulerUsesTheBudgetTests(unittest.TestCase):
    def test_the_scheduler_asks_for_a_budget(self) -> None:
        """Otherwise the rule lives in the tests, not in the run - this
        is checked by execution.

        This used to look for the substring ``worker_budget(`` in the
        scheduler source: that stays green under ``if False:`` as well.
        Now the same plan is scheduled twice - once without a limits
        snapshot and once with unlimited - and the worker limit has to
        change. It changed, so the budget really was asked for.
        """

        from codex_autopilot.scheduler import schedule
        from test_scheduler import make_plan, make_state, raw_task

        plan = make_plan([raw_task("A"), raw_task("B"), raw_task("C")], max_workers=2)
        without = schedule(plan, make_state(plan))
        unlimited_state = make_state(plan)
        unlimited_state.rate_limits = {"credits": {"hasCredits": True}}
        unlimited = schedule(plan, unlimited_state)
        self.assertEqual(without.worker_limit, 2)
        self.assertEqual(
            unlimited.worker_limit,
            len(plan.tasks),
            "budget not asked: unlimited did not lift the ceiling",
        )


if __name__ == "__main__":
    unittest.main()


class TheUserIsAskedBeforeTheFirstWorkerTests(unittest.TestCase):
    """The worker count is asked for, not substituted silently.

    The ten from the template stood through a whole run of 24 tasks with
    four independent branches, and nobody had chosen it.
    """

    def notice(self, limits, declared=None):
        from codex_autopilot.usage import capacity_notice

        return capacity_notice(limits, declared)

    def test_unlimited_is_told_it_has_no_ceiling(self) -> None:
        text = self.notice({"credits": {"unlimited": True}})
        self.assertIn("ceiling", text)
        self.assertIn("name a number", text)

    def test_auto_topup_is_told_it_has_no_ceiling(self) -> None:
        """Auto top-up is unlimited - one situation, not two."""

        text = self.notice({"credits": {"hasCredits": True}})
        self.assertIn("ceiling", text)
        self.assertIn("name a number", text)

    def test_a_plan_tier_is_named_as_the_user_knows_it(self) -> None:
        """App Server calls it prolite; the person reads their own plan
        as Pro."""

        text = self.notice({"planType": "prolite", "credits": {}})
        self.assertIn("Pro", text)
        self.assertNotIn("prolite", text)
        self.assertIn("10", text)

    def test_an_unknown_tier_is_not_invented(self) -> None:
        text = self.notice({"planType": "some-new-tier", "credits": {}})
        self.assertNotIn("some-new-tier", text)
        self.assertIn("10", text)

    def test_plus_is_told_a_narrower_default_and_why(self) -> None:
        text = self.notice({"planType": "plus", "credits": {}})
        self.assertIn("Plus", text)
        self.assertIn("3", text)
        self.assertIn("narrow", text)

    def test_a_number_the_user_named_is_confirmed_not_questioned(self) -> None:
        text = self.notice({"planType": "pro", "credits": {}}, 4)
        self.assertIn("4", text)
        self.assertIn("as you specified", text)

    # "Preflight prints the capacity" is checked by running the whole
    # report: test_preflight.test_the_printed_report_runs_end_to_end expects
    # the capacity line in what emit actually printed.

    def test_the_skill_tells_the_model_to_show_it(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for skill in root.glob("plugins/*/skills/*/SKILL.md"):
            with self.subTest(skill=skill.parts[-3]):
                self.assertIn("Capacity:", skill.read_text(encoding="utf-8"))


class AStatedSpendCapOutranksCreditsTests(unittest.TestCase):
    """A cap set by a person outranks auto top-up.

    They set it precisely so that the charging would stop. The check came
    AFTER credits, and so never fired at all for the very people it was
    written for.
    """

    def test_a_reached_cap_stops_even_with_credits(self) -> None:
        budget = worker_budget(
            10, {"credits": {"hasCredits": True}, "spendControlReached": True}
        )
        self.assertEqual(budget.workers, 1)
        self.assertIn("spending cap", budget.reason)

    def test_a_reached_rate_limit_stops_even_with_credits(self) -> None:
        budget = worker_budget(
            10, {"credits": {"unlimited": True}, "rateLimitReachedType": "primary"}
        )
        self.assertEqual(budget.workers, 1)

    def test_credits_without_a_cap_stay_unbounded(self) -> None:
        self.assertIsNone(worker_budget(10, {"credits": {"hasCredits": True}}).workers)


class PlusGetsANarrowerDefaultTests(unittest.TestCase):
    """The Plus window is narrow: ten workers would burn it in one run."""

    def workers(self, plan_type: str) -> int:
        from codex_autopilot.usage import default_workers

        return default_workers({"planType": plan_type})

    def test_plus_defaults_to_three(self) -> None:
        self.assertEqual(self.workers("plus"), 3)

    def test_pro_keeps_ten(self) -> None:
        self.assertEqual(self.workers("prolite"), 10)
        self.assertEqual(self.workers("pro"), 10)

    def test_an_unknown_tier_keeps_ten(self) -> None:
        self.assertEqual(self.workers("some-new-tier"), 10)
