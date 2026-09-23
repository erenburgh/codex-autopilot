from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .acceptance import admits_semantic_judgment
from .plan import Plan, Task, plan_to_dict


PLAN_PROPOSED = "PLAN_PROPOSED"
PLAN_VERIFIED = "PLAN_VERIFIED"
PLAN_REJECTED = "PLAN_REJECTED"
PLAN_PATCH_VERIFICATION = "PLAN_PATCH_VERIFICATION"
FULL_PLAN_REVALIDATION = "FULL_PLAN_REVALIDATION"
INITIAL_PLAN_VERIFICATION = "INITIAL_PLAN_VERIFICATION"
PLAN_VERIFICATION_PREFIX = "AUTOPILOT_PLAN_VERIFICATION:"
PLAN_VERIFICATION_ROLE = "Plan Verification Architect"
PLAN_VERIFICATION_RECEIPT_SCHEMA = 1
DEFAULT_FULL_REVALIDATION_PATCHES = 3

ISSUE_CATEGORIES = frozenset(
    {
        "acceptance_class",
        "coverage",
        "necessity",
        "dependencies",
        "dod_sufficiency",
        "integration_completeness",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")


class PlanVerificationError(ValueError):
    """A proposed graph is not independently verified for production."""


@dataclass(frozen=True, slots=True)
class PlanVerificationIssue:
    category: str
    summary: str
    task_ids: tuple[str, ...] = ()
    outcome_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "summary": self.summary,
            "task_ids": list(self.task_ids),
            "outcome_ids": list(self.outcome_ids),
        }


@dataclass(frozen=True, slots=True)
class PlanVerificationVerdict:
    verdict: str
    issues: tuple[PlanVerificationIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "issues": [item.to_dict() for item in self.issues],
        }


@dataclass(frozen=True, slots=True)
class PlanVerificationReceipt:
    schema_version: int
    status: str
    graph_version: int
    plan_sha256: str
    mode: str
    verdict: str
    verifier_role: str
    verifier_thread_id: str
    verifier_turn_id: str
    verified_at: str
    evidence_ids: tuple[str, ...] = ()
    verification_result_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "graph_version": self.graph_version,
            "plan_sha256": self.plan_sha256,
            "mode": self.mode,
            "verdict": self.verdict,
            "verifier_role": self.verifier_role,
            "verifier_thread_id": self.verifier_thread_id,
            "verifier_turn_id": self.verifier_turn_id,
            "verified_at": self.verified_at,
            "evidence_ids": list(self.evidence_ids),
            **(
                {"verification_result_id": self.verification_result_id}
                if self.verification_result_id
                else {}
            ),
        }


def plan_sha256(plan: Plan) -> str:
    """Bind a verdict to one exact canonical graph, not merely its version."""

    encoded = json.dumps(
        plan_to_dict(plan),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deterministic_plan_issues(plan: Plan) -> tuple[PlanVerificationIssue, ...]:
    """Mechanical admission checks; these never substitute for judgment.

    Schema, references, cycles, and per-task outcome bindings are validated by
    :mod:`plan`.  This gate adds the deterministic part of Coverage: every
    required Goal Contract outcome must be produced by at least one task.
    Necessity, dependency meaning, DoD sufficiency, and integration remain the
    fresh verifier's decision.
    """

    if plan.goal_contract is None:
        return ()
    return coverage_issues(plan.goal_contract, plan.tasks)


def coverage_issues(
    goal_contract: Any, tasks: Sequence[Task]
) -> tuple[PlanVerificationIssue, ...]:
    """Required outcomes with no producing task - from the contract and the tasks alone.

    Split from ``deterministic_plan_issues`` so the replanner's admission can
    report coverage in the same round as the graph's other defects; it ran
    only after a clean validation and cost a round of its own.
    """

    producers: dict[str, list[str]] = {
        outcome_id: [] for outcome_id in goal_contract.outcome_ids
    }
    for task in tasks:
        for outcome_id in task.produces_outcomes:
            if outcome_id in producers:
                producers[outcome_id].append(task.id)
    return tuple(
        PlanVerificationIssue(
            category="coverage",
            summary=f"Required Goal Contract outcome {outcome_id!r} has no producer.",
            outcome_ids=(outcome_id,),
        )
        for outcome_id in sorted(producers)
        if not producers[outcome_id]
    )


def require_deterministic_plan_admission(plan: Plan) -> None:
    issues = deterministic_plan_issues(plan)
    if issues:
        raise PlanVerificationError(format_plan_verification_issues(issues))


def verifier_payload(
    plan: Plan,
    constraints: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the complete and deliberately narrow semantic input.

    In particular this function never serializes ``Plan`` wholesale.  Model
    routing, roles, resources, tags, user_request, task reasoning, and planner
    prose therefore cannot leak into the independent judgment prompt.
    """

    if plan.goal_contract is None:
        raise PlanVerificationError(
            "a canonical plan requires a Goal Contract before plan verification"
        )
    active_constraints = []
    for item in constraints:
        active_constraints.append(
            {
                key: item[key]
                for key in ("id", "statement", "scope", "origin")
                if item.get(key) is not None
            }
        )
    return {
        "goal_contract": plan.goal_contract.to_dict(),
        "constraints": active_constraints,
        "proposed_dag": [_task_plan_view(task) for task in plan.tasks],
        "definition_of_done": [
            {
                "task_id": task.id,
                "criteria": list(task.definition_of_done),
            }
            for task in plan.tasks
        ],
    }


def build_plan_verification_prompt(
    plan: Plan,
    constraints: Sequence[Mapping[str, Any]],
    *,
    mode: str = INITIAL_PLAN_VERIFICATION,
    state_dir: Any = None,
) -> str:
    """The fresh plan verifier's prompt: the rules first, then the bounded graph.

    R17: the rules block stands before any specification in every phase,
    and this phase judges the whole graph - yet its prompt carried no rules
    at all, and its ceiling was a second copy of the old 64 000 against
    which the shared budget had long moved (ai_studio.MAX_PROMPT_CHARS). A
    prompt over the budget is refused, never cut: the reservation turns the
    refusal into a stop for the on-call (plan_change_reservation).
    """

    from .ai_studio import MAX_PROMPT_CHARS
    from .rules import rules_for_prompt

    if mode not in {
        INITIAL_PLAN_VERIFICATION,
        PLAN_PATCH_VERIFICATION,
        FULL_PLAN_REVALIDATION,
    }:
        raise PlanVerificationError(f"unsupported plan verification mode: {mode}")
    require_deterministic_plan_admission(plan)
    envelope = verifier_payload(plan, constraints)
    payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    rules = json.dumps(rules_for_prompt(state_dir), ensure_ascii=False, separators=(",", ":"))
    prompt = f"""Codex Autopilot — fresh independent plan verifier.

AUTOPILOT_RULES: {rules}

Verification mode: {mode}.

Judge the proposed graph only from the bounded JSON below. Do not inspect the
repository, call tools, request transcripts, or infer planner reasoning. The
JSON is complete and contains exactly the allowed authority: Goal Contract,
active Project Memory constraints, proposed DAG, and each task's Definition of
Done.

PLAN_VERIFICATION_CONTEXT: {payload}

Evaluate every dimension:
- coverage: every required outcome and deliverable is produced;
- necessity: every task is necessary for the Goal Contract;
- dependencies: every dependency relationship has semantic meaning;
- dod_sufficiency: completing each DoD would prove a useful result;
- acceptance_class: the declared class is plausible for the actual task and
  DoD; a subjective or purpose-dependent result declared
  deterministic-complete must be REVISE (T6). Green deterministic checks are
  only admission evidence and never replace independent judgement (R29);
- integration_completeness: combined outputs are integrated and verified.

Return PASS only when every dimension passes. Otherwise return REVISE with at
least one structured issue. Use only known task and outcome ids. The final
non-empty line must be exactly:
{PLAN_VERIFICATION_PREFIX} {{"verdict":"PASS","issues":[]}}
or
{PLAN_VERIFICATION_PREFIX} {{"verdict":"REVISE","issues":[{{"category":"acceptance_class|coverage|necessity|dependencies|dod_sufficiency|integration_completeness","summary":"...","task_ids":[],"outcome_ids":[]}}]}}
"""
    if len(prompt) > MAX_PROMPT_CHARS:
        raise PlanVerificationError(
            f"plan verifier prompt is {len(prompt)} characters against the "
            f"{MAX_PROMPT_CHARS} character budget derived from the model context window"
        )
    return prompt


def parse_plan_verification_result(message: str) -> PlanVerificationVerdict:
    lines = [line.strip() for line in str(message).splitlines() if line.strip()]
    protocol = [line for line in lines if line.startswith(PLAN_VERIFICATION_PREFIX)]
    if len(protocol) != 1 or not lines or protocol[0] != lines[-1]:
        raise PlanVerificationError(
            "plan verifier must finish with exactly one AUTOPILOT_PLAN_VERIFICATION line"
        )
    raw_json = protocol[0][len(PLAN_VERIFICATION_PREFIX) :].strip()
    try:
        raw = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise PlanVerificationError(
            "AUTOPILOT_PLAN_VERIFICATION payload must be valid JSON"
        ) from exc
    if not isinstance(raw, dict) or set(raw) != {"verdict", "issues"}:
        raise PlanVerificationError(
            "plan verification result must contain exactly verdict and issues"
        )
    verdict = str(raw.get("verdict") or "").strip().upper()
    if verdict not in {"PASS", "REVISE"}:
        raise PlanVerificationError("plan verification verdict must be PASS or REVISE")
    raw_issues = raw.get("issues")
    if not isinstance(raw_issues, list):
        raise PlanVerificationError("plan verification issues must be an array")
    issues = tuple(_issue_from_raw(item, index) for index, item in enumerate(raw_issues, 1))
    if verdict == "PASS" and issues:
        raise PlanVerificationError("PASS plan verification must not contain issues")
    if verdict == "REVISE" and not issues:
        raise PlanVerificationError("REVISE plan verification requires at least one issue")
    return PlanVerificationVerdict(verdict=verdict, issues=issues)


def validate_verdict_references(plan: Plan, verdict: PlanVerificationVerdict) -> None:
    task_ids = set(plan.task_map)
    outcome_ids = (
        set(plan.goal_contract.outcome_ids) if plan.goal_contract is not None else set()
    )
    unknown_tasks = sorted(
        {task_id for issue in verdict.issues for task_id in issue.task_ids} - task_ids
    )
    unknown_outcomes = sorted(
        {outcome_id for issue in verdict.issues for outcome_id in issue.outcome_ids}
        - outcome_ids
    )
    if unknown_tasks:
        raise PlanVerificationError(
            f"plan verification issues reference unknown tasks: {unknown_tasks}"
        )
    if unknown_outcomes:
        raise PlanVerificationError(
            f"plan verification issues reference unknown outcomes: {unknown_outcomes}"
        )


def make_plan_verification_receipt(
    plan: Plan,
    verdict: PlanVerificationVerdict,
    *,
    mode: str,
    verifier_thread_id: str,
    verifier_turn_id: str,
    verified_at: str | None = None,
) -> PlanVerificationReceipt:
    require_deterministic_plan_admission(plan)
    validate_verdict_references(plan, verdict)
    if verdict.verdict != "PASS":
        raise PlanVerificationError(
            "only a PASS verdict can produce a PLAN_VERIFIED receipt"
        )
    return PlanVerificationReceipt(
        schema_version=PLAN_VERIFICATION_RECEIPT_SCHEMA,
        status=PLAN_VERIFIED,
        graph_version=plan.graph_version,
        plan_sha256=plan_sha256(plan),
        mode=mode,
        verdict="PASS",
        verifier_role=PLAN_VERIFICATION_ROLE,
        verifier_thread_id=_required_text(
            verifier_thread_id, "verifier_thread_id", 256
        ),
        verifier_turn_id=_required_text(verifier_turn_id, "verifier_turn_id", 256),
        verified_at=verified_at or datetime.now(timezone.utc).isoformat(),
    )


def validate_plan_verification_receipt(
    plan: Plan,
    raw: Mapping[str, Any] | PlanVerificationReceipt | None,
    *,
    require_evidence: bool,
) -> PlanVerificationReceipt | None:
    # Persisted v0.9 plans predate the Goal Contract and the plan gate. Their
    # compatibility is historical provenance, not a way for a new plan to opt
    # out: new canonical validation already requires goal_contract.
    if plan.goal_contract is None and raw is None:
        return None
    if isinstance(raw, PlanVerificationReceipt):
        receipt = raw
    else:
        if not isinstance(raw, Mapping):
            raise PlanVerificationError(
                "canonical plan is PLAN_PROPOSED until a verification receipt is supplied"
            )
        allowed = {
            "schema_version",
            "status",
            "graph_version",
            "plan_sha256",
            "mode",
            "verdict",
            "verifier_role",
            "verifier_thread_id",
            "verifier_turn_id",
            "verified_at",
            "evidence_ids",
            "verification_result_id",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise PlanVerificationError(
                f"plan verification receipt has unknown fields: {sorted(unknown)}"
            )
        evidence_ids = _string_array(raw.get("evidence_ids", []), "evidence_ids")
        receipt = PlanVerificationReceipt(
            schema_version=_integer(raw.get("schema_version"), "schema_version"),
            status=_required_text(raw.get("status"), "status", 64),
            graph_version=_integer(raw.get("graph_version"), "graph_version"),
            plan_sha256=_required_text(raw.get("plan_sha256"), "plan_sha256", 64),
            mode=_required_text(raw.get("mode"), "mode", 64),
            verdict=_required_text(raw.get("verdict"), "verdict", 16),
            verifier_role=_required_text(
                raw.get("verifier_role"), "verifier_role", 128
            ),
            verifier_thread_id=_required_text(
                raw.get("verifier_thread_id"), "verifier_thread_id", 256
            ),
            verifier_turn_id=_required_text(
                raw.get("verifier_turn_id"), "verifier_turn_id", 256
            ),
            verified_at=_required_text(raw.get("verified_at"), "verified_at", 128),
            evidence_ids=evidence_ids,
            verification_result_id=(
                _required_text(
                    raw.get("verification_result_id"),
                    "verification_result_id",
                    128,
                )
                if raw.get("verification_result_id") is not None
                else None
            ),
        )
    if receipt.schema_version != PLAN_VERIFICATION_RECEIPT_SCHEMA:
        raise PlanVerificationError("unsupported plan verification receipt schema")
    if receipt.status != PLAN_VERIFIED or receipt.verdict != "PASS":
        raise PlanVerificationError("plan verification receipt is not PLAN_VERIFIED/PASS")
    if receipt.mode not in {
        INITIAL_PLAN_VERIFICATION,
        PLAN_PATCH_VERIFICATION,
        FULL_PLAN_REVALIDATION,
    }:
        raise PlanVerificationError("plan verification receipt has an invalid mode")
    if receipt.verifier_role != PLAN_VERIFICATION_ROLE:
        raise PlanVerificationError(
            "plan verification receipt must come from Plan Verification Architect"
        )
    if receipt.graph_version != plan.graph_version:
        raise PlanVerificationError(
            "plan verification graph version does not match the canonical plan"
        )
    if receipt.plan_sha256 != plan_sha256(plan):
        raise PlanVerificationError(
            "plan verification digest does not match the canonical plan"
        )
    if require_evidence and (
        not receipt.evidence_ids or not receipt.verification_result_id
    ):
        raise PlanVerificationError(
            "PLAN_VERIFIED requires recorded evidence and a verification result"
        )
    return receipt


def require_plan_verified(plan: Plan, state_or_receipt: Any) -> None:
    raw = (
        getattr(state_or_receipt, "plan_verification", None)
        if not isinstance(state_or_receipt, Mapping)
        else state_or_receipt
    )
    validate_plan_verification_receipt(plan, raw, require_evidence=True)


def record_plan_verification(
    memory: Any,
    plan: Plan,
    verdict: PlanVerificationVerdict,
    *,
    mode: str,
    verifier_thread_id: str,
    verifier_turn_id: str,
    receipt: PlanVerificationReceipt | None = None,
) -> tuple[PlanVerificationReceipt | None, str, str]:
    """Record the authoritative model outcome and finalize a PASS receipt.

    The lookup makes dispatcher replay idempotent: a completed verifier turn
    has one causal verification result even if the process crashes before the
    state transaction is committed.
    """

    validate_verdict_references(plan, verdict)
    task_id = f"PLAN-v{plan.graph_version}"
    check_id = (
        "full-plan-revalidation"
        if mode == FULL_PLAN_REVALIDATION
        else "plan-verification"
    )
    existing = _existing_verification(
        memory,
        task_id=task_id,
        check_id=check_id,
        thread_id=verifier_thread_id,
        turn_id=verifier_turn_id,
    )
    if existing is not None:
        if existing.get("verdict") != verdict.verdict:
            raise PlanVerificationError(
                "plan verifier causal identity already has a different verdict"
            )
        evidence_ids = tuple(
            str(item["id"]) for item in existing.get("evidence") or []
        )
        verification_id = str(existing["id"])
    else:
        evidence = memory.record_evidence(
            kind="tool",
            summary=(
                f"Fresh {PLAN_VERIFICATION_ROLE} returned {verdict.verdict} "
                f"for graph v{plan.graph_version} ({plan_sha256(plan)})."
            ),
            created_by=PLAN_VERIFICATION_ROLE,
            milestone_id=task_id,
            role=check_id,
            tool_name="codex-app-server/fresh-plan-verifier",
            result=json.dumps(
                {
                    "mode": mode,
                    "graph_version": plan.graph_version,
                    "plan_sha256": plan_sha256(plan),
                    **verdict.to_dict(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            provider="codex-app-server",
            provider_thread_id=verifier_thread_id,
        )
        verification = memory._record_runtime_verification_result(
            task_id=task_id,
            check_id=check_id,
            policy="independent",
            verdict=verdict.verdict,
            summary=(
                "Fresh plan verifier accepted the proposed graph."
                if verdict.verdict == "PASS"
                else f"Fresh plan verifier rejected the proposed graph with "
                f"{len(verdict.issues)} issue(s)."
            ),
            evidence_ids=[str(evidence["id"])],
            created_by=PLAN_VERIFICATION_ROLE,
            provider="codex-app-server",
            provider_thread_id=verifier_thread_id,
            provider_turn_id=verifier_turn_id,
            details={
                "mode": mode,
                "graph_version": plan.graph_version,
                "plan_sha256": plan_sha256(plan),
                "issues": [item.to_dict() for item in verdict.issues],
            },
        )
        evidence_ids = (str(evidence["id"]),)
        verification_id = str(verification["id"])
    finalized = None
    if verdict.verdict == "PASS":
        base = receipt or make_plan_verification_receipt(
            plan,
            verdict,
            mode=mode,
            verifier_thread_id=verifier_thread_id,
            verifier_turn_id=verifier_turn_id,
        )
        finalized = PlanVerificationReceipt(
            schema_version=base.schema_version,
            status=base.status,
            graph_version=base.graph_version,
            plan_sha256=base.plan_sha256,
            mode=base.mode,
            verdict=base.verdict,
            verifier_role=base.verifier_role,
            verifier_thread_id=base.verifier_thread_id,
            verifier_turn_id=base.verifier_turn_id,
            verified_at=base.verified_at,
            evidence_ids=evidence_ids,
            verification_result_id=verification_id,
        )
    return finalized, evidence_ids[0], verification_id


def load_active_memory_constraints(root: Path) -> tuple[dict[str, Any], ...]:
    """Read active constraints without creating Project Memory on a clean run."""

    database = root.resolve() / ".codex-autopilot" / "memory.sqlite3"
    if not database.is_file():
        return ()
    from .memory import ProjectMemory

    memory = ProjectMemory(root)
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = memory.list_records(
            categories=["constraint"],
            statuses=["active"],
            limit=20,
            cursor=cursor,
            full_statements=True,
        )
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if not cursor:
            break
    return tuple(records)


def plan_change_verification_mode(
    current: Plan,
    candidate: Plan,
    *,
    accepted_patches_since_full: int,
    full_revalidation_patches: int = DEFAULT_FULL_REVALIDATION_PATCHES,
) -> str:
    if (
        isinstance(full_revalidation_patches, bool)
        or not isinstance(full_revalidation_patches, int)
        or full_revalidation_patches < 1
    ):
        raise PlanVerificationError("full_revalidation_patches must be positive")
    if accepted_patches_since_full + 1 >= full_revalidation_patches:
        return FULL_PLAN_REVALIDATION
    if critical_path_signature(current) != critical_path_signature(candidate):
        return FULL_PLAN_REVALIDATION
    return PLAN_PATCH_VERIFICATION


def critical_path_signature(plan: Plan) -> tuple[Any, ...]:
    """Describe topology and verifier-visible semantics of every longest path."""

    children: dict[str, list[str]] = {task.id: [] for task in plan.tasks}
    parents: dict[str, tuple[str, ...]] = {
        task.id: tuple(task.depends_on) for task in plan.tasks
    }
    for task in plan.tasks:
        for parent in task.depends_on:
            children[parent].append(task.id)
    order = _topological_ids(plan.tasks)
    forward: dict[str, int] = {}
    for task_id in order:
        forward[task_id] = 1 + max(
            (forward[parent] for parent in parents[task_id]), default=0
        )
    backward: dict[str, int] = {}
    for task_id in reversed(order):
        backward[task_id] = 1 + max(
            (backward[child] for child in children[task_id]), default=0
        )
    longest = max(forward.values(), default=0)
    nodes = tuple(
        sorted(
            task_id
            for task_id in order
            if forward[task_id] + backward[task_id] - 1 == longest
        )
    )
    edges = tuple(
        sorted(
            (parent, child)
            for parent, child_ids in children.items()
            for child in child_ids
            if forward[parent] + backward[child] == longest
        )
    )
    task_map = plan.task_map
    semantics = tuple(
        (
            task_id,
            task_map[task_id].objective,
            task_map[task_id].definition_of_done,
            task_map[task_id].produces_outcomes,
            task_map[task_id].acceptance_class.value,
            tuple(
                (output.id, output.description, output.path, output.required)
                for output in task_map[task_id].outputs
            ),
        )
        for task_id in nodes
    )
    return longest, nodes, edges, semantics


def format_plan_verification_issues(
    issues: Iterable[PlanVerificationIssue],
) -> str:
    materialized = tuple(issues)
    return json.dumps(
        {"issues": [item.to_dict() for item in materialized]},
        ensure_ascii=False,
        sort_keys=True,
    )


def _task_plan_view(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "objective": task.objective,
        "acceptance_class": task.acceptance_class.value,
        "semantic_judgment_required": admits_semantic_judgment(
            task.acceptance_class
        ),
        "produces_outcomes": list(task.produces_outcomes),
        "depends_on": list(task.depends_on),
        "outputs": [
            {
                "id": output.id,
                "description": output.description,
                **({"path": output.path} if output.path else {}),
                "required": output.required,
            }
            for output in task.outputs
        ],
    }


def _issue_from_raw(raw: Any, index: int) -> PlanVerificationIssue:
    label = f"plan verification issue {index}"
    if not isinstance(raw, dict):
        raise PlanVerificationError(f"{label} must be an object")
    required = {"category", "summary", "task_ids", "outcome_ids"}
    if set(raw) != required:
        raise PlanVerificationError(
            f"{label} must contain exactly {sorted(required)}"
        )
    category = _required_text(raw.get("category"), f"{label}.category", 64)
    if category not in ISSUE_CATEGORIES:
        raise PlanVerificationError(
            f"{label}.category must be one of {sorted(ISSUE_CATEGORIES)}"
        )
    return PlanVerificationIssue(
        category=category,
        summary=_required_text(raw.get("summary"), f"{label}.summary", 2_000),
        task_ids=_identifier_array(raw.get("task_ids"), f"{label}.task_ids"),
        outcome_ids=_identifier_array(
            raw.get("outcome_ids"), f"{label}.outcome_ids"
        ),
    )


def _existing_verification(
    memory: Any,
    *,
    task_id: str,
    check_id: str,
    thread_id: str,
    turn_id: str,
) -> dict[str, Any] | None:
    cursor: str | None = None
    while True:
        page = memory.list_verification_results(
            task_id=task_id,
            limit=20,
            cursor=cursor,
        )
        for item in page.records:
            if (
                item.get("check_id") == check_id
                and item.get("provider_thread_id") == thread_id
                and item.get("provider_turn_id") == turn_id
            ):
                return memory.get_verification_result(str(item["id"]))
        cursor = page.next_cursor
        if not cursor:
            return None


def _topological_ids(tasks: Sequence[Task]) -> tuple[str, ...]:
    pending = {task.id: set(task.depends_on) for task in tasks}
    order: list[str] = []
    declaration = [task.id for task in tasks]
    while pending:
        ready = [task_id for task_id in declaration if task_id in pending and not pending[task_id]]
        if not ready:
            raise PlanVerificationError("critical path cannot be computed for a cyclic graph")
        for task_id in ready:
            order.append(task_id)
            pending.pop(task_id)
            for dependencies in pending.values():
                dependencies.discard(task_id)
    return tuple(order)


def _required_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanVerificationError(f"{name} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise PlanVerificationError(f"{name} exceeds {maximum} characters")
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlanVerificationError(f"{name} must be a positive integer")
    return value


def _string_array(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise PlanVerificationError(f"{name} must be an array of non-empty strings")
    result = tuple(item.strip() for item in value)
    if len(result) != len(set(result)):
        raise PlanVerificationError(f"{name} must not contain duplicates")
    return result


def _identifier_array(value: Any, name: str) -> tuple[str, ...]:
    values = _string_array(value, name)
    if any(not _IDENTIFIER.fullmatch(item) for item in values):
        raise PlanVerificationError(f"{name} contains an invalid identifier")
    return values
