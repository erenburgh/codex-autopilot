from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


CONFIG_NAME = "config.toml"
STATE_DIR_NAME = ".codex-autopilot"
PROFILES = {"adaptive", "host-settings"}


@dataclass(frozen=True, slots=True)
class DesktopConfig:
    binary: str = "codex"
    permission_profile: str = ":workspace"
    project_id: str | None = None
    title_prefix: str = "Codex Autopilot"
    turn_timeout_seconds: int = 14_400
    reconcile_timeout_seconds: int = 300


@dataclass(frozen=True, slots=True)
class RetryConfig:
    initial_seconds: int = 30
    maximum_seconds: int = 900
    maximum_attempts: int = 96


@dataclass(frozen=True, slots=True)
class Config:
    root: Path
    state_dir: Path
    roadmap: Path
    profile: str
    skill_name: str
    skill_path: Path
    desktop: DesktopConfig
    retry: RetryConfig
    auto_commit: bool = False

    @property
    def adaptive(self) -> bool:
        return self.profile == "adaptive"


def config_path(root: Path) -> Path:
    return root.resolve() / STATE_DIR_NAME / CONFIG_NAME


def load_config(root_or_path: Path) -> Config:
    candidate = root_or_path.expanduser().resolve()
    path = candidate if candidate.name == CONFIG_NAME and candidate.is_file() else config_path(candidate)
    if not path.is_file():
        raise FileNotFoundError(f"Codex Autopilot is not initialized: {path}")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project") or {}
    desktop = data.get("desktop") or {}
    retry = data.get("retry") or {}
    root = Path(str(project.get("root", path.parent.parent))).expanduser().resolve()
    state_dir = root / STATE_DIR_NAME
    profile = str(data.get("profile", "adaptive"))
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {sorted(PROFILES)}")
    permission = str(desktop.get("permission_profile", ":workspace"))
    if permission != ":workspace":
        raise ValueError("v0.7 public beta only supports the :workspace permission profile")
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
        skill_name=skill_name,
        skill_path=Path(str(skill_path_raw)).expanduser().resolve(),
        desktop=DesktopConfig(
            binary=str(desktop.get("binary", "codex")),
            permission_profile=permission,
            project_id=_optional_string(desktop.get("project_id")),
            title_prefix=str(desktop.get("title_prefix", "Codex Autopilot")),
            turn_timeout_seconds=_positive_int(desktop.get("turn_timeout_seconds", 14_400), "turn_timeout_seconds"),
            reconcile_timeout_seconds=_positive_int(desktop.get("reconcile_timeout_seconds", 300), "reconcile_timeout_seconds"),
        ),
        retry=RetryConfig(
            initial_seconds=_positive_int(retry.get("initial_seconds", 30), "retry.initial_seconds"),
            maximum_seconds=_positive_int(retry.get("maximum_seconds", 900), "retry.maximum_seconds"),
            maximum_attempts=_positive_int(retry.get("maximum_attempts", 96), "retry.maximum_attempts"),
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
