from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Sequence

from .department_acceptance import (
    DepartmentAcceptanceError,
    RubricReference,
    rubric_reference_from_raw,
)
from .department_runtime import derive_task_department
from .models import MODEL_IDS, MODEL_LABELS, logical_model
from .plan import Plan, Task, VerificationCheck


VERIFICATION_PREFIX = "AUTOPILOT_VERIFICATION: "
VERDICTS = frozenset({"PASS", "REVISE"})
MAX_CHECK_OUTPUT_CHARS = 4_000


class VerificationProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VerificationIssue:
    code: str
    summary: str
    details: str
    dod_refs: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "summary": self.summary,
            "details": self.details,
            "dod_refs": list(self.dod_refs),
        }

    @classmethod
    def from_dict(cls, raw: object) -> "VerificationIssue":
        if not isinstance(raw, dict):
            raise VerificationProtocolError("verification issues must be JSON objects")
        allowed = ("code", "summary", "details", "dod_refs")
        unknown = set(raw) - set(allowed)
        if unknown:
            # R31: a refusal must name what is accepted. Measured on a clean
            # run: the verifier returned finding/requirement/required_fix/
            # severity/id/evidence_ids - all six plausible, none of them
            # ever named to it, and the whole verdict was rejected.
            raise VerificationProtocolError(
                f"verification issue has unknown fields: {sorted(unknown)}. "
                f"an issue has exactly these fields: {', '.join(allowed)} "
                "(code: short identifier; summary: one line; details: what does "
                "not add up and how to check it; dod_refs: optional array of "
                "1-based DoD item numbers)"
            )
        code = _required_text(raw.get("code"), "issue.code", 128)
        summary = _required_text(raw.get("summary"), "issue.summary", 1_000)
        details = _required_text(raw.get("details"), "issue.details", 8_000)
        refs = raw.get("dod_refs", [])
        if not isinstance(refs, list) or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 1
            for item in refs
        ):
            raise VerificationProtocolError(
                "issue.dod_refs must be an array of positive integers"
            )
        if len(set(refs)) != len(refs):
            raise VerificationProtocolError("issue.dod_refs must not contain duplicates")
        return cls(code=code, summary=summary, details=details, dod_refs=tuple(refs))


@dataclass(frozen=True, slots=True)
class VerificationVerdict:
    verdict: str
    issues: tuple[VerificationIssue, ...]
    rubric: RubricReference | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "issues": [item.to_dict() for item in self.issues],
            **({"rubric": self.rubric.to_dict()} if self.rubric else {}),
        }


@dataclass(frozen=True, slots=True)
class DeterministicCheckResult:
    check_id: str
    kind: str
    description: str
    passed: bool
    expected: str
    actual: str
    output: str = ""
    path: str | None = None
    exit_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "kind": self.kind,
            "description": self.description,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            **({"output": self.output} if self.output else {}),
            **({"path": self.path} if self.path else {}),
            **({"exit_code": self.exit_code} if self.exit_code is not None else {}),
        }


@dataclass(frozen=True, slots=True)
class VerifierRoute:
    role_id: str
    execution_mode: str
    execution_mode_reason: str
    model_key: str | None
    model_id: str | None
    model_display: str
    reasoning: str | None


def parse_verifier_result(message: str) -> VerificationVerdict:
    """Parse one exact, final, structured verifier result.

    Free-form review prose may precede the protocol line. The protocol itself
    must occur exactly once and be the final non-empty line, which prevents a
    quoted example or an early draft verdict from advancing durable state.
    """

    lines = [line.strip() for line in message.splitlines() if line.strip()]
    protocol = [line for line in lines if line.startswith(VERIFICATION_PREFIX)]
    if len(protocol) != 1 or not lines or lines[-1] != protocol[0]:
        raise VerificationProtocolError(
            "verifier response must end with exactly one AUTOPILOT_VERIFICATION JSON line"
        )
    try:
        raw = json.loads(protocol[0][len(VERIFICATION_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise VerificationProtocolError("verifier result is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise VerificationProtocolError("verifier result must be a JSON object")
    unknown = set(raw) - {"verdict", "issues", "rubric"}
    if unknown:
        raise VerificationProtocolError(
            f"verifier result has unknown fields: {sorted(unknown)}"
        )
    verdict = str(raw.get("verdict") or "").strip().upper()
    if verdict not in VERDICTS:
        raise VerificationProtocolError("verifier verdict must be PASS or REVISE")
    issues_raw = raw.get("issues")
    if not isinstance(issues_raw, list):
        raise VerificationProtocolError("verifier issues must be an array")
    issues = tuple(VerificationIssue.from_dict(item) for item in issues_raw)
    codes = [item.code for item in issues]
    if len(codes) != len(set(codes)):
        raise VerificationProtocolError("verification issue codes must be unique")
    if verdict == "PASS" and issues:
        raise VerificationProtocolError("PASS must contain an empty issues array")
    if verdict == "REVISE" and not issues:
        raise VerificationProtocolError("REVISE must contain at least one issue")
    rubric = None
    if "rubric" in raw:
        try:
            rubric = rubric_reference_from_raw(raw["rubric"], "verifier rubric")
        except DepartmentAcceptanceError as exc:
            raise VerificationProtocolError(str(exc)) from exc
    return VerificationVerdict(verdict=verdict, issues=issues, rubric=rubric)


def run_deterministic_checks(
    project_root: Path,
    checks: Sequence[VerificationCheck],
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[DeterministicCheckResult, ...]:
    """Execute a complete declarative verification contract without a model.

    Commands are argument vectors and never pass through a shell. Artifact
    paths are confined to the canonical project root. Evidence checks match a
    milestone-evidence role to the check ID, giving the otherwise prose-free
    evidence declaration a deterministic slot contract.
    """

    root = project_root.expanduser().resolve()
    return tuple(_run_check(root, check, evidence) for check in checks)


def deterministic_issues(
    results: Sequence[DeterministicCheckResult],
) -> tuple[VerificationIssue, ...]:
    return tuple(
        VerificationIssue(
            code=f"CHECK-{item.check_id}",
            summary=item.description,
            details=(
                f"Expected {item.expected}; observed {item.actual}."
                + (f" Output: {item.output}" if item.output else "")
            ),
        )
        for item in results
        if not item.passed
    )


def verifier_route(plan: Plan, task: Task) -> VerifierRoute:
    """Route capability separately while deriving the judge from department.

    The judge is the lead of the department of the task's profession (R30).
    It was ``verifier_role or task.role`` for a task without the 0.13
    department resources - every task of every real plan - so a planner that
    left verifier_role out had the worker's own profession accept its work.
    There is no fallback now: no lead, no verifier (``department_gate``
    stops that one task for the on-call).
    """

    policy = task.verification
    try:
        role_id = derive_task_department(plan, task).lead_role_id
    except DepartmentAcceptanceError as exc:
        raise VerificationProtocolError(str(exc)) from exc
    execution_mode = policy.execution_mode or task.execution_mode
    execution_reason = (
        policy.execution_mode_reason
        or f"Verifier inherits the task's {task.execution_mode} capability boundary."
    )
    reasoning = policy.reasoning or task.reasoning or "medium"
    if plan.model_strategy == "host-settings":
        return VerifierRoute(
            role_id=role_id,
            execution_mode=execution_mode,
            execution_mode_reason=execution_reason,
            model_key=None,
            model_id=None,
            model_display="Host settings",
            reasoning=None,
        )
    key = logical_model(plan.model_strategy, execution_mode)
    return VerifierRoute(
        role_id=role_id,
        execution_mode=execution_mode,
        execution_mode_reason=execution_reason,
        model_key=key,
        model_id=MODEL_IDS[key],
        model_display=MODEL_LABELS[key],
        reasoning=reasoning,
    )


def _run_check(
    root: Path,
    check: VerificationCheck,
    evidence: Sequence[Mapping[str, Any]],
) -> DeterministicCheckResult:
    if check.kind == "command":
        expected = f"exit_code={check.expected_exit_code}"
        try:
            completed = subprocess.run(
                list(check.argv),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=check.timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = _bounded_output(exc.stdout, exc.stderr)
            return DeterministicCheckResult(
                check_id=check.id,
                kind=check.kind,
                description=check.description,
                passed=False,
                expected=expected,
                actual=f"timeout_after={check.timeout_seconds}s",
                output=output,
            )
        except OSError as exc:
            return DeterministicCheckResult(
                check_id=check.id,
                kind=check.kind,
                description=check.description,
                passed=False,
                expected=expected,
                actual=f"execution_error={type(exc).__name__}",
                output=str(exc)[:MAX_CHECK_OUTPUT_CHARS],
            )
        return DeterministicCheckResult(
            check_id=check.id,
            kind=check.kind,
            description=check.description,
            passed=completed.returncode == check.expected_exit_code,
            expected=expected,
            actual=f"exit_code={completed.returncode}",
            output=_bounded_output(completed.stdout, completed.stderr),
            exit_code=completed.returncode,
        )

    if check.kind == "artifact":
        assert check.path is not None
        candidate = Path(check.path).expanduser()
        candidate = candidate if candidate.is_absolute() else root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            relative = str(resolved.relative_to(root))
        except ValueError:
            return DeterministicCheckResult(
                check_id=check.id,
                kind=check.kind,
                description=check.description,
                passed=False,
                expected="existing artifact inside project root",
                actual="path escapes project root",
                path=check.path,
            )
        exists = resolved.exists()
        return DeterministicCheckResult(
            check_id=check.id,
            kind=check.kind,
            description=check.description,
            passed=exists,
            expected="artifact exists",
            actual="artifact exists" if exists else "artifact is missing",
            path=relative,
        )

    if check.kind == "evidence":
        matches = [item for item in evidence if item.get("role") == check.id]
        ids = [str(item.get("id")) for item in matches if item.get("id")]
        return DeterministicCheckResult(
            check_id=check.id,
            kind=check.kind,
            description=check.description,
            passed=bool(ids),
            expected=f"milestone evidence role={check.id}",
            actual=("evidence=" + ",".join(ids)) if ids else "no matching evidence",
        )

    raise ValueError(f"unsupported deterministic check kind: {check.kind}")


def _bounded_output(*values: object) -> str:
    text = "\n".join(str(value or "").strip() for value in values if value)
    if len(text) <= MAX_CHECK_OUTPUT_CHARS:
        return text
    return text[: MAX_CHECK_OUTPUT_CHARS - 14] + "…[truncated]"


def _required_text(value: object, name: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise VerificationProtocolError(f"{name} must be a non-empty string")
    if len(text) > maximum:
        raise VerificationProtocolError(f"{name} exceeds {maximum} characters")
    return text
