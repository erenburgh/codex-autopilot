"""Verification-ledger writes and runtime-owned provenance attestations."""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Sequence

from .memory import MAX_FIELD_CHARS, MemoryValidationError, utc_now
from .trust import TRUST_POLICY


RUNTIME_ATTESTATION_KEY = "runtime_attestation"
RUNTIME_VERIFICATION_AUTHORITY = "codex-autopilot-runtime"


def record_verification_result(
    memory: Any,
    *,
    task_id: str,
    check_id: str,
    policy: str,
    verdict: str,
    summary: str,
    evidence_ids: Sequence[str],
    created_by: str,
    provider_thread_id: str,
    provider_turn_id: str,
    details: Mapping[str, Any] | None,
    provider: str | None,
    runtime_attested: bool,
) -> dict[str, Any]:
    """Persist an evidence-linked outcome; only runtime calls may be attested."""

    memory.initialize()
    task = memory._required(task_id, "task_id", 128)
    check = memory._required(check_id, "check_id", 128)
    if policy not in {"self", "deterministic", "independent", "auto"}:
        raise MemoryValidationError("unsupported verification policy")
    outcome = memory._required(verdict, "verdict", 16).upper()
    if outcome not in {"PASS", "REVISE"}:
        raise MemoryValidationError("verification verdict must be PASS or REVISE")
    result_summary = memory._required(summary, "summary")
    actor = memory._required(created_by, "created_by", 256)
    thread_id = memory._required(provider_thread_id, "provider_thread_id", 256)
    turn_id = memory._required(provider_turn_id, "provider_turn_id", 256)
    normalized_provider = memory._optional(provider, "provider", 128)
    evidence = tuple(str(item).strip() for item in evidence_ids)
    if not evidence:
        raise MemoryValidationError(
            "verification results require at least one existing evidence ID"
        )
    if any(not item for item in evidence) or len(set(evidence)) != len(evidence):
        raise MemoryValidationError("verification evidence IDs must be unique")
    if details is not None and not isinstance(details, Mapping):
        raise MemoryValidationError("verification details must be an object")
    normalized_details = dict(details or {})
    if RUNTIME_ATTESTATION_KEY in normalized_details:
        raise MemoryValidationError(
            f"verification details field {RUNTIME_ATTESTATION_KEY!r} is "
            "reserved for the Codex Autopilot runtime"
        )
    if runtime_attested:
        authority_kind = {
            "deterministic": "deterministic_runner",
            "independent": "fresh_verifier",
        }.get(policy)
        if authority_kind is None:
            raise MemoryValidationError(
                "runtime attestation requires deterministic or independent policy"
            )
        normalized_details[RUNTIME_ATTESTATION_KEY] = {
            "schema_version": 1,
            "authority": RUNTIME_VERIFICATION_AUTHORITY,
            "authority_kind": authority_kind,
            "task_id": task,
            "check_id": check,
            "policy": policy,
            "provider": normalized_provider,
            "provider_thread_id": thread_id,
            "provider_turn_id": turn_id,
        }
    try:
        details_json = json.dumps(
            normalized_details,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise MemoryValidationError(
            "verification details must be JSON-serializable"
        ) from exc
    if len(details_json) > MAX_FIELD_CHARS:
        raise MemoryValidationError(
            f"verification details exceed {MAX_FIELD_CHARS} characters"
        )
    with memory._connect(write=True) as db:
        rows = db.execute(
            f"SELECT id,kind,provenance,trust_level FROM evidence "
            f"WHERE id IN ({','.join('?' for _ in evidence)})",
            evidence,
        ).fetchall()
        found = {str(row["id"]): row for row in rows}
        missing = [item for item in evidence if item not in found]
        if missing:
            raise MemoryValidationError(f"unknown evidence: {', '.join(missing)}")
        TRUST_POLICY.require_truth_rows(
            (found[item] for item in evidence),
            error_type=MemoryValidationError,
            message_prefix=(
                "R18: untrusted material cannot support verification outcomes"
            ),
        )
        existing = db.execute(
            """SELECT * FROM verification_results
               WHERE task_id=? AND check_id=?
                 AND provider_thread_id=? AND provider_turn_id=?""",
            (task, check, thread_id, turn_id),
        ).fetchone()
        if existing is not None:
            existing_evidence = {
                str(row["evidence_id"])
                for row in db.execute(
                    """SELECT evidence_id FROM verification_result_evidence
                       WHERE verification_id=?""",
                    (existing["id"],),
                ).fetchall()
            }
            expected = {
                "policy": policy,
                "verdict": outcome,
                "summary": result_summary,
                "details_json": details_json,
                "created_by": actor,
                "provider": normalized_provider,
            }
            if any(existing[key] != value for key, value in expected.items()) or (
                existing_evidence != set(evidence)
            ):
                raise MemoryValidationError(
                    "verification causal identity already has a different payload"
                )
            verification_id = str(existing["id"])
        else:
            verification_id = memory._next_id(db, "verification")
            now = utc_now()
            db.execute(
                """INSERT INTO verification_results(
                    id,task_id,check_id,policy,verdict,summary,details_json,
                    created_by,provider,provider_thread_id,provider_turn_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    verification_id,
                    task,
                    check,
                    policy,
                    outcome,
                    result_summary,
                    details_json,
                    actor,
                    normalized_provider,
                    thread_id,
                    turn_id,
                    now,
                ),
            )
            for evidence_id in evidence:
                db.execute(
                    """INSERT INTO verification_result_evidence(
                        verification_id,evidence_id,created_at
                    ) VALUES(?,?,?)""",
                    (verification_id, evidence_id, now),
                )
            memory._audit(
                db,
                "record",
                "verification",
                verification_id,
                actor,
                {
                    "task_id": task,
                    "check_id": check,
                    "policy": policy,
                    "verdict": outcome,
                    "evidence_ids": list(evidence),
                    "provider_thread_id": thread_id,
                    "provider_turn_id": turn_id,
                    "runtime_attested": runtime_attested,
                },
            )
    return memory.get_verification_result(verification_id)


def evidence_that_may_support(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    """The ids among these evidence rows that an outcome may rest on (R18).

    The acceptance used to cite the whole milestone evidence list, and
    ``require_truth_rows`` refuses the WHOLE set when one item is below
    deterministic. One such item is exactly what an honest worker records:
    the memory tool refuses evidence without a milestone_id while a task is
    active, and its own description says outside material must be recorded
    with kind "external". So a worker that obeyed the tool made its own task
    impossible to accept - and ``MemoryValidationError`` is neither a
    WorkerProtocolError nor a DesktopLifecycleError, so it escaped
    completion after the Stop hook had already fired and the turn was lost.

    Outside material stays on the record; it is simply not cited as support.
    An empty result is not decided here: the caller owns the refusal, and a
    refusal the worker can act on belongs to the worker's own protocol.
    """

    return [
        str(row["id"])
        for row in rows
        if not TRUST_POLICY.row_is_below_truth(row)
    ]
