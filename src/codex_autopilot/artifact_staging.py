"""Isolated staged artifacts and verified promotion.

The worker-facing workspace is a copy of the canonical project kept below the
runtime state directory.  Implementation, deterministic checks, and semantic
verification all observe that same copy.  The canonical project is touched
only by :meth:`ArtifactStagingStore.promote`, after a verifier attestation has
been bound to the exact proposed manifest.

Promotion is deliberately fail-closed:

* the staged manifest must still match the version the verifier saw;
* every canonical path being changed must still match its preparation
  baseline (a resource-lock bug therefore cannot become a lost update);
* a recoverable snapshot is written before the first canonical mutation;
* a partial promotion is rolled back before an error is returned.

The runtime state directory is excluded from artifact manifests.  A small
marker inside the staged workspace lets Project Memory resolve the canonical
project while the App Server keeps filesystem writes confined to the staged
workspace.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Iterator, Mapping, Sequence

from .config import STATE_DIR_NAME


STAGING_SCHEMA_VERSION = 1
STAGING_DIRECTORY = "staged-artifacts"
STAGING_MARKER = "STAGED_WORKSPACE.json"

_SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,127}$")
_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
)
_CODE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".lua",
        ".m",
        ".mm",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
)
_GENERATED_ASSET_SUFFIXES = frozenset(
    {
        ".avif",
        ".bmp",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".otf",
        ".pdf",
        ".png",
        ".svg",
        ".ttf",
        ".wav",
        ".webm",
        ".webp",
        ".woff",
        ".woff2",
    }
)


class ArtifactStagingError(RuntimeError):
    """The side-effect gate could not prove a safe transition."""


class CanonicalDriftError(ArtifactStagingError):
    """Canonical state changed after the staged baseline was captured."""


class StagedArtifactChangedError(ArtifactStagingError):
    """The staged output changed after the verifier-bound proposal."""


class ArtifactClass(str, Enum):
    CODE = "code"
    FILE = "file"
    GENERATED_ASSET = "generated_asset"


class StagingStatus(str, Enum):
    PREPARED = "PREPARED"
    PROPOSED = "PROPOSED"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    VERIFIED = "VERIFIED"
    PROMOTING = "PROMOTING"
    PROMOTED = "PROMOTED"
    ABANDONED = "ABANDONED"


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    kind: str
    sha256: str
    mode: int
    link_target: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "sha256": self.sha256,
            "mode": self.mode,
        }
        if self.link_target is not None:
            result["link_target"] = self.link_target
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ArtifactEntry":
        if set(raw) - {"kind", "sha256", "mode", "link_target"}:
            raise ArtifactStagingError("artifact entry contains unknown fields")
        kind = str(raw.get("kind") or "")
        if kind not in {"file", "symlink"}:
            raise ArtifactStagingError(f"unsupported artifact entry kind {kind!r}")
        digest = str(raw.get("sha256") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ArtifactStagingError("artifact entry has an invalid sha256")
        mode = raw.get("mode")
        if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0:
            raise ArtifactStagingError("artifact entry mode must be a non-negative integer")
        link_target = raw.get("link_target")
        if link_target is not None and not isinstance(link_target, str):
            raise ArtifactStagingError("artifact symlink target must be a string")
        if kind == "symlink" and link_target is None:
            raise ArtifactStagingError("artifact symlink is missing link_target")
        if kind == "file" and link_target is not None:
            raise ArtifactStagingError("artifact file cannot carry link_target")
        return cls(kind=kind, sha256=digest, mode=mode, link_target=link_target)


@dataclass(frozen=True, slots=True)
class ArtifactChange:
    path: str
    operation: str
    artifact_class: ArtifactClass
    baseline: ArtifactEntry | None
    staged: ArtifactEntry | None
    version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "operation": self.operation,
            "artifact_class": self.artifact_class.value,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "staged": self.staged.to_dict() if self.staged else None,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ArtifactChange":
        if set(raw) != {
            "path",
            "operation",
            "artifact_class",
            "baseline",
            "staged",
            "version",
        }:
            raise ArtifactStagingError("artifact change has an invalid schema")
        path = _relative_path(str(raw["path"]))
        operation = str(raw["operation"])
        if operation not in {"add", "modify", "delete"}:
            raise ArtifactStagingError(f"unsupported artifact operation {operation!r}")
        try:
            artifact_class = ArtifactClass(str(raw["artifact_class"]))
        except ValueError as exc:
            raise ArtifactStagingError("artifact change has an invalid class") from exc
        baseline_raw = raw["baseline"]
        staged_raw = raw["staged"]
        if baseline_raw is not None and not isinstance(baseline_raw, Mapping):
            raise ArtifactStagingError("artifact baseline must be an object or null")
        if staged_raw is not None and not isinstance(staged_raw, Mapping):
            raise ArtifactStagingError("staged artifact must be an object or null")
        baseline = ArtifactEntry.from_dict(baseline_raw) if baseline_raw else None
        staged = ArtifactEntry.from_dict(staged_raw) if staged_raw else None
        if operation == "add" and (baseline is not None or staged is None):
            raise ArtifactStagingError("add change has inconsistent entries")
        if operation == "modify" and (baseline is None or staged is None):
            raise ArtifactStagingError("modify change has inconsistent entries")
        if operation == "delete" and (baseline is None or staged is not None):
            raise ArtifactStagingError("delete change has inconsistent entries")
        version = str(raw["version"])
        if not version:
            raise ArtifactStagingError("artifact change version is empty")
        return cls(
            path=path,
            operation=operation,
            artifact_class=artifact_class,
            baseline=baseline,
            staged=staged,
            version=version,
        )


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    task_id: str
    run_id: str
    workspace: Path
    status: StagingStatus
    proposal_sha256: str | None
    verification_id: str | None
    changes: tuple[ArtifactChange, ...]
    reservation_tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromotionResult:
    task_id: str
    verification_id: str
    proposal_sha256: str
    changed_paths: tuple[str, ...]
    snapshot_path: Path


def task_requires_staging(task: Any, *, legacy_serial: bool = False) -> bool:
    """Return whether a canonical task has a filesystem artifact to isolate.

    Pre-v1 migrated plans remain on their historical execution path.  A new
    task enters the side-effect gate when it declares a filesystem deliverable
    and can write through a filesystem resource.  Required and optional
    deliverables are both isolated: ``required`` controls acceptance, not
    whether an artifact is allowed to bypass verification.  Logical-only
    outputs (for example a Project Memory rubric record) do not pretend to be
    files.
    """

    if legacy_serial:
        return False
    has_output = any(
        bool(getattr(output, "path", None))
        for output in getattr(task, "outputs", ())
    )
    has_write = any(
        getattr(claim, "kind", None) in {"path", "directory", "glob"}
        and getattr(claim, "access", None) in {"write", "exclusive"}
        for claim in getattr(task, "resources", ())
    )
    return has_output and has_write


def resolve_canonical_project_root(start: Path) -> Path:
    """Resolve Project Memory back to canonical root from a staged cwd."""

    current = start.expanduser().resolve(strict=False)
    for candidate in (current, *current.parents):
        marker = candidate / STATE_DIR_NAME / STAGING_MARKER
        if not marker.is_file():
            continue
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ArtifactStagingError(f"invalid staged workspace marker: {marker}") from exc
        canonical = Path(str(raw.get("canonical_project_root") or "")).expanduser()
        if not canonical.is_absolute():
            raise ArtifactStagingError("staged workspace marker has no absolute canonical root")
        canonical = canonical.resolve(strict=False)
        state_dir = canonical / STATE_DIR_NAME / STAGING_DIRECTORY
        try:
            candidate.relative_to(state_dir)
        except ValueError as exc:
            raise ArtifactStagingError(
                "staged workspace marker does not belong to its canonical project"
            ) from exc
        return canonical
    return current


class ArtifactStagingStore:
    """Durable side-effect gate for one canonical project."""

    def __init__(self, project_root: Path, state_dir: Path | None = None) -> None:
        self.project_root = project_root.expanduser().resolve(strict=False)
        self.state_dir = (
            state_dir.expanduser().resolve(strict=False)
            if state_dir is not None
            else self.project_root / STATE_DIR_NAME
        )
        try:
            self.state_dir.relative_to(self.project_root)
        except ValueError as exc:
            raise ArtifactStagingError("staging state must be inside the canonical project") from exc
        self.root = self.state_dir / STAGING_DIRECTORY
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".lock"

    def prepare(
        self,
        *,
        run_id: str,
        task_id: str,
        reservation_token: str,
    ) -> StagedArtifact:
        """Create or reuse the isolated workspace for one task."""

        task_id = _safe_id(task_id, "task_id")
        run_id = _nonempty(run_id, "run_id")
        reservation_token = _nonempty(reservation_token, "reservation_token")
        with self._locked():
            existing = self._load_raw(task_id, missing_ok=True)
            if existing is not None:
                if str(existing.get("run_id")) != run_id:
                    raise ArtifactStagingError(
                        f"staged task {task_id} belongs to another run"
                    )
                status = StagingStatus(str(existing["status"]))
                if status in {StagingStatus.PROMOTED, StagingStatus.ABANDONED}:
                    raise ArtifactStagingError(
                        f"staged task {task_id} is already {status.value.lower()}"
                    )
                tokens = [str(item) for item in existing.get("reservation_tokens") or ()]
                if reservation_token not in tokens:
                    tokens.append(reservation_token)
                    existing["reservation_tokens"] = tokens
                    existing["updated_at"] = _utc_now()
                    self._save_raw(task_id, existing)
                return self._from_raw(existing)

            task_root = self._task_root(task_id)
            workspace = task_root / "workspace"
            if task_root.exists():
                raise ArtifactStagingError(
                    f"untracked staging directory already exists for {task_id}: {task_root}"
                )
            task_root.mkdir(parents=True)
            try:
                self._copy_project(workspace)
                self._write_workspace_marker(workspace, run_id=run_id, task_id=task_id)
                self._copy_checkpoint_into_workspace(workspace, task_id)
                self._initialize_workspace_git(workspace)
                baseline = _manifest(workspace)
                now = _utc_now()
                raw: dict[str, Any] = {
                    "schema_version": STAGING_SCHEMA_VERSION,
                    "run_id": run_id,
                    "task_id": task_id,
                    "workspace": str(workspace.relative_to(self.project_root)),
                    "status": StagingStatus.PREPARED.value,
                    "created_at": now,
                    "updated_at": now,
                    "reservation_tokens": [reservation_token],
                    "baseline": _manifest_to_dict(baseline),
                    "proposal": None,
                    "proposal_sha256": None,
                    "verification_id": None,
                    "promotion": None,
                }
                self._save_raw(task_id, raw)
                return self._from_raw(raw)
            except Exception:
                # The directory contains no canonical state.  Preserve it for
                # diagnosis instead of destructively deleting uncertain work.
                raise

    def load(self, task_id: str) -> StagedArtifact:
        with self._locked(shared=True):
            return self._from_raw(self._load_raw(_safe_id(task_id, "task_id")))

    def workspace_for(self, task_id: str) -> Path:
        return self.load(task_id).workspace

    def seal(self, task_id: str) -> StagedArtifact:
        """Freeze the current staged manifest as the verifier's proposal."""

        task_id = _safe_id(task_id, "task_id")
        with self._locked():
            raw = self._load_raw(task_id)
            status = StagingStatus(str(raw["status"]))
            if status not in {
                StagingStatus.PREPARED,
                StagingStatus.PROPOSED,
                StagingStatus.REVISION_REQUIRED,
            }:
                raise ArtifactStagingError(
                    f"cannot seal staged task {task_id} from {status.value}"
                )
            workspace = self._workspace_from_raw(raw)
            baseline = _manifest_from_dict(raw.get("baseline"), "baseline")
            staged = _manifest(workspace)
            changes = _diff_manifests(baseline, staged)
            proposal = {
                "entries": _manifest_to_dict(staged),
                "changes": [item.to_dict() for item in changes],
            }
            proposal_sha256 = _json_sha256(proposal)
            raw["proposal"] = proposal
            raw["proposal_sha256"] = proposal_sha256
            raw["verification_id"] = None
            raw["status"] = StagingStatus.PROPOSED.value
            raw["updated_at"] = _utc_now()
            self._save_raw(task_id, raw)
            return self._from_raw(raw)

    def mark_revision_required(self, task_id: str) -> StagedArtifact:
        """Keep the proposal isolated so a revision can edit it in place."""

        task_id = _safe_id(task_id, "task_id")
        with self._locked():
            raw = self._load_raw(task_id)
            status = StagingStatus(str(raw["status"]))
            if status != StagingStatus.PROPOSED:
                raise ArtifactStagingError(
                    f"revision requires a PROPOSED staged artifact, got {status.value}"
                )
            raw["status"] = StagingStatus.REVISION_REQUIRED.value
            raw["verification_id"] = None
            raw["updated_at"] = _utc_now()
            self._save_raw(task_id, raw)
            return self._from_raw(raw)

    def mark_verified(self, task_id: str, *, verification_id: str) -> StagedArtifact:
        """Bind independent acceptance to the exact sealed proposal."""

        task_id = _safe_id(task_id, "task_id")
        verification_id = _nonempty(verification_id, "verification_id")
        with self._locked():
            raw = self._load_raw(task_id)
            status = StagingStatus(str(raw["status"]))
            if status != StagingStatus.PROPOSED:
                raise ArtifactStagingError(
                    f"verification requires a PROPOSED staged artifact, got {status.value}"
                )
            if not raw.get("proposal_sha256"):
                raise ArtifactStagingError("staged proposal has no digest")
            raw["status"] = StagingStatus.VERIFIED.value
            raw["verification_id"] = verification_id
            raw["verified_at"] = _utc_now()
            raw["updated_at"] = raw["verified_at"]
            self._save_raw(task_id, raw)
            return self._from_raw(raw)

    def promote(
        self,
        task_id: str,
        *,
        expected_verification_id: str | None = None,
    ) -> PromotionResult:
        """Promote one verifier-bound proposal into canonical shared state."""

        task_id = _safe_id(task_id, "task_id")
        with self._locked():
            raw = self._load_raw(task_id)
            status = StagingStatus(str(raw["status"]))
            if status == StagingStatus.PROMOTED:
                return self._completed_promotion_result(
                    task_id,
                    raw,
                    expected_verification_id=expected_verification_id,
                )
            if status != StagingStatus.VERIFIED:
                raise ArtifactStagingError(
                    f"promotion requires VERIFIED staged artifact, got {status.value}"
                )
            verification_id = str(raw.get("verification_id") or "")
            if not verification_id:
                raise ArtifactStagingError("verified staged artifact has no verification id")
            if (
                expected_verification_id is not None
                and verification_id != expected_verification_id
            ):
                raise ArtifactStagingError(
                    "promotion verification id does not match the accepted verdict"
                )
            proposal = raw.get("proposal")
            if not isinstance(proposal, Mapping):
                raise ArtifactStagingError("verified staged artifact has no proposal")
            expected_digest = str(raw.get("proposal_sha256") or "")
            if _json_sha256(proposal) != expected_digest:
                raise ArtifactStagingError("persisted staged proposal digest is corrupt")
            expected_entries = _manifest_from_dict(
                proposal.get("entries"), "proposal.entries"
            )
            workspace = self._workspace_from_raw(raw)
            observed_entries = _manifest(workspace)
            if observed_entries != expected_entries:
                raise StagedArtifactChangedError(
                    "staged output changed after verification; a new proposal is required"
                )
            changes = _changes_from_raw(proposal.get("changes"))
            self._require_no_canonical_drift(changes)

            snapshot_path = self._write_promotion_snapshot(task_id, changes)
            raw["status"] = StagingStatus.PROMOTING.value
            raw["promotion"] = {
                "snapshot": str(snapshot_path.relative_to(self.project_root)),
                "started_at": _utc_now(),
                "verification_id": verification_id,
            }
            raw["updated_at"] = raw["promotion"]["started_at"]
            self._save_raw(task_id, raw)
            try:
                self._apply_changes(workspace, changes)
            except Exception as exc:
                try:
                    self._restore_snapshot(snapshot_path, changes)
                except Exception as rollback_exc:
                    raw["promotion"]["rollback_error"] = str(rollback_exc)
                    raw["promotion"]["error"] = str(exc)
                    raw["updated_at"] = _utc_now()
                    self._save_raw(task_id, raw)
                    raise ArtifactStagingError(
                        f"promotion failed and rollback failed: {exc}; rollback: {rollback_exc}"
                    ) from rollback_exc
                raw["status"] = StagingStatus.VERIFIED.value
                raw["promotion"]["rolled_back_at"] = _utc_now()
                raw["promotion"]["error"] = str(exc)
                raw["updated_at"] = raw["promotion"]["rolled_back_at"]
                self._save_raw(task_id, raw)
                raise ArtifactStagingError(f"promotion failed and was rolled back: {exc}") from exc

            promoted_at = _utc_now()
            raw["status"] = StagingStatus.PROMOTED.value
            raw["promotion"]["completed_at"] = promoted_at
            raw["updated_at"] = promoted_at
            self._save_raw(task_id, raw)
            return PromotionResult(
                task_id=task_id,
                verification_id=verification_id,
                proposal_sha256=expected_digest,
                changed_paths=tuple(item.path for item in changes),
                snapshot_path=snapshot_path,
            )

    def _completed_promotion_result(
        self,
        task_id: str,
        raw: Mapping[str, Any],
        *,
        expected_verification_id: str | None,
    ) -> PromotionResult:
        """Return a crash-retried completed promotion without repeating writes."""

        verification_id = str(raw.get("verification_id") or "")
        if (
            not verification_id
            or expected_verification_id is not None
            and verification_id != expected_verification_id
        ):
            raise ArtifactStagingError(
                "completed promotion is bound to another verification result"
            )
        proposal = raw.get("proposal")
        promotion = raw.get("promotion")
        if not isinstance(proposal, Mapping) or not isinstance(promotion, Mapping):
            raise ArtifactStagingError("completed promotion record is incomplete")
        changes = _changes_from_raw(proposal.get("changes"))
        for change in changes:
            if _entry_for_path(self.project_root, change.path) != change.staged:
                raise CanonicalDriftError(
                    f"canonical path drifted after promotion: {change.path}"
                )
        snapshot_raw = str(promotion.get("snapshot") or "")
        if not snapshot_raw:
            raise ArtifactStagingError("completed promotion has no recovery snapshot")
        snapshot = _safe_path(self.project_root, snapshot_raw)
        return PromotionResult(
            task_id=task_id,
            verification_id=verification_id,
            proposal_sha256=str(raw.get("proposal_sha256") or ""),
            changed_paths=tuple(item.path for item in changes),
            snapshot_path=snapshot,
        )

    def abandon(self, task_id: str, *, reason: str) -> StagedArtifact:
        """Fence a proposal without deleting its recoverable evidence."""

        task_id = _safe_id(task_id, "task_id")
        reason = _nonempty(reason, "reason")
        with self._locked():
            raw = self._load_raw(task_id)
            if StagingStatus(str(raw["status"])) == StagingStatus.PROMOTED:
                raise ArtifactStagingError("a promoted artifact cannot be abandoned")
            raw["status"] = StagingStatus.ABANDONED.value
            raw["abandoned_reason"] = reason
            raw["updated_at"] = _utc_now()
            self._save_raw(task_id, raw)
            return self._from_raw(raw)

    def publish_checkpoint(self, task_id: str) -> Path:
        """Copy the worker checkpoint from staged control state to runtime state."""

        task_id = _safe_id(task_id, "task_id")
        with self._locked(shared=True):
            raw = self._load_raw(task_id)
            workspace = self._workspace_from_raw(raw)
            source = workspace / STATE_DIR_NAME / "handoff" / f"{task_id}.md"
            if not source.is_file():
                raise ArtifactStagingError(
                    f"staged worker did not write checkpoint {source}"
                )
            destination = self.state_dir / "handoff" / f"{task_id}.md"
            destination.parent.mkdir(parents=True, exist_ok=True)
            _atomic_copy_file(source, destination)
            return destination

    def _copy_project(self, workspace: Path) -> None:
        def ignored(directory: str, names: list[str]) -> set[str]:
            at_root = Path(directory).resolve(strict=False) == self.project_root
            result = {name for name in names if name in _IGNORED_DIRECTORY_NAMES}
            if at_root:
                result.update(
                    name
                    for name in names
                    if name == self.state_dir.name
                    or name.startswith(self.state_dir.name + ".")
                )
            return result

        try:
            shutil.copytree(
                self.project_root,
                workspace,
                symlinks=True,
                copy_function=shutil.copy2,
                ignore=ignored,
            )
        except OSError as exc:
            raise ArtifactStagingError(f"cannot prepare staged workspace: {exc}") from exc

    def _write_workspace_marker(self, workspace: Path, *, run_id: str, task_id: str) -> None:
        control = workspace / STATE_DIR_NAME
        control.mkdir(parents=True, exist_ok=True)
        _atomic_json(
            control / STAGING_MARKER,
            {
                "schema_version": STAGING_SCHEMA_VERSION,
                "canonical_project_root": str(self.project_root),
                "run_id": run_id,
                "task_id": task_id,
            },
        )

    def _copy_checkpoint_into_workspace(self, workspace: Path, task_id: str) -> None:
        source = self.state_dir / "handoff" / f"{task_id}.md"
        destination = workspace / STATE_DIR_NAME / "handoff" / f"{task_id}.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            shutil.copy2(source, destination)

    def _initialize_workspace_git(self, workspace: Path) -> None:
        """Give the isolated copy a clean, hook-free synthetic baseline."""

        def git(*args: str, input_text: str | None = None) -> str:
            env = dict(os.environ)
            env.update(
                {
                    "GIT_AUTHOR_NAME": "Codex Autopilot",
                    "GIT_AUTHOR_EMAIL": "autopilot@local.invalid",
                    "GIT_COMMITTER_NAME": "Codex Autopilot",
                    "GIT_COMMITTER_EMAIL": "autopilot@local.invalid",
                    "GIT_CONFIG_NOSYSTEM": "1",
                }
            )
            try:
                completed = subprocess.run(
                    ("git", "-C", str(workspace), *args),
                    input=input_text,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                    env=env,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ArtifactStagingError(f"cannot initialize staged Git baseline: {exc}") from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise ArtifactStagingError(
                    f"staged Git command failed ({' '.join(args)}): {detail}"
                )
            return completed.stdout.strip()

        git("init", "--quiet")
        exclude = workspace / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(f"/{STATE_DIR_NAME}/\n", encoding="utf-8")
        git("add", "-A")
        tree = git("write-tree")
        commit = git("commit-tree", tree, "-m", "Codex Autopilot staged baseline")
        git("update-ref", "refs/heads/autopilot-stage", commit)
        git("symbolic-ref", "HEAD", "refs/heads/autopilot-stage")

    def _require_no_canonical_drift(self, changes: Sequence[ArtifactChange]) -> None:
        for change in changes:
            observed = _entry_for_path(self.project_root, change.path)
            if observed != change.baseline:
                raise CanonicalDriftError(
                    f"canonical path drifted before promotion: {change.path}"
                )

    def _write_promotion_snapshot(
        self,
        task_id: str,
        changes: Sequence[ArtifactChange],
    ) -> Path:
        snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        snapshot = self._task_root(task_id) / "promotion-snapshots" / snapshot_id
        files = snapshot / "files"
        files.mkdir(parents=True, exist_ok=False)
        entries: dict[str, Any] = {}
        existing_directories: set[str] = set()
        for change in changes:
            entry = _entry_for_path(self.project_root, change.path)
            entries[change.path] = entry.to_dict() if entry else None
            parent = Path(change.path).parent
            while parent != Path("."):
                if _safe_path(self.project_root, parent.as_posix()).is_dir():
                    existing_directories.add(parent.as_posix())
                parent = parent.parent
            if entry is not None:
                _copy_entry(
                    _safe_path(self.project_root, change.path),
                    _safe_path(files, change.path),
                    entry,
                )
        _atomic_json(
            snapshot / "snapshot.json",
            {
                "schema_version": STAGING_SCHEMA_VERSION,
                "task_id": task_id,
                "created_at": _utc_now(),
                "entries": entries,
                "existing_directories": sorted(existing_directories),
            },
        )
        return snapshot

    def _apply_changes(
        self,
        workspace: Path,
        changes: Sequence[ArtifactChange],
    ) -> None:
        for change in changes:
            destination = _safe_path(self.project_root, change.path)
            _require_safe_parents(self.project_root, change.path)
            if change.operation == "delete":
                _remove_entry(destination)
                continue
            assert change.staged is not None
            source = _safe_path(workspace, change.path)
            if change.staged.kind == "symlink":
                target = Path(str(change.staged.link_target))
                if target.is_absolute():
                    raise ArtifactStagingError(
                        f"staged symlink must be project-relative: {change.path}"
                    )
                resolved_target = (destination.parent / target).resolve(strict=False)
                try:
                    resolved_target.relative_to(self.project_root)
                except ValueError as exc:
                    raise ArtifactStagingError(
                        f"staged symlink escapes the canonical project: {change.path}"
                    ) from exc
            _atomic_install_entry(source, destination, change.staged)

    def _restore_snapshot(
        self,
        snapshot: Path,
        changes: Sequence[ArtifactChange],
    ) -> None:
        raw = json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8"))
        entries_raw = raw.get("entries")
        if not isinstance(entries_raw, Mapping):
            raise ArtifactStagingError("promotion snapshot has no entries")
        for change in reversed(tuple(changes)):
            destination = _safe_path(self.project_root, change.path)
            saved = entries_raw.get(change.path)
            if saved is None:
                _remove_entry(destination)
                continue
            if not isinstance(saved, Mapping):
                raise ArtifactStagingError("promotion snapshot entry is invalid")
            entry = ArtifactEntry.from_dict(saved)
            source = _safe_path(snapshot / "files", change.path)
            _atomic_install_entry(source, destination, entry)
        existing_directories_raw = raw.get("existing_directories") or []
        if not isinstance(existing_directories_raw, list) or not all(
            isinstance(item, str) for item in existing_directories_raw
        ):
            raise ArtifactStagingError("promotion snapshot directories are invalid")
        existing_directories = set(existing_directories_raw)
        parents = {
            parent.as_posix()
            for change in changes
            for parent in Path(change.path).parents
            if parent != Path(".")
        }
        for relative in sorted(parents, key=lambda item: len(Path(item).parts), reverse=True):
            if relative in existing_directories:
                continue
            directory = _safe_path(self.project_root, relative)
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()

    def _workspace_from_raw(self, raw: Mapping[str, Any]) -> Path:
        relative = _relative_path(str(raw.get("workspace") or ""))
        workspace = _safe_path(self.project_root, relative)
        expected_parent = self.root / _safe_id(str(raw.get("task_id") or ""), "task_id")
        try:
            workspace.relative_to(expected_parent)
        except ValueError as exc:
            raise ArtifactStagingError("staged workspace escaped its task directory") from exc
        if not workspace.is_dir():
            raise ArtifactStagingError(f"staged workspace is missing: {workspace}")
        return workspace

    def _from_raw(self, raw: Mapping[str, Any]) -> StagedArtifact:
        if raw.get("schema_version") != STAGING_SCHEMA_VERSION:
            raise ArtifactStagingError("unsupported staged artifact schema")
        task_id = _safe_id(str(raw.get("task_id") or ""), "task_id")
        status = StagingStatus(str(raw.get("status") or ""))
        proposal = raw.get("proposal")
        changes: tuple[ArtifactChange, ...] = ()
        if proposal is not None:
            if not isinstance(proposal, Mapping):
                raise ArtifactStagingError("staged proposal must be an object")
            changes = _changes_from_raw(proposal.get("changes"))
        tokens_raw = raw.get("reservation_tokens") or []
        if not isinstance(tokens_raw, list) or not all(
            isinstance(item, str) and item for item in tokens_raw
        ):
            raise ArtifactStagingError("staged reservation tokens are invalid")
        return StagedArtifact(
            task_id=task_id,
            run_id=_nonempty(str(raw.get("run_id") or ""), "run_id"),
            workspace=self._workspace_from_raw(raw),
            status=status,
            proposal_sha256=(
                str(raw["proposal_sha256"])
                if raw.get("proposal_sha256") is not None
                else None
            ),
            verification_id=(
                str(raw["verification_id"])
                if raw.get("verification_id") is not None
                else None
            ),
            changes=changes,
            reservation_tokens=tuple(tokens_raw),
        )

    def _task_root(self, task_id: str) -> Path:
        return self.root / _safe_id(task_id, "task_id")

    def _record_path(self, task_id: str) -> Path:
        return self._task_root(task_id) / "record.json"

    def _load_raw(
        self,
        task_id: str,
        *,
        missing_ok: bool = False,
    ) -> dict[str, Any] | None:
        path = self._record_path(task_id)
        if not path.is_file():
            if missing_ok:
                return None
            raise ArtifactStagingError(f"no staged artifact exists for {task_id}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ArtifactStagingError(f"cannot read staged artifact record: {path}") from exc
        if not isinstance(raw, dict):
            raise ArtifactStagingError("staged artifact record must be an object")
        return raw

    def _save_raw(self, task_id: str, raw: Mapping[str, Any]) -> None:
        _atomic_json(self._record_path(task_id), raw)

    @contextmanager
    def _locked(self, *, shared: bool = False) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _manifest(root: Path) -> dict[str, ArtifactEntry]:
    result: dict[str, ArtifactEntry] = {}
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        relative_dir = Path(directory).relative_to(root)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _IGNORED_DIRECTORY_NAMES and name != STATE_DIR_NAME
        )
        filenames.sort()
        for name in filenames:
            path = Path(directory) / name
            relative = (relative_dir / name).as_posix()
            result[relative] = _entry(path)
        # os.walk lists symlinked directories in dirnames.  With
        # followlinks=False they are not traversed, so record them explicitly.
        for name in tuple(dirnames):
            path = Path(directory) / name
            if path.is_symlink():
                relative = (relative_dir / name).as_posix()
                result[relative] = _entry(path)
                dirnames.remove(name)
    return result


def _entry(path: Path) -> ArtifactEntry:
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        return ArtifactEntry(
            kind="symlink",
            sha256=hashlib.sha256(target.encode("utf-8")).hexdigest(),
            mode=mode,
            link_target=target,
        )
    if not stat.S_ISREG(info.st_mode):
        raise ArtifactStagingError(f"unsupported staged filesystem entry: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return ArtifactEntry(kind="file", sha256=digest.hexdigest(), mode=mode)


def _entry_for_path(root: Path, relative: str) -> ArtifactEntry | None:
    path = _safe_path(root, relative)
    if not path.exists() and not path.is_symlink():
        return None
    return _entry(path)


def _diff_manifests(
    baseline: Mapping[str, ArtifactEntry],
    staged: Mapping[str, ArtifactEntry],
) -> tuple[ArtifactChange, ...]:
    changes: list[ArtifactChange] = []
    for path in sorted(set(baseline) | set(staged)):
        before = baseline.get(path)
        after = staged.get(path)
        if before == after:
            continue
        operation = "add" if before is None else "delete" if after is None else "modify"
        version = after.sha256 if after is not None else f"deleted:{before.sha256}"
        changes.append(
            ArtifactChange(
                path=path,
                operation=operation,
                artifact_class=_artifact_class(path),
                baseline=before,
                staged=after,
                version=version,
            )
        )
    return tuple(changes)


def _artifact_class(path: str) -> ArtifactClass:
    suffix = Path(path).suffix.casefold()
    if suffix in _CODE_SUFFIXES:
        return ArtifactClass.CODE
    if suffix in _GENERATED_ASSET_SUFFIXES:
        return ArtifactClass.GENERATED_ASSET
    return ArtifactClass.FILE


def _manifest_to_dict(entries: Mapping[str, ArtifactEntry]) -> dict[str, Any]:
    return {path: entries[path].to_dict() for path in sorted(entries)}


def _manifest_from_dict(raw: Any, label: str) -> dict[str, ArtifactEntry]:
    if not isinstance(raw, Mapping):
        raise ArtifactStagingError(f"{label} must be an object")
    result: dict[str, ArtifactEntry] = {}
    for path, entry in raw.items():
        relative = _relative_path(str(path))
        if not isinstance(entry, Mapping):
            raise ArtifactStagingError(f"{label}.{relative} must be an object")
        result[relative] = ArtifactEntry.from_dict(entry)
    return result


def _changes_from_raw(raw: Any) -> tuple[ArtifactChange, ...]:
    if not isinstance(raw, list):
        raise ArtifactStagingError("proposal.changes must be an array")
    changes = tuple(
        ArtifactChange.from_dict(item)
        for item in raw
        if isinstance(item, Mapping)
    )
    if len(changes) != len(raw):
        raise ArtifactStagingError("proposal change must be an object")
    paths = [item.path for item in changes]
    if paths != sorted(set(paths)):
        raise ArtifactStagingError("proposal changes must have unique sorted paths")
    return changes


def _copy_entry(source: Path, destination: Path, entry: ArtifactEntry) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if entry.kind == "symlink":
        os.symlink(entry.link_target, destination)
        return
    shutil.copy2(source, destination, follow_symlinks=False)
    os.chmod(destination, entry.mode, follow_symlinks=False)


def _atomic_install_entry(source: Path, destination: Path, entry: ArtifactEntry) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.autopilot-",
        dir=destination.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        if entry.kind == "symlink":
            os.symlink(entry.link_target, temporary)
        else:
            shutil.copy2(source, temporary, follow_symlinks=False)
            os.chmod(temporary, entry.mode, follow_symlinks=False)
        os.replace(temporary, destination)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _atomic_copy_file(source: Path, destination: Path) -> None:
    entry = _entry(source)
    if entry.kind != "file":
        raise ArtifactStagingError("checkpoint must be a regular file")
    _atomic_install_entry(source, destination, entry)


def _remove_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise ArtifactStagingError(f"promotion refuses to remove directory entry: {path}")


def _require_safe_parents(root: Path, relative: str) -> None:
    current = root
    parts = Path(_relative_path(relative)).parts
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ArtifactStagingError(
                f"promotion path crosses a symlinked directory: {relative}"
            )


def _safe_path(root: Path, relative: str) -> Path:
    relative = _relative_path(relative)
    candidate = root.joinpath(*Path(relative).parts)
    # This is intentionally lexical.  Existing leaf symlinks are artifacts
    # themselves and may be replaced; parent symlinks are rejected separately.
    if candidate == root or not candidate.is_relative_to(root):
        raise ArtifactStagingError(f"artifact path escapes its root: {relative}")
    return candidate


def _relative_path(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or value in {".", ".."} or ".." in path.parts:
        raise ArtifactStagingError(f"artifact path must be project-relative: {value!r}")
    normalized = path.as_posix()
    if normalized.startswith("./") or "\x00" in normalized:
        raise ArtifactStagingError(f"artifact path is not normalized: {value!r}")
    return normalized


def _safe_id(value: str, label: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ArtifactStagingError(f"{label} is not a safe identifier")
    return value


def _nonempty(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ArtifactStagingError(f"{label} must be non-empty")
    if "\x00" in value:
        raise ArtifactStagingError(f"{label} contains a NUL byte")
    return value


def _json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ArtifactClass",
    "ArtifactChange",
    "ArtifactEntry",
    "ArtifactStagingError",
    "ArtifactStagingStore",
    "CanonicalDriftError",
    "PromotionResult",
    "STAGING_DIRECTORY",
    "STAGING_MARKER",
    "StagedArtifact",
    "StagedArtifactChangedError",
    "StagingStatus",
    "resolve_canonical_project_root",
    "task_requires_staging",
]
