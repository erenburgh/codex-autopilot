from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from _plan_contract import canonicalize_plan
from test_ai_studio import role, task
from codex_autopilot.plan import plan_to_dict, save_plan, validate_plan
from codex_autopilot.role_specification import ROLE_SPECIFICATIONS_FILE


def role_plan(*, version: str = "1.0.0", responsibility: str = "Own Builder results."):
    profile = role("builder", "Builder")
    profile["version"] = version
    profile["responsibilities"] = [responsibility]
    reviewer = role("reviewer", "Runtime Engineering Lead")
    reviewer["version"] = "1.0.0"
    return validate_plan(
        canonicalize_plan(
            {
                "schema_version": 3,
                "goal": "Derive a project organization and reuse its professions.",
                "user_request": "Hire the required project professions once and reuse them.",
                "model_strategy": "auto",
                "roles": [profile, reviewer],
                "tasks": [task("M1", "builder", verifier_role="reviewer")],
            }
        ),
        "adaptive",
    )


class RoleSpecificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex-autopilot-roles-")
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / ".codex-autopilot"

    def registry(self) -> dict:
        return json.loads(
            (self.state_dir / ROLE_SPECIFICATIONS_FILE).read_text(encoding="utf-8")
        )

    def test_first_hire_creates_a_versioned_profile_and_next_save_reuses_it(self) -> None:
        plan = role_plan()
        save_plan(self.state_dir, plan)
        first = self.registry()

        save_plan(self.state_dir, plan)

        self.assertEqual(self.registry(), first)
        builder = first["profiles"]["builder"]
        self.assertEqual(builder["current_version"], "1.0.0")
        self.assertEqual(len(builder["revisions"]), 1)
        self.assertEqual(builder["revisions"][0]["profile"]["name"], "Builder")
        self.assertRegex(builder["revisions"][0]["sha256"], r"^[0-9a-f]{64}$")

    def test_same_version_cannot_silently_change_the_profession(self) -> None:
        original = role_plan()
        save_plan(self.state_dir, original)
        plan_before = (self.state_dir / "plan.json").read_text(encoding="utf-8")

        changed = role_plan(responsibility="Do different work under the old revision.")
        with self.assertRaisesRegex(ValueError, "immutable.*increment the role version"):
            save_plan(self.state_dir, changed)

        self.assertEqual(
            (self.state_dir / "plan.json").read_text(encoding="utf-8"),
            plan_before,
        )
        self.assertEqual(
            self.registry()["profiles"]["builder"]["current_version"], "1.0.0"
        )

    def test_newer_revision_preserves_history_and_becomes_current(self) -> None:
        save_plan(self.state_dir, role_plan())
        revised = role_plan(
            version="1.1.0",
            responsibility="Own Builder results for this project's release pipeline.",
        )
        save_plan(self.state_dir, revised)

        builder = self.registry()["profiles"]["builder"]
        self.assertEqual(builder["current_version"], "1.1.0")
        self.assertEqual(
            [item["version"] for item in builder["revisions"]],
            ["1.0.0", "1.1.0"],
        )
        self.assertEqual(plan_to_dict(revised)["roles"][0]["version"], "1.1.0")

    def test_invalid_role_version_is_rejected_at_plan_validation(self) -> None:
        raw = canonicalize_plan(
            {
                "schema_version": 3,
                "goal": "Reject an unversioned profession revision.",
                "user_request": "Use exact semantic versions.",
                "model_strategy": "auto",
                "roles": [
                    {
                        **role("builder", "Builder"),
                        "version": "latest",
                    },
                    role("reviewer", "Runtime Engineering Lead"),
                ],
                "tasks": [task("M1", "builder", verifier_role="reviewer")],
            }
        )
        with self.assertRaisesRegex(ValueError, "role 1.version must be a semantic version"):
            validate_plan(raw, "adaptive")

    def test_numeric_prerelease_identifiers_cannot_have_leading_zeroes(self) -> None:
        raw = canonicalize_plan(
            {
                "schema_version": 3,
                "goal": "Keep profession revisions valid SemVer.",
                "user_request": "Reject ambiguous profession revision numbers.",
                "model_strategy": "auto",
                "roles": [
                    {
                        **role("builder", "Builder"),
                        "version": "1.0.0-01",
                    },
                    role("reviewer", "Runtime Engineering Lead"),
                ],
                "tasks": [task("M1", "builder", verifier_role="reviewer")],
            }
        )
        with self.assertRaisesRegex(ValueError, "role 1.version must be a semantic version"):
            validate_plan(raw, "adaptive")


if __name__ == "__main__":
    unittest.main()
