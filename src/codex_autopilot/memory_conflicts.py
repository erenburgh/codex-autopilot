"""Disputes over Truth: opening a conflict and resolving it.

Moved out of the ProjectMemory class by the same rule as the views and the
durability helpers: these are operations OVER memory, not its internals,
and memory.py stands at the section-0 limit, where the answer is
decomposition rather than shorter explanations. Thin delegating methods
stay on the class, so the API and every call site are unchanged.

Two defects were repaired here, both measured on a live database:

- resolution was the ONLY writer of the `verified` transition for a Truth
  and re-checked nothing, so three calls of the exposed memory tool turned
  unverified external material into binding support (R18);
- it hardcoded `verified` for every outcome but `supersede_existing`, so a
  retired Truth came back to life.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .memory import MemoryValidationError, utc_now


def open_conflict(memory, *, existing_record_id: str, statement: str, created_by: str, incoming_record_id: str | None = None, incoming_evidence_id: str | None = None) -> dict[str, Any]:
    memory.initialize()
    with memory._connect(write=True) as db:
        conflict_id = open_conflict_in_transaction(memory, 
            db,
            existing_record_id=existing_record_id,
            statement=statement,
            created_by=created_by,
            incoming_record_id=incoming_record_id,
            incoming_evidence_id=incoming_evidence_id,
        )
    return memory.get_conflict(conflict_id)


def open_conflict_in_transaction(
    memory,
    db,
    *,
    existing_record_id: str,
    statement: str,
    created_by: str,
    incoming_record_id: str | None = None,
    incoming_evidence_id: str | None = None,
) -> str:
    existing = db.execute(
        "SELECT category FROM records WHERE id=?", (existing_record_id,)
    ).fetchone()
    if not existing or existing["category"] != "truth":
        raise MemoryValidationError(
            "conflicts must reference an existing Truth record"
        )
    if incoming_record_id and not db.execute(
        "SELECT 1 FROM records WHERE id=?", (incoming_record_id,)
    ).fetchone():
        raise MemoryValidationError(f"unknown incoming record: {incoming_record_id}")
    if incoming_evidence_id and not db.execute(
        "SELECT 1 FROM evidence WHERE id=?", (incoming_evidence_id,)
    ).fetchone():
        raise MemoryValidationError(
            f"unknown incoming evidence: {incoming_evidence_id}"
        )
    conflict_id = memory._next_id(db, "conflict")
    now = utc_now()
    normalized_statement = memory._required(statement, "statement")
    actor = memory._required(created_by, "created_by", 256)
    db.execute(
        """INSERT INTO conflicts(
            id,existing_record_id,incoming_record_id,incoming_evidence_id,
            statement,status,created_by,created_at
        ) VALUES(?,?,?,?,?,'needs_review',?,?)""",
        (
            conflict_id,
            existing_record_id,
            incoming_record_id,
            incoming_evidence_id,
            normalized_statement,
            actor,
            now,
        ),
    )
    # What the record was before the dispute. Resolution used to hardcode
    # `verified` for anything but supersede_existing, so a retired Truth
    # came back to life: measured - a `superseded` fact, one conflict
    # opened and rejected, and it was `verified` again.
    previous_status = str(
        db.execute(
            "SELECT status FROM records WHERE id=?", (existing_record_id,)
        ).fetchone()["status"]
    )
    db.execute(
        "UPDATE records SET status='disputed',updated_at=? WHERE id=?",
        (now, existing_record_id),
    )
    db.execute(
        """INSERT INTO conflict_history(
            conflict_id,action,details,actor,created_at
        ) VALUES(?,?,?,?,?)""",
        (conflict_id, "opened", normalized_statement, actor, now),
    )
    memory._audit(
        db,
        "open",
        "conflict",
        conflict_id,
        actor,
        {
            "existing_record_id": existing_record_id,
            "previous_status": previous_status,
        },
    )
    return conflict_id


def get_conflict(memory, conflict_id: str) -> dict[str, Any]:
    memory.initialize()
    with memory._connect() as db:
        row = db.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if not row:
            raise MemoryValidationError(f"unknown conflict: {conflict_id}")
        result = dict(row)
        result["history"] = [dict(item) for item in db.execute("SELECT * FROM conflict_history WHERE conflict_id=? ORDER BY id", (conflict_id,)).fetchall()]
        return result


def resolve_conflict(memory, conflict_id: str, *, outcome: str, resolution: str, actor: str) -> dict[str, Any]:
    if outcome not in {"supersede_existing", "reject_incoming", "reverified_existing"}:
        raise MemoryValidationError("invalid conflict outcome")
    memory.initialize()
    with memory._connect(write=True) as db:
        row = db.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if not row or row["status"] != "needs_review":
            raise MemoryValidationError("conflict is missing or already resolved")
        now = utc_now()
        existing_status = (
            "superseded"
            if outcome == "supersede_existing"
            else status_before_conflict(db, conflict_id)
        )
        record_id = str(row["existing_record_id"])
        if existing_status in memory._BINDING_STATUSES:
            origin = str(
                (
                    db.execute(
                        "SELECT origin FROM records WHERE id=?", (record_id,)
                    ).fetchone()
                    or {"origin": ""}
                )["origin"]
                or ""
            )
            # R18, the second moment: entering a binding status re-checks
            # the taint. Resolution is the ONLY writer of that transition
            # for a Truth, and it had no check at all - so three calls of
            # the exposed memory tool (attach contradicts, attach
            # supports while disputed, resolve) turned an unverified
            # external item into binding support of a verified fact.
            # Measured end to end.
            if origin != "user" and memory._rests_on_external(db, record_id):
                raise MemoryValidationError(
                    f"R18: {record_id} now rests on external content and "
                    f"cannot return to {existing_status} by anyone but the "
                    "user; detach the external support or let the user "
                    "decide - external content does not decide"
                )
        db.execute("UPDATE records SET status=?,updated_at=? WHERE id=?", (existing_status, now, record_id))
        db.execute("UPDATE conflicts SET status='resolved',resolution=?,resolved_at=? WHERE id=?", (memory._required(resolution, "resolution"), now, conflict_id))
        db.execute("INSERT INTO conflict_history(conflict_id,action,details,actor,created_at) VALUES(?,?,?,?,?)", (conflict_id, outcome, resolution, actor, now))
        memory._audit(db, "resolve", "conflict", conflict_id, actor, {"outcome": outcome})
    return memory.get_conflict(conflict_id)


def status_before_conflict(db, conflict_id: str) -> str:
    """The status the record held when this conflict opened.

    Recorded in the audit entry of the opening, so no schema migration is
    needed. A conflict opened by an older build carries no such entry;
    `verified` is then the honest default, because only a Truth can be
    disputed and every path that opened one before this change left it
    verified.
    """

    row = db.execute(
        """SELECT details_json FROM audit_log
           WHERE entity_type='conflict' AND entity_id=? AND action='open'
           ORDER BY id DESC LIMIT 1""",
        (conflict_id,),
    ).fetchone()
    if row is None:
        return "verified"
    try:
        details = json.loads(str(row["details_json"] or "{}"))
    except ValueError:
        return "verified"
    status = str(details.get("previous_status") or "").strip()
    return status or "verified"
