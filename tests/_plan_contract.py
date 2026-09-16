from __future__ import annotations

from pathlib import Path
import json
import sys
from typing import Any, Sequence

from codex_autopilot.plan_verification import (
    INITIAL_PLAN_VERIFICATION,
    PlanVerificationReceipt,
    PlanVerificationVerdict,
    make_plan_verification_receipt,
)


TEST_OUTCOME_ID = "test-outcome"


def canonical_goal_contract() -> dict[str, Any]:
    """Small structured Goal Contract for tests unrelated to goal semantics."""

    return {
        "required_outcomes": [
            {
                "id": TEST_OUTCOME_ID,
                "description": "The test plan's declared result is produced.",
            }
        ],
        "deliverables": [
            {
                "id": "test-deliverable",
                "description": "The test plan's bounded deliverable exists.",
            }
        ],
        "constraints": [],
        "global_acceptance": [
            {
                "id": "test-accepted",
                "description": "The test plan is accepted under its declared checks.",
            }
        ],
    }


def canonicalize_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the v1 goal fields to a canonical schema-3 test plan in place."""

    if payload.get("schema_version") != 3:
        return payload
    payload.setdefault("goal_contract", canonical_goal_contract())
    tasks = payload.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict):
                task.setdefault("produces_outcomes", [TEST_OUTCOME_ID])
                task.setdefault("acceptance_class", "mixed")
    return payload


def canonical_plan_verification(plan: Any, *, recorded: bool = True) -> dict[str, Any]:
    """Independent-verifier receipt fixture bound to the exact test graph."""

    receipt = make_plan_verification_receipt(
        plan,
        PlanVerificationVerdict("PASS"),
        mode=INITIAL_PLAN_VERIFICATION,
        verifier_thread_id="test-plan-verifier-thread",
        verifier_turn_id="test-plan-verifier-turn",
        verified_at="2026-09-16T00:00:00+00:00",
    )
    if not recorded:
        return receipt.to_dict()
    return PlanVerificationReceipt(
        schema_version=receipt.schema_version,
        status=receipt.status,
        graph_version=receipt.graph_version,
        plan_sha256=receipt.plan_sha256,
        mode=receipt.mode,
        verdict=receipt.verdict,
        verifier_role=receipt.verifier_role,
        verifier_thread_id=receipt.verifier_thread_id,
        verifier_turn_id=receipt.verifier_turn_id,
        verified_at=receipt.verified_at,
        evidence_ids=("EVID-TEST-PLAN",),
        verification_result_id="VERIFY-TEST-PLAN",
    ).to_dict()


def initialize_verified_project(
    root: Path,
    plan_file: Path,
    **kwargs: Any,
) -> Any:
    """Test adapter: model the fresh preflight verifier before bootstrap."""

    from codex_autopilot.bootstrap import initialize_project
    from codex_autopilot.plan import validate_migrating_plan

    raw = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    receipt = None
    if isinstance(raw, dict) and raw.get("goal_contract") is not None:
        state_dir = Path(root).resolve() / ".codex-autopilot"
        state_path = state_dir / "run-state.json"
        existing = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.is_file()
            else None
        )
        plan = validate_migrating_plan(
            raw,
            str(kwargs.get("profile", "adaptive")),
            state_dir=state_dir,
            state_payload=existing,
        )
        receipt = canonical_plan_verification(plan, recorded=False)
    return initialize_project(
        root,
        plan_file,
        plan_verification=receipt,
        **kwargs,
    )


def clean_suite_check() -> dict[str, Any]:
    """Return the canonical full-suite admission check used by test plans.

    Lifecycle tests run against synthetic temporary projects with no on-disk
    suite.  Их полный набор лежит рядом, в ``_synthetic_suite/tests``, и
    объявляется настоящей командой обнаружения.  Прежде здесь стоял запуск
    файла с словом ``suite`` в имени - и это принималось за весь
    репозиторий: имя не доказательство.  Production plans name their real
    repository-wide runner (the Autopilot plan uses unittest discovery under
    ``tests``).
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
            "-m",
            "unittest",
            "discover",
            "-s",
            str(Path(__file__).with_name("_synthetic_suite") / "tests"),
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
