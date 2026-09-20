"""The runtime notices when the model it pins stops being the current one.

`MODEL_IDS` holds exact ids, `resolve_selection` accepts nothing else, and
no fallback is ever applied - deliberately, because silently swapping the
model swaps the declared capability. The consequence nobody had covered:
on the day an id is retired, every installed copy refuses at the same
moment, and `doctor` - whose whole job is to say what is missing - would
have answered PASS right up to it.

So the catalog is compared rather than merely listed. Nothing here ever
changes a model; the verdicts feed messages, and the choice stays the
owner's.
"""

from __future__ import annotations

from pathlib import Path
import unittest

from codex_autopilot.models import MODEL_IDS, ModelRoutingError, catalog_verdicts, resolve_selection


def _catalog(*ids: str) -> list[dict[str, object]]:
    return [
        {
            "id": model_id,
            "supportedReasoningEfforts": [
                {"reasoningEffort": effort}
                for effort in ("medium", "high", "xhigh", "max")
            ],
        }
        for model_id in ids
    ]


class VerdictTests(unittest.TestCase):
    def _for(self, key: str, catalog: list[dict[str, object]]):
        return next(v for v in catalog_verdicts(catalog) if v.key == key)

    def test_today_s_catalog_is_simply_present(self) -> None:
        verdict = self._for("sol", _catalog("gpt-5.6-sol", "gpt-6-astra"))
        self.assertEqual(verdict.state, "present")

    def test_a_newer_sibling_is_named_and_changes_nothing(self) -> None:
        verdict = self._for("sol", _catalog("gpt-5.6-sol", "gpt-6-sol", "gpt-6-astra"))
        self.assertEqual(verdict.state, "superseded")
        self.assertEqual(verdict.newer, "gpt-6-sol")
        self.assertIn("still works", verdict.message)
        self.assertIn("until you say otherwise", verdict.message)

    def test_the_newest_of_several_is_the_one_named(self) -> None:
        verdict = self._for(
            "sol", _catalog("gpt-5.6-sol", "gpt-6-sol", "gpt-7-sol", "gpt-6-astra")
        )
        self.assertEqual(verdict.newer, "gpt-7-sol")

    def test_a_retired_pin_names_what_is_served_instead(self) -> None:
        """R31: a refusal names what IS accepted."""

        verdict = self._for("sol", _catalog("gpt-6-sol", "gpt-6-astra"))
        self.assertEqual(verdict.state, "missing")
        self.assertEqual(verdict.available, ("gpt-6-sol",))
        self.assertIn("gpt-6-sol", verdict.message)
        self.assertIn("Nothing is substituted", verdict.message)

    def test_families_do_not_bleed_into_each_other(self) -> None:
        """A newer Astra must never be offered as a newer Sol."""

        verdict = self._for("sol", _catalog("gpt-5.6-sol", "gpt-9-astra"))
        self.assertEqual(verdict.state, "present")
        self.assertIsNone(verdict.newer)

    def test_an_empty_catalog_reports_missing_and_does_not_raise(self) -> None:
        verdict = self._for("sol", [])
        self.assertEqual(verdict.state, "missing")
        self.assertEqual(verdict.available, ())

    def test_an_unparseable_id_cannot_stop_anything(self) -> None:
        for junk in ([{"id": ""}], [{"id": "sol"}], [{}], [None]):
            with self.subTest(catalog=junk):
                self.assertEqual(len(catalog_verdicts(junk)), len(MODEL_IDS))

    def test_a_pin_is_never_moved_by_the_check(self) -> None:
        before = dict(MODEL_IDS)
        catalog_verdicts(_catalog("gpt-9-sol", "gpt-9-astra"))
        self.assertEqual(MODEL_IDS, before)


class TheRefusalExplainsItselfTests(unittest.TestCase):
    def test_resolve_selection_names_the_alternative_it_will_not_take(self) -> None:
        with self.assertRaises(ModelRoutingError) as caught:
            resolve_selection(
                _catalog("gpt-6-sol"),
                strategy="auto",
                execution_mode="code",
                requested_reasoning="medium",
                execution_reason="Repository files are sufficient.",
            )
        message = str(caught.exception)
        self.assertIn("gpt-5.6-sol", message)
        self.assertIn("gpt-6-sol", message)
        self.assertIn("no fallback", message)

    def test_it_still_refuses_rather_than_substituting(self) -> None:
        """The whole point: naming an alternative is not taking it."""

        with self.assertRaises(ModelRoutingError):
            resolve_selection(
                _catalog("gpt-6-sol", "gpt-6-astra"),
                strategy="sol-only",
                execution_mode="code",
                requested_reasoning="high",
                execution_reason="Repository files are sufficient.",
            )


class ItIsWiredWhereItWillBeSeenTests(unittest.TestCase):
    def _source(self, relative: str) -> str:
        return (
            Path(__file__).resolve().parents[1] / relative
        ).read_text(encoding="utf-8")

    def test_doctor_compares_the_catalog_instead_of_listing_it(self) -> None:
        source = self._source("src/codex_autopilot/cli.py")
        self.assertIn("catalog_verdicts", source)
        self.assertIn("verdict.message", source)

    def test_preflight_warns_before_the_first_worker(self) -> None:
        source = self._source("src/codex_autopilot/preflight.py")
        self.assertIn("catalog_verdicts", source)
        self.assertIn('"superseded"', source)


if __name__ == "__main__":
    unittest.main()
