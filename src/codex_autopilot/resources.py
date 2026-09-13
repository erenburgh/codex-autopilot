from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import fcntl
import fnmatch
from pathlib import Path, PurePath
import threading
import unicodedata
import uuid
from typing import Iterator, Mapping, Sequence

from .plan import RESOURCE_ACCESS_MODES, RESOURCE_KINDS, Plan, ResourceClaim, Task
from .pipeline_engineer import PipelineIncidentStore
from .run_state import RunState, StateStore
from .scheduler import SchedulerAvailability


FILESYSTEM_RESOURCE_KINDS = frozenset({"path", "directory", "glob"})
NAMED_RESOURCE_KINDS = frozenset(RESOURCE_KINDS) - FILESYSTEM_RESOURCE_KINDS
LOCK_EVENTS = frozenset({"acquired", "released", "reconciled_release"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class NormalizedResourceClaim:
    id: str
    kind: str
    target: str
    access: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "access": self.access,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> NormalizedResourceClaim:
        _require_exact_keys(raw, {"id", "kind", "target", "access"}, "resource claim")
        claim = cls(
            id=_nonempty_string(raw["id"], "resource claim id"),
            kind=_nonempty_string(raw["kind"], "resource claim kind"),
            target=_nonempty_string(raw["target"], "resource claim target"),
            access=_nonempty_string(raw["access"], "resource claim access"),
        )
        if claim.kind not in RESOURCE_KINDS:
            raise ValueError(f"unknown normalized resource kind {claim.kind!r}")
        if claim.access not in RESOURCE_ACCESS_MODES:
            raise ValueError(f"unknown normalized resource access {claim.access!r}")
        if claim.kind in FILESYSTEM_RESOURCE_KINDS and not Path(claim.target).is_absolute():
            raise ValueError("normalized filesystem resource targets must be absolute")
        return claim


@dataclass(frozen=True, slots=True)
class LockOwner:
    ownership_token: str
    run_id: str
    task_id: str
    attempt: int
    worker_id: str
    thread_id: str | None = None
    turn_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        task_id: str,
        attempt: int,
        worker_id: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
        ownership_token: str | None = None,
    ) -> LockOwner:
        return cls(
            ownership_token=ownership_token or str(uuid.uuid4()),
            run_id=run_id,
            task_id=task_id,
            attempt=attempt,
            worker_id=worker_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ownership_token": self.ownership_token,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "worker_id": self.worker_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> LockOwner:
        _require_exact_keys(
            raw,
            {
                "ownership_token",
                "run_id",
                "task_id",
                "attempt",
                "worker_id",
                "thread_id",
                "turn_id",
            },
            "lock owner",
        )
        attempt = raw["attempt"]
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raise ValueError("lock owner attempt must be a non-negative integer")
        return cls(
            ownership_token=_nonempty_string(raw["ownership_token"], "ownership token"),
            run_id=_nonempty_string(raw["run_id"], "owner run id"),
            task_id=_nonempty_string(raw["task_id"], "owner task id"),
            attempt=attempt,
            worker_id=_nonempty_string(raw["worker_id"], "owner worker id"),
            thread_id=_optional_string(raw["thread_id"], "owner thread id"),
            turn_id=_optional_string(raw["turn_id"], "owner turn id"),
        )


@dataclass(frozen=True, slots=True)
class DurableResourceLock:
    lock_id: str
    owner: LockOwner
    claims: tuple[NormalizedResourceClaim, ...]
    acquired_at: str
    heartbeat_at: str
    computer_use_slot: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "lock_id": self.lock_id,
            "owner": self.owner.to_dict(),
            "claims": [claim.to_dict() for claim in self.claims],
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            "computer_use_slot": self.computer_use_slot,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> DurableResourceLock:
        _require_exact_keys(
            raw,
            {
                "lock_id",
                "owner",
                "claims",
                "acquired_at",
                "heartbeat_at",
                "computer_use_slot",
            },
            "resource lock",
        )
        owner_raw = raw["owner"]
        claims_raw = raw["claims"]
        if not isinstance(owner_raw, Mapping):
            raise ValueError("resource lock owner must be an object")
        if not isinstance(claims_raw, list) or not all(isinstance(item, Mapping) for item in claims_raw):
            raise ValueError("resource lock claims must be an array of objects")
        slot = raw["computer_use_slot"]
        if slot is not None and (
            isinstance(slot, bool) or not isinstance(slot, int) or slot < 0
        ):
            raise ValueError("computer_use_slot must be a non-negative integer or null")
        acquired_at = _timestamp(raw["acquired_at"], "resource lock acquired_at")
        heartbeat_at = _timestamp(raw["heartbeat_at"], "resource lock heartbeat_at")
        return cls(
            lock_id=_nonempty_string(raw["lock_id"], "resource lock id"),
            owner=LockOwner.from_dict(owner_raw),
            claims=tuple(NormalizedResourceClaim.from_dict(item) for item in claims_raw),
            acquired_at=acquired_at,
            heartbeat_at=heartbeat_at,
            computer_use_slot=slot,
        )


@dataclass(frozen=True, slots=True)
class ResourceConflict:
    requested_claim_id: str
    held_lock_id: str
    held_task_id: str
    held_claim_id: str


@dataclass(frozen=True, slots=True)
class AcquisitionResult:
    acquired: bool
    lock_id: str | None
    computer_use_slot: int | None
    conflicts: tuple[ResourceConflict, ...] = ()
    reason: str | None = None
    reused: bool = False


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    released_lock_ids: tuple[str, ...]
    active_lock_ids: tuple[str, ...]
    unresolved_lock_ids: tuple[str, ...]


class AuthoritativeWorkerState(str, Enum):
    ACTIVE = "active"
    TERMINAL = "terminal"
    ABSENT = "absent"
    UNKNOWN = "unknown"


def normalize_claim(claim: ResourceClaim, project_root: Path) -> NormalizedResourceClaim:
    """Return a stable identity without expanding environment variables or globs."""

    if "\x00" in claim.target:
        raise ValueError(f"resource claim {claim.id!r} contains a NUL byte")
    root = project_root.resolve(strict=False)
    if claim.kind == "glob":
        target = _normalize_glob(claim.target, root)
    elif claim.kind in {"path", "directory"}:
        raw = Path(claim.target)
        target = str((raw if raw.is_absolute() else root / raw).resolve(strict=False))
    else:
        # Named resource identities are intentionally case-insensitive and
        # whitespace-normalized. Cross-kind aliases are never inferred.
        target = " ".join(unicodedata.normalize("NFKC", claim.target).split()).casefold()
    return NormalizedResourceClaim(
        id=claim.id,
        kind=claim.kind,
        target=target,
        access=claim.access,
    )


def normalize_task_claims(task: Task, project_root: Path) -> tuple[NormalizedResourceClaim, ...]:
    return tuple(normalize_claim(claim, project_root) for claim in task.resources)


def claims_match(left: NormalizedResourceClaim, right: NormalizedResourceClaim) -> bool:
    if left.kind in FILESYSTEM_RESOURCE_KINDS and right.kind in FILESYSTEM_RESOURCE_KINDS:
        return _filesystem_targets_overlap(left, right)
    return left.kind == right.kind and left.target == right.target


def claims_conflict(left: NormalizedResourceClaim, right: NormalizedResourceClaim) -> bool:
    if not claims_match(left, right):
        return False
    if "exclusive" in {left.access, right.access}:
        return True
    return not (left.access == "read" and right.access == "read")


def validate_persisted_resource_state(
    raw_locks: Sequence[object],
    raw_journal: Sequence[object],
    sequence: int,
) -> tuple[DurableResourceLock, ...]:
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("resource journal sequence must be a non-negative integer")
    if not isinstance(raw_locks, list):
        raise ValueError("resource_locks must be an array")
    if not isinstance(raw_journal, list):
        raise ValueError("resource_lock_journal must be an array")
    if not all(isinstance(item, Mapping) for item in raw_locks):
        raise ValueError("resource_locks entries must be objects")
    locks = tuple(DurableResourceLock.from_dict(item) for item in raw_locks)
    lock_ids = [lock.lock_id for lock in locks]
    owner_tokens = [lock.owner.ownership_token for lock in locks]
    task_ids = [lock.owner.task_id for lock in locks]
    slots = [lock.computer_use_slot for lock in locks if lock.computer_use_slot is not None]
    if len(lock_ids) != len(set(lock_ids)):
        raise ValueError("resource lock ids must be unique")
    if len(owner_tokens) != len(set(owner_tokens)):
        raise ValueError("resource lock ownership tokens must be unique")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("a task must not have more than one held resource lock")
    if len(slots) != len(set(slots)):
        raise ValueError("computer_use slots must not have duplicate owners")
    for lock in locks:
        claim_ids = [claim.id for claim in lock.claims]
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError(f"resource lock {lock.lock_id!r} has duplicate claim ids")
    for index, lock in enumerate(locks):
        for other in locks[index + 1 :]:
            if any(
                claims_conflict(left, right)
                for left in lock.claims
                for right in other.claims
            ):
                raise ValueError(
                    f"held resource locks {lock.lock_id!r} and {other.lock_id!r} conflict"
                )

    journal_sequences: list[int] = []
    acquired_lock_ids: set[str] = set()
    acquired_owner_tokens: set[str] = set()
    replayed: dict[str, tuple[str, str, tuple[str, ...], int | None]] = {}
    for raw in raw_journal:
        if not isinstance(raw, Mapping):
            raise ValueError("resource lock journal entries must be objects")
        _require_exact_keys(
            raw,
            {
                "sequence",
                "event",
                "lock_id",
                "ownership_token",
                "task_id",
                "at",
                "claim_ids",
                "computer_use_slot",
                "reason",
            },
            "resource lock journal entry",
        )
        item_sequence = raw["sequence"]
        if isinstance(item_sequence, bool) or not isinstance(item_sequence, int) or item_sequence <= 0:
            raise ValueError("resource lock journal sequence must be a positive integer")
        event = _nonempty_string(raw["event"], "resource lock journal event")
        if event not in LOCK_EVENTS:
            raise ValueError(f"unknown resource lock journal event {event!r}")
        lock_id = _nonempty_string(raw["lock_id"], "journal lock id")
        ownership_token = _nonempty_string(raw["ownership_token"], "journal ownership token")
        task_id = _nonempty_string(raw["task_id"], "journal task id")
        _timestamp(raw["at"], "journal timestamp")
        if not isinstance(raw["claim_ids"], list) or not all(
            isinstance(item, str) and item for item in raw["claim_ids"]
        ):
            raise ValueError("journal claim_ids must be an array of non-empty strings")
        claim_ids = tuple(raw["claim_ids"])
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("journal claim_ids must not contain duplicates")
        slot = raw["computer_use_slot"]
        if slot is not None and (
            isinstance(slot, bool) or not isinstance(slot, int) or slot < 0
        ):
            raise ValueError("journal computer_use_slot must be a non-negative integer or null")
        reason = _optional_string(raw["reason"], "journal reason")
        journal_sequences.append(item_sequence)
        if event == "acquired":
            if lock_id in acquired_lock_ids:
                raise ValueError(f"resource lock {lock_id!r} has duplicate acquisition events")
            if ownership_token in acquired_owner_tokens:
                raise ValueError(
                    f"ownership token {ownership_token!r} has duplicate acquisition events"
                )
            if reason is not None:
                raise ValueError("resource lock acquisition events must not have a reason")
            acquired_lock_ids.add(lock_id)
            acquired_owner_tokens.add(ownership_token)
            replayed[lock_id] = (
                ownership_token,
                task_id,
                claim_ids,
                slot,
            )
        else:
            if reason is None:
                raise ValueError("resource lock release events require a reason")
            active = replayed.get(lock_id)
            expected = (ownership_token, task_id, claim_ids, slot)
            if active is None:
                raise ValueError(
                    f"resource lock {lock_id!r} was released without an active acquisition"
                )
            if active != expected:
                raise ValueError(
                    f"resource lock {lock_id!r} release metadata differs from acquisition"
                )
            del replayed[lock_id]

    if journal_sequences != list(range(1, sequence + 1)):
        raise ValueError("resource lock journal must be contiguous and end at its sequence")
    held = {
        lock.lock_id: (
            lock.owner.ownership_token,
            lock.owner.task_id,
            tuple(claim.id for claim in lock.claims),
            lock.computer_use_slot,
        )
        for lock in locks
    }
    if replayed != held:
        raise ValueError("held resource locks do not match the replayed durable journal")
    return locks


def build_scheduler_availability(
    plan: Plan,
    state: RunState,
    project_root: Path,
) -> SchedulerAvailability:
    """Build lock availability plus pairwise candidate conflicts for M2."""

    locks = validate_persisted_resource_state(
        state.resource_locks,
        state.resource_lock_journal,
        state.resource_journal_sequence,
    )
    normalized = {task.id: normalize_task_claims(task, project_root) for task in plan.tasks}
    used_computer_slots = sum(lock.computer_use_slot is not None for lock in locks)
    computer_limit = min(plan.computer_use_slots, state.computer_use_slots)
    incident_snapshot = PipelineIncidentStore(
        project_root / ".codex-autopilot"
    ).status_snapshot()
    paused_by_incident = set(incident_snapshot["paused_task_ids"])
    incident_reasons: dict[str, tuple[str, ...]] = {}
    for incident in incident_snapshot["incidents"]:
        reason = f"incident:{incident['incident_id']}:{incident['phase']}"
        for task_id in incident["affected_task_ids"]:
            if task_id in plan.task_map and task_id in paused_by_incident:
                incident_reasons.setdefault(task_id, ())
                incident_reasons[task_id] += (reason,)
    available: dict[str, bool] = {}
    pairwise: dict[str, frozenset[str]] = {}

    for task in plan.tasks:
        conflicts_held = any(
            claims_conflict(requested, held)
            for requested in normalized[task.id]
            for lock in locks
            for held in lock.claims
        )
        computer_available = not (
            task.execution_mode == "computer_use" and used_computer_slots >= computer_limit
        )
        available[task.id] = (
            not conflicts_held
            and computer_available
            and task.id not in paused_by_incident
        )

    for task in plan.tasks:
        conflicts: set[str] = set()
        for other in plan.tasks:
            if task.id == other.id:
                continue
            if any(
                claims_conflict(left, right)
                for left in normalized[task.id]
                for right in normalized[other.id]
            ):
                conflicts.add(other.id)
        pairwise[task.id] = frozenset(conflicts)
    return SchedulerAvailability(
        resource_available=available,
        resource_conflicts=pairwise,
        blocked_reasons=incident_reasons,
    )


def acquire_resources_in_state(
    plan: Plan,
    state: RunState,
    project_root: Path,
    task_id: str,
    owner: LockOwner,
    *,
    execution_mode: str | None = None,
    now: str | None = None,
) -> AcquisitionResult:
    """Acquire a task bundle in an already-locked in-memory transaction.

    The Desktop lifecycle uses this primitive so the READY->RUNNING edge,
    reservation token, resource ownership, and launch descriptor land in one
    atomic ``run-state.json`` replacement. ``ResourceLockCoordinator.acquire``
    remains the standalone compatibility wrapper.
    """

    if task_id not in plan.task_map:
        raise ValueError(f"unknown task id {task_id!r}")
    if owner.task_id != task_id:
        raise ValueError("lock owner task id must match the acquired task")
    LockOwner.from_dict(owner.to_dict())
    if state.run_id != owner.run_id:
        raise ValueError("lock owner run id does not match durable run state")
    if state.graph_version != plan.graph_version:
        raise ValueError("resource coordinator graph version mismatch")
    if state.task_states and owner.task_id not in state.task_states:
        raise ValueError("lock owner task does not exist in durable task state")
    expected_attempt = state.task_attempts.get(owner.task_id)
    if expected_attempt is not None and owner.attempt != expected_attempt:
        raise ValueError("lock owner attempt does not match durable task attempt")

    effective_execution_mode = execution_mode or plan.task_map[task_id].execution_mode
    if effective_execution_mode not in {"code", "computer_use"}:
        raise ValueError("resource execution_mode must be code or computer_use")
    timestamp = _timestamp(now or _utc_now(), "acquisition timestamp")
    locks = list(
        validate_persisted_resource_state(
            state.resource_locks,
            state.resource_lock_journal,
            state.resource_journal_sequence,
        )
    )
    claims = normalize_task_claims(plan.task_map[task_id], project_root)
    existing = next(
        (lock for lock in locks if lock.owner.ownership_token == owner.ownership_token),
        None,
    )
    if existing is not None:
        if existing.owner != owner or existing.claims != claims:
            raise ValueError("ownership token is already bound to different lock metadata")
        return AcquisitionResult(
            acquired=True,
            lock_id=existing.lock_id,
            computer_use_slot=existing.computer_use_slot,
            reused=True,
        )
    if any(lock.owner.task_id == task_id for lock in locks):
        return AcquisitionResult(False, None, None, reason="owner_conflict")

    conflicts = tuple(
        ResourceConflict(requested.id, lock.lock_id, lock.owner.task_id, held.id)
        for requested in claims
        for lock in locks
        for held in lock.claims
        if claims_conflict(requested, held)
    )
    if conflicts:
        return AcquisitionResult(
            False,
            None,
            None,
            conflicts=conflicts,
            reason="resource_conflict",
        )

    computer_use_slot: int | None = None
    if effective_execution_mode == "computer_use":
        limit = min(plan.computer_use_slots, state.computer_use_slots)
        used = {
            lock.computer_use_slot
            for lock in locks
            if lock.computer_use_slot is not None
        }
        computer_use_slot = next((slot for slot in range(limit) if slot not in used), None)
        if computer_use_slot is None:
            return AcquisitionResult(False, None, None, reason="computer_use_capacity")

    sequence = state.resource_journal_sequence + 1
    lock = DurableResourceLock(
        lock_id=f"resource-lock-{sequence:06d}",
        owner=owner,
        claims=claims,
        acquired_at=timestamp,
        heartbeat_at=timestamp,
        computer_use_slot=computer_use_slot,
    )
    state.resource_locks.append(lock.to_dict())
    append_resource_event(state, lock, "acquired", timestamp, reason=None)
    return AcquisitionResult(True, lock.lock_id, computer_use_slot)


def release_resources_in_state(
    state: RunState,
    ownership_token: str,
    *,
    reason: str,
    now: str | None = None,
) -> bool:
    """Release a task bundle inside the caller's existing transaction."""

    token = _nonempty_string(ownership_token, "ownership token")
    release_reason = _nonempty_string(reason, "release reason")
    timestamp = _timestamp(now or _utc_now(), "release timestamp")
    locks = list(
        validate_persisted_resource_state(
            state.resource_locks,
            state.resource_lock_journal,
            state.resource_journal_sequence,
        )
    )
    lock = next((item for item in locks if item.owner.ownership_token == token), None)
    if lock is None:
        return False
    state.resource_locks = [
        item.to_dict() for item in locks if item.owner.ownership_token != token
    ]
    append_resource_event(state, lock, "released", timestamp, reason=release_reason)
    return True


def append_resource_event(
    state: RunState,
    lock: DurableResourceLock,
    event: str,
    timestamp: str,
    *,
    reason: str | None,
) -> None:
    state.resource_journal_sequence += 1
    state.resource_lock_journal.append(
        {
            "sequence": state.resource_journal_sequence,
            "event": event,
            "lock_id": lock.lock_id,
            "ownership_token": lock.owner.ownership_token,
            "task_id": lock.owner.task_id,
            "at": timestamp,
            "claim_ids": [claim.id for claim in lock.claims],
            "computer_use_slot": lock.computer_use_slot,
            "reason": reason,
        }
    )


class ResourceLockCoordinator:
    """Atomic durable ownership over resource bundles in ``run-state.json``."""

    def __init__(self, state_store: StateStore, project_root: Path) -> None:
        self.state_store = state_store
        self.project_root = project_root.resolve(strict=False)
        self.transaction_path = state_store.state_dir / "resource-coordinator.lock"

    def snapshot(self) -> tuple[DurableResourceLock, ...]:
        with self._transaction():
            state = self.state_store.load()
            return validate_persisted_resource_state(
                state.resource_locks,
                state.resource_lock_journal,
                state.resource_journal_sequence,
            )

    def availability(self, plan: Plan) -> SchedulerAvailability:
        with self._transaction():
            return build_scheduler_availability(plan, self.state_store.load(), self.project_root)

    def acquire(
        self,
        plan: Plan,
        task_id: str,
        owner: LockOwner,
        *,
        now: str | None = None,
    ) -> AcquisitionResult:
        with self._transaction():
            state = self.state_store.load()
            result = acquire_resources_in_state(
                plan,
                state,
                self.project_root,
                task_id,
                owner,
                now=now,
            )
            if result.acquired and not result.reused:
                self.state_store.save(state)
            return result

    def release(
        self,
        ownership_token: str,
        *,
        reason: str,
        now: str | None = None,
    ) -> bool:
        with self._transaction():
            state = self.state_store.load()
            released = release_resources_in_state(
                state,
                ownership_token,
                reason=reason,
                now=now,
            )
            if released:
                self.state_store.save(state)
            return released

    def heartbeat(self, ownership_token: str, *, now: str | None = None) -> bool:
        token = _nonempty_string(ownership_token, "ownership token")
        timestamp = _timestamp(now or _utc_now(), "heartbeat timestamp")
        with self._transaction():
            state = self.state_store.load()
            locks = list(
                validate_persisted_resource_state(
                    state.resource_locks,
                    state.resource_lock_journal,
                    state.resource_journal_sequence,
                )
            )
            changed = False
            updated: list[DurableResourceLock] = []
            for lock in locks:
                if lock.owner.ownership_token == token:
                    lock = DurableResourceLock(
                        lock_id=lock.lock_id,
                        owner=lock.owner,
                        claims=lock.claims,
                        acquired_at=lock.acquired_at,
                        heartbeat_at=timestamp,
                        computer_use_slot=lock.computer_use_slot,
                    )
                    changed = True
                updated.append(lock)
            if changed:
                state.resource_locks = [item.to_dict() for item in updated]
                self.state_store.save(state)
            return changed

    def reconcile(
        self,
        authoritative_states: Mapping[str, AuthoritativeWorkerState | str],
        *,
        now: str | None = None,
    ) -> ReconciliationResult:
        """Release only owners authoritatively known terminal or absent.

        Missing and explicitly unknown owner states retain their locks. A stale
        wall-clock heartbeat is never, by itself, authority to permit a second
        writer.
        """

        resolved = {
            _nonempty_string(token, "authoritative ownership token"): (
                value if isinstance(value, AuthoritativeWorkerState) else AuthoritativeWorkerState(value)
            )
            for token, value in authoritative_states.items()
        }
        timestamp = _timestamp(now or _utc_now(), "reconciliation timestamp")
        with self._transaction():
            state = self.state_store.load()
            locks = list(
                validate_persisted_resource_state(
                    state.resource_locks,
                    state.resource_lock_journal,
                    state.resource_journal_sequence,
                )
            )
            kept: list[DurableResourceLock] = []
            released: list[str] = []
            active: list[str] = []
            unresolved: list[str] = []
            for lock in locks:
                owner_state = resolved.get(
                    lock.owner.ownership_token,
                    AuthoritativeWorkerState.UNKNOWN,
                )
                if owner_state in {
                    AuthoritativeWorkerState.TERMINAL,
                    AuthoritativeWorkerState.ABSENT,
                }:
                    released.append(lock.lock_id)
                    self._append_event(
                        state,
                        lock,
                        "reconciled_release",
                        timestamp,
                        reason=f"authoritative_owner_{owner_state.value}",
                    )
                else:
                    kept.append(lock)
                    if owner_state is AuthoritativeWorkerState.ACTIVE:
                        active.append(lock.lock_id)
                    else:
                        unresolved.append(lock.lock_id)
            if released:
                state.resource_locks = [lock.to_dict() for lock in kept]
                self.state_store.save(state)
            return ReconciliationResult(
                released_lock_ids=tuple(released),
                active_lock_ids=tuple(active),
                unresolved_lock_ids=tuple(unresolved),
            )


    @staticmethod
    def _append_event(
        state: RunState,
        lock: DurableResourceLock,
        event: str,
        timestamp: str,
        *,
        reason: str | None,
    ) -> None:
        append_resource_event(state, lock, event, timestamp, reason=reason)

    def transaction(self) -> Iterator[None]:
        """Public shared transaction boundary for deterministic lifecycle work."""

        return self._transaction()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.state_store.state_dir.mkdir(parents=True, exist_ok=True)
        thread_lock = _thread_lock_for(self.transaction_path)
        with thread_lock:
            handle = self.transaction_path.open("a", encoding="utf-8")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()


_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve(strict=False))
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _filesystem_targets_overlap(
    left: NormalizedResourceClaim,
    right: NormalizedResourceClaim,
) -> bool:
    if left.kind == "path" and right.kind == "path":
        return left.target == right.target
    if left.kind == "directory" and right.kind == "directory":
        return _paths_nested(left.target, right.target)
    if left.kind == "directory" and right.kind == "path":
        return _is_within(right.target, left.target)
    if left.kind == "path" and right.kind == "directory":
        return _is_within(left.target, right.target)
    if left.kind == "glob" and right.kind == "path":
        return _glob_matches(left.target, right.target)
    if left.kind == "path" and right.kind == "glob":
        return _glob_matches(right.target, left.target)
    if left.kind == "glob" and right.kind == "glob":
        # Exact intersection of two arbitrary glob languages is unnecessary
        # for admission. Overlapping literal roots fail closed.
        return _paths_nested(_glob_static_root(left.target), _glob_static_root(right.target))
    if left.kind == "glob" and right.kind == "directory":
        return _paths_nested(_glob_static_root(left.target), right.target)
    if left.kind == "directory" and right.kind == "glob":
        return _paths_nested(left.target, _glob_static_root(right.target))
    raise AssertionError(f"unsupported filesystem resource pair: {left.kind}, {right.kind}")


def _normalize_glob(target: str, root: Path) -> str:
    parts = PurePath(target).parts
    magic_at = next((index for index, part in enumerate(parts) if _has_magic(part)), len(parts))
    literal_parts = parts[:magic_at]
    suffix = parts[magic_at:]
    if Path(target).is_absolute():
        prefix = Path(*literal_parts)
    else:
        prefix = root.joinpath(*literal_parts)
    normalized_prefix = prefix.resolve(strict=False).as_posix()
    if not suffix:
        return normalized_prefix
    separator = "" if normalized_prefix == "/" else "/"
    return normalized_prefix + separator + "/".join(suffix)


def _glob_static_root(pattern: str) -> str:
    parts = PurePath(pattern).parts
    magic_at = next((index for index, part in enumerate(parts) if _has_magic(part)), len(parts))
    literal = parts[:magic_at]
    if not literal:
        return "/"
    return str(Path(*literal))


def _glob_matches(pattern: str, path: str) -> bool:
    pattern_parts = pattern.split("/")
    path_parts = path.split("/")
    memo: dict[tuple[int, int], bool] = {}

    def match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = match(pattern_index + 1, path_index) or (
                path_index < len(path_parts) and match(pattern_index, path_index + 1)
            )
        else:
            result = path_index < len(path_parts) and fnmatch.fnmatchcase(
                path_parts[path_index],
                pattern_parts[pattern_index],
            ) and match(pattern_index + 1, path_index + 1)
        memo[key] = result
        return result

    return match(0, 0)


def _has_magic(part: str) -> bool:
    return any(character in part for character in "*?[")


def _paths_nested(left: str, right: str) -> bool:
    return _is_within(left, right) or _is_within(right, left)


def _is_within(path: str, directory: str) -> bool:
    candidate = Path(path)
    parent = Path(directory)
    return candidate == parent or parent in candidate.parents


def _require_exact_keys(raw: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(f"{label} keys are invalid; missing={missing}, unknown={unknown}")


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _nonempty_string(value, label)


def _timestamp(value: object, label: str) -> str:
    text = _nonempty_string(value, label)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return text
