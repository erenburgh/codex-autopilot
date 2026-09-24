"""A verdict the runtime refuses teaches the next lead what to return.

Two doors led to the same class of failure, and both now reach the one
recorder (``_reject_verifier_result``): an unreadable verdict, and a verdict
that parses but does not attest its department's rubric. The second door
used to raise straight to WorkerProtocolError, recording nothing: the next
verifier was told nothing, repeated it, and its turn was interrupted again -
measured three times on one live run (twice on M6, once on M11A).

That was when `rubric` belonged to no task. Now every acceptance is a lead's
(R30) and the verdict carries three fields; the note the recorder reaches
said "exactly two", which would have told the lead to drop the very field
the runtime refuses it without.
"""

from __future__ import annotations

import json
import unittest

from _departments import DepartmentRun


class TheNoteNamesTheThreeFieldsTests(DepartmentRun):
    def test_the_next_lead_is_told_the_three_fields_and_the_exact_attestation(self) -> None:
        """Mutation: the note back to "exactly two top-level fields"."""

        verifier = self.implement(self.reserve()[0], "worker-M01").descriptors[0]
        outcome = self.judge(verifier, "lead-1", 'AUTOPILOT_VERIFICATION: {"verdict":"PASS","issues":[]}')
        fresh = outcome.descriptors[0]
        note = fresh.prompt[fresh.prompt.rindex("The previous verdict was rejected by the runtime"):]
        self.assertIn('exactly three top-level fields: "verdict", "issues" and "rubric"', note)
        self.assertNotIn("exactly two", note)
        attestation = json.loads(self.verdict("M01").split("AUTOPILOT_VERIFICATION: ", 1)[1])["rubric"]
        self.assertIn('"rubric":' + json.dumps(attestation, separators=(",", ":")), note)

    def test_a_wrong_title_is_still_the_runtimes_error(self) -> None:
        """Only the lead's own inventable mistake is recorded; a runtime fault raises."""

        from codex_autopilot.lifecycle_base import WorkerProtocolError

        verifier = self.implement(self.reserve()[0], "worker-M01").descriptors[0]
        state = self.store.load()
        session = next(item for item in state.worker_sessions if item["reservation_token"] == verifier.reservation_token)
        session["descriptor"]["title"] = "Somebody | Verify M01 | Model part M01"
        self.store.save(state)
        with self.assertRaisesRegex(WorkerProtocolError, "does not identify the pinned Lead Role"):
            self.judge(verifier, "lead-1", self.verdict("M01"))


if __name__ == "__main__":
    unittest.main()
