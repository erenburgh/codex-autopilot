"""Lifecycle adapter for the staged-artifact side-effect gate.

The filesystem transaction lives in :mod:`artifact_staging`; this module keeps
Desktop completion orchestration small and translates its errors into the
lifecycle's fail-closed protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .artifact_staging import (
    ArtifactStagingError,
    ArtifactStagingStore,
    PromotionResult,
    StagedArtifact,
    StagingStatus,
)
from .config import Config, STATE_DIR_NAME
from .lifecycle_base import (
    DesktopLifecycleError,
    _append_event,
    task_checkpoint,
    task_checkpoint_path,
)
from .memory import ProjectMemory
from .plan import Plan
from .rules import record_violation
from .run_state import RunState
from .scope import ScopeNotObservable, audit_declared_scope, observe_changed_paths


@dataclass(frozen=True, slots=True)
class CompletionArtifactGate:
    workspace: Path
    store: ArtifactStagingStore | None
    # The run's config: a promotion of a task filed at the root is followed
    # by the canonical check of isolation_guard, whose ticket needs it.
    cfg: Any = None

    @property
    def staged(self) -> bool:
        return self.store is not None

    def seal_and_record(
        self,
        task_id: str,
        memory: ProjectMemory,
        *,
        provider_thread_id: str,
    ) -> StagedArtifact | None:
        if self.store is None:
            return None
        try:
            artifact = self.store.seal(task_id)
        except ArtifactStagingError as exc:
            raise DesktopLifecycleError(str(exc)) from exc
        common = {
            "created_by": "codex-autopilot artifact staging runtime",
            "milestone_id": task_id,
            "role": "staged-artifact",
            "provider": "deterministic-runtime",
            "provider_thread_id": provider_thread_id,
        }
        if not artifact.changes:
            memory.record_evidence(
                kind="environment_probe",
                summary=(
                    f"Staged proposal {artifact.proposal_sha256} contains no filesystem changes."
                ),
                environment_probe=json.dumps(
                    {"proposal_sha256": artifact.proposal_sha256, "changes": []},
                    sort_keys=True,
                ),
                **common,
            )
        for change in artifact.changes:
            summary = (
                f"Staged {change.artifact_class.value} {change.operation}: "
                f"{change.path}; version={change.version}."
            )
            if change.staged is not None:
                memory.record_evidence(
                    kind="artifact",
                    summary=summary,
                    artifact_path=str(artifact.workspace / change.path),
                    **common,
                )
            else:
                memory.record_evidence(
                    kind="environment_probe",
                    summary=summary,
                    environment_probe=json.dumps(change.to_dict(), sort_keys=True),
                    **common,
                )
        return artifact

    def require_revision(self, task_id: str) -> None:
        if self.store is None:
            return
        try:
            self.store.mark_revision_required(task_id)
        except ArtifactStagingError as exc:
            raise DesktopLifecycleError(str(exc)) from exc

    def promote(
        self,
        task_id: str,
        verification_id: str | None,
        *,
        state: RunState,
        session: dict[str, Any],
        at: str,
    ) -> PromotionResult | None:
        if self.store is None:
            return None
        if verification_id is None:
            raise DesktopLifecycleError(
                "staged promotion requires an independent verification record"
            )
        try:
            artifact = self.store.load(task_id)
            accepted_id = artifact.verification_id
            if artifact.status == StagingStatus.PROPOSED:
                artifact = self.store.mark_verified(
                    task_id, verification_id=verification_id
                )
                accepted_id = artifact.verification_id
            elif artifact.status not in {
                StagingStatus.VERIFIED,
                StagingStatus.PROMOTED,
            }:
                raise ArtifactStagingError(
                    f"cannot promote staged task from {artifact.status.value}"
                )
            if not accepted_id:
                raise ArtifactStagingError(
                    "accepted staged artifact has no verification id"
                )
            promotion = self.store.promote(
                task_id,
                expected_verification_id=accepted_id,
            )
        except ArtifactStagingError as exc:
            raise DesktopLifecycleError(str(exc)) from exc
        session["artifact_promotion"] = {
            "verification_id": promotion.verification_id,
            "proposal_sha256": promotion.proposal_sha256,
            "changed_paths": list(promotion.changed_paths),
            "snapshot_path": str(promotion.snapshot_path),
        }
        _append_event(
            state,
            "staged_artifact_promoted",
            session,
            at,
            detail=json.dumps(
                session["artifact_promotion"],
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        if self.cfg is not None and any(
            item.get("placement_contract") == 2
            for item in state.worker_sessions
            if item.get("task_id") == task_id
        ):
            # The task's threads were filed at the root, where she can open
            # them and Desktop can widen their roots: was the canonical root
            # changed outside what was promoted (isolation_guard)?
            from .isolation_guard import canonical_outside_manifest, record_outside_manifest

            record_outside_manifest(
                self.cfg, state, session, task_id,
                canonical_outside_manifest(self.store, task_id), at,
            )
        return promotion


def bind_completion_artifact_gate(
    cfg: Config,
    session: dict[str, Any],
    task_id: str,
    checkpoint_before: str,
) -> CompletionArtifactGate:
    """Authenticate cwd, require this session's checkpoint, and publish it."""

    workspace = Path(
        str((session.get("descriptor") or {}).get("cwd") or cfg.root)
    ).resolve(strict=False)
    staged = workspace != cfg.root
    checkpoint_state_dir = workspace / STATE_DIR_NAME if staged else cfg.state_dir
    checkpoint_path = task_checkpoint_path(checkpoint_state_dir, task_id)
    if task_checkpoint(checkpoint_state_dir, task_id) == checkpoint_before:
        raise DesktopLifecycleError(
            f"worker did not update its own checkpoint file: {checkpoint_path}"
        )
    if not staged:
        return CompletionArtifactGate(workspace=workspace, store=None)
    store = ArtifactStagingStore(cfg.root, cfg.state_dir)
    try:
        artifact = store.load(task_id)
        if artifact.workspace != workspace:
            raise ArtifactStagingError(
                "completion cwd does not match the durable staged workspace"
            )
        store.publish_checkpoint(task_id)
    except ArtifactStagingError as exc:
        raise DesktopLifecycleError(str(exc)) from exc
    return CompletionArtifactGate(workspace=workspace, store=store, cfg=cfg)


def audit_completed_task_scope(
    cfg: Config,
    plan: Plan,
    state: RunState,
    session: dict[str, Any],
    at: str,
) -> None:
    """Audit staged paths in canonical coordinates (R7)."""

    task = plan.task_map.get(str(session.get("task_id") or ""))
    if task is None:
        return
    descriptor = session.get("descriptor") or {}
    observed_root = Path(str(descriptor.get("cwd") or cfg.root)).resolve(strict=False)
    try:
        changed = observe_changed_paths(observed_root, session.get("scope_baseline"))
    except ScopeNotObservable as error:
        _append_event(state, "scope_not_observed", session, at, detail=str(error))
        return
    if observed_root != cfg.root:
        mapped: list[str] = []
        for path in changed:
            try:
                relative = Path(path).resolve(strict=False).relative_to(observed_root)
            except ValueError as exc:
                raise DesktopLifecycleError(
                    "staged scope observation escaped its workspace"
                ) from exc
            mapped.append(str((cfg.root / relative).resolve(strict=False)))
        changed = tuple(mapped)
    violations = audit_declared_scope(task, changed, project_root=cfg.root)
    for detail in violations:
        record_violation(cfg.state_dir, "R7", detail=detail)
        _append_event(state, "scope_violation_recorded", session, at, detail=detail)


__all__ = [
    "CompletionArtifactGate",
    "audit_completed_task_scope",
    "bind_completion_artifact_gate",
]
