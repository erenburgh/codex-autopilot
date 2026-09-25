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
        _attach_test_lead(payload, tasks)
    return payload


TEST_LEAD_ROLE = {
    "id": "acceptance-lead",
    "name": "Acceptance Lead",
    "responsibilities": ["Accept the department's work against its versioned rubric."],
}


def _attach_test_lead(payload: dict[str, Any], tasks: list[Any]) -> None:
    """R30: every task names its department's lead; one lead per profession.

    Tests unrelated to departments get one shared lead for every profession
    that names none - the shape of a real plan (the art run: three roles, one
    art-reviewer). ``canonical_verification`` names it by default; a
    profession whose other tasks name a lead of their own takes that one.
    """

    shared = TEST_LEAD_ROLE["id"]
    named: dict[str, str] = {}
    for task in tasks:
        if isinstance(task, dict) and isinstance(task.get("verification"), dict):
            lead = task["verification"].get("verifier_role")
            if lead and lead != shared:
                named.setdefault(str(task.get("role")), lead)
    attached = False
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("verification"), dict):
            continue
        verification = task["verification"]
        lead = verification.get("verifier_role")
        if (not lead and verification.get("policy") == "independent") or lead == shared:
            verification["verifier_role"] = named.get(str(task.get("role"))) or shared
        attached = attached or verification.get("verifier_role") == shared
    roles = payload.get("roles")
    if attached and isinstance(roles, list) and not any(
        isinstance(role, dict) and role.get("id") == shared for role in roles
    ):
        roles.append(dict(TEST_LEAD_ROLE))


def attested_verdict(cfg: Any, task_id: str, verdict: str = "PASS", issues: Sequence[Any] = (), *, prefix: str = "") -> str:
    """A verifier's final line carrying the exact rubric attestation (R30).

    Every acceptance is a lead's by its department's current rubric, and a
    verdict without the attestation is refused - the form the runtime asks
    of a real lead, read from the same place the lead's prompt is built from.
    """

    from codex_autopilot.config import load_config
    from codex_autopilot.department_runtime import load_task_department_acceptance, settled_task_ids
    from codex_autopilot.memory import ProjectMemory
    from codex_autopilot.plan import load_plan
    from codex_autopilot.run_state import StateStore

    if isinstance(cfg, (str, Path)):
        cfg = load_config(Path(cfg))
    plan = load_plan(cfg.state_dir, cfg.profile)
    settled = settled_task_ids(StateStore(cfg.state_dir).load().task_states)
    loaded = load_task_department_acceptance(
        ProjectMemory(cfg.root), plan, plan.task_map[task_id], ensure=True, settled=settled
    )
    payload = {
        "verdict": verdict,
        "issues": [dict(item) for item in issues],
        "rubric": loaded.department.rubric.to_dict(),
    }
    return prefix + "AUTOPILOT_VERIFICATION: " + json.dumps(payload, separators=(",", ":"))


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
    if isinstance(raw, dict) and raw.get("schema_version") == 3 and isinstance(raw.get("tasks"), list):
        # A test may append tasks after canonicalize_plan: they get the lead too.
        before = json.dumps(raw, sort_keys=True)
        _attach_test_lead(raw, raw["tasks"])
        if json.dumps(raw, sort_keys=True) != before:
            Path(plan_file).write_text(json.dumps(raw), encoding="utf-8")
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
    # Hiring is ON in the product. It is pinned OFF here because these
    # fixtures build projects for tests about something else - wake-ups,
    # rate limits, resource locks, verification - and a screening session
    # before every task would make each of them depend on a default they
    # are not testing. Tests that are about hiring pass the mode they mean;
    # the shipped default is asserted directly in test_skill_screening.
    kwargs.setdefault("skill_screening", "never")
    return initialize_project(
        root,
        plan_file,
        plan_verification=receipt,
        **kwargs,
    )


def clean_suite_check() -> dict[str, Any]:
    """Return the canonical full-suite admission check used by test plans.

    Lifecycle tests run against synthetic temporary projects with no on-disk
    suite.  Their own full suite lies next to them, in
    ``_synthetic_suite/tests``, and is declared by the real discovery command.
    Before, this was a run of a file with the word ``suite`` in its name - and
    that was taken for the whole repository: a name is not proof.  Production
    plans name their real repository-wide runner (the Autopilot plan uses
    unittest discovery under ``tests``).
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
    # R30: every task names its department's lead; canonicalize_plan and
    # initialize_verified_project add the shared test lead role it names.
    verification["verifier_role"] = verifier_role or TEST_LEAD_ROLE["id"]
    return verification
