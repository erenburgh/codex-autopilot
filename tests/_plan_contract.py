from __future__ import annotations

from pathlib import Path
import sys
from typing import Any, Sequence


def clean_suite_check() -> dict[str, Any]:
    """Return the canonical full-suite admission check used by test plans.

    Lifecycle tests run against synthetic temporary projects with no on-disk
    suite.  Their complete synthetic suite is therefore the successful direct
    command below.  Production plans name their real repository-wide runner
    (the Autopilot plan uses unittest discovery under ``tests``).
    """

    return {
        "id": "suite",
        "kind": "command",
        "description": "Run the full suite in a clean identity environment.",
        "argv": [
            "env",
            "CODEX_THREAD_ID=",
            "CODEX_TURN_ID=",
            "CODEX_SESSION_ID=",
            sys.executable,
            str(Path(__file__).with_name("_synthetic_project_suite.py")),
        ],
        "timeout_seconds": 600,
        "expected_exit_code": 0,
    }


def canonical_verification(
    *,
    checks: Sequence[dict[str, Any]] = (),
    verifier_role: str | None = None,
    max_revision_attempts: int = 2,
) -> dict[str, Any]:
    verification: dict[str, Any] = {
        "policy": "independent",
        "required": True,
        "deterministic_checks": [clean_suite_check(), *checks],
        "max_revision_attempts": max_revision_attempts,
    }
    if verifier_role:
        verification["verifier_role"] = verifier_role
    return verification
