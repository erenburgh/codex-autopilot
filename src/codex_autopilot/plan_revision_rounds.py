"""The initial plan verifier's REVISE goes back to the planner, not the owner.

Before run-state exists the planner is the initiating agent: it wrote the
plan, so a fresh verifier's REVISE is its to fix. Preflight used to turn
every REVISE into "Plan verification: FAIL"; the skill reads a non-zero
exit as a finished answer, so the agent printed the verdict and stopped,
and a run that needed no decision of the owner waited for her. Measured on
15 Oct with the real verifier at the production effort, on one and the same
good plan: two PASS, one REVISE with substantive Definition of Done remarks.

Now a REVISE is a machine-readable answer with an exit code of its own: the
marker line, every issue, and the exact next step. The rounds are counted in
the project, beside the plan, and bound to the plan's canonical sha: only a
changed plan opens a new round, so a rerun without an edit gets the same
answer and starts no verifier. A REVISE in the last round is the FAIL it
always was, carrying every round's issues for the owner. A PASS in any round
starts the run as before and clears the count.

The file is a counter, never a verdict: it holds only REVISE rounds, and
nothing in it can stand in for a PASS or skip a verifier. A file that cannot
be read, or that is not this project's record, is ignored with a warning,
and the series starts again at round 1 with a fresh verifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .config import STATE_DIR_NAME
from .plan import Plan
from .plan_verification import (
    PlanVerificationError,
    PlanVerificationIssue,
    PlanVerificationVerdict,
    _issue_from_raw,
    format_plan_verification_issues,
    plan_sha256,
)


# EX_TEMPFAIL from sysexits.h: "retry later". Distinct from success (0),
# from every refusal (2) and from an approval only a human gives (77).
PLAN_REVISION_EXIT_CODE = 75
MAX_INITIAL_PLAN_ROUNDS = 3
ROUNDS_FILE_NAME = "plan-verification-rounds.json"
REVISE_MARKER = "Plan verification: REVISE"
_RECORD_KIND = "codex-autopilot/initial-plan-verification-rounds"
_RECORD_SCHEMA = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class InitialPlanRevise(PlanVerificationError):
    """A well-formed REVISE from the fresh initial plan verifier.

    A PlanVerificationError still, so a caller that does not know rounds
    refuses the start exactly as before.
    """

    def __init__(
        self,
        verdict: PlanVerificationVerdict,
        *,
        verifier_thread_id: str,
        verifier_turn_id: str,
    ) -> None:
        self.verdict = verdict
        self.verifier_thread_id = verifier_thread_id
        self.verifier_turn_id = verifier_turn_id
        super().__init__(
            "proposed plan was rejected by the fresh verifier: "
            + format_plan_verification_issues(verdict.issues)
        )


@dataclass(frozen=True, slots=True)
class RevisionRound:
    round: int
    plan_sha256: str
    graph_version: int
    issues: tuple[PlanVerificationIssue, ...]
    verifier_thread_id: str
    verifier_turn_id: str
    recorded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "plan_sha256": self.plan_sha256,
            "graph_version": self.graph_version,
            "verdict": "REVISE",
            "issues": [item.to_dict() for item in self.issues],
            "verifier_thread_id": self.verifier_thread_id,
            "verifier_turn_id": self.verifier_turn_id,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class RevisionRounds:
    rounds: tuple[RevisionRound, ...] = ()
    # Why an existing file was not used; None when there was none or it was.
    ignored: str | None = None

    def recorded_for(self, sha: str) -> tuple[RevisionRound, ...] | None:
        """The series whose last answer is for exactly this plan, if any.

        Only the last round counts: a plan that went back to an earlier
        round's text is still a change since the last verdict.
        """

        if self.rounds and self.rounds[-1].plan_sha256 == sha:
            return self.rounds
        return None

    def continued(self) -> tuple[RevisionRound, ...]:
        """The rounds a new REVISE extends.

        A series that already ended in FAIL went to the owner; a plan
        changed after that is a new start, not a fourth round. Without this
        her own word could never start the project again short of deleting
        the file by hand.
        """

        return () if len(self.rounds) >= MAX_INITIAL_PLAN_ROUNDS else self.rounds


def rounds_path(project: Path) -> Path:
    return project / STATE_DIR_NAME / ROUNDS_FILE_NAME


def load_plan_revision_rounds(project: Path) -> RevisionRounds:
    """This project's recorded rounds; never raises on what it finds."""

    path = rounds_path(project)
    if not os.path.lexists(path):
        return RevisionRounds()
    if path.is_symlink():
        return RevisionRounds(ignored=_ignored(path, "it is a symbolic link"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return RevisionRounds(ignored=_ignored(path, f"unreadable ({type(exc).__name__})"))
    if not isinstance(raw, dict) or raw.get("kind") != _RECORD_KIND or raw.get("schema_version") != _RECORD_SCHEMA:
        return RevisionRounds(ignored=_ignored(path, "not an Autopilot plan-rounds record"))
    if raw.get("project") != str(project):
        return RevisionRounds(ignored=_ignored(path, "written for another project"))
    try:
        rounds = _rounds_from_raw(raw.get("rounds"))
    except (PlanVerificationError, TypeError, ValueError) as exc:
        return RevisionRounds(ignored=_ignored(path, f"malformed: {exc}"))
    return RevisionRounds(rounds=rounds)


def record_plan_revise(
    project: Path,
    previous: tuple[RevisionRound, ...],
    *,
    plan: Plan,
    revise: InitialPlanRevise,
) -> tuple[RevisionRound, ...]:
    """Append this REVISE as the next round and write the series atomically."""

    current = RevisionRound(
        round=len(previous) + 1,
        plan_sha256=plan_sha256(plan),
        graph_version=plan.graph_version,
        issues=tuple(revise.verdict.issues),
        verifier_thread_id=revise.verifier_thread_id,
        verifier_turn_id=revise.verifier_turn_id,
        recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    rounds = (*previous, current)
    _atomic_json(
        rounds_path(project),
        {
            "kind": _RECORD_KIND,
            "schema_version": _RECORD_SCHEMA,
            "project": str(project),
            "limit": MAX_INITIAL_PLAN_ROUNDS,
            "rounds": [item.to_dict() for item in rounds],
        },
    )
    return rounds


def clear_plan_revision_rounds(project: Path) -> None:
    """A PASS ends the series. A path that cannot be removed is left alone:
    it is ignored at the next load anyway, and a PASS is not undone by it."""

    try:
        rounds_path(project).unlink(missing_ok=True)
    except OSError:
        pass


def revise_answer(
    rounds: tuple[RevisionRound, ...],
    *,
    plan_file: Path | None,
    replayed: bool,
) -> str:
    """What the planner reads: the marker on its own line, then one issue per line."""

    current = rounds[-1]
    lines = [f"{REVISE_MARKER} (round {current.round} of {MAX_INITIAL_PLAN_ROUNDS})"]
    lines.extend(
        f"Issue {index} of {len(current.issues)}: "
        + json.dumps(issue.to_dict(), ensure_ascii=False, sort_keys=True)
        for index, issue in enumerate(current.issues, 1)
    )
    target = str(plan_file) if plan_file else "the plan file given as --plan-file"
    lines.append(
        f"Next: revise {target} so that every issue above is resolved, then run "
        "the same start-skill command again. Fix the plan itself: do not argue "
        "with the verifier, and do not drop, weaken or narrow any requirement of "
        "the user's request or any Definition of Done item to get a PASS. A rerun "
        "with an unchanged plan returns this same answer and starts no verifier. "
        f"If round {MAX_INITIAL_PLAN_ROUNDS} of {MAX_INITIAL_PLAN_ROUNDS} is "
        "REVISE as well, the start fails and goes to the owner with every round's issues."
    )
    lines.append(_provenance(current, replayed=replayed))
    return "\n".join(lines)


def exhausted_answer(rounds: tuple[RevisionRound, ...], *, replayed: bool) -> str:
    """The FAIL for the owner: the verdict and every round's issues."""

    current = rounds[-1]
    lines = [
        f"verdict REVISE in round {current.round} of {MAX_INITIAL_PLAN_ROUNDS}: the "
        "plan was revised after every round and the fresh verifier still asks for "
        "changes, so the start now needs the owner."
    ]
    lines.extend(
        f"Round {item.round} (plan sha256 {item.plan_sha256}, graph v{item.graph_version}): "
        + format_plan_verification_issues(item.issues)
        for item in rounds
    )
    lines.append(_provenance(current, replayed=replayed))
    return "\n".join(lines)


def _provenance(current: RevisionRound, *, replayed: bool) -> str:
    if replayed:
        return (
            f"The plan is unchanged since round {current.round} (sha256 "
            f"{current.plan_sha256}): this is that round's answer, and no new verifier ran."
        )
    return (
        f"Fresh verifier task {current.verifier_thread_id} judged plan sha256 "
        f"{current.plan_sha256} (graph v{current.graph_version})."
    )


def _ignored(path: Path, reason: str) -> str:
    return (
        f"{path} ignored: {reason}; counting starts again at round 1 and a fresh "
        "verifier judges the plan"
    )


def _rounds_from_raw(raw: Any) -> tuple[RevisionRound, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_INITIAL_PLAN_ROUNDS:
        raise ValueError(f"rounds must be a list of 1..{MAX_INITIAL_PLAN_ROUNDS}")
    rounds = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"round {index} is not an object")
        # Only REVISE is ever written; a record claiming anything else is not ours.
        if item.get("verdict") != "REVISE" or item.get("round") != index:
            raise ValueError(f"round {index} is not REVISE round {index}")
        sha = item.get("plan_sha256")
        version = item.get("graph_version")
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise ValueError(f"round {index} has no plan sha256")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError(f"round {index} has no graph version")
        issues = item.get("issues")
        if not isinstance(issues, list) or not issues:
            raise ValueError(f"round {index} has no issues")
        rounds.append(
            RevisionRound(
                round=index,
                plan_sha256=sha,
                graph_version=version,
                issues=tuple(_issue_from_raw(issue, number) for number, issue in enumerate(issues, 1)),
                verifier_thread_id=str(item.get("verifier_thread_id") or ""),
                verifier_turn_id=str(item.get("verifier_turn_id") or ""),
                recorded_at=str(item.get("recorded_at") or ""),
            )
        )
    return tuple(rounds)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
