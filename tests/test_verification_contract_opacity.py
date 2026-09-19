"""R31 for the verifier's verdict: a closed schema must be named.

Measured on the clean Thread Tools run. Verifier M4 returned issues with
the fields finding, requirement, required_fix, severity, id,
evidence_ids - all six plausible, none of them ever named to it. The
prompt asked for "a non-empty array of structured issues" and stopped
there. The parser rejected the whole verdict, the turn stopped, a ticket
opened.

The same defect as the opaque memory contract: the schema exists, the
knowledge of it does not.
"""

from __future__ import annotations

from pathlib import Path
import unittest


ALLOWED = ("code", "summary", "details", "dod_refs")


class VerifierIssueContractTests(unittest.TestCase):
    def test_the_refusal_names_every_allowed_field(self) -> None:
        from codex_autopilot.verification import (
            VerificationIssue,
            VerificationProtocolError,
        )

        with self.assertRaises(VerificationProtocolError) as caught:
            VerificationIssue.from_dict(
                {
                    "id": "I-1",
                    "finding": "нет сухого прогона",
                    "requirement": "DoD 3",
                    "required_fix": "печатать список",
                    "severity": "high",
                    "evidence_ids": ["EVID-001"],
                }
            )
        message = str(caught.exception)
        for field in ALLOWED:
            with self.subTest(field=field):
                self.assertIn(field, message)

    def test_the_prompt_names_the_shape_before_the_first_attempt(self) -> None:
        """Naming the fields in the refusal is not enough: the verifier's
        turn has already ended."""

        source = (
            Path(__file__).resolve().parents[1] / "src/codex_autopilot/ai_studio.py"
        ).read_text(encoding="utf-8")
        for field in ALLOWED:
            with self.subTest(field=field):
                self.assertIn(field, source)
        self.assertIn("Any extra field rejects the whole verdict", source)
        self.assertIn("Лишнее поле отвергает весь вердикт целиком", source)

    def test_the_named_shape_actually_parses(self) -> None:
        """What the prompt promises must pass the parser."""

        from codex_autopilot.verification import VerificationIssue

        issue = VerificationIssue.from_dict(
            {
                "code": "missing-dry-run",
                "summary": "archive без --yes ничего не печатает",
                "details": "Запуск вывел пустую строку вместо списка веток",
                "dod_refs": [3],
            }
        )
        self.assertEqual(issue.code, "missing-dry-run")
        self.assertEqual(issue.dod_refs, (3,))


if __name__ == "__main__":
    unittest.main()
