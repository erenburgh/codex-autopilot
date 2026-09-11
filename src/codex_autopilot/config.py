from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import tomllib

from .language import DEFAULT_LANGUAGE, normalize_language
from .plan import (
    DEFAULT_COMPUTER_USE_SLOTS,
    DEFAULT_EXECUTION_STRATEGY,
    DEFAULT_MAX_PARALLEL_WORKERS,
    EXECUTION_STRATEGIES,
)


CONFIG_NAME = "config.toml"
STATE_DIR_NAME = ".codex-autopilot"
PROFILES = {"adaptive", "host-settings"}
DESKTOP_OWNED_SURFACE = "desktop_owned"
HEADLESS_APP_SERVER_SURFACE = "headless_app_server"
WORKER_SURFACES = {DESKTOP_OWNED_SURFACE, HEADLESS_APP_SERVER_SURFACE}


@dataclass(frozen=True, slots=True)
class DesktopConfig:
    binary: str = "codex"
    permission_profile: str = ":workspace"
    # App Server project ids and Desktop sidebar project ids are different
    # namespaces. project_id is App Server-owned metadata only.
    project_id: str | None = None
    desktop_project_id: str | None = None
    worker_thread_ids: tuple[str, ...] = ()
    title_prefix: str = "Codex Autopilot"
    turn_timeout_seconds: int = 14_400
    reconcile_timeout_seconds: int = 300


@dataclass(frozen=True, slots=True)
class RetryConfig:
    initial_seconds: int = 30
    maximum_seconds: int = 900
    maximum_attempts: int = 96


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Operational scheduler limits; absent v0.8 sections load fail-closed."""

    execution_strategy: str = DEFAULT_EXECUTION_STRATEGY
    max_parallel_workers: int = DEFAULT_MAX_PARALLEL_WORKERS
    computer_use_slots: int = DEFAULT_COMPUTER_USE_SLOTS
    # Missing values and new runs retain the historical controller-owned App
    # Server behavior. Desktop ownership must be selected explicitly.
    worker_surface: str = HEADLESS_APP_SERVER_SURFACE


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    state_dir: Path
    roadmap: Path
    profile: str
    language: str
    skill_name: str
    skill_path: Path
    desktop: DesktopConfig
    retry: RetryConfig
    runtime: RuntimeConfig = RuntimeConfig()
    auto_commit: bool = False

    @property
    def adaptive(self) -> bool:
        return self.profile == "adaptive"

    @property
    def memory_database(self) -> Path:
        return self.state_dir / "memory.sqlite3"


def config_path(root: Path) -> Path:
    return root.resolve() / STATE_DIR_NAME / CONFIG_NAME


def append_worker_slot(root: Path, thread_id: str, desktop_project_id: str) -> bool:
    """Append one app-verified Desktop task to a stopped run, atomically."""
    path = config_path(root)
    if not path.is_file():
        raise FileNotFoundError(f"Codex Autopilot is not initialized: {path}")
    if not thread_id:
        raise ValueError("worker thread id must be non-empty")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    desktop = data.get("desktop") or {}
    configured_project = _optional_string(desktop.get("desktop_project_id"))
    if configured_project != desktop_project_id:
        raise ValueError("Desktop worker slot project does not match the initialized run")
    ids = list(_string_tuple(desktop.get("worker_thread_ids"), "desktop.worker_thread_ids"))
    if thread_id in ids:
        return False
    ids.append(thread_id)
    lines = path.read_text(encoding="utf-8").splitlines()
    replacement = f"worker_thread_ids = {json.dumps(ids, ensure_ascii=False)}"
    for index, line in enumerate(lines):
        if line.startswith("worker_thread_ids ="):
            lines[index] = replacement
            break
    else:
        marker = next(index for index, line in enumerate(lines) if line.startswith("turn_timeout_seconds ="))
        lines.insert(marker, replacement)
    fd, raw = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def set_worker_surface(root: Path, worker_surface: str) -> bool:
    """Atomically select the worker transport without changing project metadata."""

    if worker_surface not in WORKER_SURFACES:
        raise ValueError(f"worker_surface must be one of {sorted(WORKER_SURFACES)}")
    path = config_path(root)
    if not path.is_file():
        raise FileNotFoundError(f"Codex Autopilot is not initialized: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    replacement = f"worker_surface = {json.dumps(worker_surface)}"
    changed = False
    for index, line in enumerate(lines):
        if line.startswith("worker_surface ="):
            changed = line != replacement
            lines[index] = replacement
            break
    else:
        raise ValueError("config is missing runtime.worker_surface")
    if not changed:
        return False
    fd, raw = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def load_config(root_or_path: Path) -> Config:
    candidate = root_or_path.expanduser().resolve()
    path = candidate if candidate.name == CONFIG_NAME and candidate.is_file() else config_path(candidate)
    if not path.is_file():
        raise FileNotFoundError(f"Codex Autopilot is not initialized: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project") or {}
    desktop = data.get("desktop") or {}
    retry = data.get("retry") or {}
    runtime = data.get("runtime") or {}
    root = Path(str(project.get("root", path.parent.parent))).expanduser().resolve()
    if root != path.parent.parent.resolve():
        raise ValueError("config project.root does not match the initialized project directory")
    state_dir = root / STATE_DIR_NAME
    profile = str(data.get("profile", "adaptive"))
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {sorted(PROFILES)}")
    permission = str(desktop.get("permission_profile", ":workspace"))
    if permission != ":workspace":
        raise ValueError("v0.8 public beta only supports the :workspace permission profile")
    skill_name = "codex-autopilot-adaptive" if profile == "adaptive" else "codex-autopilot-host-settings"
    skill_path_raw = desktop.get("skill_path")
    if not skill_path_raw:
        raise ValueError("desktop.skill_path is required")
    auto_commit = bool((data.get("git") or {}).get("auto_commit", False))
    return Config(
        root=root,
        state_dir=state_dir,
        roadmap=root / "ROADMAP.md",
        profile=profile,
        language=normalize_language(data.get("language", DEFAULT_LANGUAGE)),
        skill_name=skill_name,
        skill_path=Path(str(skill_path_raw)).expanduser().resolve(),
        desktop=DesktopConfig(
            binary=str(desktop.get("binary", "codex")),
            permission_profile=permission,
            project_id=_optional_string(desktop.get("project_id")),
            desktop_project_id=_optional_string(desktop.get("desktop_project_id")),
            worker_thread_ids=_string_tuple(desktop.get("worker_thread_ids"), "desktop.worker_thread_ids"),
            title_prefix=str(desktop.get("title_prefix", "Codex Autopilot")),
            turn_timeout_seconds=_positive_int(desktop.get("turn_timeout_seconds", 14_400), "turn_timeout_seconds"),
            reconcile_timeout_seconds=_positive_int(desktop.get("reconcile_timeout_seconds", 300), "reconcile_timeout_seconds"),
        ),
        retry=RetryConfig(
            initial_seconds=_positive_int(retry.get("initial_seconds", 30), "retry.initial_seconds"),
            maximum_seconds=_positive_int(retry.get("maximum_seconds", 900), "retry.maximum_seconds"),
            maximum_attempts=_positive_int(retry.get("maximum_attempts", 96), "retry.maximum_attempts"),
        ),
        runtime=RuntimeConfig(
            execution_strategy=_execution_strategy(
                runtime.get("execution_strategy", DEFAULT_EXECUTION_STRATEGY)
            ),
            max_parallel_workers=_positive_int(
                runtime.get("max_parallel_workers", DEFAULT_MAX_PARALLEL_WORKERS),
                "runtime.max_parallel_workers",
            ),
            computer_use_slots=_positive_int(
                runtime.get("computer_use_slots", DEFAULT_COMPUTER_USE_SLOTS),
                "runtime.computer_use_slots",
            ),
            worker_surface=_worker_surface(
                runtime.get("worker_surface", HEADLESS_APP_SERVER_SURFACE)
            ),
        ),
        auto_commit=auto_commit,
    )


def _optional_string(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _positive_int(value: object, name: str) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be an array of non-empty strings")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _execution_strategy(value: object) -> str:
    result = str(value).strip()
    if result not in EXECUTION_STRATEGIES:
        raise ValueError(
            f"runtime.execution_strategy must be one of {sorted(EXECUTION_STRATEGIES)}"
        )
    return result


def _worker_surface(value: object) -> str:
    result = str(value).strip()
    if result not in WORKER_SURFACES:
        raise ValueError(
            f"runtime.worker_surface must be one of {sorted(WORKER_SURFACES)}"
        )
    return result
