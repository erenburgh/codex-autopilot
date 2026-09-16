"""Durable, versioned profession profiles for project-scoped hiring.

A planner still declares structured ``RoleProfile`` records in the canonical
plan.  This module turns those declarations into durable profession revisions:
the first canonical save hires the profession, later saves reuse the exact
revision, and changed content must use a strictly newer semantic version.

The registry is deliberately project-local.  Autopilot derives an organization
from each project's goal, artifacts, capabilities, and selected Skill Packs;
there is no industry-template enum and no global personality catalog here.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .skill_packs import SkillReference


ROLE_SPECIFICATIONS_FILE = "role-specifications.json"
ROLE_SPECIFICATIONS_SCHEMA_VERSION = 1
DEFAULT_ROLE_SPECIFICATION_VERSION = ".".join(str(item) for item in (1, 0, 0))

_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class RoleSpecificationError(ValueError):
    """A profession revision is malformed or rewrites durable history."""


@dataclass(frozen=True, slots=True)
class RoleProfile:
    """Planner-defined, versioned profession; it never selects a model."""

    id: str
    name: str
    responsibilities: tuple[str, ...]
    version: str = DEFAULT_ROLE_SPECIFICATION_VERSION
    domain_focus: tuple[str, ...] = ()
    preferred_tools: tuple[str, ...] = ()
    context_priorities: tuple[str, ...] = ()
    verification_expectations: tuple[str, ...] = ()
    skill_requirements: tuple[SkillReference, ...] = ()


@dataclass(frozen=True, slots=True)
class RoleHiringResult:
    """Deterministic result of materializing the plan's professions."""

    created: tuple[str, ...] = ()
    reused: tuple[str, ...] = ()
    revised: tuple[str, ...] = ()


def validate_role_version(value: Any, label: str) -> str:
    """Return a canonical SemVer role revision or fail with its field name."""

    match = _SEMVER.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise RoleSpecificationError(f"{label} must be a semantic version")
    prerelease = match.group(4)
    if prerelease is not None and any(
        len(identifier) > 1 and identifier.isdigit() and identifier.startswith("0")
        for identifier in prerelease.split(".")
    ):
        raise RoleSpecificationError(f"{label} must be a semantic version")
    return value


def role_profile_snapshot(role: Any) -> dict[str, Any]:
    """Serialize the professional contract whose revision is immutable."""

    version = validate_role_version(
        getattr(role, "version", DEFAULT_ROLE_SPECIFICATION_VERSION),
        f"role {getattr(role, 'id', '<unknown>')}.version",
    )
    return {
        "id": str(role.id),
        "name": str(role.name),
        "version": version,
        "responsibilities": list(role.responsibilities),
        "domain_focus": list(role.domain_focus),
        "preferred_tools": list(role.preferred_tools),
        "context_priorities": list(role.context_priorities),
        "verification_expectations": list(role.verification_expectations),
        "skill_requirements": [item.to_dict() for item in role.skill_requirements],
    }


def role_profile_to_dict(role: RoleProfile) -> dict[str, Any]:
    """Serialize one plan role while retaining an explicit revision."""

    return {
        "id": role.id,
        "name": role.name,
        "version": role.version,
        "responsibilities": list(role.responsibilities),
        **({"domain_focus": list(role.domain_focus)} if role.domain_focus else {}),
        **({"preferred_tools": list(role.preferred_tools)} if role.preferred_tools else {}),
        **({"context_priorities": list(role.context_priorities)} if role.context_priorities else {}),
        **(
            {"verification_expectations": list(role.verification_expectations)}
            if role.verification_expectations
            else {}
        ),
        **(
            {"skill_requirements": [item.to_dict() for item in role.skill_requirements]}
            if role.skill_requirements
            else {}
        ),
    }


def role_profile_sha256(snapshot: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(snapshot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_role_specifications(
    state_dir: Path,
    roles: Iterable[Any],
) -> tuple[dict[str, Any], RoleHiringResult]:
    """Validate hiring/reuse and return the next deterministic registry.

    No file is changed here.  Callers can finish all validation before making
    the canonical plan and registry visible.
    """

    path = state_dir / ROLE_SPECIFICATIONS_FILE
    registry = _load_registry(path)
    profiles = dict(registry["profiles"])
    created: list[str] = []
    reused: list[str] = []
    revised: list[str] = []

    for role in roles:
        snapshot = role_profile_snapshot(role)
        role_id = snapshot["id"]
        version = snapshot["version"]
        digest = role_profile_sha256(snapshot)
        revision = {
            "version": version,
            "sha256": digest,
            "profile": snapshot,
        }
        existing = profiles.get(role_id)
        if existing is None:
            profiles[role_id] = {
                "id": role_id,
                "name": snapshot["name"],
                "current_version": version,
                "revisions": [revision],
            }
            created.append(f"{role_id}@{version}")
            continue

        _validate_profile_record(existing, role_id)
        revisions = list(existing["revisions"])
        by_version = {item["version"]: item for item in revisions}
        same = by_version.get(version)
        if same is not None:
            if same["sha256"] != digest or same["profile"] != snapshot:
                raise RoleSpecificationError(
                    f"role {role_id}@{version} is immutable; increment the role version"
                )
            if existing["current_version"] != version:
                raise RoleSpecificationError(
                    f"role {role_id}@{version} cannot replace newer current revision "
                    f"{existing['current_version']}"
                )
            reused.append(f"{role_id}@{version}")
            continue

        current = str(existing["current_version"])
        if _semver_key(version) <= _semver_key(current):
            raise RoleSpecificationError(
                f"role {role_id} revision must increase ({current} -> {version})"
            )
        revisions.append(revision)
        profiles[role_id] = {
            "id": role_id,
            "name": snapshot["name"],
            "current_version": version,
            "revisions": revisions,
        }
        revised.append(f"{role_id}@{version}")

    payload = {
        "schema_version": ROLE_SPECIFICATIONS_SCHEMA_VERSION,
        "profiles": {key: profiles[key] for key in sorted(profiles)},
    }
    return payload, RoleHiringResult(
        created=tuple(created),
        reused=tuple(reused),
        revised=tuple(revised),
    )


def validate_role_specification_transition(
    previous: Iterable[Any],
    candidate: Iterable[Any],
) -> None:
    """Reject same-version mutation during a canonical graph replacement."""

    before = {str(role.id): role_profile_snapshot(role) for role in previous}
    for role in candidate:
        after = role_profile_snapshot(role)
        prior = before.get(after["id"])
        if prior is None:
            continue
        if after == prior:
            continue
        if after["version"] == prior["version"]:
            raise RoleSpecificationError(
                f"role {after['id']}@{after['version']} is immutable; "
                "increment the role version"
            )
        if _semver_key(after["version"]) <= _semver_key(prior["version"]):
            raise RoleSpecificationError(
                f"role {after['id']} revision must increase "
                f"({prior['version']} -> {after['version']})"
            )


def _load_registry(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "schema_version": ROLE_SPECIFICATIONS_SCHEMA_VERSION,
            "profiles": {},
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoleSpecificationError(f"invalid role specification registry: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "profiles"}:
        raise RoleSpecificationError("role specification registry has invalid fields")
    if raw["schema_version"] != ROLE_SPECIFICATIONS_SCHEMA_VERSION:
        raise RoleSpecificationError("unsupported role specification registry schema")
    if not isinstance(raw["profiles"], dict):
        raise RoleSpecificationError("role specification registry profiles must be an object")
    for role_id, record in raw["profiles"].items():
        _validate_profile_record(record, str(role_id))
    return raw


def _validate_profile_record(raw: Any, role_id: str) -> None:
    if not isinstance(raw, dict) or set(raw) != {
        "id",
        "name",
        "current_version",
        "revisions",
    }:
        raise RoleSpecificationError(f"role specification {role_id} has invalid fields")
    if raw["id"] != role_id or not isinstance(raw["name"], str) or not raw["name"].strip():
        raise RoleSpecificationError(f"role specification {role_id} has invalid identity")
    current = validate_role_version(
        raw["current_version"], f"role specification {role_id}.current_version"
    )
    revisions = raw["revisions"]
    if not isinstance(revisions, list) or not revisions:
        raise RoleSpecificationError(f"role specification {role_id} needs revisions")
    versions: list[str] = []
    for revision in revisions:
        if not isinstance(revision, dict) or set(revision) != {
            "version",
            "sha256",
            "profile",
        }:
            raise RoleSpecificationError(f"role specification {role_id} has invalid revision")
        version = validate_role_version(
            revision["version"], f"role specification {role_id} revision.version"
        )
        profile = revision["profile"]
        if not isinstance(profile, dict) or profile.get("id") != role_id:
            raise RoleSpecificationError(f"role specification {role_id} revision identity drift")
        if profile.get("version") != version:
            raise RoleSpecificationError(f"role specification {role_id} revision version drift")
        if revision["sha256"] != role_profile_sha256(profile):
            raise RoleSpecificationError(f"role specification {role_id}@{version} digest mismatch")
        versions.append(version)
    if len(versions) != len(set(versions)) or current != versions[-1]:
        raise RoleSpecificationError(f"role specification {role_id} revision history is invalid")
    if versions != sorted(versions, key=_semver_key):
        raise RoleSpecificationError(f"role specification {role_id} revisions are unordered")


def _semver_key(value: str) -> tuple[int, int, int, int, tuple[tuple[int, Any], ...]]:
    match = _SEMVER.fullmatch(value)
    if match is None:  # all callers validate first
        raise RoleSpecificationError(f"invalid semantic version {value!r}")
    prerelease = match.group(4)
    if prerelease is None:
        return int(match.group(1)), int(match.group(2)), int(match.group(3)), 1, ()
    identifiers: list[tuple[int, Any]] = []
    for item in prerelease.split("."):
        identifiers.append((0, int(item)) if item.isdigit() else (1, item))
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), 0, tuple(identifiers)
