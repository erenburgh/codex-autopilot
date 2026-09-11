from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterator, Mapping, Sequence


INCIDENT_STATE_SCHEMA_VERSION = 1
INCIDENT_STATE_FILE = "pipeline-incidents.json"
RECOVERY_LOCK_FILE = "pipeline-recovery.lock"
MAX_RECENT_EVENTS = 20
MAX_EVENT_CHARS = 2_000
MUTATING_TRANSPORT_OPERATIONS = frozenset({"create_thread", "send_message_to_thread"})
LEGACY_PERSISTED_AUTHORITY_KINDS = frozenset({"PIPELINE_RECOVERY_MANDATE"})


class IncidentClass(str, Enum):
    PRODUCTION = "PRODUCTION"
    PIPELINE = "PIPELINE"
    RUNTIME = "RUNTIME"
    INTEGRATION = "INTEGRATION"
    TOOLING = "TOOLING"
    POLICY = "POLICY"
    AMBIGUOUS_SIDE_EFFECT = "AMBIGUOUS_SIDE_EFFECT"


INFRASTRUCTURE_INCIDENT_CLASSES = frozenset(
    {
        IncidentClass.PIPELINE,
        IncidentClass.RUNTIME,
        IncidentClass.INTEGRATION,
        IncidentClass.TOOLING,
    }
)


class IncidentPhase(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTO_RECOVERY = "AUTO_RECOVERY"
    RECOVERED = "RECOVERED"
    AUTO_RECOVERY_FAILED = "AUTO_RECOVERY_FAILED"
    PIPELINE_ENGINEER = "PIPELINE_ENGINEER"
    RESOLVED = "RESOLVED"
    ESCALATE_TO_USER = "ESCALATE_TO_USER"


class SideEffectOutcome(str, Enum):
    NONE = "NONE"
    KNOWN_SUCCEEDED = "KNOWN_SUCCEEDED"
    KNOWN_FAILED = "KNOWN_FAILED"
    UNKNOWN = "UNKNOWN"


class AuthorityKind(str, Enum):
    USER_AUTHORIZED_TASK = "USER_AUTHORIZED_TASK"
    OFFICIAL_PLATFORM_CAPABILITY = "OFFICIAL_PLATFORM_CAPABILITY"
    AUTOPILOT_RUN = "AUTOPILOT_RUN"


class TransportStatus(str, Enum):
    RESERVED = "RESERVED"
    CLAIMED = "CLAIMED"
    SIDE_EFFECT_REQUESTED = "SIDE_EFFECT_REQUESTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    AMBIGUOUS = "AMBIGUOUS"
    FAILED = "FAILED"


class PipelineIncidentError(RuntimeError):
    pass


class AuthorizationTopologyError(PipelineIncidentError):
    pass


@dataclass(frozen=True, slots=True)
class IncidentSignal:
    signal_id: str
    code: str
    surface: IncidentClass
    summary: str
    affected_task_ids: tuple[str, ...]
    operation: str | None = None
    side_effect_outcome: SideEffectOutcome = SideEffectOutcome.NONE
    system_state: Mapping[str, Any] = field(default_factory=dict)
    recent_events: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class HealthcheckResult:
    name: str
    passed: bool
    checks: tuple[str, ...]
    observed_at: str


@dataclass(frozen=True, slots=True)
class RecoveryRunbook:
    id: str
    incident_classes: frozenset[IncidentClass]
    signal_codes: frozenset[str]
    actions: tuple[str, ...]
    healthcheck: str


@dataclass(frozen=True, slots=True)
class RecoveryClaim:
    incident_id: str
    token: str
    owner_id: str
    slot: int
    attempt: int
    runbook_id: str
    allowed_actions: tuple[str, ...]
    healthcheck: str


@dataclass(frozen=True, slots=True)
class AuthorityProof:
    kind: AuthorityKind
    evidence_id: str
    subject_thread_id: str


@dataclass(frozen=True, slots=True)
class TransportClaim:
    reservation_id: str
    token: str
    operation: str
    payload_sha256: str
    actor_thread_id: str
    authority_kind: AuthorityKind
    authority_evidence_id: str
    destination_task_id: str


READ_ONLY_DIAGNOSTIC_ACTIONS = (
    "inspect_bounded_system_state",
    "inspect_recent_events",
    "reconcile_durable_journal",
    "run_declared_healthcheck",
)

FORBIDDEN_ACTIONS = (
    "fix_production_quality_failures",
    "bypass_trust_or_permission_checks",
    "impersonate_or_speak_for_the_user",
    "change_global_codex_settings",
    "delete_project_state",
    "perform_destructive_or_unbounded_repairs",
    "repeat_ambiguous_create_thread_or_send_message_to_thread",
    "create_or_message_codex_tasks_without_real_user_authority_or_an_official_platform_capability",
)


RUNBOOKS = (
    RecoveryRunbook(
        id="restart-owned-runtime-child",
        incident_classes=frozenset({IncidentClass.PIPELINE, IncidentClass.RUNTIME}),
        signal_codes=frozenset({"pipeline_child_exited", "owned_runtime_process_crashed"}),
        actions=("reconcile_durable_journal", "restart_owned_runtime_child"),
        healthcheck="owned_runtime_child_healthy",
    ),
    RecoveryRunbook(
        id="reopen-local-runtime-channel",
        incident_classes=frozenset({IncidentClass.RUNTIME, IncidentClass.INTEGRATION}),
        signal_codes=frozenset({"runtime_channel_closed", "transient_local_rpc_unavailable"}),
        actions=("reconcile_durable_journal", "reopen_owned_local_channel"),
        healthcheck="local_runtime_round_trip",
    ),
    RecoveryRunbook(
        id="refresh-ephemeral-integration-metadata",
        incident_classes=frozenset({IncidentClass.INTEGRATION}),
        signal_codes=frozenset({"ephemeral_project_metadata_stale"}),
        actions=("refresh_ephemeral_project_metadata",),
        healthcheck="canonical_project_metadata_matches",
    ),
    RecoveryRunbook(
        id="rebuild-owned-tool-cache",
        incident_classes=frozenset({IncidentClass.TOOLING}),
        signal_codes=frozenset({"owned_ephemeral_tool_cache_stale"}),
        actions=("rebuild_owned_ephemeral_tool_cache",),
        healthcheck="tool_inventory_matches_expected_runtime",
    ),
)


def classify_incident(signal: IncidentSignal) -> IncidentClass:
    """Classify only structured fields; free-form prose never changes routing."""

    _validate_signal(signal)
    if (
        signal.operation in MUTATING_TRANSPORT_OPERATIONS
        and signal.side_effect_outcome is SideEffectOutcome.UNKNOWN
    ):
        return IncidentClass.AMBIGUOUS_SIDE_EFFECT
    return signal.surface


def select_runbook(signal: IncidentSignal) -> RecoveryRunbook | None:
    classification = classify_incident(signal)
    for runbook in RUNBOOKS:
        if classification in runbook.incident_classes and signal.code in runbook.signal_codes:
            return runbook
    return None


def requires_pipeline_engineer(incident: Mapping[str, Any]) -> bool:
    return (
        IncidentClass(str(incident["classification"])) in INFRASTRUCTURE_INCIDENT_CLASSES
        and IncidentPhase(str(incident["phase"])) is IncidentPhase.AUTO_RECOVERY_FAILED
    )


class PipelineIncidentStore:
    """Crash-safe incident, recovery, and transport control-plane state.

    This store is deliberately separate from production resource locks and worker
    slots. It never executes a shell command, creates a Codex task, answers an
    approval, or performs a transport side effect.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.expanduser().resolve()
        self.path = self.state_dir / INCIDENT_STATE_FILE
        self.lock_path = self.state_dir / RECOVERY_LOCK_FILE

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return _empty_state()
        return _validate_state(json.loads(self.path.read_text(encoding="utf-8")))

    def open_incident(
        self,
        signal: IncidentSignal,
        *,
        at: str,
        retry_budget: int = 2,
        retry_initial_seconds: int = 15,
        retry_maximum_seconds: int = 300,
    ) -> dict[str, Any]:
        classification = classify_incident(signal)
        runbook = select_runbook(signal)
        _positive(retry_budget, "retry_budget")
        _positive(retry_initial_seconds, "retry_initial_seconds")
        _positive(retry_maximum_seconds, "retry_maximum_seconds")
        incident_id = "incident-" + hashlib.sha256(signal.signal_id.encode("utf-8")).hexdigest()[:16]
        with self._transaction() as state:
            existing = _incident(state, incident_id, required=False)
            if existing is not None:
                return _copy(existing)
            incident = {
                "incident_id": incident_id,
                "signal_id": signal.signal_id,
                "code": signal.code,
                "classification": classification.value,
                "summary": _bounded(signal.summary, MAX_EVENT_CHARS),
                "affected_task_ids": list(signal.affected_task_ids),
                "operation": signal.operation,
                "side_effect_outcome": signal.side_effect_outcome.value,
                "phase": IncidentPhase.DEGRADED.value,
                "runbook_id": runbook.id if runbook else None,
                "recovery_attempts": 0,
                "retry_budget": retry_budget,
                "retry_initial_seconds": retry_initial_seconds,
                "retry_maximum_seconds": retry_maximum_seconds,
                "next_retry_at": None,
                "recovery_lock_token": None,
                "recovery_owner_id": None,
                "healthcheck": None,
                "system_state": _bounded_mapping(signal.system_state),
                "recent_events": _bounded_events(signal.recent_events),
                "created_at": at,
                "updated_at": at,
                "resolved_at": None,
            }
            state["incidents"].append(incident)
            _append_event(state, "incident_opened", at, incident=incident)
            return _copy(incident)

    def route_incident(self, incident_id: str, *, at: str) -> IncidentPhase:
        """Choose the deterministic owner without invoking a model."""

        with self._transaction() as state:
            incident = _incident(state, incident_id)
            phase = IncidentPhase(str(incident["phase"]))
            if phase is not IncidentPhase.DEGRADED:
                return phase
            classification = IncidentClass(str(incident["classification"]))
            if classification not in INFRASTRUCTURE_INCIDENT_CLASSES:
                incident["phase"] = IncidentPhase.ESCALATE_TO_USER.value
                incident["updated_at"] = at
                _append_event(
                    state,
                    "incident_routed_to_user",
                    at,
                    incident=incident,
                    detail="production, policy, and ambiguous side effects are outside Pipeline Engineer authority",
                )
                return IncidentPhase.ESCALATE_TO_USER
            if incident.get("runbook_id") is None:
                incident["phase"] = IncidentPhase.AUTO_RECOVERY_FAILED.value
                incident["updated_at"] = at
                _append_event(
                    state,
                    "auto_recovery_unavailable",
                    at,
                    incident=incident,
                    detail="no allowlisted runbook matches the structured signal",
                )
                return IncidentPhase.AUTO_RECOVERY_FAILED
            return IncidentPhase.DEGRADED

    def claim_auto_recovery(
        self,
        incident_id: str,
        *,
        owner_id: str,
        now_epoch: int | None = None,
        at: str,
    ) -> RecoveryClaim:
        owner = _nonempty(owner_id, "recovery owner")
        epoch = int(time.time()) if now_epoch is None else now_epoch
        with self._transaction() as state:
            incident = _incident(state, incident_id)
            if IncidentPhase(str(incident["phase"])) is not IncidentPhase.DEGRADED:
                raise PipelineIncidentError("auto recovery requires a DEGRADED incident")
            classification = IncidentClass(str(incident["classification"]))
            if classification not in INFRASTRUCTURE_INCIDENT_CLASSES:
                raise PipelineIncidentError("auto recovery is forbidden for non-infrastructure incidents")
            runbook = _runbook(str(incident.get("runbook_id") or ""))
            if runbook is None:
                raise PipelineIncidentError("auto recovery requires an allowlisted runbook")
            retry_at = incident.get("next_retry_at")
            if isinstance(retry_at, int) and retry_at > epoch:
                raise PipelineIncidentError(f"auto recovery backoff is active until epoch {retry_at}")
            attempt = int(incident["recovery_attempts"]) + 1
            if attempt > int(incident["retry_budget"]):
                raise PipelineIncidentError("auto recovery retry budget is exhausted")
            if state["recovery_slot"] is not None:
                raise PipelineIncidentError("the Pipeline Engineer recovery slot is occupied")
            token = _stable_token(incident_id, str(attempt), owner)
            state["recovery_slot"] = {
                "slot": 0,
                "incident_id": incident_id,
                "token": token,
                "owner_id": owner,
                "claimed_at": at,
            }
            incident["phase"] = IncidentPhase.AUTO_RECOVERY.value
            incident["recovery_attempts"] = attempt
            incident["recovery_lock_token"] = token
            incident["recovery_owner_id"] = owner
            incident["next_retry_at"] = None
            incident["updated_at"] = at
            _append_event(state, "auto_recovery_claimed", at, incident=incident, token=token)
            return RecoveryClaim(
                incident_id=incident_id,
                token=token,
                owner_id=owner,
                slot=0,
                attempt=attempt,
                runbook_id=runbook.id,
                allowed_actions=runbook.actions,
                healthcheck=runbook.healthcheck,
            )

    def record_recovery_action(
        self,
        incident_id: str,
        token: str,
        *,
        action: str,
        at: str,
        detail: str = "",
    ) -> None:
        with self._transaction() as state:
            incident = _incident(state, incident_id)
            runbook = _runbook(str(incident.get("runbook_id") or ""))
            self._require_recovery_claim(state, incident, token)
            if runbook is None or action not in runbook.actions:
                raise PipelineIncidentError(f"recovery action is not allowlisted: {action}")
            _append_event(
                state,
                "recovery_action_recorded",
                at,
                incident=incident,
                token=token,
                detail=json.dumps(
                    {"action": action, "detail": _bounded(detail, MAX_EVENT_CHARS)},
                    sort_keys=True,
                ),
            )

    def complete_auto_recovery(
        self,
        incident_id: str,
        token: str,
        *,
        success: bool,
        at: str,
        now_epoch: int | None = None,
        healthcheck: HealthcheckResult | None = None,
        reason: str = "",
    ) -> IncidentPhase:
        epoch = int(time.time()) if now_epoch is None else now_epoch
        with self._transaction() as state:
            incident = _incident(state, incident_id)
            self._require_recovery_claim(state, incident, token)
            if success:
                runbook = _runbook(str(incident.get("runbook_id") or ""))
                if runbook is None:
                    raise PipelineIncidentError("auto recovery requires its declared runbook")
                _require_passing_healthcheck(
                    healthcheck,
                    expected_name=runbook.healthcheck,
                )
                incident["healthcheck"] = _healthcheck_dict(healthcheck)
                incident["phase"] = IncidentPhase.RECOVERED.value
                event = "auto_recovery_healthcheck_passed"
            else:
                attempts = int(incident["recovery_attempts"])
                if attempts < int(incident["retry_budget"]):
                    delay = min(
                        int(incident["retry_maximum_seconds"]),
                        int(incident["retry_initial_seconds"]) * (2 ** max(0, attempts - 1)),
                    )
                    incident["next_retry_at"] = epoch + delay
                    incident["phase"] = IncidentPhase.DEGRADED.value
                    event = "auto_recovery_retry_scheduled"
                else:
                    incident["phase"] = IncidentPhase.AUTO_RECOVERY_FAILED.value
                    event = "auto_recovery_failed"
            incident["recovery_lock_token"] = None
            incident["recovery_owner_id"] = None
            incident["updated_at"] = at
            state["recovery_slot"] = None
            _append_event(
                state,
                event,
                at,
                incident=incident,
                token=token,
                detail=_bounded(reason, MAX_EVENT_CHARS),
            )
            return IncidentPhase(str(incident["phase"]))

    def reconcile_recovery_after_crash(
        self,
        authoritative_owner_states: Mapping[str, str],
        *,
        now_epoch: int | None = None,
        at: str,
    ) -> IncidentPhase:
        """Reconcile the recovery slot without treating silence as completion.

        Missing and UNKNOWN owner state retain the lock. A terminal recovery
        process never implies recovery success because the mandatory healthcheck
        has not been observed.
        """

        epoch = int(time.time()) if now_epoch is None else now_epoch
        allowed = {"ACTIVE", "UNKNOWN", "TERMINAL_SUCCEEDED", "TERMINAL_FAILED"}
        if any(value not in allowed for value in authoritative_owner_states.values()):
            raise ValueError("unknown authoritative recovery owner state")
        with self._transaction() as state:
            slot = state["recovery_slot"]
            if slot is None:
                return IncidentPhase.HEALTHY
            incident = _incident(state, str(slot["incident_id"]))
            token = str(slot["token"])
            owner_state = authoritative_owner_states.get(token, "UNKNOWN")
            if owner_state in {"ACTIVE", "UNKNOWN"}:
                return IncidentPhase.AUTO_RECOVERY

            attempts = int(incident["recovery_attempts"])
            if attempts < int(incident["retry_budget"]):
                delay = min(
                    int(incident["retry_maximum_seconds"]),
                    int(incident["retry_initial_seconds"]) * (2 ** max(0, attempts - 1)),
                )
                incident["phase"] = IncidentPhase.DEGRADED.value
                incident["next_retry_at"] = epoch + delay
                event = "recovery_process_ended_without_healthcheck"
            else:
                incident["phase"] = IncidentPhase.AUTO_RECOVERY_FAILED.value
                event = "recovery_process_failed_after_crash"
            incident["recovery_lock_token"] = None
            incident["recovery_owner_id"] = None
            incident["updated_at"] = at
            state["recovery_slot"] = None
            _append_event(
                state,
                event,
                at,
                incident=incident,
                token=token,
                detail=owner_state,
            )
            return IncidentPhase(str(incident["phase"]))

    def activate_pipeline_engineer(self, incident_id: str, *, at: str) -> dict[str, Any]:
        with self._transaction() as state:
            incident = _incident(state, incident_id)
            if not requires_pipeline_engineer(incident):
                raise PipelineIncidentError(
                    "a fresh Pipeline Engineer is allowed only after infrastructure auto-recovery fails"
                )
            incident["phase"] = IncidentPhase.PIPELINE_ENGINEER.value
            incident["updated_at"] = at
            _append_event(state, "pipeline_engineer_requested", at, incident=incident)
            return self._incident_package(state, incident)

    def ensure_pipeline_engineer(self, incident_id: str, *, at: str) -> dict[str, Any]:
        """Idempotently route an infrastructure incident to one engineer lane.

        A structured, definitively failed transport can have no safe automatic
        runbook.  In that case routing and activation must be one crash-safe
        transaction: repeated hook delivery returns the existing package and
        never appends another request or creates another incident.
        """

        with self._transaction() as state:
            incident = _incident(state, incident_id)
            phase = IncidentPhase(str(incident["phase"]))
            classification = IncidentClass(str(incident["classification"]))
            if classification not in INFRASTRUCTURE_INCIDENT_CLASSES:
                raise PipelineIncidentError(
                    "Pipeline Engineer activation is infrastructure-only"
                )
            if phase in {IncidentPhase.PIPELINE_ENGINEER, IncidentPhase.RESOLVED}:
                return self._incident_package(state, incident)
            if phase is IncidentPhase.DEGRADED and incident.get("runbook_id") is None:
                incident["phase"] = IncidentPhase.AUTO_RECOVERY_FAILED.value
                incident["updated_at"] = at
                _append_event(
                    state,
                    "auto_recovery_unavailable",
                    at,
                    incident=incident,
                    detail="no allowlisted runbook matches the structured signal",
                )
                phase = IncidentPhase.AUTO_RECOVERY_FAILED
            if phase is not IncidentPhase.AUTO_RECOVERY_FAILED:
                raise PipelineIncidentError(
                    "Pipeline Engineer requires an infrastructure incident with exhausted or unavailable auto-recovery"
                )
            incident["phase"] = IncidentPhase.PIPELINE_ENGINEER.value
            incident["updated_at"] = at
            _append_event(state, "pipeline_engineer_requested", at, incident=incident)
            return self._incident_package(state, incident)

    def complete_pipeline_engineer(
        self,
        incident_id: str,
        *,
        success: bool,
        at: str,
        healthcheck: HealthcheckResult | None = None,
        reason: str = "",
    ) -> IncidentPhase:
        with self._transaction() as state:
            incident = _incident(state, incident_id)
            if IncidentPhase(str(incident["phase"])) is not IncidentPhase.PIPELINE_ENGINEER:
                raise PipelineIncidentError("Pipeline Engineer completion requires PIPELINE_ENGINEER")
            if success:
                runbook = _runbook(str(incident.get("runbook_id") or ""))
                _require_passing_healthcheck(
                    healthcheck,
                    expected_name=runbook.healthcheck if runbook else None,
                )
                incident["healthcheck"] = _healthcheck_dict(healthcheck)
                incident["phase"] = IncidentPhase.RESOLVED.value
                incident["resolved_at"] = at
                event = "pipeline_engineer_resolved"
            else:
                incident["phase"] = IncidentPhase.ESCALATE_TO_USER.value
                event = "pipeline_engineer_escalated_to_user"
            incident["updated_at"] = at
            _append_event(
                state,
                event,
                at,
                incident=incident,
                detail=_bounded(reason, MAX_EVENT_CHARS),
            )
            return IncidentPhase(str(incident["phase"]))

    def invalidate_pipeline_engineer_resolution(
        self,
        incident_id: str,
        *,
        at: str,
        reason: str,
    ) -> dict[str, Any]:
        """Fail closed when a post-healthcheck re-arm precondition changes."""

        with self._transaction() as state:
            incident = _incident(state, incident_id)
            phase = IncidentPhase(str(incident["phase"]))
            if phase is IncidentPhase.PIPELINE_ENGINEER:
                return self._incident_package(state, incident)
            if phase is not IncidentPhase.RESOLVED:
                raise PipelineIncidentError(
                    "only a resolved Pipeline Engineer incident can be invalidated"
                )
            if IncidentClass(str(incident["classification"])) not in INFRASTRUCTURE_INCIDENT_CLASSES:
                raise PipelineIncidentError(
                    "Pipeline Engineer resolution invalidation is infrastructure-only"
                )
            incident["phase"] = IncidentPhase.PIPELINE_ENGINEER.value
            incident["healthcheck"] = None
            incident["resolved_at"] = None
            incident["updated_at"] = at
            _append_event(
                state,
                "pipeline_engineer_resolution_invalidated",
                at,
                incident=incident,
                detail=_bounded(reason, MAX_EVENT_CHARS),
            )
            return self._incident_package(state, incident)

    def resolve_recovered(self, incident_id: str, *, at: str) -> None:
        with self._transaction() as state:
            incident = _incident(state, incident_id)
            if IncidentPhase(str(incident["phase"])) is not IncidentPhase.RECOVERED:
                raise PipelineIncidentError("only a healthchecked RECOVERED incident can resolve")
            if not _healthcheck_passed(incident):
                raise PipelineIncidentError("resume is forbidden until the healthcheck passes")
            incident["phase"] = IncidentPhase.RESOLVED.value
            incident["resolved_at"] = at
            incident["updated_at"] = at
            _append_event(state, "incident_resolved", at, incident=incident)

    def paused_task_ids(self) -> frozenset[str]:
        paused: set[str] = set()
        for incident in self.load()["incidents"]:
            phase = IncidentPhase(str(incident["phase"]))
            if phase not in {IncidentPhase.RECOVERED, IncidentPhase.RESOLVED}:
                paused.update(str(item) for item in incident["affected_task_ids"])
        return frozenset(paused)

    def can_resume_task(self, task_id: str) -> bool:
        task = _nonempty(task_id, "task id")
        for incident in self.load()["incidents"]:
            if task not in incident["affected_task_ids"]:
                continue
            phase = IncidentPhase(str(incident["phase"]))
            if phase is IncidentPhase.RESOLVED:
                continue
            if phase is IncidentPhase.RECOVERED and _healthcheck_passed(incident):
                continue
            return False
        return True

    def status_snapshot(self) -> dict[str, Any]:
        state = self.load()
        open_incidents = [
            _copy(item)
            for item in state["incidents"]
            if IncidentPhase(str(item["phase"])) is not IncidentPhase.RESOLVED
        ]
        if not open_incidents:
            phase = IncidentPhase.HEALTHY
        else:
            phase = max(
                (IncidentPhase(str(item["phase"])) for item in open_incidents),
                key=_phase_priority,
            )
        paused = sorted(
            {
                str(task_id)
                for incident in state["incidents"]
                if IncidentPhase(str(incident["phase"]))
                not in {IncidentPhase.RECOVERED, IncidentPhase.RESOLVED}
                for task_id in incident["affected_task_ids"]
            }
        )
        return {
            "role": "Pipeline Engineer · On call",
            "phase": phase.value,
            "incident_count": len(open_incidents),
            "paused_task_ids": paused,
            "recovery_slot": _copy(state["recovery_slot"]),
            "incidents": open_incidents,
            "pending_transport": [
                _copy(item)
                for item in state["transport_reservations"]
                if item["status"] not in {TransportStatus.ACKNOWLEDGED.value, TransportStatus.FAILED.value}
            ],
        }

    def incident_package(self, incident_id: str) -> dict[str, Any]:
        state = self.load()
        incident = _incident(state, incident_id)
        return self._incident_package(state, incident)

    def reserve_transport_from_lifecycle(
        self,
        run_state: Any,
        *,
        causal_event_sequence: int,
        operation: str,
        payload_sha256: str,
        destination_task_id: str,
        at: str,
    ) -> dict[str, Any]:
        """Project a verified M7 completion event into a transport reservation.

        The reservation proves causality only. It intentionally carries no
        authority. A causal task may consume it only after Pipeline Engineer
        recovery binds a separate, exact run-scoped mandate claim.
        """

        if operation not in MUTATING_TRANSPORT_OPERATIONS:
            raise ValueError("transport operation is not allowlisted")
        _sha256(payload_sha256)
        destination = _nonempty(destination_task_id, "destination task id")
        events = [
            item
            for item in getattr(run_state, "lifecycle_journal", ())
            if item.get("sequence") == causal_event_sequence
        ]
        if len(events) != 1:
            raise AuthorizationTopologyError("causal lifecycle event is missing or non-unique")
        event = events[0]
        if event.get("event") != "turn_completed":
            raise AuthorizationTopologyError("transport requires an authoritative turn_completed event")
        causal_thread_id = _nonempty(str(event.get("thread_id") or ""), "causal thread id")
        causal_turn_id = _nonempty(str(event.get("turn_id") or ""), "causal turn id")
        causal_task_id = _nonempty(str(event.get("task_id") or ""), "causal task id")
        run_id = _nonempty(str(getattr(run_state, "run_id", "")), "run id")
        reservation_id = "transport-" + hashlib.sha256(
            (
                f"{run_id}:{causal_event_sequence}:{operation}:"
                f"{destination}:{payload_sha256}"
            ).encode("utf-8")
        ).hexdigest()[:20]
        with self._transaction() as state:
            existing = _transport(state, reservation_id, required=False)
            if existing is not None:
                if existing["payload_sha256"] != payload_sha256:
                    raise AuthorizationTopologyError("transport reservation payload changed")
                return _copy(existing)
            reservation = {
                "reservation_id": reservation_id,
                "run_id": run_id,
                "operation": operation,
                "payload_sha256": payload_sha256,
                "destination_task_id": destination,
                "causal": {
                    "journal_sequence": causal_event_sequence,
                    "task_id": causal_task_id,
                    "thread_id": causal_thread_id,
                    "turn_id": causal_turn_id,
                },
                "status": TransportStatus.RESERVED.value,
                "actor_thread_id": None,
                "authority_kind": None,
                "authority_evidence_id": None,
                "claim_token": None,
                "receipt_id": None,
                "created_at": at,
                "updated_at": at,
            }
            state["transport_reservations"].append(reservation)
            _append_event(
                state,
                "transport_reserved_without_authority",
                at,
                transport=reservation,
            )
            return _copy(reservation)

    def claim_transport(
        self,
        reservation_id: str,
        *,
        actor_thread_id: str,
        proof: AuthorityProof,
        at: str,
    ) -> TransportClaim:
        actor = _nonempty(actor_thread_id, "transport actor thread id")
        evidence = _nonempty(proof.evidence_id, "authority evidence id")
        if proof.subject_thread_id != actor:
            raise AuthorizationTopologyError("authority proof is bound to another task")
        if not isinstance(proof.kind, AuthorityKind):
            raise AuthorizationTopologyError("forwarded user text is not transport authority")
        with self._transaction() as state:
            reservation = _transport(state, reservation_id)
            if reservation["status"] != TransportStatus.RESERVED.value:
                raise AuthorizationTopologyError("transport reservation is not claimable")
            causal_actor = reservation["causal"]["thread_id"] == actor
            if causal_actor and proof.kind is not AuthorityKind.AUTOPILOT_RUN:
                raise AuthorizationTopologyError(
                    "the causal task requires the durable Autopilot run authorization"
                )
            if not causal_actor and proof.kind is AuthorityKind.AUTOPILOT_RUN:
                raise AuthorizationTopologyError(
                    "Autopilot run authorization is bound to the causal predecessor task"
                )
            token = _stable_token(reservation_id, actor, proof.kind.value, evidence)
            reservation.update(
                {
                    "status": TransportStatus.CLAIMED.value,
                    "actor_thread_id": actor,
                    "authority_kind": proof.kind.value,
                    "authority_evidence_id": evidence,
                    "claim_token": token,
                    "updated_at": at,
                }
            )
            _append_event(state, "transport_claimed", at, transport=reservation, token=token)
            return TransportClaim(
                reservation_id=reservation_id,
                token=token,
                operation=str(reservation["operation"]),
                payload_sha256=str(reservation["payload_sha256"]),
                actor_thread_id=actor,
                authority_kind=proof.kind,
                authority_evidence_id=evidence,
                destination_task_id=str(reservation["destination_task_id"]),
            )

    def verify_transport_claim(self, claim: TransportClaim) -> None:
        """Verify that a claim was issued by this durable authority store.

        Constructing a ``TransportClaim`` value is not itself authority. The
        exact claim must already exist in the crash-safe journal and still be
        awaiting its one transport side effect.
        """

        if not isinstance(claim, TransportClaim) or not isinstance(
            claim.authority_kind, AuthorityKind
        ):
            raise AuthorizationTopologyError("transport claim type is invalid")
        reservation = _transport(self.load(), claim.reservation_id)
        expected = {
            "status": TransportStatus.CLAIMED.value,
            "claim_token": claim.token,
            "operation": claim.operation,
            "payload_sha256": claim.payload_sha256,
            "actor_thread_id": claim.actor_thread_id,
            "authority_kind": claim.authority_kind.value,
            "authority_evidence_id": claim.authority_evidence_id,
            "destination_task_id": claim.destination_task_id,
        }
        if any(reservation.get(key) != value for key, value in expected.items()):
            raise AuthorizationTopologyError(
                "transport claim does not match the durable authority journal"
            )

    def mark_transport_requested(
        self,
        reservation_id: str,
        token: str,
        *,
        actor_thread_id: str,
        at: str,
    ) -> None:
        with self._transaction() as state:
            reservation = _transport(state, reservation_id)
            _require_transport_claim(reservation, token, actor_thread_id)
            if reservation["status"] != TransportStatus.CLAIMED.value:
                raise AuthorizationTopologyError("transport side effect was already requested")
            reservation["status"] = TransportStatus.SIDE_EFFECT_REQUESTED.value
            reservation["updated_at"] = at
            _append_event(state, "transport_side_effect_requested", at, transport=reservation, token=token)

    def reconcile_transport(
        self,
        reservation_id: str,
        *,
        outcome: SideEffectOutcome,
        at: str,
        receipt_id: str | None = None,
        detail: str = "",
    ) -> TransportStatus:
        if outcome is SideEffectOutcome.NONE:
            raise ValueError("transport reconciliation requires an authoritative outcome")
        with self._transaction() as state:
            reservation = _transport(state, reservation_id)
            current = TransportStatus(str(reservation["status"]))
            if current not in {TransportStatus.SIDE_EFFECT_REQUESTED, TransportStatus.AMBIGUOUS}:
                raise AuthorizationTopologyError("transport is not awaiting reconciliation")
            if outcome is SideEffectOutcome.KNOWN_SUCCEEDED:
                reservation["receipt_id"] = _nonempty(receipt_id or "", "transport receipt id")
                target = TransportStatus.ACKNOWLEDGED
                event = "transport_acknowledged"
            elif outcome is SideEffectOutcome.KNOWN_FAILED:
                target = TransportStatus.FAILED
                event = "transport_definitively_failed"
            else:
                target = TransportStatus.AMBIGUOUS
                event = "transport_outcome_ambiguous"
            reservation["status"] = target.value
            reservation["updated_at"] = at
            _append_event(
                state,
                event,
                at,
                transport=reservation,
                detail=_bounded(detail, MAX_EVENT_CHARS),
            )
            return target

    def _incident_package(
        self,
        state: Mapping[str, Any],
        incident: Mapping[str, Any],
    ) -> dict[str, Any]:
        classification = IncidentClass(str(incident["classification"]))
        if classification not in INFRASTRUCTURE_INCIDENT_CLASSES:
            raise PipelineIncidentError("Pipeline Engineer packages are infrastructure-only")
        runbook = _runbook(str(incident.get("runbook_id") or ""))
        allowed = list(READ_ONLY_DIAGNOSTIC_ACTIONS)
        if runbook:
            allowed.extend(action for action in runbook.actions if action not in allowed)
        recent = [
            _copy(item)
            for item in state["journal"]
            if item.get("incident_id") == incident["incident_id"]
        ][-MAX_RECENT_EVENTS:]
        return {
            "role": {"id": "pipeline-engineer", "name": "Pipeline Engineer · On call"},
            "incident": _copy(incident),
            "system_state": _copy(incident["system_state"]),
            "recent_events": recent or _copy(incident["recent_events"]),
            "allowed_actions": allowed,
            "forbidden_actions": list(FORBIDDEN_ACTIONS),
            "recovery": {
                "slot": _copy(state["recovery_slot"]),
                "attempts": incident["recovery_attempts"],
                "retry_budget": incident["retry_budget"],
                "next_retry_at": incident["next_retry_at"],
                "runbook_id": incident["runbook_id"],
                "healthcheck_required_before_resume": True,
            },
        }

    @staticmethod
    def _require_recovery_claim(
        state: Mapping[str, Any],
        incident: Mapping[str, Any],
        token: str,
    ) -> None:
        slot = state.get("recovery_slot")
        if (
            IncidentPhase(str(incident["phase"])) is not IncidentPhase.AUTO_RECOVERY
            or incident.get("recovery_lock_token") != token
            or not isinstance(slot, Mapping)
            or slot.get("incident_id") != incident["incident_id"]
            or slot.get("token") != token
        ):
            raise PipelineIncidentError("recovery lock or slot ownership does not match")

    @contextmanager
    def _transaction(self) -> Iterator[dict[str, Any]]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                state = self.load()
                yield state
                _validate_state(state)
                _atomic_json(self.path, state)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def render_pipeline_status(snapshot: Mapping[str, Any]) -> str:
    lines = [
        f"Pipeline Engineer · On call — {snapshot['phase']}",
        f"Open incidents: {snapshot['incident_count']}",
    ]
    paused = snapshot.get("paused_task_ids") or []
    lines.append(f"Affected tasks paused: {', '.join(paused) if paused else 'none'}")
    slot = snapshot.get("recovery_slot")
    lines.append(
        "Recovery slot: free"
        if not slot
        else f"Recovery slot: incident={slot['incident_id']} owner={slot['owner_id']}"
    )
    pending = snapshot.get("pending_transport") or []
    lines.append(f"Pending authorized transport: {len(pending)}")
    for incident in snapshot.get("incidents") or []:
        lines.append(
            f"- {incident['incident_id']}: {incident['classification']} / {incident['phase']} — {incident['summary']}"
        )
    return "\n".join(lines)


def _validate_signal(signal: IncidentSignal) -> None:
    _nonempty(signal.signal_id, "signal id")
    _nonempty(signal.code, "signal code")
    _nonempty(signal.summary, "incident summary")
    if not isinstance(signal.surface, IncidentClass):
        raise ValueError("incident surface must be an IncidentClass")
    if not isinstance(signal.side_effect_outcome, SideEffectOutcome):
        raise ValueError("side effect outcome must be structured")
    if len(set(signal.affected_task_ids)) != len(signal.affected_task_ids):
        raise ValueError("affected task ids must be unique")
    for task_id in signal.affected_task_ids:
        _nonempty(task_id, "affected task id")
    if signal.operation and signal.operation not in MUTATING_TRANSPORT_OPERATIONS:
        raise ValueError("incident operation is not allowlisted")


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": INCIDENT_STATE_SCHEMA_VERSION,
        "sequence": 0,
        "recovery_slot": None,
        "incidents": [],
        "transport_reservations": [],
        "journal": [],
    }


def _validate_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != INCIDENT_STATE_SCHEMA_VERSION:
        raise PipelineIncidentError("unsupported or corrupt Pipeline Engineer state")
    required = {"sequence", "recovery_slot", "incidents", "transport_reservations", "journal"}
    if not required.issubset(raw):
        raise PipelineIncidentError("incomplete Pipeline Engineer state")
    if not all(isinstance(raw[key], list) for key in ("incidents", "transport_reservations", "journal")):
        raise PipelineIncidentError("Pipeline Engineer collections must be arrays")
    sequences = [item.get("sequence") for item in raw["journal"]]
    if sequences != list(range(1, int(raw["sequence"]) + 1)):
        raise PipelineIncidentError("Pipeline Engineer journal sequence is not contiguous")
    incident_ids = [item.get("incident_id") for item in raw["incidents"]]
    if len(set(incident_ids)) != len(incident_ids):
        raise PipelineIncidentError("duplicate Pipeline Engineer incident id")
    recovery_incidents = []
    for incident in raw["incidents"]:
        try:
            IncidentClass(str(incident["classification"]))
            phase = IncidentPhase(str(incident["phase"]))
        except (KeyError, ValueError) as exc:
            raise PipelineIncidentError("incident classification or phase is invalid") from exc
        if not isinstance(incident.get("affected_task_ids"), list) or not all(
            isinstance(item, str) and item for item in incident["affected_task_ids"]
        ):
            raise PipelineIncidentError("incident affected_task_ids are invalid")
        if phase is IncidentPhase.AUTO_RECOVERY:
            recovery_incidents.append(incident)
    if len(recovery_incidents) > 1:
        raise PipelineIncidentError("only one incident may own the recovery slot")
    reservation_ids = [item.get("reservation_id") for item in raw["transport_reservations"]]
    if len(set(reservation_ids)) != len(reservation_ids):
        raise PipelineIncidentError("duplicate transport reservation id")
    for reservation in raw["transport_reservations"]:
        try:
            status = TransportStatus(str(reservation["status"]))
        except (KeyError, ValueError) as exc:
            raise PipelineIncidentError("transport reservation status is invalid") from exc
        if status is not TransportStatus.RESERVED:
            if reservation.get("authority_kind") not in (
                {item.value for item in AuthorityKind}
                | LEGACY_PERSISTED_AUTHORITY_KINDS
            ):
                raise PipelineIncidentError("claimed transport lacks valid authority")
            if not reservation.get("actor_thread_id") or not reservation.get("claim_token"):
                raise PipelineIncidentError("claimed transport lacks actor ownership")
    slot = raw["recovery_slot"]
    if slot is not None:
        incident = _incident(raw, str(slot.get("incident_id") or ""))
        if (
            incident.get("phase") != IncidentPhase.AUTO_RECOVERY.value
            or incident.get("recovery_lock_token") != slot.get("token")
        ):
            raise PipelineIncidentError("recovery slot does not match its durable incident lock")
    return raw


def _append_event(
    state: dict[str, Any],
    event: str,
    at: str,
    *,
    incident: Mapping[str, Any] | None = None,
    transport: Mapping[str, Any] | None = None,
    token: str | None = None,
    detail: str = "",
) -> None:
    state["sequence"] += 1
    state["journal"].append(
        {
            "sequence": state["sequence"],
            "event": event,
            "at": at,
            "incident_id": incident.get("incident_id") if incident else None,
            "incident_phase": incident.get("phase") if incident else None,
            "reservation_id": transport.get("reservation_id") if transport else None,
            "transport_status": transport.get("status") if transport else None,
            "token": token,
            "detail": _bounded(detail, MAX_EVENT_CHARS),
        }
    )


def _incident(
    state: Mapping[str, Any], incident_id: str, *, required: bool = True
) -> dict[str, Any] | None:
    matches = [item for item in state["incidents"] if item.get("incident_id") == incident_id]
    if len(matches) == 1:
        return matches[0]
    if not matches and not required:
        return None
    raise PipelineIncidentError("unknown or non-unique incident id")


def _transport(
    state: Mapping[str, Any], reservation_id: str, *, required: bool = True
) -> dict[str, Any] | None:
    matches = [
        item for item in state["transport_reservations"]
        if item.get("reservation_id") == reservation_id
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches and not required:
        return None
    raise AuthorizationTopologyError("unknown or non-unique transport reservation")


def _runbook(runbook_id: str) -> RecoveryRunbook | None:
    return next((item for item in RUNBOOKS if item.id == runbook_id), None)


def _require_transport_claim(
    reservation: Mapping[str, Any], token: str, actor_thread_id: str
) -> None:
    if (
        reservation.get("claim_token") != token
        or reservation.get("actor_thread_id") != actor_thread_id
        or reservation.get("authority_kind") not in {item.value for item in AuthorityKind}
    ):
        raise AuthorizationTopologyError("transport claim ownership does not match")


def _require_passing_healthcheck(
    result: HealthcheckResult | None,
    *,
    expected_name: str | None = None,
) -> None:
    if result is None or not result.passed or not result.checks:
        raise PipelineIncidentError("a passing non-empty healthcheck is required before resume")
    _nonempty(result.name, "healthcheck name")
    _nonempty(result.observed_at, "healthcheck timestamp")
    if not all(isinstance(check, str) and check.strip() for check in result.checks):
        raise PipelineIncidentError("healthcheck observations must be non-empty strings")
    if expected_name is not None and result.name != expected_name:
        raise PipelineIncidentError(
            f"healthcheck must match the declared runbook check: {expected_name}"
        )


def _healthcheck_dict(result: HealthcheckResult | None) -> dict[str, Any]:
    assert result is not None
    return asdict(result)


def _healthcheck_passed(incident: Mapping[str, Any]) -> bool:
    check = incident.get("healthcheck")
    return isinstance(check, Mapping) and check.get("passed") is True and bool(check.get("checks"))


def _bounded_mapping(raw: Mapping[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(dict(raw), ensure_ascii=False, sort_keys=True, default=str)
    if len(encoded) > 16_000:
        raise ValueError("incident system state exceeds 16000 characters")
    return json.loads(encoded)


def _bounded_events(raw: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in raw[-MAX_RECENT_EVENTS:]:
        encoded = json.dumps(dict(item), ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) > MAX_EVENT_CHARS:
            encoded = json.dumps({"truncated_sha256": hashlib.sha256(encoded.encode()).hexdigest()})
        result.append(json.loads(encoded))
    return result


def _phase_priority(phase: IncidentPhase) -> int:
    order = {
        IncidentPhase.HEALTHY: 0,
        IncidentPhase.RESOLVED: 1,
        IncidentPhase.RECOVERED: 2,
        IncidentPhase.DEGRADED: 3,
        IncidentPhase.AUTO_RECOVERY: 4,
        IncidentPhase.AUTO_RECOVERY_FAILED: 5,
        IncidentPhase.PIPELINE_ENGINEER: 6,
        IncidentPhase.ESCALATE_TO_USER: 7,
    }
    return order[phase]


def _stable_token(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _bounded(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _nonempty(value: str, name: str) -> str:
    result = value.strip()
    if not result:
        raise ValueError(f"{name} must be non-empty")
    return result


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _sha256(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")
    return value
