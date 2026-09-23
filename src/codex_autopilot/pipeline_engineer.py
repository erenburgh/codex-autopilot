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
from typing import Any, Iterator, Mapping, Sequence

# The engineer's authority is declared separately and is out of reach for
# a repair: it fixes the runtime but does not redraw the bounds of what it
# may do.
from .engineer_authority import (
    ADVISORY_INCIDENT_CLASSES,
    AUTO_REPLAYABLE_ACTIONS,
    ENGINEER_LANE_CLASSES,
    RESOLVE_FORBIDDEN_CLASSES,
    FORBIDDEN_ACTIONS,
    INFRASTRUCTURE_INCIDENT_CLASSES,
    MUTATING_TRANSPORT_OPERATIONS,
    PROMOTION_THRESHOLD,
    READ_ONLY_DIAGNOSTIC_ACTIONS,
    RECOVERY_ACTIONS,
    REPAIR_ACTIONS,
    IncidentClass,
    SideEffectOutcome,
)


INCIDENT_STATE_SCHEMA_VERSION = 1
INCIDENT_STATE_FILE = "pipeline-incidents.json"
RECOVERY_LOCK_FILE = "pipeline-recovery.lock"
MAX_RECENT_EVENTS = 20
MAX_EVENT_CHARS = 2_000
LEGACY_PERSISTED_AUTHORITY_KINDS = frozenset({"PIPELINE_RECOVERY_MANDATE"})
# The code prefix of a ticket the stop door opened (blocked_runs). Named so a
# reader of the journal can tell a stop from a transport fault, and so no
# learned runbook ever stands between a stop and the on-call: a run that
# stopped needs someone to look, not a replay.
STOP_CODE_PREFIX = "run_stopped:"


class IncidentPhase(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    AUTO_RECOVERY = "AUTO_RECOVERY"
    RECOVERED = "RECOVERED"
    AUTO_RECOVERY_FAILED = "AUTO_RECOVERY_FAILED"
    PIPELINE_ENGINEER = "PIPELINE_ENGINEER"
    RESOLVED = "RESOLVED"
    ESCALATE_TO_USER = "ESCALATE_TO_USER"


class EscalationReason(str, Enum):
    """Rule R13: the closed list of reasons for turning to the user.

    The user takes no part in choosing how infrastructure bugs are fixed -
    that is DevOps' work on their behalf. An escalation is allowed only for
    one of these reasons, and the reason code is mandatory.
    """

    DANGEROUS_PERMISSION = "DANGEROUS_PERMISSION"
    GLOBAL_CONFIG_CHANGE = "GLOBAL_CONFIG_CHANGE"
    PROJECT_DAMAGE_RISK = "PROJECT_DAMAGE_RISK"
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"
    PRODUCT_DECISION = "PRODUCT_DECISION"
    ARCHITECTURE_DECISION = "ARCHITECTURE_DECISION"


ESCALATION_REASONS = frozenset(item.value for item in EscalationReason)
# What an escalation carries to the owner besides its code.
ESCALATION_FIELDS = (
    "diagnosis", "repaired", "decision_needed", "recommendation", "options", "scope",
)


def escalate_to_user(
    incident: dict[str, Any],
    reason: "EscalationReason | str",
    *,
    at: str,
    detail: str = "",
) -> None:
    """Move the incident to ESCALATE_TO_USER with a mandatory reason code.

    An escalation without a code, or with a code outside the list, is
    refused: that is exactly how the user stopped being the one who repairs
    the pipeline.
    """
    value = reason.value if isinstance(reason, EscalationReason) else str(reason)
    if value not in ESCALATION_REASONS:
        raise AuthorizationTopologyError(
            f"R13: an escalation requires a reason code from the closed list "
            f"{sorted(ESCALATION_REASONS)}; got {value!r}"
        )
    incident["phase"] = IncidentPhase.ESCALATE_TO_USER.value
    incident["escalation_reason"] = value
    incident["escalation_detail"] = detail
    incident["escalated_at"] = at


class TransportStatus(str, Enum):
    """The state of a transport reservation.

    The definition existed in no commit although the code referenced it in
    five places: any read of a state with a non-empty reservation list
    crashed with NameError. Tests did not catch it because none created
    reservations, while a live run has them.

    The values are restored from durable run state (ACKNOWLEDGED, CLAIMED)
    and from the places of use (RESERVED, FAILED).
    """

    RESERVED = "RESERVED"
    CLAIMED = "CLAIMED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FAILED = "FAILED"


class AuthorityKind(str, Enum):
    """On what authority a transport may be used.

    The set is deliberately narrow: only USER_AUTHORIZED_TASK is confirmed -
    it occurs in durable state. An extra member here would mean the system
    accepts an authority nobody ever introduced. PIPELINE_RECOVERY_MANDATE is
    not here: it is declared legacy in LEGACY_PERSISTED_AUTHORITY_KINDS and
    accepted only on already recorded reservations.
    """

    USER_AUTHORIZED_TASK = "USER_AUTHORIZED_TASK"


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
    # Where the on-call's thread is anchored when the ticket holds no task.
    # `affected_task_ids` pause their tasks; this pauses nothing.
    context_task_id: str = ""


@dataclass(frozen=True, slots=True)
class HealthcheckResult:
    name: str
    passed: bool
    checks: tuple[str, ...]
    observed_at: str


def classify_incident(signal: IncidentSignal) -> IncidentClass:
    """Classify only structured fields; free-form prose never changes routing."""

    _validate_signal(signal)
    if (
        signal.operation in MUTATING_TRANSPORT_OPERATIONS
        and signal.side_effect_outcome is SideEffectOutcome.UNKNOWN
    ):
        return IncidentClass.AMBIGUOUS_SIDE_EFFECT
    return signal.surface


SIGNATURE_VERSION = "v1"



def incident_signature(signal: IncidentSignal) -> str:
    """The normalized failure signature - the identity of a ticket.

    Computed ONLY from structural fields. Deliberately excluded:

    - signal_id: it has the attempt identifier mixed in, so the same failure
      looked new every time. On the live v0.9 run two of three incidents
      were one failure split apart by this field;
    - summary: free text, it varies from case to case and must not affect
      routing (the same grounds as in classify_incident);
    - affected_task_ids: a transport failure on M4 and on M9 is one
      infrastructure fault, not two.

    The signature answers "what broke", not "when and to whom".
    """

    parts = (
        SIGNATURE_VERSION,
        signal.code,
        classify_incident(signal).value,
        signal.operation or "-",
        signal.side_effect_outcome.value,
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class RecoveryRunbook:
    id: str
    incident_classes: frozenset[IncidentClass]
    signal_codes: frozenset[str]
    actions: tuple[str, ...]
    healthcheck: str


# The catalogue of deterministic runbooks. It is empty on purpose: entries
# are learned from two identical named repairs of one signature, never
# seeded from a single observation. Routing works; there are no automatic
# actions until they are learned, and that is visible rather than looking
# like forgotten code.
RUNBOOKS: tuple[RecoveryRunbook, ...] = ()


def select_runbook(signal: IncidentSignal) -> RecoveryRunbook | None:
    classification = classify_incident(signal)
    for runbook in RUNBOOKS:
        if classification in runbook.incident_classes and signal.code in runbook.signal_codes:
            return runbook
    return None


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
        signature = incident_signature(signal)
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
                "signature": signature,
                "summary": _bounded(signal.summary, MAX_EVENT_CHARS),
                "affected_task_ids": list(signal.affected_task_ids),
                "context_task_id": str(signal.context_task_id or ""),
                "operation": signal.operation,
                "side_effect_outcome": signal.side_effect_outcome.value,
                "phase": IncidentPhase.DEGRADED.value,
                "runbook_id": runbook.id if runbook else None,
                "runbook_healthcheck": runbook.healthcheck if runbook else None,
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
            if incident["runbook_id"] is None:
                learned = _promoted_runbook(state, signature)
                if learned is not None:
                    # The repair was learned on earlier repeats: no engineer needed.
                    incident["runbook_id"] = learned["id"]
                    incident["runbook_healthcheck"] = learned.get("healthcheck")
            state["incidents"].append(incident)
            occurrences = _record_signature(state, signature, incident, at=at)
            _append_event(state, "incident_opened", at, incident=incident)
            if occurrences > 1:
                # A repeat of the same fault is a known one, not a new mystery.
                _append_event(
                    state,
                    "incident_recurrence_observed",
                    at,
                    incident=incident,
                    detail=f"signature {signature} seen {occurrences} times",
                )
            return _copy(incident)

    def route_incident(self, incident_id: str, *, at: str) -> IncidentPhase:
        """Choose the deterministic owner without invoking a model."""

        with self._transaction() as state:
            incident = _incident(state, incident_id)
            phase = IncidentPhase(str(incident["phase"]))
            if phase is not IncidentPhase.DEGRADED:
                return phase
            classification = IncidentClass(str(incident["classification"]))
            if classification in ADVISORY_INCIDENT_CLASSES:
                # Production, policy and an ambiguous side effect used to go
                # straight to the owner here - the one road to her that
                # skipped DevOps. Her requirement has no exceptions: the
                # on-call looks first. It reads, diagnoses and hands the
                # decision up with a recommendation; it cannot close these
                # (RESOLVE_FORBIDDEN_CLASSES) and repairs nothing - its
                # package holds only diagnostics.
                incident["phase"] = IncidentPhase.PIPELINE_ENGINEER.value
                incident["updated_at"] = at
                _append_event(
                    state,
                    "pipeline_engineer_requested",
                    at,
                    incident=incident,
                    detail="advisory: diagnosis and a recommendation for the owner",
                )
                return IncidentPhase.PIPELINE_ENGINEER
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


    def begin_auto_recovery(
        self,
        incident_id: str,
        *,
        at: str,
        owner_id: str,
    ) -> dict[str, Any]:
        """Level 1: a deterministic attempt with no model involved.

        Takes the single recovery slot, spends one attempt of the budget and
        schedules the next with a growing delay. No model takes part here: if
        the repair is known, it is applied by itself.
        """

        _nonempty(owner_id, "owner_id")
        with self._transaction() as state:
            incident = _incident(state, incident_id)
            phase = IncidentPhase(str(incident["phase"]))
            if phase is not IncidentPhase.DEGRADED:
                raise PipelineIncidentError("auto-recovery requires a DEGRADED incident")
            if incident.get("runbook_id") is None:
                raise PipelineIncidentError(
                    "auto-recovery requires an allowlisted or promoted runbook"
                )
            attempts = int(incident["recovery_attempts"])
            if attempts >= int(incident["retry_budget"]):
                raise PipelineIncidentError("auto-recovery budget is already exhausted")
            slot = state.get("recovery_slot")
            if isinstance(slot, Mapping) and slot.get("incident_id") != incident_id:
                raise PipelineIncidentError("another incident owns the recovery slot")
            token = _stable_token(incident_id, str(attempts + 1), at)
            incident["recovery_attempts"] = attempts + 1
            incident["phase"] = IncidentPhase.AUTO_RECOVERY.value
            incident["recovery_lock_token"] = token
            incident["recovery_owner_id"] = owner_id
            incident["next_retry_at"] = _backoff_seconds(incident)
            incident["updated_at"] = at
            # owner_id is written here because the status reads it from here.
            # The writer stored two keys, the reader asked for a third - and
            # `status` crashed with KeyError exactly when a person came to
            # look into the busy slot.
            state["recovery_slot"] = {
                "incident_id": incident_id,
                "token": token,
                "owner_id": owner_id,
            }
            _append_event(
                state,
                "auto_recovery_started",
                at,
                incident=incident,
                detail=f"attempt {attempts + 1} of {incident['retry_budget']}",
            )
            return _copy(incident)

    def complete_auto_recovery(
        self,
        incident_id: str,
        *,
        token: str,
        success: bool,
        at: str,
        healthcheck: HealthcheckResult | None = None,
    ) -> IncidentPhase:
        """The outcome of level 1. An exhausted budget opens the way to level 2."""

        with self._transaction() as state:
            incident = _incident(state, incident_id)
            self._require_recovery_claim(state, incident, token)
            state["recovery_slot"] = None
            incident["recovery_lock_token"] = None
            incident["recovery_owner_id"] = None
            if success:
                _require_passing_healthcheck(
                    healthcheck,
                    expected_name=_expected_healthcheck(incident),
                )
                incident["healthcheck"] = _healthcheck_dict(healthcheck)
                incident["phase"] = IncidentPhase.RECOVERED.value
                incident["resolved_at"] = at
                incident["next_retry_at"] = None
                event = "auto_recovery_succeeded"
            elif int(incident["recovery_attempts"]) >= int(incident["retry_budget"]):
                incident["phase"] = IncidentPhase.AUTO_RECOVERY_FAILED.value
                event = "auto_recovery_exhausted"
            else:
                # The budget is not exhausted: the incident awaits the next attempt.
                incident["phase"] = IncidentPhase.DEGRADED.value
                event = "auto_recovery_attempt_failed"
            incident["updated_at"] = at
            _append_event(state, event, at, incident=incident)
            return IncidentPhase(str(incident["phase"]))

    def attempt_known_recovery(
        self,
        incident_id: str,
        *,
        at: str,
        owner_id: str,
    ) -> IncidentPhase:
        """Level 1 whole, in one go: take the slot and release it.

        A known fault must not raise a model session. The check here is
        deterministic and relies only on the incident record:

        - an ambiguous side effect must not be retried blindly (the same
          grounds on which classify_incident puts it in its own class);
        - an incident of a foreign class is not the pipeline engineer's job.

        If the conditions hold, the incident is marked recovered and the
        ordinary bounded retry goes its own way.

        The refusal branch is a guard, not a working path: routing brings
        neither an ambiguous side effect here (it goes to the user) nor a
        foreign class (no runbook is learned for it). The check is kept in
        case a future change opens such a path: blindly retrying an
        operation with an unknown side effect is exactly how extra threads
        appeared in a live run.

        The slot is not held between calls: an incident stuck in
        AUTO_RECOVERY would block recovery for everyone else.
        """

        incident = self.begin_auto_recovery(incident_id, at=at, owner_id=owner_id)
        token = str(incident["recovery_lock_token"])
        side_effect = SideEffectOutcome(str(incident["side_effect_outcome"]))
        classification = IncidentClass(str(incident["classification"]))
        safe = (
            side_effect is not SideEffectOutcome.UNKNOWN
            and classification in INFRASTRUCTURE_INCIDENT_CLASSES
        )
        check = _expected_healthcheck(incident) or "known_recovery_preconditions"
        return self.complete_auto_recovery(
            incident_id,
            token=token,
            success=safe,
            at=at,
            healthcheck=HealthcheckResult(
                name=check,
                passed=True,
                checks=(
                    f"side_effect_outcome={side_effect.value}",
                    f"classification={classification.value}",
                ),
                observed_at=at,
            )
            if safe
            else None,
        )

    def signature_ledger(self) -> dict[str, Any]:
        """The signature ledger: how often what broke and what repaired it."""

        return _copy(self.load().get("signatures", {}))

    def require_engineer_incident(self, incident_id: str) -> dict[str, Any]:
        """The ticket the engineer holds right now - or a refusal."""

        state = self.load()
        incident = _incident(state, incident_id)
        if IncidentPhase(str(incident["phase"])) is not IncidentPhase.PIPELINE_ENGINEER:
            raise PipelineIncidentError(
                f"incident {incident_id} is in phase {incident['phase']}: "
                "a runtime repair belongs to the engineer holding the incident"
            )
        return _copy(incident)

    def record_runtime_patch(
        self, incident_id: str, *, patch: Mapping[str, str], at: str
    ) -> dict[str, Any]:
        """Record a runtime code repair in the ticket's journal.

        A repair absent from the journal does not exist for next time. A
        code repair does not become a runbook - it must not be replayed
        blindly - but stays visible: which module, which hashes before and
        after, which test proved it.
        """

        with self._transaction() as state:
            incident = _incident(state, incident_id)
            if IncidentPhase(str(incident["phase"])) is not IncidentPhase.PIPELINE_ENGINEER:
                raise PipelineIncidentError(
                    "a runtime repair belongs to the engineer holding the incident"
                )
            patches = incident.setdefault("runtime_patches", [])
            patches.append(dict(patch))
            incident["updated_at"] = at
            _append_event(
                state,
                "runtime_patched",
                at,
                incident=incident,
                detail=json.dumps(dict(patch), ensure_ascii=False, sort_keys=True),
            )
            return _copy(incident)

    def record_engineer_action(
        self,
        incident_id: str,
        *,
        field: str,
        event: str,
        entry: Mapping[str, Any],
        at: str,
    ) -> dict[str, Any]:
        """Record what the on-call did to a stopped task on the ticket itself.

        A return or a plan change requested by the engineer is part of the
        ticket's story: the next engineer, her status card and the R23
        bound all read it from here. Only the engineer holding the ticket
        records it.
        """

        with self._transaction() as state:
            incident = _incident(state, incident_id)
            if IncidentPhase(str(incident["phase"])) is not IncidentPhase.PIPELINE_ENGINEER:
                raise PipelineIncidentError(
                    f"incident {incident_id} is in phase {incident['phase']}: only the "
                    "engineer holding it acts on its tasks"
                )
            incident.setdefault(field, []).append(_copy(dict(entry)))
            incident["updated_at"] = at
            _append_event(
                state,
                event,
                at,
                incident=incident,
                detail=_bounded(
                    json.dumps(dict(entry), ensure_ascii=False, sort_keys=True), MAX_EVENT_CHARS
                ),
            )
            return _copy(incident)

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
            if classification not in ENGINEER_LANE_CLASSES:
                raise PipelineIncidentError(
                    f"Pipeline Engineer activation does not know class {classification.value}"
                )
            if phase in {IncidentPhase.PIPELINE_ENGINEER, IncidentPhase.RESOLVED}:
                return self._incident_package(state, incident)
            if phase is IncidentPhase.DEGRADED and (
                classification in ADVISORY_INCIDENT_CLASSES
                or incident.get("runbook_id") is None
                or str(incident.get("code") or "").startswith(STOP_CODE_PREFIX)
            ):
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
        actions: Sequence[str] = (),
        note: str = "",
    ) -> IncidentPhase:
        with self._transaction() as state:
            incident = _incident(state, incident_id)
            if IncidentPhase(str(incident["phase"])) is not IncidentPhase.PIPELINE_ENGINEER:
                raise PipelineIncidentError("Pipeline Engineer completion requires PIPELINE_ENGINEER")
            if success and IncidentClass(str(incident["classification"])) in (
                RESOLVE_FORBIDDEN_CLASSES
            ):
                raise PipelineIncidentError(
                    f"a {incident['classification']} ticket is not the on-call's to close: "
                    "diagnose it and hand it to the owner with ESCALATE_TO_USER and a "
                    "recommendation"
                )
            if success:
                _require_named_actions(actions)
                _require_passing_healthcheck(
                    healthcheck,
                    expected_name=_expected_healthcheck(incident),
                )
                incident["healthcheck"] = _healthcheck_dict(healthcheck)
                incident["phase"] = IncidentPhase.RESOLVED.value
                incident["resolved_at"] = at
                event = "pipeline_engineer_resolved"
                promoted = _record_resolution(
                    state, incident, actions=actions, healthcheck=healthcheck, at=at, note=note
                )
            else:
                # The Pipeline Engineer has exhausted its means - the one
                # reason it may turn to the user (R13).
                escalate_to_user(
                    incident,
                    EscalationReason.RECOVERY_EXHAUSTED,
                    at=at,
                    detail="Pipeline Engineer could not resolve the incident",
                )
                event = "pipeline_engineer_escalated_to_user"
                promoted = None
            incident["updated_at"] = at
            _append_event(
                state,
                event,
                at,
                incident=incident,
                detail=_bounded(reason, MAX_EVENT_CHARS),
            )
            if promoted is not None:
                _append_event(
                    state,
                    "runbook_promoted",
                    at,
                    incident=incident,
                    detail=(
                        f"{promoted}: the same repair resolved signature "
                        f"{incident.get('signature')} {PROMOTION_THRESHOLD} times; "
                        "from now on it is applied without the engineer"
                    ),
                )
            return IncidentPhase(str(incident["phase"]))

    def escalate_incident_to_user(
        self,
        incident_id: str,
        *,
        reason_code: str,
        at: str,
        detail: str = "",
        escalation: Mapping[str, Any] | None = None,
        blocks_run: bool = False,
    ) -> IncidentPhase:
        """Move the ticket to ESCALATE_TO_USER with the code the engineer named.

        `escalation` is what the owner decides with - the engineer's
        diagnosis, what it already repaired, the decision needed and its
        recommendation - kept on the ticket, not only in a thread nobody
        opens. `blocks_run` is the engineer's own finding that the whole run
        must wait (revoked hook trust, a global setting): then the ticket
        holds every task, not just its own.

        The run was marked BLOCKED/PIPELINE_ENGINEER_ESCALATED while the
        ticket itself stayed in PIPELINE_ENGINEER: the store believed the
        engineer was still working. So the task stayed paused forever, and
        nobody - neither the engineer nor the user - could close the ticket.
        """

        with self._transaction() as state:
            incident = _incident(state, incident_id)
            phase = IncidentPhase(str(incident["phase"]))
            if phase is IncidentPhase.ESCALATE_TO_USER:
                return phase
            # The engineer may close the fault itself and still raise a
            # decision to the owner: these are different things. It repairs
            # the ticket, but cannot resolve a rule conflict - not its
            # authority.
            #
            # Escalation used to be allowed only from the "held by the
            # engineer" phase, and a closed ticket rejected it. On 16 Sep 2026
            # this stopped the whole run twice: the engineer closed the fault,
            # escalated an R31 conflict, the refusal propagated, the
            # dispatcher crashed, nobody was left to accept the completion.
            # Such an escalation must not be lost either - a decision nobody
            # sees is no different from one never made.
            if phase not in {
                IncidentPhase.PIPELINE_ENGINEER,
                IncidentPhase.RESOLVED,
                IncidentPhase.RECOVERED,
            }:
                raise PipelineIncidentError(
                    "escalation requires an incident held, resolved or recovered "
                    "by Pipeline Engineer"
                )
            escalate_to_user(incident, reason_code, at=at, detail=detail)
            if escalation:
                incident["escalation"] = _bounded_mapping(
                    {key: escalation[key] for key in ESCALATION_FIELDS if key in escalation}
                )
            incident["blocks_run"] = bool(blocks_run)
            incident["updated_at"] = at
            _append_event(
                state,
                "pipeline_engineer_escalated_to_user",
                at,
                incident=incident,
                detail=_bounded(f"{reason_code}: {detail}", MAX_EVENT_CHARS),
            )
            return IncidentPhase(str(incident["phase"]))

    def resolve_escalation_by_user(
        self,
        incident_id: str,
        *,
        at: str,
        note: str = "",
    ) -> IncidentPhase:
        """Close an escalation because the user answered it.

        R13 allows turning to the user as an exception - but an appeal with
        no way back is not an exception, it is a dead end. The engineer
        declared ESCALATE_TO_USER, the run went to BLOCKED, and resuming
        refused precisely because the run was BLOCKED. A person who had
        already fixed everything had no way to say so.

        No runbook is promoted here: the repair happened outside, and there
        is nothing to replay it with. If the cause remains, the same fault
        returns under the same signature, and the repeat is recognized.
        """

        with self._transaction() as state:
            incident = _incident(state, incident_id)
            phase = IncidentPhase(str(incident["phase"]))
            # PIPELINE_ENGINEER is accepted alongside ESCALATE_TO_USER. Runs
            # escalated by the previous version left the ticket in the
            # engineer's phase: the run went to BLOCKED and the store never
            # learned of the escalation. Refusing such a ticket would fix
            # only future cases and leave locked the very state the fix was
            # made for.
            if phase not in {
                IncidentPhase.ESCALATE_TO_USER,
                IncidentPhase.PIPELINE_ENGINEER,
            }:
                raise PipelineIncidentError(
                    "only an incident handed to the user is closed by the user"
                )
            incident["phase"] = IncidentPhase.RESOLVED.value
            incident["resolved_at"] = at
            incident["updated_at"] = at
            _append_event(
                state,
                "escalation_resolved_by_user",
                at,
                incident=incident,
                detail=_bounded(note or "user answered the escalation", MAX_EVENT_CHARS),
            )
            return IncidentPhase(str(incident["phase"]))

    def incident_ids_awaiting_the_user(self) -> tuple[str, ...]:
        """Tickets awaiting the user's answer.

        Those stuck in PIPELINE_ENGINEER land here too: the previous version
        marked the escalation only on the run, and nobody - neither the
        engineer nor a human - could close such a ticket.
        """

        state = self.load()
        return tuple(
            str(item["incident_id"])
            for item in state.get("incidents") or []
            if IncidentPhase(str(item["phase"]))
            in {IncidentPhase.ESCALATE_TO_USER, IncidentPhase.PIPELINE_ENGINEER}
        )

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
            # R23 demands three things of the report: the signature, the
            # attempt count and what changed between attempts. The ledger
            # carries the first two, its resolution list the third: that is
            # the list of what the fault was repaired with.
            "repeat_breakages": _repeat_breakages(self.signature_ledger()),
        }

    def incident_package(self, incident_id: str) -> dict[str, Any]:
        state = self.load()
        incident = _incident(state, incident_id)
        return self._incident_package(state, incident)


    def _incident_package(
        self,
        state: Mapping[str, Any],
        incident: Mapping[str, Any],
    ) -> dict[str, Any]:
        classification = IncidentClass(str(incident["classification"]))
        if classification not in ENGINEER_LANE_CLASSES:
            raise PipelineIncidentError(
                f"Pipeline Engineer packages do not know class {classification.value}"
            )
        # An infrastructure ticket gets the whole vocabulary. It used to get
        # only the diagnostics (plus a runbook's actions), while the prompt
        # said "execute only actions listed in allowed_actions": the
        # engineer was asked to repair and allowed to look. An advisory
        # ticket gets exactly the diagnostics - nothing there is its to fix.
        if classification in ADVISORY_INCIDENT_CLASSES:
            allowed = list(READ_ONLY_DIAGNOSTIC_ACTIONS)
        else:
            allowed = list(RECOVERY_ACTIONS)
            runbook = _runbook(str(incident.get("runbook_id") or ""))
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


def _repeat_breakages(ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Faults that happened more than once, and what they were repaired with.

    A single event is no reason for a report: R23 bounds REPETITION. Ordered
    by repeat count, so the most persistent reads first.
    """

    repeats = []
    for signature, entry in ledger.items():
        if not isinstance(entry, Mapping):
            continue
        occurrences = int(entry.get("occurrences") or 0)
        if occurrences < 2:
            continue
        tried = sorted(
            {
                action
                for item in entry.get("resolutions") or []
                if isinstance(item, Mapping)
                for action in (item.get("actions") or [])
            }
        )
        repeats.append(
            {
                "signature": signature,
                "code": str(entry.get("code") or ""),
                "occurrences": occurrences,
                "tried": tried,
            }
        )
    return sorted(repeats, key=lambda item: (-item["occurrences"], item["signature"]))


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
        # The reader does not crash on a missing key: the status is where a
        # person comes to look into things, and it must always read. The
        # writer stored two keys, the reader asked for a third, and `status`
        # crashed with KeyError exactly on a busy slot.
        else "Recovery slot: incident={} owner={}".format(
            slot.get("incident_id", "?"), slot.get("owner_id", "?")
        )
    )
    pending = snapshot.get("pending_transport") or []
    lines.append(f"Pending authorized transport: {len(pending)}")
    for incident in snapshot.get("incidents") or []:
        lines.append(
            f"- {incident['incident_id']}: {incident['classification']} / {incident['phase']} — {incident['summary']}"
        )
    for repeat in snapshot.get("repeat_breakages") or []:
        tried = ", ".join(repeat["tried"]) if repeat["tried"] else "nothing recorded"
        lines.append(
            f"- repeat {repeat['code']} ({repeat['signature']}): "
            f"{repeat['occurrences']} times; tried: {tried}"
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
        "signatures": {},
    }


def _validate_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != INCIDENT_STATE_SCHEMA_VERSION:
        raise PipelineIncidentError("unsupported or corrupt Pipeline Engineer state")
    required = {"sequence", "recovery_slot", "incidents", "transport_reservations", "journal"}
    if not required.issubset(raw):
        raise PipelineIncidentError("incomplete Pipeline Engineer state")
    if not all(isinstance(raw[key], list) for key in ("incidents", "transport_reservations", "journal")):
        raise PipelineIncidentError("Pipeline Engineer collections must be arrays")
    # The signature ledger was added later and deliberately does not bump
    # the schema version: live version-1 run state must read as is.
    if "signatures" not in raw:
        raw["signatures"] = {}
    if not isinstance(raw["signatures"], dict):
        raise PipelineIncidentError("Pipeline Engineer signature ledger must be a map")
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


def _backoff_seconds(incident: Mapping[str, Any]) -> int:
    """The growing delay before the next attempt, capped by a ceiling."""

    attempt = int(incident["recovery_attempts"])
    initial = int(incident["retry_initial_seconds"])
    maximum = int(incident["retry_maximum_seconds"])
    return min(initial * (2 ** max(0, attempt - 1)), maximum)


def _expected_healthcheck(incident: Mapping[str, Any]) -> str | None:
    """The healthcheck name: for a learned runbook it lives on the incident."""

    declared = incident.get("runbook_healthcheck")
    if isinstance(declared, str) and declared:
        return declared
    runbook = _runbook(str(incident.get("runbook_id") or ""))
    return runbook.healthcheck if runbook else None


def _promoted_runbook(state: Mapping[str, Any], signature: str) -> dict[str, Any] | None:
    entry = state.get("signatures", {}).get(signature)
    if not isinstance(entry, Mapping):
        return None
    promoted = entry.get("promoted_runbook")
    return dict(promoted) if isinstance(promoted, Mapping) else None


def _record_resolution(
    state: dict[str, Any],
    incident: Mapping[str, Any],
    *,
    actions: Sequence[str],
    healthcheck: HealthcheckResult | None,
    at: str,
    note: str = "",
) -> str | None:
    """Accumulate a resolution under the signature and, when due, make a runbook.

    Returns the id of the promoted runbook if promotion happened.

    Actions here are vocabulary identifiers, not a retelling. Measured on
    the v1.0 run: the main signature accumulated 15 resolutions and not one
    runbook, because they were recorded as prose ("attempted
    incident-scoped relay-owner reactivation; helper refused because…") and
    promotion compares them against an enumeration. Prose never matches an
    enumeration - the learning path was closed on itself. Prose belongs in
    the note: it explains circumstances and affects nothing.
    """

    signature = str(incident.get("signature") or "")
    entry = state.get("signatures", {}).get(signature)
    if not signature or not isinstance(entry, dict):
        # An old-schema incident: no signature, nothing to accumulate under.
        return None
    normalized = tuple(sorted({str(item) for item in actions if str(item).strip()}))
    check = healthcheck.name if healthcheck is not None else None
    entry["resolutions"].append(
        {
            "actions": list(normalized),
            "healthcheck": check,
            "at": at,
            "note": _bounded(note, MAX_EVENT_CHARS),
        }
    )
    if entry.get("promoted_runbook") is not None or not normalized:
        return None
    # No forbidden-action filter here on purpose: the action vocabulary and
    # the forbidden list do not intersect, and closing a ticket accepts only
    # the vocabulary. A forbidden check here would be an unreachable branch
    # - the very dead code R19 says to remove rather than keep "just in
    # case".
    if not set(normalized).issubset(AUTO_REPLAYABLE_ACTIONS):
        # Only actions level 1 may replay blindly are promoted: it works
        # without a human. Code repairs and targeted commands with foreign
        # identifiers do not qualify - they stay as knowledge in the ledger
        # but never become a procedure.
        return None
    identical = [
        item
        for item in entry["resolutions"]
        if tuple(item.get("actions") or ()) == normalized
        and item.get("healthcheck") == check
    ]
    if len(identical) < PROMOTION_THRESHOLD:
        return None
    runbook_id = f"learned-{signature}"
    entry["promoted_runbook"] = {
        "id": runbook_id,
        "actions": list(normalized),
        "healthcheck": check,
        "promoted_at": at,
        "from_resolutions": len(identical),
    }
    entry["promoted_runbook_id"] = runbook_id
    return runbook_id


def _record_signature(
    state: dict[str, Any],
    signature: str,
    incident: Mapping[str, Any],
    *,
    at: str,
) -> int:
    """Count the incident in the signature ledger and return the repeat count."""

    ledger = state["signatures"]
    entry = ledger.get(signature)
    if entry is None:
        entry = {
            "signature": signature,
            "code": incident["code"],
            "classification": incident["classification"],
            "operation": incident.get("operation"),
            "side_effect_outcome": incident.get("side_effect_outcome"),
            "occurrences": 0,
            "incident_ids": [],
            "first_seen_at": at,
            "last_seen_at": at,
            "resolutions": [],
            "promoted_runbook_id": None,
        }
        ledger[signature] = entry
    entry["occurrences"] = int(entry["occurrences"]) + 1
    entry["last_seen_at"] = at
    incident_id = str(incident["incident_id"])
    if incident_id not in entry["incident_ids"]:
        entry["incident_ids"].append(incident_id)
    return int(entry["occurrences"])


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


def _runbook(runbook_id: str) -> RecoveryRunbook | None:
    return next((item for item in RUNBOOKS if item.id == runbook_id), None)


def _require_named_actions(actions: Sequence[str]) -> None:
    """A repair names WHAT was done with an identifier from the vocabulary.

    A prose report is no use for learning: it compares with nothing, and the
    signature ledger accumulates it with no way out. So closing a ticket
    without a single named action is refused, and an unknown name even more
    so: the vocabulary is limited to the commands the runtime actually has.
    """

    named = [str(item).strip() for item in actions if str(item).strip()]
    if not named:
        raise PipelineIncidentError(
            "resolving an incident requires at least one named action from: "
            + ", ".join(RECOVERY_ACTIONS)
        )
    unknown = sorted({item for item in named if item not in RECOVERY_ACTIONS})
    if unknown:
        raise PipelineIncidentError(
            "unknown recovery actions "
            + ", ".join(unknown)
            + "; prose belongs in the note, actions must come from: "
            + ", ".join(RECOVERY_ACTIONS)
        )


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


