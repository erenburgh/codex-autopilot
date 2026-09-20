from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

from .language import DEFAULT_LANGUAGE, normalize_language
from .plan import (
    DEFAULT_COMPUTER_USE_SLOTS,
    COMPAT_EXECUTION_STRATEGY,
    COMPAT_MAX_PARALLEL_WORKERS,
    EXECUTION_STRATEGIES,
)
from .plan_verification import DEFAULT_FULL_REVALIDATION_PATCHES


CONFIG_NAME = "config.toml"
STATE_DIR_NAME = ".codex-autopilot"
PROFILES = {"adaptive", "host-settings"}
# The only worker surface. The second, headless_app_server, led to
# orchestrator.py, which could not execute in the product: run, resume and
# _dispatch refused under desktop_owned, and the default everywhere was
# desktop_owned. In 0.8.1 both the path and the surface were removed.
DESKTOP_OWNED_SURFACE = "desktop_owned"
WORKER_SURFACES = {DESKTOP_OWNED_SURFACE}
SKILL_SCREENING_MODES = {"auto", "always", "never"}
# Hiring ships enabled: the owner chose it over off and over a per-run
# ceiling, knowing the cost - one extra Codex thread per task, and on her
# own stalled run of 25 tasks with 25 distinct roles that is +25 threads
# with no reuse to offset it. A feature that is on by default has to be
# able to say what it spent, so the status card reports it.
DEFAULT_SKILL_SCREENING = "always"


@dataclass(frozen=True, slots=True)
class DesktopConfig:
    binary: str = "codex"
    permission_profile: str = ":workspace"
    # App Server project ids and Desktop sidebar project ids are different
    # namespaces. project_id is App Server-owned metadata only.
    project_id: str | None = None
    desktop_project_id: str | None = None
    title_prefix: str = "Codex Autopilot"
    turn_timeout_seconds: int = 14_400
    reconcile_timeout_seconds: int = 300


@dataclass(frozen=True, slots=True)
class RetryConfig:
    initial_seconds: int = 30
    maximum_seconds: int = 900
    # Attempts per ONE failure signature, not per task (R23). It was 96 and
    # read by nobody: retries ran with no ceiling at all. Five at a 30..900
    # s delay is about eight minutes of spinning - the same order as the
    # engineer's auto-recovery budget (retry_budget=2), not a day.
    maximum_attempts: int = 5


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Operational scheduler limits; absent v0.8 sections load fail-closed."""

    execution_strategy: str = COMPAT_EXECUTION_STRATEGY
    max_parallel_workers: int = COMPAT_MAX_PARALLEL_WORKERS
    # Whether the human named the number in this file. The default here is
    # one, for v0.8 compatibility, and taking it for the user's choice would
    # force any run with an old config into a single lane.
    max_parallel_workers_declared: bool = False
    computer_use_slots: int = DEFAULT_COMPUTER_USE_SLOTS
    worker_surface: str = DESKTOP_OWNED_SURFACE
    # Until which Desktop placement of its thread a task may not start.
    # "in_project" - only inside the project; "visible" - Desktop knowing
    # of it is enough; "any" - do not check. An invisible task devalues
    # Autopilot: it cannot be opened and read, so the default is strict.
    required_thread_placement: str = "in_project"
    # A system banner when a task is verified, stopped, or the run is done.
    # Off by default: it is a side effect on the human's machine. Measured
    # that there is no other way - App Server declares no unread API at
    # all, and thread/metadata/update accepts only projectId. See notify.py.
    desktop_notifications: bool = False
    full_plan_revalidation_patches: int = DEFAULT_FULL_REVALIDATION_PATCHES
    # Whether a task is screened for skills before it gets a worker.
    # Off by default for the same reason desktop_notifications is: one
    # screening is one more Codex thread per task out of the user's limits,
    # and spending them is the user's decision, not a default.
    # "auto" screens only when the plan or the installed library holds at
    # least one pack - with nothing to hire from, a screening turn can only
    # answer "nothing available". "always" screens every task, which is what
    # records unmet needs in a project that has no skills yet.
    skill_screening: str = DEFAULT_SKILL_SCREENING


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


def config_path(root: Path) -> Path:
    return root.resolve() / STATE_DIR_NAME / CONFIG_NAME


def _install_root() -> Path:
    """The install root from the runtime's own location.

    The runtime lives in <install_root>/current/runtime/src/codex_autopilot.
    absolute(), not resolve(): resolving would expand the `current`
    symlink into the versioned directory - the very one the next
    installation deletes.
    """

    return Path(__file__).absolute().parents[3]


def stable_skill_path(recorded: Path) -> Path | None:
    """The same skill by a path that survives an installation.

    The installer puts the plugin in a versioned, timestamped directory and
    deletes the previous one, while the run stored the whole path - version
    included. The first installation left the reference dangling: a live
    run died on `could not resolve installed plugin root from skill`, and
    the failure landed in the AMBIGUOUS_SIDE_EFFECT class, which has no way
    out.

    The layouts of the two stores differ - in the cache it is
    <plugin>/<version>/skills/..., in the installation plugins/<plugin>/skills/...
    - so the plugin name is searched for, not computed by position.
    """

    parts = Path(recorded).parts
    if "skills" not in parts:
        return None
    index = len(parts) - 1 - parts[::-1].index("skills")
    tail = parts[index:]
    pattern = str(Path("plugins", "*", *tail))
    for candidate in sorted(_install_root().glob(pattern)):
        if candidate.is_file():
            return candidate
    return None


def durable_skill_path(skill_path: Path) -> Path:
    """The path written into the run config."""

    stable = stable_skill_path(Path(skill_path))
    return stable if stable is not None else Path(skill_path).resolve()


def resolve_skill_path(raw: str) -> Path:
    """The recorded path, or its stable equivalent if it vanished."""

    recorded = Path(str(raw)).expanduser()
    if recorded.is_file():
        return recorded.resolve()
    stable = stable_skill_path(recorded)
    if stable is not None:
        return stable
    # Neither: let the refusal happen in the same place with the same text
    # as before, rather than turn into a riddle at the configuration level.
    return recorded.resolve()


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
        skill_path=resolve_skill_path(str(skill_path_raw)),
        desktop=DesktopConfig(
            binary=str(desktop.get("binary", "codex")),
            permission_profile=permission,
            project_id=_optional_string(desktop.get("project_id")),
            desktop_project_id=_optional_string(desktop.get("desktop_project_id")),
            title_prefix=str(desktop.get("title_prefix", "Codex Autopilot")),
            turn_timeout_seconds=_positive_int(desktop.get("turn_timeout_seconds", 14_400), "turn_timeout_seconds"),
            reconcile_timeout_seconds=_positive_int(desktop.get("reconcile_timeout_seconds", 300), "reconcile_timeout_seconds"),
        ),
        retry=RetryConfig(
            initial_seconds=_positive_int(retry.get("initial_seconds", 30), "retry.initial_seconds"),
            maximum_seconds=_positive_int(retry.get("maximum_seconds", 900), "retry.maximum_seconds"),
            maximum_attempts=_positive_int(retry.get("maximum_attempts", 5), "retry.maximum_attempts"),
        ),
        runtime=RuntimeConfig(
            execution_strategy=_execution_strategy(
                runtime.get("execution_strategy", COMPAT_EXECUTION_STRATEGY)
            ),
            max_parallel_workers=_positive_int(
                runtime.get("max_parallel_workers", COMPAT_MAX_PARALLEL_WORKERS),
                "runtime.max_parallel_workers",
            ),
            max_parallel_workers_declared="max_parallel_workers" in runtime,
            computer_use_slots=_positive_int(
                runtime.get("computer_use_slots", DEFAULT_COMPUTER_USE_SLOTS),
                "runtime.computer_use_slots",
            ),
            worker_surface=_worker_surface(
                runtime.get("worker_surface", DESKTOP_OWNED_SURFACE)
            ),
            required_thread_placement=_thread_placement(
                runtime.get("required_thread_placement", "in_project")
            ),
            desktop_notifications=_bool(
                runtime.get("desktop_notifications", False),
                "runtime.desktop_notifications",
            ),
            skill_screening=_skill_screening(
                runtime.get("skill_screening", DEFAULT_SKILL_SCREENING)
            ),
            full_plan_revalidation_patches=_positive_int(
                runtime.get(
                    "full_plan_revalidation_patches",
                    DEFAULT_FULL_REVALIDATION_PATCHES,
                ),
                "runtime.full_plan_revalidation_patches",
            ),
        ),
        auto_commit=auto_commit,
    )


def _skill_screening(value: object) -> str:
    text = str(value)
    if text not in SKILL_SCREENING_MODES:
        raise ValueError(
            f"runtime.skill_screening must be one of {sorted(SKILL_SCREENING_MODES)}"
        )
    return text


def _optional_string(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _positive_int(value: object, name: str) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _execution_strategy(value: object) -> str:
    result = str(value).strip()
    if result not in EXECUTION_STRATEGIES:
        raise ValueError(
            f"runtime.execution_strategy must be one of {sorted(EXECUTION_STRATEGIES)}"
        )
    return result


THREAD_PLACEMENT_LEVELS = ("in_project", "visible", "any")


def _bool(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be true or false")


def _thread_placement(value: object) -> str:
    text = str(value or "").strip().lower()
    if text not in THREAD_PLACEMENT_LEVELS:
        raise ValueError(
            "runtime.required_thread_placement must be one of "
            + ", ".join(THREAD_PLACEMENT_LEVELS)
        )
    return text


def _worker_surface(value: object) -> str:
    result = str(value).strip()
    if result not in WORKER_SURFACES:
        raise ValueError(
            f"runtime.worker_surface must be one of {sorted(WORKER_SURFACES)}"
        )
    return result
