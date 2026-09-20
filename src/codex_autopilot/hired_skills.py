"""Admitting a skill bundle a screener fetched, and never anything else.

The owner's boundary: a skill may be installed, a plugin may never be. That
line protects her absolute rule that hook trust is never touched. A plugin
registers hooks, MCP servers and commands and lives in the Codex plugin
cache, so installing one changes the host's trust surface. A skill is a
``SKILL.md`` bundle - instructions and the files beside them - and registers
nothing.

Measured on this machine against codex-cli 0.154.0, not inferred from how any
other tool works. Codex ships ``~/.codex/skills/.system/skill-installer``,
whose documented behaviour is to download from a GitHub repo path into
``$CODEX_HOME/skills/<name>``, available on the next turn, touching no plugin.
And ``codex app-server generate-json-schema`` shows ``TurnStartParams.input``
is an array whose ``SkillUserInput`` variant carries an arbitrary ``path`` -
which is how this runtime already hands a worker its own skill, from the
install root and nowhere near ``$CODEX_HOME``.

So a hired bundle lives in the project, under ``hired-skills/<name>@<digest>``,
and the worker is told its exact path. Installing into ``$CODEX_HOME`` would
be a machine-wide side effect for one task's decision, would collide with
skills the user installed herself - the first-party installer aborts when the
destination exists - and could not hold two revisions for two projects.

The rule against reaching the host is structural, not prose. Every
destination is resolved under the project's own ``hired-skills`` directory and
anything else is refused, so this module cannot name the plugin cache, the
Codex skills directory, hooks or MCP configuration at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Mapping


# Where an admitted bundle lives, and where a screener leaves one it fetched.
HIRED_SKILLS_DIRNAME = "hired-skills"
STAGED_SKILLS_DIRNAME = "staged-skills"
SKILL_FILE = "SKILL.md"
# A skill name is a single directory component: letters, digits, dash, dot and
# underscore, never starting with a dot. Anything that could climb a directory
# or hide is refused before it is joined to a path.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Entries that would make a bundle a plugin wearing a skill's clothes. The
# boundary is the whole point, so it is checked on the content, not promised.
_REGISTRATION_ENTRIES = frozenset(
    {".codex-plugin", "hooks", ".mcp.json", "mcp.json", "plugin.json", "commands"}
)
# A bundle is instructions and the files beside them. These bounds are a
# ceiling on what one admission may write into the project, not a measurement:
# the curated skills read on this machine are a SKILL.md plus a handful of
# references, scripts and assets, three orders of magnitude below them.
MAX_BUNDLE_FILES = 200
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_SKILL_FILE_BYTES = 512 * 1024


# Codex's own preinstalled skills, excluded by the dotted-entry rule in
# installed_skill_bundles. They are available to every session already, so
# offering them to a screener adds nothing and crowds the brief.
CODEX_SYSTEM_SKILLS_DIRNAME = ".system"


class HiredSkillError(ValueError):
    """The staged bundle is not a skill, or is not where it must be."""


def codex_skills_root(codex_home: Path | None = None) -> Path:
    """The user's own skills directory, honouring CODEX_HOME."""

    home = codex_home or Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    )
    return Path(home).expanduser() / "skills"


def installed_skill_bundles(
    codex_home: Path | None = None,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """List the skills the user installed in her own Codex home. Read-only.

    This function only reads. Autopilot writes nothing into ``$CODEX_HOME``
    - that is why a hired bundle lives in the project - and reading hers must
    not become the exception that reopens it.

    An entry that is not a skill is named and skipped rather than refusing
    the whole read. Her skills directory is not Autopilot's configuration: it
    is a general-purpose directory this runtime merely observes, and one
    unusable folder in it is not a reason to stop her run. That is the
    opposite of the rule for the project's own manifest library, where a
    broken file IS configuration somebody wrote for Autopilot and stops it.

    Returns the bundles and, separately, the refusals - so the screener can
    be told that something is there which could not be offered, and why.
    """

    root = codex_skills_root(codex_home)
    if not root.is_dir():
        return (), ()
    bundles: list[dict[str, Any]] = []
    refused: list[str] = []
    for path in sorted(root.iterdir()):
        # Dotted entries are skipped, and that is what excludes Codex's own
        # preinstalled skills: they live in `.system`. A separate check for
        # the name was written here and a mutation proved it dead - this
        # line already covered it - so the reason lives where the rule is.
        if not path.is_dir() or path.name.startswith("."):
            continue
        skill_file = path / SKILL_FILE
        if not skill_file.is_file():
            refused.append(
                f"{path.name} carries no {SKILL_FILE}, so it is not a skill bundle; "
                f"an installed skill is a directory holding {SKILL_FILE}"
            )
            continue
        try:
            files = _bundle_files(path)
            # The digest reads every file, so it belongs inside the guard:
            # one unreadable file in HER directory must be a named refusal,
            # not an exception that stops the whole read of a directory
            # Autopilot does not own.
            digest = _bundle_digest(files)
        except (HiredSkillError, OSError) as exc:
            refused.append(f"{path.name}: {exc}")
            continue
        bundles.append(
            {
                "name": path.name,
                "path": str(path),
                "digest": digest,
                "files": len(files),
            }
        )
    return tuple(bundles), tuple(refused)


def admit_skill_bundle(
    state_dir: Path,
    *,
    staged_path: Path,
    name: str,
    provider: str,
    locator: str = "",
    destination_root: Path | None = None,
) -> dict[str, Any]:
    """Admit one staged bundle into the project, or refuse it by name.

    ``destination_root`` exists so a caller can be told no: it is resolved
    against the one directory bundles may occupy and refused otherwise. There
    is no argument that moves the destination somewhere else.
    """

    project_state = _project_state_dir(state_dir)
    root = _destination_root(project_state, destination_root)
    skill_name = _skill_name(name)
    provider_name = _required(provider, "provider")
    staged = _staged_bundle(project_state, staged_path)
    files = _bundle_files(staged)
    digest = _bundle_digest(files)
    destination = root / f"{skill_name}@{digest[:12]}"
    if not destination.exists():
        root.mkdir(parents=True, exist_ok=True)
        # Copied into place, never moved: a half-finished move leaves the
        # project with a directory that looks admitted and is not. The
        # temporary name is replaced atomically once the copy is complete.
        pending = root / f".{skill_name}@{digest[:12]}.pending"
        shutil.rmtree(pending, ignore_errors=True)
        shutil.copytree(staged, pending, symlinks=False)
        pending.replace(destination)
    record = {
        "id": f"{skill_name}@{digest[:12]}",
        "name": skill_name,
        "digest": digest,
        "provider": provider_name,
        "locator": str(locator or ""),
        "path": str(destination),
        "files": len(files),
    }
    return record


def hired_skill_records(state_dir: Path) -> tuple[dict[str, Any], ...]:
    """Every bundle this project currently holds, in a stable order."""

    root = _project_state_dir(state_dir) / HIRED_SKILLS_DIRNAME
    if not root.is_dir():
        return ()
    records: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        name, _, digest = path.name.partition("@")
        records.append(
            {
                "id": path.name,
                "name": name,
                "digest": digest,
                "path": str(path),
            }
        )
    return tuple(records)


def revoke_hired_skill(state_dir: Path, skill_id: str) -> dict[str, Any]:
    """Remove one bundle. This is the named reversal of an admission."""

    records = {item["id"]: item for item in hired_skill_records(state_dir)}
    record = records.get(str(skill_id or "").strip())
    if record is None:
        # R31: a refusal names what is accepted. "Unknown skill" sends
        # somebody to guess at a directory listing they cannot see.
        present = ", ".join(sorted(records)) or "nothing is installed"
        raise HiredSkillError(
            f"no hired skill {skill_id!r} in this project; present: {present}"
        )
    shutil.rmtree(record["path"])
    return record


def _project_state_dir(state_dir: Path) -> Path:
    resolved = Path(state_dir).expanduser().resolve(strict=False)
    if resolved.name != ".codex-autopilot":
        raise HiredSkillError(
            "hired skills live in a project's .codex-autopilot directory; "
            f"got {resolved}"
        )
    return resolved


def _destination_root(project_state: Path, requested: Path | None) -> Path:
    root = (project_state / HIRED_SKILLS_DIRNAME).resolve(strict=False)
    if requested is None:
        return root
    asked = Path(requested).expanduser().resolve(strict=False)
    if asked != root:
        raise HiredSkillError(
            f"a hired skill bundle may only be written to {root}; refused {asked}. "
            "the plugin cache, the Codex skills directory, hooks and MCP "
            "configuration are not reachable from here"
        )
    return root


def _skill_name(value: Any) -> str:
    text = _required(value, "name")
    if _NAME.fullmatch(text) is None:
        raise HiredSkillError(
            f"skill name {text!r} is not a single safe directory component "
            "(letters, digits, dot, dash, underscore; not starting with a dot)"
        )
    return text


def _staged_bundle(project_state: Path, staged_path: Path) -> Path:
    staged = Path(staged_path).expanduser().resolve(strict=False)
    if not staged.is_dir():
        raise HiredSkillError(f"staged skill bundle is not a directory: {staged}")
    if not staged.is_relative_to(project_state):
        raise HiredSkillError(
            "a staged skill bundle must be inside the project's "
            f".codex-autopilot directory; got {staged}"
        )
    skill_file = staged / SKILL_FILE
    if not skill_file.is_file():
        raise HiredSkillError(
            f"{staged} carries no {SKILL_FILE}, so it is not a skill bundle"
        )
    if skill_file.stat().st_size > MAX_SKILL_FILE_BYTES:
        raise HiredSkillError(
            f"{SKILL_FILE} exceeds {MAX_SKILL_FILE_BYTES} bytes"
        )
    return staged


def _bundle_files(staged: Path) -> tuple[tuple[str, Path], ...]:
    files: list[tuple[str, Path]] = []
    total = 0
    for path in sorted(staged.rglob("*")):
        relative = path.relative_to(staged)
        head = relative.parts[0]
        if head in _REGISTRATION_ENTRIES or path.name in _REGISTRATION_ENTRIES:
            raise HiredSkillError(
                f"{staged} ships {head!r}: that registers a plugin, hooks or an "
                "MCP server, and a skill registers nothing"
            )
        if path.is_symlink():
            raise HiredSkillError(
                f"{relative} is a symbolic link; a bundle carries its own files"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise HiredSkillError(f"{relative} is not a regular file")
        size = path.stat().st_size
        total += size
        if total > MAX_BUNDLE_BYTES:
            raise HiredSkillError(
                f"skill bundle exceeds {MAX_BUNDLE_BYTES} bytes"
            )
        files.append((relative.as_posix(), path))
        if len(files) > MAX_BUNDLE_FILES:
            raise HiredSkillError(
                f"skill bundle carries more than {MAX_BUNDLE_FILES} files"
            )
    return tuple(files)


def _bundle_digest(files: tuple[tuple[str, Path], ...]) -> str:
    """Digest path and content together, so a rename is a new revision."""

    digest = hashlib.sha256()
    for relative, path in files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _required(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HiredSkillError(f"hired skill {label} must be a non-empty string")
    return value.strip()
