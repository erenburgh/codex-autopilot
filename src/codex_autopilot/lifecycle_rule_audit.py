"""Rule R16 bookkeeping for a completed turn, whatever kind of turn it was.

Extracted from lifecycle_completion.py when that module reached the 1500-line
ceiling. These three functions are the audit half of completion: they read a
finished report for its applied-rule ids and its disagreements, and write
violations, observations and Conflicts. They touch no session state machine
and decide no successor, so they are the part that leaves cleanly.

Every completion path uses them - the worker, the on-call engineer - and R16
is CHECKED, not ENFORCED: a missing or wrong declaration is recorded as a
defect and raises R16 for the next worker, and never fails the completion.
"""

from __future__ import annotations

from typing import Any

from .config import Config
from .lifecycle_base import _append_event, parse_applied_rules
from .rules import record_violation
from .run_state import RunState


def _audit_rule_declaration(
    cfg: Config,
    state: RunState,
    session: dict[str, Any],
    final_message: str,
    at: str,
) -> None:
    """Rule R16: the report must list the applied rule ids.

    The rule is CHECKED: a missing list is recorded as a defect and raises
    R16 in the next worker's rule priority, but does not fail the completion.
    A reference to a non-existent id is a defect too: that is how a rule gets
    "observed" by citing what does not exist.
    """

    from .rules import RULES

    declared = parse_applied_rules(final_message)
    known = {item.id for item in RULES}
    if not declared:
        detail = (
            f"R16: the report of task {session.get('task_id')} did not list the applied "
            "rules; an AUTOPILOT_RULES line is expected before AUTOPILOT_STATUS"
        )
    else:
        unknown = [item for item in declared if item not in known]
        if not unknown:
            return
        detail = (
            f"R16: the report of task {session.get('task_id')} references rules that do "
            f"not exist: {', '.join(unknown)}"
        )
    record_violation(cfg.state_dir, "R16", detail=detail)
    _append_event(state, "rule_declaration_missing", session, at, detail=detail)


def _record_rule_conflicts(
    cfg: Config,
    state: RunState,
    session: dict[str, Any],
    final_message: str,
    at: str,
    memory,
) -> None:
    """Rule R16: a disagreement with the wording becomes a Conflict.

    The worker does not resolve it itself. The conflict opens between the
    recorded wording of the rule and how the executor read it, and stays
    open: a human or a separate task resolves it, never the one who filed it.

    The rule's wording is recorded as an observation once per project - a
    conflict needs an existing record, and the rule lives in code, not in
    memory. From then on every disagreement about this rule argues with the
    same record, and the rule's whole history is visible.
    """

    from .lifecycle_base import parse_rule_conflicts
    from .rules import rule as rule_by_id

    conflicts = parse_rule_conflicts(final_message)
    if not conflicts:
        return
    task_id = str(session.get("task_id") or "")
    for rule_id, detail in conflicts:
        try:
            canonical = rule_by_id(rule_id)
        except KeyError:
            record_violation(
                cfg.state_dir,
                "R16",
                detail=f"R16: {task_id} disputed a rule that does not exist: {rule_id}",
            )
            continue
        try:
            recorded = _rule_statement_record(memory, rule_id, canonical.statement)
            reading = memory.add_observation(
                statement=f"{rule_id}: executor {task_id} read the rule differently — {detail}",
                created_by=f"task:{task_id}",
                confidence="medium",
            )
            conflict = memory.open_conflict(
                existing_record_id=str(recorded["id"]),
                incoming_record_id=str(reading["id"]),
                statement=(
                    f"{rule_id}: the recorded wording and the reading of task "
                    f"{task_id} disagree; the executor does not resolve it"
                ),
                created_by=f"task:{task_id}",
            )
        except Exception as exc:
            _append_event(
                state,
                "rule_conflict_not_recorded",
                session,
                at,
                detail=f"{rule_id}: {exc}",
            )
            continue
        _append_event(
            state,
            "rule_conflict_opened",
            session,
            at,
            detail=f"{rule_id}: {conflict.get('id')}",
        )


def _rule_statement_record(memory, rule_id: str, statement: str) -> dict[str, Any]:
    """The canonical wording of a rule as Truth, one per project.

    A conflict opens only against Truth - rightly so: one can argue with what
    is established, not with someone's opinion. The wording is established:
    it is read from the running runtime, which is evidence of the
    environment_probe kind. A file cannot confirm it - rules.py lives in
    Autopilot, not in the user's project.
    """

    marker = f"{rule_id} (recorded wording)"
    page = memory.search(query=rule_id, categories=["truth"], limit=20)
    for record in page.records:
        if str(record.get("statement", "")).startswith(marker):
            return record
    evidence = memory.record_evidence(
        kind="environment_probe",
        summary=f"The wording of {rule_id}, read from the installed runtime.",
        created_by="codex-autopilot",
        environment_probe=statement,
        role="rule_statement",
    )
    return memory.record_verified_fact(
        statement=f"{marker}: {statement}",
        created_by="codex-autopilot",
        verification_method="read from the rules block of the installed runtime",
        evidence_ids=[str(evidence["id"])],
    )
